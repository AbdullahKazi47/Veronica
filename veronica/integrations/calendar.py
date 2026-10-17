"""
Calendar: the user's real calendars (read-only, via their private iCal links) plus
VERONICA's own events added by voice.

Google Calendar gives every calendar a "secret address in iCal format" (Settings ->
the calendar -> Integrate calendar). Put one or more in config/.env as
CALENDAR_ICS_URLS=url1,url2 — that link is a password for reading the calendar, so it
lives only there. No Google sign-in or OAuth app is needed; the trade-off is read-only.
Events added by voice go to data/calendar.json (VERONICA's own calendar).

A small iCalendar reader (VEVENT: SUMMARY, DTSTART/DTEND or DURATION, TZID, all-day
dates, RRULE + EXDATE repeats, RECURRENCE-ID overrides, cancelled events) built on
python-dateutil, so no extra packages are needed. Calendar text is data, never
instructions.
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from veronica.core import jsonsafe
from veronica.logging_setup import get_logger

logger = get_logger("integrations.calendar")

_CACHE_S = 300
_lock = threading.Lock()


@dataclass
class Event:
    title: str
    start: dt.datetime            # local, naive (wall-clock on this PC)
    end: dt.datetime
    all_day: bool = False
    location: str = ""
    source: str = "calendar"      # "calendar" (iCal link) | "veronica" (added by voice)
    uid: str = ""


def _local(d: dt.datetime) -> dt.datetime:
    return d.astimezone().replace(tzinfo=None) if d.tzinfo else d


# ---------------------------------------------------------------- iCalendar parsing

def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(v: str) -> str:
    return v.replace("\\n", " ").replace("\\N", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\").strip()


def _parse_dt(value: str, params: dict) -> tuple[dt.datetime, bool]:
    """-> (datetime, all_day). Aware when the source says so (Z or TZID)."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return dt.datetime.strptime(value[:8], "%Y%m%d"), True
    utc = value.endswith("Z")
    base = dt.datetime.strptime(value.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        return base.replace(tzinfo=dt.timezone.utc), False
    tzid = params.get("TZID")
    if tzid:
        try:
            from zoneinfo import ZoneInfo
            return base.replace(tzinfo=ZoneInfo(tzid.strip('"'))), False
        except Exception:  # noqa: BLE001 - unknown zone name: treat as local wall-clock
            pass
    return base, False


def _parse_duration(v: str) -> dt.timedelta:
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v.strip())
    if not m:
        return dt.timedelta(hours=1)
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return dt.timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def parse_ics(text: str) -> list[dict]:
    """Raw VEVENT dicts: {props: {NAME: (value, params)}, multi: {NAME: [(value, params)]}}."""
    events, cur = [], None
    for line in _unfold(text or ""):
        if line == "BEGIN:VEVENT":
            cur = {"props": {}, "multi": {}}
            continue
        if line == "END:VEVENT":
            if cur is not None:
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        name, *rawp = head.split(";")
        params = {}
        for p in rawp:
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.upper()] = v
        name = name.upper()
        cur["multi"].setdefault(name, []).append((value, params))
        cur["props"].setdefault(name, (value, params))
    return events


def expand(raw_events: list[dict], start: dt.datetime, end: dt.datetime) -> list[Event]:
    """Occurrences overlapping [start, end) in local time, repeats expanded."""
    from dateutil.rrule import rrulestr
    out: list[Event] = []
    overrides: dict[tuple[str, dt.datetime], dict] = {}
    for ev in raw_events:
        if "RECURRENCE-ID" in ev["props"]:
            rid, rp = ev["props"]["RECURRENCE-ID"]
            when, _ = _parse_dt(rid, rp)
            overrides[(ev["props"].get("UID", ("", {}))[0], _local(when))] = ev
    for ev in raw_events:
        p = ev["props"]
        if "DTSTART" not in p or "RECURRENCE-ID" in p:
            continue
        try:
            first, all_day = _parse_dt(*p["DTSTART"])
            if "DTEND" in p:
                last, _ = _parse_dt(*p["DTEND"])
                length = last - first
            elif "DURATION" in p:
                length = _parse_duration(p["DURATION"][0])
            else:
                length = dt.timedelta(days=1) if all_day else dt.timedelta(hours=1)
        except (ValueError, TypeError):
            continue
        uid = p.get("UID", ("", {}))[0]
        starts: list[dt.datetime] = []
        if "RRULE" in p:
            try:
                rule_text = f"RRULE:{p['RRULE'][0]}"
                if first.tzinfo is None:
                    rule_text = re.sub(r"(UNTIL=\d{8}(?:T\d{6})?)Z", r"\1", rule_text)
                rule = rrulestr(rule_text, dtstart=first)
                lo = (start.astimezone(first.tzinfo) if first.tzinfo else start) - length
                hi = end.astimezone(first.tzinfo) if first.tzinfo else end
                if first.tzinfo is not None:
                    lo = lo.replace(tzinfo=first.tzinfo) if lo.tzinfo is None else lo
                starts = rule.between(lo, hi, inc=True)
            except Exception as e:  # noqa: BLE001 - an odd rule: keep the first occurrence
                logger.debug("Couldn't expand repeat rule %r: %s", p["RRULE"][0], e)
                starts = [first]
            exdates = set()
            for value, params in ev["multi"].get("EXDATE", []):
                for part in value.split(","):
                    try:
                        exdates.add(_local(_parse_dt(part, params)[0]))
                    except ValueError:
                        pass
            starts = [s for s in starts if _local(s) not in exdates]
        else:
            starts = [first]
        for s in starts:
            override = overrides.get((uid, _local(s)))
            src = override["props"] if override else p
            if src.get("STATUS", ("", {}))[0].upper() == "CANCELLED":
                continue
            if override and "DTSTART" in src:
                try:
                    s, all_day = _parse_dt(*src["DTSTART"])
                except ValueError:
                    pass
            s_local = _local(s)
            e_local = s_local + length
            if e_local <= start or s_local >= end:
                continue
            out.append(Event(title=_unescape(src.get("SUMMARY", ("(no title)", {}))[0]) or "(no title)",
                             start=s_local, end=e_local, all_day=all_day,
                             location=_unescape(src.get("LOCATION", ("", {}))[0]), uid=uid))
    return out


