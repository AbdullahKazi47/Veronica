"""
Everyday voice productivity: home city, reminders/timers, and a to-do list.

Ideas taken (not code) from the J.A.R.V.I.S to-do list and Prodigy-AI's reminder/timer
roadmap. Everything is local JSON under data/, SAFE-level (no external effects), and
fast-path routable so "remind me in 10 minutes to call mom" never waits on the LLM.
Reminders are *announced* by the voice CLI (see ui/cli.py ReminderAnnouncer), which
polls `ReminderStore.pop_due()`.
"""
from __future__ import annotations

import datetime as dt
import difflib
import json
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Optional

from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty five": 45, "fifty": 50,
    "sixty": 60, "ninety": 90, "couple of": 2, "few": 3,
}
_UNIT_SECONDS = {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600, "hr": 3600}
_NUM = r"(\d+(?:\.\d+)?|" + "|".join(sorted(map(re.escape, _NUMBER_WORDS), key=len, reverse=True)) + r")"
_RELATIVE = re.compile(rf"\b(?:in|for|after)\s+{_NUM}\s*(second|sec|minute|min|hour|hr)s?\b", re.I)
_HALF_HOUR = re.compile(r"\b(?:in|for|after)\s+(?:half an hour|half hour|30 mins?)\b", re.I)
_ABSOLUTE = re.compile(r"\bat\s+(\d{1,2})(?:[:.](\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?(?=\s|$|[,.!?])", re.I)
_TOMORROW = re.compile(r"\btomorrow\b", re.I)


def _data_path(config, name: str) -> Path:
    return config.resolve_path(f"data/{name}") if config else Path("data") / name


_PATH_LOCKS: dict[str, threading.RLock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    # One lock per file, shared by every store instance: the CLI's reminder announcer
    # and the reminder tools hold separate ReminderStore objects on the same file.
    key = str(path.resolve())
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(key, threading.RLock())


class _JsonStore:
    """Tiny thread-safe JSON file store."""

    def __init__(self, path: Path, default) -> None:
        self.path = path
        self._default = default
        self._lock = _lock_for(path)

    def update(self, fn):
        """Atomically load, transform with fn(data) -> (new_data, result), save, return result."""
        with self._lock:
            data, result = fn(self.load())
            self.save(data)
            return result

    def load(self):
        with self._lock:
            if not self.path.exists():
                return json.loads(json.dumps(self._default))
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                return json.loads(json.dumps(self._default))

    def save(self, data) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)


# ---------------------------------------------------------------- profile / home city

_DETECT_TTL_S = 6 * 3600       # re-check every few hours: laptops move


_PS_LOCATION = (
    "Add-Type -AssemblyName System.Device; "
    "$w = New-Object System.Device.Location.GeoCoordinateWatcher('High'); "
    "$null = $w.TryStart($false, [TimeSpan]::FromSeconds(8)); "
    "$t = (Get-Date).AddSeconds(10); "
    "while ($w.Status -ne 'Ready' -and $w.Permission -ne 'Denied' -and (Get-Date) -lt $t) { Start-Sleep -Milliseconds 250 }; "
    "$c = $w.Position.Location; $w.Stop(); "
    "if (-not $c.IsUnknown) { '{0} {1} {2}' -f $c.Latitude, $c.Longitude, $c.HorizontalAccuracy }")


# Windows' estimate is only trusted down to a neighbourhood when it's this precise. On a
# PC without Wi-Fi/GPS positioning it is a ~5 km guess from the internet connection, and
# naming the village under that point ("Naganahalli", "Kokti") sounds exact but is wrong.
PRECISE_ENOUGH_M = 1500


def _windows_location() -> Optional[tuple[float, float, float]]:
    """(lat, lon, accuracy in metres) from Windows location services. None when location
    is off or unavailable."""
    import platform
    import subprocess
    if platform.system() != "Windows":
        return None
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", _PS_LOCATION], capture_output=True,
                             text=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        lat, lon, acc = (float(x) for x in out.stdout.split()[:3])
        return lat, lon, acc
    except Exception:  # noqa: BLE001
        return None


