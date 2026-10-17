"""Phase A voice features: reminders/timers, to-do list, home city, news, daily briefing,
voice permission answers and the reminder announcer. No network, no audio."""
from __future__ import annotations

import datetime as dt
import sys
import threading
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.tools.base import ToolResult
from veronica.tools.briefing_tools import DailyBriefingTool
from veronica.tools.news_tools import NewsTool
from veronica.tools.productivity_tools import (
    CancelReminderTool, ListRemindersTool, Profile, ReminderStore, SetHomeCityTool,
    SetReminderTool, TodoStore, TodoTool, parse_when,
)
from veronica.tools.registry import build_default_registry
from veronica.tools.weather_tools import WeatherTool
from veronica.ui import cli as cli_mod
from veronica.ui.cli import CLIApp, parse_confirmation, spoken_permission

NOW = dt.datetime(2026, 10, 15, 18, 0, 0)  # Thursday 6:00 pm


@pytest.fixture(autouse=True)
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # stores without a config write to ./data


# ---------------------------------------------------------------- time parsing

@pytest.mark.parametrize("text,expected", [
    ("remind me in 10 minutes", NOW + dt.timedelta(minutes=10)),
    ("set a timer for five minutes", NOW + dt.timedelta(minutes=5)),
    ("in an hour", NOW + dt.timedelta(hours=1)),
    ("for half an hour", NOW + dt.timedelta(minutes=30)),
    ("in 90 seconds", NOW + dt.timedelta(seconds=90)),
    ("at 7 pm", NOW.replace(hour=19)),
    ("at 9:30 am", NOW.replace(hour=9, minute=30) + dt.timedelta(days=1)),  # already past today
    ("at 7", NOW.replace(hour=19)),                                          # 7 said at 6 pm = 7 pm
    ("tomorrow at 8 am", NOW.replace(hour=8) + dt.timedelta(days=1)),
    ("at 12 am", NOW.replace(hour=0) + dt.timedelta(days=1)),
])
def test_parse_when(text, expected):
    assert parse_when(text, NOW) == expected


@pytest.mark.parametrize("text", ["remind me to call mom", "at 25:00", "at 13 pm", "sometime soon"])
def test_parse_when_rejects_unclear(text):
    assert parse_when(text, NOW) is None


# ---------------------------------------------------------------- reminders

def test_reminder_fast_path_and_announcement_text():
    tool = SetReminderTool()
    text = "remind me in 10 minutes to call mom"
    assert tool.matches(text)
    args = tool.extract_arguments(text)
    assert args["text"] == "call mom"
    result = tool.run(**args)
    assert result.success and "call mom" in result.message
    assert [r["text"] for r in tool.store.pending()] == ["call mom"]


def test_timer_without_task_uses_default_label():
    tool = SetReminderTool()
    result = tool.run(**tool.extract_arguments("set a timer for 5 minutes"))
    assert result.message == "Timer set for 5 minutes."
    assert tool.store.pending()[0]["text"] == "Your timer is done"


def test_remind_without_time_is_left_to_the_llm():
    assert not SetReminderTool().matches("remind me to buy milk")


def test_pop_due_list_and_cancel():
    store = ReminderStore()
    store.add("old", NOW - dt.timedelta(minutes=5))
    store.add("later", NOW + dt.timedelta(hours=2))
    store.add("water plants", NOW + dt.timedelta(hours=3))
    assert [r["text"] for r in store.pop_due(NOW)] == ["old"]
    assert "2 reminders" in ListRemindersTool(store=store).run().message
    cancel = CancelReminderTool(store=store)
    assert cancel.matches("cancel the reminder to water plants")
    assert cancel.run(**cancel.extract_arguments("cancel the reminder to water plants")).success
    assert [r["text"] for r in store.pending()] == ["later"]
    assert cancel.run(which="nonexistent thing").success is False


