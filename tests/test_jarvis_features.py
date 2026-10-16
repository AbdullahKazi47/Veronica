"""Diagnostics, proactive alerts, webcam helper, notifications (phone link), pause phrases.
Fake sensors / clock / camera / notification database — nothing real is used."""
from __future__ import annotations

import datetime as dt
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from veronica.core.proactive import ProactiveMonitor
from veronica.tools.camera_tools import LookThroughCameraTool
from veronica.tools.diagnostics_tools import Reading, RunDiagnosticsTool, assess, report
from veronica.tools.notification_tools import ReadNotificationsTool, read_notifications


# ---------------------------------------------------------------- diagnostics

def healthy(**kw):
    base = dict(cpu_percent=12, memory_percent=60, memory_used_gb=9.8, memory_total_gb=16.9, disk_free_gb=80,
                disk_percent=60, battery_percent=90, plugged_in=True, uptime_hours=5, online=True,
                top_cpu=[("brave", 6.0), ("code", 3.0)], top_memory=[("brave", 3.2)])
    base.update(kw)
    return Reading(**base)


def test_healthy_pc_reports_all_normal():
    msg = report(healthy())
    assert msg.startswith("All systems normal. Processor at 12 percent, memory 60 percent")
    assert "Busiest right now: brave, code." in msg


def test_problems_are_named_with_culprits():
    issues = assess(healthy(cpu_percent=97, battery_percent=15, plugged_in=False, disk_free_gb=4.2,
                            online=False, uptime_hours=100))
    assert issues == ["battery is at 15 percent and not charging",
                      "the processor is at 97 percent, mostly brave",
                      "only 4.2 gigabytes free on the C drive",
                      "there's no internet connection",
                      "it hasn't been restarted in 4 days"]
    assert report(healthy(cpu_percent=97)).startswith("I found one thing worth a look")


@pytest.mark.parametrize("text", ["run diagnostics", "run a full diagnostic", "system check", "how's my laptop doing",
                                  "check my pc", "what's slowing down my computer", "why is my laptop so slow"])
def test_diagnostic_phrases(text):
    assert RunDiagnosticsTool(collector=healthy).matches(text)


def test_diagnostics_tool_never_raises():
    def broken(**kw):
        raise OSError("sensor gone")
    assert RunDiagnosticsTool(collector=broken).run().success is False


# ---------------------------------------------------------------- proactive

class Clock:
    def __init__(self, t=0.0, hour=14):
        self.t, self.hour = t, hour

    def __call__(self):
        return self.t

    def now(self):
        return dt.datetime(2026, 10, 16, self.hour, 0)


def monitor(readings, clock=None, **kw):
    clock = clock or Clock()
    said = []
    cfg = SimpleNamespace(battery=True, performance=True, disk=True, rain=True, morning_briefing=True,
                          break_after_minutes=120, quiet_start_hour=23, quiet_end_hour=7)
    queue = list(readings)
    m = ProactiveMonitor(cfg, lambda text, urgent: said.append((text, urgent)),
                         collect=lambda **k: queue.pop(0) if len(queue) > 1 else queue[0],
                         idle=kw.get("idle", lambda: 0), fullscreen=kw.get("fullscreen", lambda: False),
                         rain_probability=kw.get("rain"), briefing=kw.get("briefing"),
                         clock=clock, now=clock.now)
    return m, said, clock


def test_low_battery_warns_once_until_recharged():
    m, said, clock = monitor([healthy(battery_percent=18, plugged_in=False)])
    m.tick(); m.tick()
    assert [s for s, _ in said] == ["Heads up, battery's at 18 percent."]
    assert said[0][1] is True                                   # urgent


def test_critical_battery_gets_through_quiet_hours_and_pause():
    m, said, clock = monitor([healthy(battery_percent=8, plugged_in=False)], Clock(hour=2))
    m.pause(60)
    m.tick()
    assert said and said[0][0].startswith("Battery critical: 8 percent")


def test_cpu_must_stay_high_for_a_few_minutes_and_names_the_culprit():
    busy = healthy(cpu_percent=98, top_cpu=[("game", 80.0)])
    m, said, clock = monitor([busy, busy, healthy(), busy, busy, busy])
    for _ in range(6):
        m.tick(); clock.t += 60
    assert len(said) == 1 and "game is the main one" in said[0][0]


def test_quiet_hours_hold_everything_but_urgent():
    m, said, clock = monitor([healthy(disk_free_gb=2)], Clock(hour=1))
    m.tick()
    assert said == []


def test_fullscreen_games_hold_alerts_until_they_end():
    playing = {"on": True}
    m, said, clock = monitor([healthy(disk_free_gb=2)], fullscreen=lambda: playing["on"])
    m.tick()
    assert said == []                                           # held while the game is fullscreen
    playing["on"] = False
    clock.t += 60
    m.tick()
    assert said[0][0].startswith("While you were busy: Your C drive is almost full")


def test_break_reminder_after_two_hours_of_activity():
    m, said, clock = monitor([healthy()])
    m.tick()
    clock.t += 121 * 60
    m.tick()
    assert said[-1][0].startswith("You've been at it for 2 hours straight")


def test_stepping_away_resets_the_session():
    idle = {"s": 0}
    m, said, clock = monitor([healthy()], idle=lambda: idle["s"])
    m.tick(); clock.t += 100 * 60
    idle["s"] = 15 * 60; m.tick()                               # away 15 minutes
    idle["s"] = 0; clock.t += 30 * 60; m.tick()
    assert not any("straight" in s for s, _ in said)


