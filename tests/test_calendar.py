"""Calendar: iCal reading (repeats, time zones, all-day, exceptions), voice questions,
adding events by voice. No network."""
from __future__ import annotations

import datetime as dt

import pytest

from veronica.integrations.calendar import Calendar, LocalCalendar, expand, parse_ics
from veronica.tools.calendar_tools import (
    AddEventTool, CalendarTool, ConnectCalendarTool, RemoveEventTool, describe, query_range,
)

NOW = dt.datetime(2026, 10, 16, 9, 0)          # a Friday morning (local)

ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:standup-1
SUMMARY:Team standup
DTSTART;TZID=Asia/Kolkata:20261012T100000
DTEND;TZID=Asia/Kolkata:20261012T101500
RRULE:FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR;COUNT=20
EXDATE;TZID=Asia/Kolkata:20261020T100000
END:VEVENT
BEGIN:VEVENT
UID:standup-1
RECURRENCE-ID;TZID=Asia/Kolkata:20261019T100000
SUMMARY:Team standup (moved)
DTSTART;TZID=Asia/Kolkata:20261019T113000
DTEND;TZID=Asia/Kolkata:20261019T114500
END:VEVENT
BEGIN:VEVENT
UID:diwali
SUMMARY:Diwali
DTSTART;VALUE=DATE:20261108
DTEND;VALUE=DATE:20261109
END:VEVENT
BEGIN:VEVENT
UID:dentist
SUMMARY:Dentist\\, Dr. Rao
LOCATION:Koramangala
DTSTART:20261016T103000Z
DURATION:PT45M
END:VEVENT
BEGIN:VEVENT
UID:gone
SUMMARY:Cancelled thing
STATUS:CANCELLED
DTSTART:20261016T120000Z
END:VEVENT
BEGIN:VEVENT
UID:long-title
SUMMARY:A very long
  folded title