# ---------------------------------------------------------------- sources

class LocalCalendar:
    """VERONICA's own calendar (events added by voice)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> list[dict]:
        data, _ = jsonsafe.read_json(self.path, default=[])
        return data if isinstance(data, list) else []

    def add(self, title: str, start: dt.datetime, end: Optional[dt.datetime] = None, all_day: bool = False) -> Event:
        ev = Event(title=title.strip(), start=start, end=end or start + dt.timedelta(hours=1), all_day=all_day,
                   source="veronica", uid=uuid.uuid4().hex[:12])
        with _lock:
            items = self._load()
            items.append({**asdict(ev), "start": ev.start.isoformat(), "end": ev.end.isoformat()})
            jsonsafe.write_json(self.path, items)
        return ev

    def remove(self, words: str) -> Optional[Event]:
        words = (words or "").lower().strip()
        with _lock:
            items = self._load()
            for i, raw in enumerate(sorted(items, key=lambda r: r["start"])):
                if words and words in raw["title"].lower():
                    items.remove(raw)
                    jsonsafe.write_json(self.path, items)
                    return _from_raw(raw)
        return None

    def between(self, start: dt.datetime, end: dt.datetime) -> list[Event]:
        out = []
        for raw in self._load():
            try:
                ev = _from_raw(raw)
            except (KeyError, ValueError):
                continue
            if ev.end > start and ev.start < end:
                out.append(ev)
        return out


def _from_raw(raw: dict) -> Event:
    return Event(title=raw["title"], start=dt.datetime.fromisoformat(raw["start"]),
                 end=dt.datetime.fromisoformat(raw["end"]), all_day=bool(raw.get("all_day")),
                 location=raw.get("location", ""), source="veronica", uid=raw.get("uid", ""))


class Calendar:
    """All of the user's events: their iCal links (cached) + VERONICA's own."""

    def __init__(self, config=None, urls: Optional[list[str]] = None, fetch: Optional[Callable[[str], str]] = None,
                 local: Optional[LocalCalendar] = None) -> None:
        if urls is None:
            raw = (config.secret("CALENDAR_ICS_URLS") if config is not None else None) or ""
            urls = [u.strip() for u in raw.split(",") if u.strip()]
        self.urls = urls
        self._fetch = fetch or self._http_get
        self.local = local or LocalCalendar(config.resolve_path("data/calendar.json") if config is not None
                                            else Path("data/calendar.json"))
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self.last_error = ""

    @property
    def connected(self) -> bool:
        return bool(self.urls)

    @staticmethod
    def _http_get(url: str) -> str:
        import requests
        if url.startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        resp = requests.get(url, timeout=10, headers={"User-Agent": "VERONICA/1.0"})
        resp.raise_for_status()
        return resp.text

    def _raw(self, url: str) -> list[dict]:
        hit = self._cache.get(url)
        if hit and time.time() - hit[0] < _CACHE_S:
            return hit[1]
        try:
            raw = parse_ics(self._fetch(url))
            self._cache[url] = (time.time(), raw)
            self.last_error = ""
            return raw
        except Exception as e:  # noqa: BLE001 - a calendar being unreachable must not break the rest
            self.last_error = str(e)
            logger.warning("Calendar feed unreachable: %s", type(e).__name__)
            return hit[1] if hit else []

    def events(self, start: dt.datetime, end: dt.datetime) -> list[Event]:
        out: list[Event] = []
        for url in self.urls:
            out.extend(expand(self._raw(url), start, end))
        out.extend(self.local.between(start, end))
        seen, unique = set(), []
        for ev in sorted(out, key=lambda e: (not e.all_day, e.start)):
            sig = (ev.title.lower(), ev.start)
            if sig not in seen:
                seen.add(sig)
                unique.append(ev)
        return unique