def _clean_city(name: str) -> str:
    """'Bengaluru Urban' (a district) -> 'Bengaluru'."""
    return re.sub(r"\s+(?:urban|rural|district)$", "", (name or "").strip(), flags=re.I)


def _place_name(lat: float, lon: float, accuracy_m: Optional[float] = None) -> Optional[str]:
    """Place name for coordinates (OpenStreetMap Nominatim, keyless). Only as specific as
    the position is: a rough fix gives the city, a precise one 'neighbourhood, city'."""
    import requests
    coarse = accuracy_m is not None and accuracy_m > PRECISE_ENOUGH_M
    try:
        a = requests.get("https://nominatim.openstreetmap.org/reverse", timeout=6,
                         params={"lat": lat, "lon": lon, "format": "json", "zoom": 10 if coarse else 14,
                                 "addressdetails": 1},
                         headers={"User-Agent": "VERONICA/1.0 personal voice assistant"}).json().get("address", {})
    except Exception:  # noqa: BLE001
        return None
    city = _clean_city(next((a[k] for k in ("city", "town", "state_district", "county") if a.get(k)), ""))
    if coarse:
        return city or None
    local = next((a[k] for k in ("suburb", "neighbourhood", "village", "town", "city_district") if a.get(k)), "")
    if local and city and local != city:
        return f"{local}, {city}"
    return local or city or None


def geocode_place(name: str) -> Optional[dict]:
    """'Indiranagar, Bengaluru' -> {'lat', 'lon', 'name'} (Nominatim search). None if unknown."""
    import requests
    try:
        hits = requests.get("https://nominatim.openstreetmap.org/search", timeout=8,
                            params={"q": name, "format": "json", "limit": 1, "addressdetails": 1},
                            headers={"User-Agent": "VERONICA/1.0 personal voice assistant"}).json()
    except Exception:  # noqa: BLE001
        return None
    if not hits:
        return None
    h = hits[0]
    a = h.get("address", {})
    local = next((a[k] for k in ("suburb", "neighbourhood", "village", "quarter", "city_district") if a.get(k)), "")
    city = _clean_city(next((a[k] for k in ("city", "town", "state_district", "county") if a.get(k)), ""))
    label = f"{local}, {city}" if local and city and local != city else (city or local or name)
    return {"lat": float(h["lat"]), "lon": float(h["lon"]), "name": label}


def _detect_city_by_ip() -> Optional[str]:
    """Approximate city from the public IP (keyless services; HTTPS). None on failure.
    Often the internet provider's hub city, not where the user is — the last resort."""
    import requests
    for url, field in (("https://ipwho.is/", "city"), ("https://ipapi.co/json/", "city")):
        try:
            data = requests.get(url, timeout=3, headers={"User-Agent": "VERONICA/1.0"}).json()
            city = (data.get(field) or "").strip()
            if city:
                return city
        except Exception:  # noqa: BLE001 — try the next service
            continue
    return None


def _detect_location() -> Optional[dict]:
    """{'name', 'lat', 'lon', 'via'}: Windows location first, then the IP guess."""
    fix = _windows_location()
    if fix:
        lat, lon, acc = fix
        name = _place_name(lat, lon, acc)
        if name:
            return {"name": name, "lat": lat, "lon": lon, "via": "Windows location", "accuracy_m": acc}
    city = _detect_city_by_ip()
    return {"name": city, "via": "your internet connection"} if city else None


def _gps_fix():
    from veronica.integrations.gps import current_fix
    return current_fix()