DTSTART:20261017T053000Z
DTEND:20261017T063000Z
END:VEVENT
END:VCALENDAR
"""


def _local(y, mo, d, h, mi, tz="Asia/Kolkata"):
    from zoneinfo import ZoneInfo
    return dt.datetime(y, mo, d, h, mi, tzinfo=ZoneInfo(tz)).astimezone().replace(tzinfo=None)


@pytest.fixture
def raw():
    return parse_ics(ICS)


def test_repeats_exceptions_and_overrides(raw):
    week = expand(raw, dt.datetime(2026, 10, 19), dt.datetime(2026, 10, 24))
    standups = sorted((e for e in week if "standup" in e.title.lower()), key=lambda e: e.start)
    starts = [e.start for e in standups]
    assert _local(2026, 10, 19, 11, 30) in starts and _local(2026, 10, 19, 10, 0) not in starts   # moved
    assert _local(2026, 10, 20, 10, 0) not in starts                                              # skipped
    assert len(standups) == 4 and standups[0].title == "Team standup (moved)"


def test_all_day_utc_cancelled_and_folding(raw):
    day = expand(raw, dt.datetime(2026, 10, 16), dt.datetime(2026, 10, 18))
    titles = [e.title for e in day]
    assert "Dentist, Dr. Rao" in titles and "Cancelled thing" not in titles
    assert "A very long folded title" in titles
    dentist = next(e for e in day if e.title.startswith("Dentist"))
    assert dentist.end - dentist.start == dt.timedelta(minutes=45) and dentist.location == "Koramangala"
    diwali = expand(raw, dt.datetime(2026, 11, 8), dt.datetime(2026, 11, 9))
    assert diwali[0].title == "Diwali" and diwali[0].all_day


def _cal(tmp_path, ics=ICS):
    return Calendar(urls=["https://example/cal.ics"], fetch=lambda url: ics,
                    local=LocalCalendar(tmp_path / "calendar.json"))


@pytest.mark.parametrize("text,label", [
    ("what's on today", "today"), ("what's on my calendar tomorrow", "tomorrow"),
    ("what do I have this week", "this week"), ("anything on monday?", "Monday"),
    ("am I free tomorrow afternoon", "tomorrow afternoon"), ("what's on next week", "next week"),
])
def test_query_ranges(text, label):
    assert query_range(text, NOW)[2] == label


def test_today_and_next(tmp_path):
    tool = CalendarTool(calendar=_cal(tmp_path), now=lambda: NOW)
    today = tool.run(**tool.extract_arguments("what's on today?")).message
    assert "Team standup" in today and "Dentist" in today and "Cancelled" not in today
    nxt = tool.run(**tool.extract_arguments("what's my next meeting?")).message
    assert nxt.startswith("Next up: Team standup")


def test_free_check(tmp_path):
    tool = CalendarTool(calendar=_cal(tmp_path), now=lambda: NOW)
    assert "free" in tool.run(**tool.extract_arguments("am I free on sunday evening")).message.lower()
    assert "Not entirely" in tool.run(**tool.extract_arguments("am I free today")).message


def test_unconnected_calendar_says_how(tmp_path):
    cal = Calendar(urls=[], local=LocalCalendar(tmp_path / "c.json"))
    msg = CalendarTool(calendar=cal, now=lambda: NOW).run(when="today").message
    assert "Nothing on your calendar today" in msg and "connect my calendar" in msg
    assert "Secret address in iCal format" in ConnectCalendarTool(calendar=cal).run().message


def test_unreachable_feed_keeps_local_events(tmp_path):
    def boom(url):
        raise OSError("offline")
    cal = Calendar(urls=["https://x"], fetch=boom, local=LocalCalendar(tmp_path / "c.json"))
    cal.local.add("Gym", NOW.replace(hour=18))
    assert [e.title for e in cal.events(NOW, NOW + dt.timedelta(days=1))] == ["Gym"]


def test_add_event_by_voice_with_reminder(tmp_path):
    cal = _cal(tmp_path)
    added_reminders = []

    class Rem:
        def add(self, text, due): added_reminders.append((text, due))
    tool = AddEventTool(calendar=cal, reminders=Rem(), now=lambda: NOW)
    text = "add dentist checkup to my calendar tomorrow at 4 pm"
    assert tool.matches(text)
    args = tool.extract_arguments(text)
    assert args["title"] == "dentist checkup" and args["when"].startswith("2026-10-17T16:00")
    res = tool.run(**args)
    assert res.success and "tomorrow at 4 PM" in res.message
    assert added_reminders and added_reminders[0][1] == dt.datetime(2026, 10, 17, 15, 50)
    assert "dentist checkup" in [e.title for e in cal.events(dt.datetime(2026, 10, 17), dt.datetime(2026, 10, 18))]
    assert RemoveEventTool(calendar=cal).run(what="dentist").success


def test_adding_in_the_past_is_refused(tmp_path):
    tool = AddEventTool(calendar=_cal(tmp_path), reminders=None, now=lambda: NOW)
    assert not tool.run(title="x", when=dt.datetime(2026, 10, 15, 9).isoformat()).success


def test_routing(tmp_path):
    from veronica.safety.permissions import PermissionGate
    from veronica.tools.registry import build_default_registry
    reg = build_default_registry(PermissionGate())
    for t, name in [("what's on today", "calendar"), ("what's my next meeting", "calendar"),
                    ("am I free tomorrow afternoon", "calendar"), ("what's on my calendar this week", "calendar"),
                    ("schedule a call with ravi on friday at 3 pm", "add_event"),
                    ("add lunch with mom to my calendar on sunday at 1 pm", "add_event"),
                    ("remove lunch from my calendar", "remove_event"),
                    ("connect my google calendar", "connect_calendar"),
                    ("what's on my to-do list", "todo_list")]:
        m = reg.find_fast_match(t)
        assert m is not None and m.name == name, (t, m and m.name)


def test_briefing_mentions_todays_events(tmp_path):
    from veronica.tools.briefing_tools import DailyBriefingTool
    cal = _cal(tmp_path)

    class NoCity:
        def home_city(self): return ""
    b = DailyBriefingTool(profile=NoCity(), headlines=lambda: [], calendar=cal)
    msg = b.run(include_tasks=False).message
    # "today" is the real today here; just check the line appears when events exist
    cal.local.add("Call the bank", dt.datetime.now().replace(hour=23, minute=0))
    msg = b.run(include_tasks=False).message
    assert "On your calendar today" in msg and "Call the bank" in msg