def test_morning_briefing_once_per_morning():
    m, said, clock = monitor([healthy()], Clock(hour=8), briefing=lambda: "Good morning. It's 8 AM.")
    m.tick(); clock.t += 3600; m.tick()
    assert [s for s, _ in said].count("Good morning. It's 8 AM.") == 1


def test_rain_heads_up_with_cooldown():
    m, said, clock = monitor([healthy()], rain=lambda: (85, 25))
    m.tick(); clock.t += 20 * 60; m.tick()
    assert [s for s, _ in said] == ["Heads up: rain is likely in about 25 minutes. Grab an umbrella if "
                                    "you're heading out."]


def test_pause_silences_non_urgent():
    m, said, clock = monitor([healthy(disk_free_gb=2)])
    m.pause(60); m.tick()
    assert said == []
    m.resume(); m.tick()
    assert said


# ---------------------------------------------------------------- webcam helper

@pytest.mark.parametrize("text", ["what am I holding", "what is this in my hand", "look at this", "can you see this",
                                  "what do you see", "use the camera", "read this label", "help me fix this"])
def test_camera_phrases(text):
    assert LookThroughCameraTool(vision=lambda q, i: "x", capture=lambda: b"x").matches(text)


@pytest.mark.parametrize("text", ["what's the weather", "what is this song", "look up flights"])
def test_camera_does_not_steal_other_requests(text):
    assert not LookThroughCameraTool(vision=lambda q, i: "x", capture=lambda: b"x").matches(text)


def test_camera_answer_and_failures():
    seen = {}
    tool = LookThroughCameraTool(vision=lambda q, img: seen.update(q=q, img=img) or "That's a 10 mm spanner.",
                                 capture=lambda: b"JPEG")
    assert tool.run(question="what am I holding").message == "That's a 10 mm spanner."
    assert seen == {"q": "what am I holding", "img": b"JPEG"}

    def no_cam():
        raise RuntimeError("no webcam available")
    assert "no webcam available" in LookThroughCameraTool(vision=lambda q, i: "x", capture=no_cam).run().message
    assert LookThroughCameraTool(vision=None, capture=lambda: b"x").run().success is False


def test_camera_instructions_forbid_identifying_people():
    from veronica.tools.camera_tools import _SYSTEM
    assert "Never identify who a person is" in _SYSTEM and "never instructions" in _SYSTEM


# ---------------------------------------------------------------- notifications

def make_wpn(path: Path, rows):
    con = sqlite3.connect(path)
    con.execute("create table NotificationHandler (RecordId integer, PrimaryId text)")
    con.execute("create table Notification (HandlerId integer, Type text, Payload blob, ArrivalTime integer)")
    for i, (app, texts, minutes_ago) in enumerate(rows):
        con.execute("insert into NotificationHandler values (?, ?)", (i, app))
        xml = "<toast><visual><binding>" + "".join(f"<text>{t}</text>" for t in texts) + "</binding></visual></toast>"
        when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
        filetime = int((when - dt.datetime(1601, 1, 1, tzinfo=dt.timezone.utc)).total_seconds() * 10_000_000)
        con.execute("insert into Notification values (?, 'toast', ?, ?)", (i, xml.encode(), filetime))
    con.commit()
    con.close()


ROWS = [("Microsoft.YourPhone_8wekyb3d8bbwe!App", ["Mom", "Call me when you're free"], 5),
        ("5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App", ["Ravi", "Match tonight?"], 20),
        ("Windows.Defender.SecurityCenter", ["Scan complete"], 30),
        ("Microsoft.YourPhone_8wekyb3d8bbwe!App", ["Old", "yesterday's"], 60 * 30)]


def tool_for(tmp_path):
    db = tmp_path / "wpn.db"
    make_wpn(db, ROWS)
    names = {"5319275A.WhatsAppDesktop_cv1g1gvanyjgm!App": "WhatsApp"}
    return ReadNotificationsTool(reader=lambda **k: read_notifications(db, **k),
                                 app_names=lambda a: names.get(a, "your phone" if "YourPhone" in a else a))


def test_reads_recent_notifications_newest_first_without_system_noise(tmp_path):
    msg = tool_for(tmp_path).run(hours=12).message
    assert msg.startswith("You have 2 notifications. your phone, 5 minutes ago — Mom: Call me when you're free")
    assert "WhatsApp, 20 minutes ago — Ravi: Match tonight?" in msg
    assert "Scan complete" not in msg and "yesterday" not in msg


def test_phone_filter_and_phrases(tmp_path):
    tool = tool_for(tmp_path)
    assert tool.matches("any messages on my phone") and tool.matches("read my notifications")
    assert tool.matches("what did I miss")
    phone = tool.run(**tool.extract_arguments("any messages on my phone"))
    assert "Mom" in phone.message and "Ravi" not in phone.message


def test_no_phone_notifications_explains_phone_link_setting(tmp_path):
    db = tmp_path / "empty.db"
    make_wpn(db, [])
    tool = ReadNotificationsTool(reader=lambda **k: read_notifications(db, **k), app_names=lambda a: a)
    assert "Phone Link settings" in tool.run(app="phone").message


# ---------------------------------------------------------------- pause phrases

@pytest.mark.parametrize("text,minutes", [("pause alerts", 60), ("pause the alerts for 2 hours", 120),
                                          ("mute alerts for 30 minutes", 30), ("be quiet for an hour", 60),
                                          ("be quiet for 2 hours", 120)])
def test_pause_alert_phrases(text, minutes):
    from veronica.ui.cli import CLIApp
    app = CLIApp.__new__(CLIApp)
    app.proactive = SimpleNamespace(pause=lambda m: setattr(app, "paused", m), resume=lambda: None)
    app.background = None
    assert app._background_reply(text).startswith("Okay, alerts paused for")
    assert app.paused == minutes