class Profile:
    """User facts VERONICA needs to act on by default (home city). Order: what the user
    said by voice > config `assistant.home_city` > detected from the IP (only when
    `assistant.auto_detect_location` is on; cached for a week)."""

    def __init__(self, config=None, detector=None, gps=None, namer=None) -> None:
        self.config = config
        self._store = _JsonStore(_data_path(config, "profile.json"), {})
        # detector() -> {'name', 'lat'?, 'lon'?, 'via'} or a plain city string (tests)
        self._detector = detector or _detect_location
        self._gps = gps or _gps_fix              # () -> a fresh gps.Fix or None
        self._namer = namer or _place_name
        self._gps_names: dict[tuple[int, int], str] = {}

    def gps_place(self) -> Optional[tuple[str, float, float, float]]:
        """(place, lat, lon, accuracy) from a live GPS fix, or None. A real GPS is where the
        user is right now, so it beats every other source. Names are looked up once per
        ~1 km square."""
        try:
            fix = self._gps()
        except Exception:  # noqa: BLE001
            fix = None
        if fix is None:
            return None
        cell = (round(fix.lat * 100), round(fix.lon * 100))
        name = self._gps_names.get(cell)
        if name is None:
            name = self._namer(fix.lat, fix.lon, fix.accuracy_m)
            if not name:
                return None
            self._gps_names[cell] = name
        return name, fix.lat, fix.lon, fix.accuracy_m

    def _assistant(self, field: str, default=None):
        return getattr(getattr(self.config, "assistant", None), field, default)

    def home_city(self) -> Optional[str]:
        return self.city_and_source()[0]

    def city_and_source(self) -> tuple[Optional[str], str]:
        live = self.gps_place()
        if live:
            return live[0], "gps"
        data = self._store.load()
        if data.get("home_city"):
            return data["home_city"], "user"
        if self._assistant("home_city"):
            return self._assistant("home_city"), "config"
        if not self._assistant("auto_detect_location", False):
            return None, ""
        detected = data.get("detected_city")
        if detected and dt.datetime.now().timestamp() - data.get("detected_at", 0) < _DETECT_TTL_S:
            return detected, "detected"
        found = self._detector()
        if isinstance(found, str):
            found = {"name": found, "via": "your internet connection"}
        if found and found.get("name"):
            self._store.update(lambda d: ({**d, "detected_city": found["name"], "detected_lat": found.get("lat"),
                                           "detected_lon": found.get("lon"), "detected_via": found.get("via", ""),
                                           "detected_accuracy_m": found.get("accuracy_m"),
                                           "detected_at": dt.datetime.now().timestamp()}, None))
            return found["name"], "detected"
        return detected, "detected" if detected else ""  # stale beats nothing when offline

    def detected_coords(self) -> Optional[tuple[float, float]]:
        """Coordinates for 'here': the area the user told me (looked up when they said it),
        else Windows location. None when only a city name is known."""
        live = self.gps_place()
        if live:
            return live[1], live[2]
        city, source = self.city_and_source()
        data = self._store.load()
        if source == "user" and data.get("home_lat") is not None and data.get("home_lon") is not None:
            return float(data["home_lat"]), float(data["home_lon"])
        if source == "detected" and data.get("detected_lat") is not None and data.get("detected_lon") is not None:
            return float(data["detected_lat"]), float(data["detected_lon"])
        return None

    def detection_notice(self) -> str:
        """One-time 'I detected you're in X' (so a silent guess is never a surprise)."""
        city, source = self.city_and_source()
        if source != "detected" or not city:
            return ""
        data = self._store.load()
        if data.get("detected_announced") == city:
            return ""
        self._store.update(lambda d: ({**d, "detected_announced": city}, None))
        via = data.get("detected_via") or "your internet connection"
        acc = data.get("detected_accuracy_m")
        rough = (f" Windows only knows your position to within about {round(acc / 1000)} kilometres, so tell me "
                 f"your area, like 'I live in Indiranagar, Bengaluru', for exact weather."
                 if acc and acc > PRECISE_ENOUGH_M else " Say 'I live in ...' to change it.")
        return f" (I think you're in {city}, from {via}.{rough})"

    def set_home_city(self, city: str, lat: Optional[float] = None, lon: Optional[float] = None) -> None:
        self._store.update(lambda data: ({**data, "home_city": city, "home_lat": lat, "home_lon": lon}, None))


