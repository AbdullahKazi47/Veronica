"""
Calendar by voice:
  "what's on today?" / "what's on my calendar tomorrow?" / "what's my next meeting?"
  "what do I have this week?" / "am I free tomorrow afternoon?" / "anything on friday?"
  "add dentist to my calendar tomorrow at 4 pm" / "schedule a call with Ravi on friday at 3"
  "remove dentist from my calendar"  /  "connect my google calendar"
Your Google calendars are read-only (their private iCal links); events added by voice go to
VERONICA's own calendar, with a reminder 10 minutes before.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Callable, Optional

from veronica.integrations.calendar import Calendar, Event
from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("tools.calendar")

_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_PARTS = {"morning": (6, 12), "afternoon": (12, 17), "evening": (17, 22), "night": (19, 24)}

_ASK = re.compile(
    r"\b(?:what'?s|what\s+is|what\s+do\s+i\s+have|do\s+i\s+have\s+(?:anything|any\s+(?:meetings?|events?|plans?))|"
    r"anything|any\s+(?:meetings?|events?|plans?))\b.*\b(?:on|today|tomorrow|tonight|this\s+week|next\s+week|"
    r"calendar|schedule|agenda|planned|"
    + "|".join(_WEEKDAYS) + r")\b|"
    r"\b(?:my|the)\s+(?:calendar|schedule|agenda)\b(?!.*\b(?:add|put|schedule|remove|delete|cancel)\b)|"
    r"\bwhat'?s\s+(?:my\s+)?next\s+(?:meeting|event|appointment|thing)\b|\bwhat'?s\s+next\b|"
    r"\bam\s+i\s+(?:free|busy)\b", re.I)
_ADD = re.compile(r"^\s*(?:please\s+)?(?:add|put|create|schedule|book|set\s+up|make)\s+(?P<rest>.+)$", re.I)
_REMOVE = re.compile(r"^\s*(?:please\s+)?(?:remove|delete|cancel|take)\s+(?:the\s+|my\s+)?(?P<what>.+?)\s+"
                     r"(?:from|off|out\s+of)\s+(?:my\s+|the\s+)?calendar\s*[.!]?\s*$", re.I)
_CONNECT = re.compile(r"\b(?:connect|link|set\s+up|add|sync)\s+(?:my\s+)?(?:google\s+)?calendar\b", re.I)


def _clock(t: dt.datetime) -> str:
    return t.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")


def _day_name(d: dt.date, today: dt.date) -> str:
    if d == today:
        return "today"
    if d == today + dt.timedelta(days=1):
        return "tomorrow"
    return d.strftime("%A %d %B")


def query_range(text: str, now: dt.datetime) -> tuple[dt.datetime, dt.datetime, str]:
    """The span of time a calendar question is about, and how to say it."""
    t = (text or "").lower()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if re.search(r"\bnext\s+week\b", t):
        start = day0 + dt.timedelta(days=7 - day0.weekday())
        return start, start + dt.timedelta(days=7), "next week"
    if re.search(r"\bthis\s+week\b|\bweek\b", t):
        return now, day0 + dt.timedelta(days=7 - day0.weekday()), "this week"
    if re.search(r"\b(?:this\s+)?weekend\b", t):
        sat = day0 + dt.timedelta(days=(5 - day0.weekday()) % 7)
        return sat, sat + dt.timedelta(days=2), "this weekend"
    base, label = day0, "today"
    if re.search(r"\btomorrow\b", t):
        base, label = day0 + dt.timedelta(days=1), "tomorrow"
    else:
        for i, name in enumerate(_WEEKDAYS):
            if re.search(rf"\b{name}\b", t):
                ahead = (i - day0.weekday()) % 7
                base = day0 + dt.timedelta(days=ahead)
                label = "today" if ahead == 0 else name.capitalize()
                break
        else:
            m = re.search(r"\bon\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)?(?:\s+(?:of\s+)?([a-z]+))?\b", t)
            if m:
                try:
                    import dateparser
                    when = dateparser.parse(m.group(0)[3:], settings={"PREFER_DATES_FROM": "future"})
                    if when:
                        base, label = when.replace(hour=0, minute=0, second=0, microsecond=0), when.strftime("%A %d %B")
                except Exception:  # noqa: BLE001
                    pass
    for part, (h0, h1) in _PARTS.items():
        if re.search(rf"\b{part}\b", t) or (part == "night" and "tonight" in t):
            return base.replace(hour=h0), base + dt.timedelta(hours=h1), f"{label} {part}".replace("today night", "tonight")
    return base, base + dt.timedelta(days=1), label


def describe(events: list[Event], label: str, now: dt.datetime) -> str:
    if not events:
        return f"Nothing on your calendar {label}." if label not in ("", "today") else "Nothing on your calendar today."
    multi_day = len({e.start.date() for e in events}) > 1
    bits = []
    for e in events[:8]:
        when = "all day" if e.all_day else _clock(e.start)
        if multi_day:
            when = f"{_day_name(e.start.date(), now.date())} {when}"
        where = f" at {e.location}" if e.location and len(e.location) < 40 else ""
        bits.append(f"{when}, {e.title}{where}")
    more = f", and {len(events) - 8} more" if len(events) > 8 else ""
    n = len(events)
    return f"{label.capitalize()} you have {n} thing{'s' if n != 1 else ''}: " + "; ".join(bits) + more + "."


class CalendarTool(Tool):
    name = "calendar"
    description = ("Read the user's calendar: what's on today/tomorrow/a weekday/this week, the next event, or "
                   "whether they're free at some time. `when` is the user's words for the time span.")
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"when": {"type": "string"},
                                                   "next_only": {"type": "boolean"}}, "required": []}

    def __init__(self, config=None, calendar: Optional[Calendar] = None,
                 now: Callable[[], dt.datetime] = dt.datetime.now) -> None:
        self.calendar = calendar or Calendar(config)
        self._now = now

    def matches(self, text: str) -> bool:
        if _ADD.match(text) and "calendar" in text.lower():
            return False
        # "what's on my to-do list / the screen / TV" and "any news" are other tools' questions.
        return bool(_ASK.search(text)) and not re.search(
            r"\bweather|news|headlines|time\s+is\s+it\b|\bto-?\s?do\b|\blist\b|\breminders?\b|\bscreen\b|"
            r"\btv\b|\bplaylist\b|\bnetflix\b|\byoutube\b|\bspotify\b|\bmind\b", text, re.I)

    def extract_arguments(self, text: str) -> dict[str, Any]:
        nxt = bool(re.search(r"\bnext\s+(?:meeting|event|appointment|thing)\b|\bwhat'?s\s+next\b", text, re.I))
        free = bool(re.search(r"\bam\s+i\s+(?:free|busy)\b", text, re.I))
        return {"when": text, "next_only": nxt, "free_check": free}

    def run(self, when: str = "", next_only: bool = False, free_check: bool = False, **kwargs: Any) -> ToolResult:
        now = self._now()
        if next_only:
            upcoming = [e for e in self.calendar.events(now, now + dt.timedelta(days=30)) if e.start >= now or e.all_day]
            upcoming = [e for e in upcoming if not e.all_day] or upcoming
            if not upcoming:
                return ToolResult.ok("Nothing coming up in the next month." + self._setup_hint())
            e = upcoming[0]
            return ToolResult.ok(f"Next up: {e.title}, {_day_name(e.start.date(), now.date())} at {_clock(e.start)}.",
                                 title=e.title, start=e.start.isoformat())
        start, end, label = query_range(when, now)
        events = self.calendar.events(start, end)
        if free_check:
            busy = [e for e in events if not e.all_day]
            if not busy:
                return ToolResult.ok(f"You're free {label}." + self._setup_hint())
            return ToolResult.ok(f"Not entirely. {describe(busy, label, now)}")
        return ToolResult.ok(describe(events, label, now) + (self._setup_hint() if not events else ""),
                             count=len(events))

    def _setup_hint(self) -> str:
        if self.calendar.connected:
            return ""
        return " (Your Google Calendar isn't connected yet; say 'connect my calendar' to see how.)"


class AddEventTool(Tool):
    name = "add_event"
    description = ("Add an event to VERONICA's own calendar (with a reminder 10 minutes before). `title` and "
                   "`when` in the user's words, e.g. title 'dentist', when 'tomorrow at 4 pm'. The user's Google "
                   "calendars are read-only.")
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"title": {"type": "string"}, "when": {"type": "string"},
                                                   "minutes": {"type": "integer", "description": "Length, default 60."}},
                  "required": ["title", "when"]}

    def __init__(self, config=None, calendar: Optional[Calendar] = None, reminders=None,
                 now: Callable[[], dt.datetime] = dt.datetime.now) -> None:
        self.calendar = calendar or Calendar(config)
        self._reminders = reminders
        self._config = config
        self._now = now

    def matches(self, text: str) -> bool:
        m = _ADD.match(text)
        if not m:
            return False
        low = text.lower()
        return ("calendar" in low or re.match(r"^\s*(?:please\s+)?(?:schedule|book)\b", low) is not None) \
            and self._split(m.group("rest"))[1] is not None

    def _split(self, rest: str) -> tuple[str, Optional[dt.datetime]]:
        rest = re.sub(r"\s+(?:to|in|on|into)\s+(?:my\s+|the\s+)?calendar\b", "", rest, flags=re.I).strip(" .")
        when = None
        try:
            from dateparser.search import search_dates
            found = search_dates(rest, settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": self._now()},
                                 languages=["en"]) or []
        except Exception:  # noqa: BLE001
            found = []
        title = rest
        if found:
            phrase = " ".join(p for p, _ in found)
            when = found[0][1]
            if len(found) > 1:     # "tomorrow" + "at 4 pm" found separately: combine day and time
                day = next((d for p, d in found if not re.search(r"\d", p)), found[0][1])
                hour = next((d for p, d in found if re.search(r"\d", p)), found[-1][1])
                when = day.replace(hour=hour.hour, minute=hour.minute)
            for p, _ in found:
                title = title.replace(p, " ")
            title = re.sub(r"\s+(?:on|at|for|from|this|next)\s*$", "", re.sub(r"\s+", " ", title)).strip(" ,.")
        title = re.sub(r"^(?:an?\s+|my\s+|the\s+)?(?:event|meeting|appointment)?\s*(?:called|named|for|about)?\s*", "",
                       title, flags=re.I).strip(" ,.") or "Event"
        return title, when

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _ADD.match(text)
        title, when = self._split(m.group("rest")) if m else ("", None)
        return {"title": title, "when": when.isoformat() if when else ""}

    def run(self, title: str = "", when: str = "", minutes: int = 60, **kwargs: Any) -> ToolResult:
        start = None
        if when:
            try:
                start = dt.datetime.fromisoformat(when)
            except ValueError:
                _, start = self._split(f"{title} {when}")
        if start is None:
            return ToolResult.fail("When is it? Say something like 'tomorrow at 4 pm'.")
        now = self._now()
        if start < now - dt.timedelta(minutes=1):
            return ToolResult.fail(f"That time has already passed: {_day_name(start.date(), now.date())} at {_clock(start)}.")
        ev = self.calendar.local.add(title, start, start + dt.timedelta(minutes=int(minutes or 60)))
        note = ""
        try:
            reminders = self._reminders
            if reminders is None:
                from veronica.tools.productivity_tools import ReminderStore
                reminders = ReminderStore(self._config)
            due = start - dt.timedelta(minutes=10)
            if due > now:
                reminders.add(f"{title} in 10 minutes", due)
                note = " I'll remind you 10 minutes before."
        except Exception as e:  # noqa: BLE001 - the event is saved even if the reminder isn't
            logger.warning("Event reminder not set: %s", e)
        return ToolResult.ok(f"Added {ev.title}, {_day_name(start.date(), now.date())} at {_clock(start)}.{note}",
                             start=start.isoformat(), title=ev.title)


class RemoveEventTool(Tool):
    name = "remove_event"
    description = "Remove an event VERONICA added to her own calendar (Google calendars are read-only)."
    permission = PermissionLevel.MODERATE
    parameters = {"type": "object", "properties": {"what": {"type": "string"}}, "required": ["what"]}

    def __init__(self, config=None, calendar: Optional[Calendar] = None, reminders=None) -> None:
        self.calendar = calendar or Calendar(config)
        self._config = config
        self._reminders = reminders

    def matches(self, text: str) -> bool:
        return bool(_REMOVE.match(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _REMOVE.match(text)
        return {"what": m.group("what")} if m else {}

    def run(self, what: str = "", **kwargs: Any) -> ToolResult:
        gone = self.calendar.local.remove(what)
        if gone is None:
            extra = " Events from your Google Calendar can only be changed in Google Calendar." if self.calendar.connected else ""
            return ToolResult.fail(f"I don't have an event called {what} in my calendar.{extra}")
        try:     # the "in 10 minutes" reminder added with the event goes with it
            reminders = self._reminders
            if reminders is None:
                from veronica.tools.productivity_tools import ReminderStore
                reminders = ReminderStore(self._config)
            reminders.cancel(f"{gone.title} in 10 minutes")
        except Exception as e:  # noqa: BLE001
            logger.warning("Event reminder not removed: %s", e)
        return ToolResult.ok(f"Removed {gone.title} on {gone.start.strftime('%A %d %B')}.")


class ConnectCalendarTool(Tool):
    name = "connect_calendar"
    description = "Explain how to connect the user's Google Calendar (read-only, via its private iCal link)."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, config=None, calendar: Optional[Calendar] = None) -> None:
        self.calendar = calendar or Calendar(config)

    def matches(self, text: str) -> bool:
        return bool(_CONNECT.search(text)) and not _ADD.match(text) or bool(_CONNECT.match(text))

    def run(self, **kwargs: Any) -> ToolResult:
        status = (f"You have {len(self.calendar.urls)} calendar connected. " if self.calendar.connected else "")
        return ToolResult.ok(
            status + "To connect a Google Calendar: open Google Calendar settings in your browser, pick the "
            "calendar, scroll to Integrate calendar, and copy the Secret address in iCal format. Then add a line "
            "to config slash dot env: CALENDAR_ICS_URLS equals that address, and restart me. Keep that address "
            "private; it lets anyone read the calendar. It's read-only: I can see your events but not change them.")
