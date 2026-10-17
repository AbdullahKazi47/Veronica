"""
Spoken daily briefing ("good morning", "brief me") — the idea behind OpenJarvis's
morning digest: greeting, time, home-city weather, today's reminders, open to-dos, and
a few headlines, in one short spoken paragraph. Every part is optional and fails soft:
a missing city or an offline news feed just drops that sentence.
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Any, Optional

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult
from veronica.tools.news_tools import fetch_headlines
from veronica.tools.productivity_tools import Profile, ReminderStore, TodoStore

logger = get_logger("tools.briefing")

_BRIEFING = re.compile(
    r"^\s*(?:(?:hey\s+)?veronica[,\s]+)?(?:good\s+morning|morning\s+briefing|daily\s+briefing|"
    r"brief\s+me|give\s+me\s+(?:my|a|the)\s+(?:daily\s+|morning\s+)?briefing|"
    r"what(?:'s| is| does)\s+my\s+day\s+look(?:ing)?\s+like|what'?s\s+(?:on\s+)?(?:for\s+)?today)\b", re.I)


def _greeting(now: dt.datetime) -> str:
    if now.hour < 12:
        return "Good morning"
    if now.hour < 17:
        return "Good afternoon"
    return "Good evening"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f", and {items[-1]}"


class DailyBriefingTool(Tool):
    name = "daily_briefing"
    description = ("Give a short spoken daily briefing: time, home-city weather, today's reminders, "
                   "the to-do list, and top headlines.")
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, config=None, weather_tool=None, profile: Optional[Profile] = None,
                 reminders: Optional[ReminderStore] = None, todos: Optional[TodoStore] = None,
                 headlines=None, calendar=None) -> None:
        from veronica.tools.weather_tools import WeatherTool
        self.calendar = calendar
        self.profile = profile or Profile(config)
        self.weather = weather_tool or WeatherTool(config=config, profile=self.profile)
        self.reminders = reminders or ReminderStore(config)
        self.todos = todos or TodoStore(config)
        assistant = getattr(config, "assistant", None)
        region = getattr(assistant, "news_region", "IN") or "IN"
        language = getattr(assistant, "news_language", "en") or "en"
        world = getattr(assistant, "news_world_region", "") or ""
        self._headlines = headlines or (lambda: fetch_headlines("", region, language, limit=3, world_region=world))

    def matches(self, text: str) -> bool:
        return bool(_BRIEFING.match(text))

    def run(self, include_tasks: bool = True, **kwargs: Any) -> ToolResult:
        """`include_tasks=False` is the greeting when the laptop opens: greeting, time,
        weather and headlines only."""
        now = dt.datetime.now()
        parts = [f"{_greeting(now)}. It's {now.strftime('%I:%M %p').lstrip('0')} on {now.strftime('%A, %d %B')}."]

        city = self.profile.home_city()
        if city:
            wx = self.weather.run(city=city)
            if wx.success:
                d = wx.data
                parts.append(f"In {d.get('city', city)} it's {round(d['temperature_c'])} degrees and {d['description']}.")
        else:
            parts.append("Tell me your city, like 'my city is Mumbai', and I'll include the weather.")

        today = [r for r in self.reminders.pending()
                 if dt.datetime.fromisoformat(r["due"]).date() == now.date()] if include_tasks else []
        if today:
            items = [f"{r['text']} at {dt.datetime.fromisoformat(r['due']).strftime('%I:%M %p').lstrip('0')}"
                     for r in today[:3]]
            parts.append(f"You have {len(today)} reminder{'s' if len(today) != 1 else ''} today: {_join(items)}.")

        events_today = 0
        if self.calendar is not None:
            try:
                from veronica.tools.calendar_tools import _clock
                rest_of_day = now.replace(hour=23, minute=59)
                evs = [e for e in self.calendar.events(now, rest_of_day) if not e.all_day]
                events_today = len(evs)
                if evs:
                    items = [f"{e.title} at {_clock(e.start)}" for e in evs[:3]]
                    more = f" and {len(evs) - 3} more" if len(evs) > 3 else ""
                    parts.append(f"On your calendar today: {_join(items)}{more}.")
            except Exception as e:  # noqa: BLE001 - the greeting never fails over a calendar
                logger.warning("Briefing calendar unavailable: %s", e)

        todo = [t["text"] for t in self.todos.open_items()] if include_tasks else []
        if todo:
            shown = todo[:3] + ([f"{len(todo) - 3} more"] if len(todo) > 3 else [])
            parts.append(f"On your to-do list: {_join(shown)}.")

        try:
            heads = self._headlines()
        except Exception as e:  # noqa: BLE001
            logger.warning("Briefing headlines unavailable: %s", e)
            heads = []
        if heads:
            parts.append("Top headlines: " + "; ".join(h.rsplit(" (", 1)[0] for h in heads) + ".")

        return ToolResult.ok(" ".join(parts), city=city, reminders=len(today), todos=len(todo),
                             headlines=len(heads), events=events_today)