_SET_CITY = re.compile(
    r"^\s*(?:(?:please\s+)?set\s+my\s+(?:home\s+)?(?:city|location)\s+(?:to|as)|"
    r"my\s+(?:home\s+)?(?:city|location|area)\s+is|i\s+live\s+in|i(?:'m|\s+am)\s+(?:based|located|staying)\s+in|"
    r"i\s+stay\s+in|my\s+home\s+is\s+in)\s+"
    r"([a-zA-Z][a-zA-Z\s.,'\-]{1,60}?)\s*[.!]?\s*$", re.I)


class SetHomeCityTool(Tool):
    name = "set_home_city"
    description = ("Remember the user's home city, used by default for weather and the daily "
                   "briefing. Use when the user says where they live.")
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "City name, e.g. 'Mumbai'."}},
        "required": ["city"],
    }

    def __init__(self, config=None, profile: Optional[Profile] = None, geocode=None) -> None:
        self.profile = profile or Profile(config)
        self._geocode = geocode           # None = look up via geocode_place at call time

    def matches(self, text: str) -> bool:
        return bool(_SET_CITY.match(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _SET_CITY.match(text)
        return {"city": m.group(1).strip(" ,").title()} if m else {}

    def run(self, city: str = "", **kwargs: Any) -> ToolResult:
        city = (city or "").strip(" ,.")
        if not city:
            return ToolResult.fail("Which city or area should I use?")
        found = None
        try:
            found = (self._geocode or geocode_place)(city)
        except Exception:  # noqa: BLE001 - offline: keep the name, look it up later
            found = None
        if found:
            self.profile.set_home_city(found["name"], found["lat"], found["lon"])
            return ToolResult.ok(f"Got it, you're in {found['name']}. I'll use that for weather and anything "
                                 "near you.", city=found["name"], lat=found["lat"], lon=found["lon"])
        self.profile.set_home_city(city)
        return ToolResult.ok(f"Got it, your home is {city}. I couldn't find it on the map, so I'll use the name.",
                             city=city)


# ---------------------------------------------------------------- reminders and timers

def parse_when(text: str, now: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
    """'in 10 minutes', 'for half an hour', 'at 5 pm', 'tomorrow at 9:30' -> datetime."""
    now = now or dt.datetime.now()
    if _HALF_HOUR.search(text):
        return now + dt.timedelta(minutes=30)
    m = _RELATIVE.search(text)
    if m:
        raw = m.group(1).lower()
        amount = float(raw) if raw[0].isdigit() else _NUMBER_WORDS[raw]
        return now + dt.timedelta(seconds=amount * _UNIT_SECONDS[m.group(2).lower()])
    m = _ABSOLUTE.search(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        ampm = (m.group(3) or "").replace(".", "").lower()
        if hour > 23 or minute > 59 or (ampm and not 1 <= hour <= 12):
            return None
        if ampm == "pm" and hour != 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        when = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if _TOMORROW.search(text):
            when += dt.timedelta(days=1)
        elif when <= now:
            # "at 7" said at 6 pm with no am/pm most likely means 7 pm; otherwise tomorrow.
            if not ampm and hour < 12 and when + dt.timedelta(hours=12) > now:
                when += dt.timedelta(hours=12)
            else:
                when += dt.timedelta(days=1)
        return when
    return None


def _speak_time(when: dt.datetime, now: dt.datetime) -> str:
    clock = when.strftime("%I:%M %p").lstrip("0")
    if when.date() == now.date():
        return f"at {clock}"
    if when.date() == (now + dt.timedelta(days=1)).date():
        return f"tomorrow at {clock}"
    return f"on {when.strftime('%A %d %B')} at {clock}"


class ReminderStore:
    def __init__(self, config=None) -> None:
        self._store = _JsonStore(_data_path(config, "reminders.json"), [])

    def add(self, text: str, due: dt.datetime) -> dict:
        item = {"id": uuid.uuid4().hex[:8], "text": text, "due": due.isoformat(timespec="seconds"),
                "created": dt.datetime.now().isoformat(timespec="seconds")}
        return self._store.update(lambda items: (items + [item], item))

    def pending(self) -> list[dict]:
        return sorted(self._store.load(), key=lambda r: r["due"])

    def pop_due(self, now: Optional[dt.datetime] = None) -> list[dict]:
        """Remove and return every reminder that is due (including missed ones)."""
        now = now or dt.datetime.now()

        def take(items):
            due = [r for r in items if dt.datetime.fromisoformat(r["due"]) <= now]
            return [r for r in items if r not in due], sorted(due, key=lambda r: r["due"])
        return self._store.update(take)

    def cancel(self, query: str) -> Optional[dict]:
        q = (query or "").lower().strip()

        def drop(items):
            if not items:
                return items, None
            if q in ("", "last", "latest", "it", "that", "the reminder", "my reminder", "timer", "the timer"):
                target = max(items, key=lambda r: r["created"])
            else:
                target = next((r for r in items if q in r["text"].lower()), None)
                if target is None:
                    close = difflib.get_close_matches(q, [r["text"].lower() for r in items], n=1, cutoff=0.5)
                    target = next((r for r in items if close and r["text"].lower() == close[0]), None)
            if target is None:
                return items, None
            return [r for r in items if r["id"] != target["id"]], target
        return self._store.update(drop)


_REMIND = re.compile(r"^\s*(?:please\s+|hey\s+)?(?:remind\s+me|set\s+(?:a|an|the)?\s*(?:reminder|timer|alarm)|"
                     r"start\s+(?:a|the)?\s*timer|wake\s+me\s+up)\b", re.I)
_TASK = re.compile(r"\b(?:to|that|about)\s+(.+)$", re.I)


def _strip_when(text: str) -> str:
    for pattern in (_HALF_HOUR, _RELATIVE, _ABSOLUTE, _TOMORROW):
        text = pattern.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip(" ,.!?")


class SetReminderTool(Tool):
    name = "set_reminder"
    description = ("Set a reminder or timer that VERONICA will announce out loud when it is due. "
                   "`when` is natural language: 'in 10 minutes', 'for 25 minutes', 'at 5 pm', "
                   "'tomorrow at 9:30 am'.")
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to remind about, e.g. 'call mom'."},
            "when": {"type": "string", "description": "When it is due, e.g. 'in 10 minutes' or 'at 5 pm'."},
        },
        "required": ["when"],
    }

    def __init__(self, config=None, store: Optional[ReminderStore] = None) -> None:
        self.store = store or ReminderStore(config)

    def matches(self, text: str) -> bool:
        return bool(_REMIND.match(text)) and parse_when(text) is not None

    def extract_arguments(self, text: str) -> dict[str, Any]:
        rest = _REMIND.sub("", text, count=1)
        task = _TASK.search(_strip_when(rest))
        return {"text": task.group(1).strip(" .!?") if task else "", "when": text}

    def run(self, when: str = "", text: str = "", **kwargs: Any) -> ToolResult:
        now = dt.datetime.now()
        due = parse_when(when or "", now)
        if due is None:
            return ToolResult.fail("I didn't catch when. Try 'in 10 minutes' or 'at 5 pm'.")
        label = (text or "").strip() or "Your timer is done"
        item = self.store.add(label, due)
        if not text:
            mins = round((due - now).total_seconds() / 60)
            spoken = f"{mins} minute{'s' if mins != 1 else ''}" if mins < 120 else _speak_time(due, now)
            return ToolResult.ok(f"Timer set for {spoken}.", id=item["id"], due=item["due"])
        return ToolResult.ok(f"Okay, I'll remind you to {label} {_speak_time(due, now)}.",
                             id=item["id"], due=item["due"])


_LIST_REMINDERS = re.compile(r"\b(?:what(?:'s| are| is)?\s+my\s+reminders|list\s+(?:my\s+)?reminders|"
                             r"any\s+reminders|do\s+i\s+have\s+(?:any\s+)?reminders|show\s+(?:my\s+)?reminders)\b", re.I)


class ListRemindersTool(Tool):
    name = "list_reminders"
    description = "List the reminders and timers that are still pending."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, config=None, store: Optional[ReminderStore] = None) -> None:
        self.store = store or ReminderStore(config)

    def matches(self, text: str) -> bool:
        return bool(_LIST_REMINDERS.search(text))

    def run(self, **kwargs: Any) -> ToolResult:
        items = self.store.pending()
        if not items:
            return ToolResult.ok("You have no reminders set.", count=0)
        now = dt.datetime.now()
        parts = [f"{r['text']} {_speak_time(dt.datetime.fromisoformat(r['due']), now)}" for r in items[:5]]
        more = f", and {len(items) - 5} more" if len(items) > 5 else ""
        return ToolResult.ok(f"You have {len(items)} reminder{'s' if len(items) != 1 else ''}: "
                             + "; ".join(parts) + more + ".", count=len(items))


_CANCEL = re.compile(r"^\s*(?:please\s+)?(?:cancel|delete|remove|stop)\s+(?:the\s+|my\s+)?"
                     r"(?:(reminder|timer|alarm)(?:\s+(?:to|about|for)\s+(.+))?|(.+?)\s+reminder)\s*[.!]?\s*$", re.I)


class CancelReminderTool(Tool):
    name = "cancel_reminder"
    description = "Cancel a pending reminder or timer. `which` is part of its text, or 'last'."
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"which": {"type": "string", "description": "Words from the reminder, or 'last'."}},
        "required": [],
    }

    def __init__(self, config=None, store: Optional[ReminderStore] = None) -> None:
        self.store = store or ReminderStore(config)

    def matches(self, text: str) -> bool:
        return bool(_CANCEL.match(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _CANCEL.match(text)
        return {"which": (m.group(2) or m.group(3) or "last").strip()} if m else {}

    def run(self, which: str = "last", **kwargs: Any) -> ToolResult:
        item = self.store.cancel(which)
        if item is None:
            return ToolResult.fail("I couldn't find that reminder.")
        return ToolResult.ok(f"Cancelled the reminder: {item['text']}.", id=item["id"])


# ---------------------------------------------------------------- to-do list

class TodoStore:
    def __init__(self, config=None) -> None:
        self._store = _JsonStore(_data_path(config, "todos.json"), [])

    def open_items(self) -> list[dict]:
        return [t for t in self._store.load() if not t.get("done")]

    def add(self, text: str) -> dict:
        item = {"id": uuid.uuid4().hex[:8], "text": text, "done": False,
                "created": dt.datetime.now().isoformat(timespec="seconds")}
        return self._store.update(lambda items: (items + [item], item))

    def _find(self, items: list[dict], query: str) -> Optional[dict]:
        q = query.lower().strip()
        open_ = [t for t in items if not t.get("done")]
        hit = next((t for t in open_ if t["text"].lower() == q), None) or \
            next((t for t in open_ if q in t["text"].lower()), None)
        if hit is None:
            close = difflib.get_close_matches(q, [t["text"].lower() for t in open_], n=1, cutoff=0.6)
            hit = next((t for t in open_ if close and t["text"].lower() == close[0]), None)
        return hit

    def complete(self, query: str) -> Optional[dict]:
        def mark(items):
            hit = self._find(items, query)
            if hit is not None:
                hit["done"] = True
                hit["completed"] = dt.datetime.now().isoformat(timespec="seconds")
            return items, hit
        return self._store.update(mark)

    def remove(self, query: str) -> Optional[dict]:
        def drop(items):
            hit = self._find(items, query)
            return ([t for t in items if hit is None or t["id"] != hit["id"]], hit)
        return self._store.update(drop)


_LIST_WORD = r"(?:to-?\s?do|todo|task|shopping)\s+list"
_TODO_ADD = re.compile(rf"^\s*(?:please\s+)?(?:add|put)\s+(.+?)\s+(?:to|on)\s+(?:my|the)\s+{_LIST_WORD}\s*[.!]?\s*$", re.I)
_TODO_LIST = re.compile(rf"\b(?:what(?:'s| is)\s+on|read|show|list|check)\s+(?:me\s+)?(?:my|the)\s+{_LIST_WORD}\b|"
                        rf"\bwhat\s+(?:do\s+i\s+have\s+to\s+do|are\s+my\s+tasks)\b", re.I)
_TODO_DONE = re.compile(rf"^\s*(?:please\s+)?(?:mark|tick\s+off|check\s+off|cross\s+off|complete)\s+(.+?)"
                        rf"(?:\s+as\s+(?:done|complete|completed|finished))?(?:\s+(?:on|from)\s+(?:my|the)\s+{_LIST_WORD})?"
                        r"\s*[.!]?\s*$", re.I)
_TODO_REMOVE = re.compile(rf"^\s*(?:please\s+)?(?:remove|delete|take)\s+(.+?)\s+(?:off|from)\s+(?:my|the)\s+{_LIST_WORD}"
                          r"\s*[.!]?\s*$", re.I)


class TodoTool(Tool):
    name = "todo_list"
    description = ("Manage the user's to-do list. action: 'add' (item), 'list', 'done' (item), "
                   "or 'remove' (item).")
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "list", "done", "remove"]},
            "item": {"type": "string", "description": "The to-do item (not needed for 'list')."},
        },
        "required": ["action"],
    }

    def __init__(self, config=None, store: Optional[TodoStore] = None) -> None:
        self.store = store or TodoStore(config)

    def matches(self, text: str) -> bool:
        return any(p.search(text) for p in (_TODO_ADD, _TODO_LIST, _TODO_REMOVE)) or \
            (bool(_TODO_DONE.match(text)) and bool(re.search(r"\b(done|complete|completed|finished|list)\b|tick|check|cross", text, re.I)))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        for action, pattern in (("add", _TODO_ADD), ("remove", _TODO_REMOVE), ("done", _TODO_DONE)):
            m = pattern.match(text)
            if m:
                return {"action": action, "item": m.group(1).strip()}
        return {"action": "list"}

    def run(self, action: str = "list", item: str = "", **kwargs: Any) -> ToolResult:
        action = (action or "list").lower()
        item = (item or "").strip().strip(".!?")
        if action == "add":
            if not item:
                return ToolResult.fail("What should I add?")
            self.store.add(item)
            n = len(self.store.open_items())
            return ToolResult.ok(f"Added {item} to your to-do list. That's {n} item{'s' if n != 1 else ''}.", count=n)
        if action in ("done", "remove"):
            if not item:
                return ToolResult.fail("Which item?")
            hit = self.store.complete(item) if action == "done" else self.store.remove(item)
            if hit is None:
                return ToolResult.fail(f"I couldn't find {item} on your to-do list.")
            verb = "Marked" if action == "done" else "Removed"
            tail = " as done" if action == "done" else " from your list"
            return ToolResult.ok(f"{verb} {hit['text']}{tail}.", id=hit["id"])
        items = self.store.open_items()
        if not items:
            return ToolResult.ok("Your to-do list is empty.", count=0)
        names = [t["text"] for t in items]
        listed = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f", and {names[-1]}"
        return ToolResult.ok(f"You have {len(names)} thing{'s' if len(names) != 1 else ''} to do: {listed}.",
                             count=len(names))