def test_two_store_instances_share_the_file_safely():
    a, b = ReminderStore(), ReminderStore()
    threads = [threading.Thread(target=a.add, args=(f"r{i}", NOW)) for i in range(20)] + \
              [threading.Thread(target=b.add, args=(f"s{i}", NOW)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(a.pending()) == 40  # no lost writes between the tool and the announcer


# ---------------------------------------------------------------- to-do list

def test_todo_voice_phrases_round_trip():
    tool = TodoTool()

    def say(text):
        assert tool.matches(text), text
        return tool.run(**tool.extract_arguments(text)).message

    assert say("add buy milk to my to-do list").startswith("Added buy milk")
    say("add call the bank to my todo list")
    assert say("what's on my to-do list") == "You have 2 things to do: buy milk, and call the bank."
    assert say("mark buy milk as done") == "Marked buy milk as done."
    assert say("remove call the bank from my to-do list") == "Removed call the bank from your list."
    assert say("read my to-do list") == "Your to-do list is empty."


def test_todo_does_not_steal_unrelated_sentences():
    tool = TodoTool()
    for text in ("I am done with work", "mark my words", "what time is it"):
        assert not tool.matches(text), text


# ---------------------------------------------------------------- home city + weather

def test_home_city_set_by_voice_and_used_by_weather():
    profile = Profile()
    setter = SetHomeCityTool(profile=profile)
    assert setter.matches("my city is mumbai")
    setter._geocode = lambda name: None          # no network in unit tests
    assert setter.run(**setter.extract_arguments("my city is mumbai")).message.startswith("Got it, your home is Mumbai.")
    weather = WeatherTool(profile=profile)
    assert weather.matches("how's the weather today?")
    assert weather.matches("is it going to rain tomorrow")
    assert not weather.matches("what's the temperature of the sun")
    with patch.object(WeatherTool, "run", wraps=weather.run):
        fake = types.SimpleNamespace(get=MagicMock(side_effect=[
            SimpleNamespace(json=lambda: {"results": [{"latitude": 1, "longitude": 2, "name": "Mumbai"}]}),
            SimpleNamespace(json=lambda: {"current_weather": {"temperature": 31, "windspeed": 8, "weathercode": 1}}),
        ]), RequestException=Exception)
        with patch.dict(sys.modules, {"requests": fake}):
            assert weather.run().message.startswith("Mumbai: 31")
        assert fake.get.call_args_list[0].kwargs["params"]["name"] == "Mumbai"


def test_weather_without_any_city_asks_for_one():
    weather = WeatherTool(profile=Profile())
    assert not weather.matches("how's the weather")  # no home city: leave it to the LLM
    assert "my city is" in weather.run().message


# ---------------------------------------------------------------- news

RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Rain lashes Mumbai - The Hindu</title><source>The Hindu</source></item>
<item><title>Markets rally on results - Mint</title><source>Mint</source></item>
</channel></rss>"""


def test_news_reads_headlines_and_never_raises():
    tool = NewsTool()
    assert tool.matches("what's the news today") and tool.matches("news about cricket")
    assert not tool.matches("that is good news for me")
    assert tool.extract_arguments("tell me the news about cricket") == {"topic": "cricket"}
    resp = SimpleNamespace(content=RSS, raise_for_status=lambda: None)
    with patch.dict(sys.modules, {"requests": SimpleNamespace(get=MagicMock(return_value=resp))}):
        msg = tool.run().message
    assert msg == "Here are the top headlines: 1. Rain lashes Mumbai (The Hindu). 2. Markets rally on results (Mint)."
    with patch.dict(sys.modules, {"requests": SimpleNamespace(get=MagicMock(side_effect=OSError("offline")))}):
        assert tool.run().success is True           # just fetched: answered from the 3-minute cache
        assert NewsTool().run().success is False    # nothing cached and offline: clean failure


# ---------------------------------------------------------------- briefing

def test_briefing_combines_everything_and_fails_soft():
    profile, reminders, todos = Profile(), ReminderStore(), TodoStore()
    profile.set_home_city("Pune")
    reminders.add("dentist", dt.datetime.now().replace(hour=23, minute=0))
    todos.add("pay rent")
    weather = SimpleNamespace(run=lambda city: ToolResult.ok("", city=city, temperature_c=27.4, description="clear sky"))
    tool = DailyBriefingTool(weather_tool=weather, profile=profile, reminders=reminders, todos=todos,
                             headlines=lambda: ["Big news (Src)"])
    assert tool.matches("good morning") and tool.matches("brief me")
    msg = tool.run().message
    assert "In Pune it's 27 degrees and clear sky." in msg
    assert "dentist at 11:00 PM" in msg and "pay rent" in msg and "Top headlines: Big news." in msg

    def offline():
        raise OSError("offline")
    quiet = DailyBriefingTool(weather_tool=weather, profile=Profile(), reminders=ReminderStore(),
                              todos=TodoStore(), headlines=offline)
    assert quiet.run().success  # news down: briefing still works


# ---------------------------------------------------------------- fast-path routing order

@pytest.mark.parametrize("text,tool", [
    ("remind me in 10 minutes to check the weather", "set_reminder"),
    ("good morning", "daily_briefing"),
    ("what's on my to-do list", "todo_list"),
    ("my city is Delhi", "set_home_city"),
    ("what's the news", "get_news"),
    ("what time is it", "get_current_time"),
    ("weather in Delhi", "get_weather"),
])
def test_fast_path_reaches_the_right_tool(text, tool):
    registry = build_default_registry(PermissionGate())
    assert registry.find_fast_match(text).name == tool


# ---------------------------------------------------------------- voice permission

@pytest.mark.parametrize("heard,level,expected", [
    ("yes", PermissionLevel.SENSITIVE, True),
    ("yeah go ahead", PermissionLevel.SENSITIVE, True),
    ("no", PermissionLevel.SENSITIVE, False),
    ("yes, no wait", PermissionLevel.SENSITIVE, False),  # any no wins
    ("hmm", PermissionLevel.SENSITIVE, None),
    ("yes", PermissionLevel.CRITICAL, None),             # critical needs "confirm"
    ("confirm", PermissionLevel.CRITICAL, True),
    ("no don't confirm", PermissionLevel.CRITICAL, False),
])
def test_parse_confirmation(heard, level, expected):
    assert parse_confirmation(heard, level) is expected


def test_spoken_permission_is_readable():
    assert spoken_permission("write_file({'path': 'notes.txt', 'content': 'hi'})") == \
        "write file, path notes.txt, content hi"
    assert spoken_permission("click_element 'OK'\nmore") == "click_element 'OK'"


def voice_cli(answers):
    cli = CLIApp.__new__(CLIApp)
    cli.config = SimpleNamespace(assistant=SimpleNamespace(name="VERONICA"))
    cli.voice_mode = False  # print only; no TTS in tests
    cli._speak_lock, cli._capturing, cli._stop = threading.RLock(), threading.Event(), threading.Event()
    said = []
    cli._say = said.append
    queue = list(answers)
    cli.stt = SimpleNamespace(listen_once=lambda timeout_s=6: SimpleNamespace(
        success=bool(queue[0]), text=queue.pop(0), error="") if queue else SimpleNamespace(success=False, text="", error=""))
    return cli, said


def test_voice_confirmation_retries_then_denies_on_silence():
    cli, said = voice_cli(["", ""])
    assert cli._confirm_by_voice("delete_file({'path': 'x'})", PermissionLevel.CRITICAL) is False
    assert "Say confirm" in said[0] and said[-1] == "I'll take that as a no."


def test_voice_confirmation_second_try_yes():
    cli, said = voice_cli(["umm", "yes please"])
    assert cli._confirm_by_voice("write_file({'path': 'x'})", PermissionLevel.SENSITIVE) is True


def test_gate_callback_delegates_to_voice_confirmer(monkeypatch):
    monkeypatch.setattr(cli_mod, "_voice_confirmer", lambda d, lvl: d == "ok")
    assert cli_mod._confirm_in_terminal("ok", PermissionLevel.SENSITIVE) is True
    assert cli_mod._confirm_in_terminal("nope", PermissionLevel.SENSITIVE) is False


# ---------------------------------------------------------------- announcer

def test_announcer_speaks_due_and_missed_reminders():
    store = ReminderStore()
    store.add("stretch", dt.datetime.now() - dt.timedelta(seconds=1))
    store.add("old call", dt.datetime.now() - dt.timedelta(hours=1))
    cli, said = voice_cli([])
    cli._beep = lambda: None
    cli.config = SimpleNamespace(assistant=SimpleNamespace(name="VERONICA"),
                                 resolve_path=lambda p: __import__("pathlib").Path(p))
    t = threading.Thread(target=cli._announce_reminders, kwargs={"poll_s": 0.05})
    t.start()
    for _ in range(100):
        if len(said) >= 2:
            break
        threading.Event().wait(0.02)
    cli._stop.set()
    t.join(2)
    assert said[0].startswith("Missed reminder from") and "old call" in said[0]
    assert said[1] == "Reminder: stretch."
    assert store.pending() == []
