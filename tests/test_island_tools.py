"""The island's hover tools: timer, music, system stats, keep-awake, screenshots, and the mic button."""
from __future__ import annotations

import datetime as dt
import json
import socket
import subprocess
import time

import pytest

from veronica.ui import island_tools as tools
from veronica.ui.island_link import COMMANDS, IslandLink


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_timer_counts_down_pauses_and_fires_once():
    c = Clock()
    tm = tools.Timer(5, clock=c)
    assert tm.label() == "05:00" and not tm.running
    tm.toggle(); c.t = 60
    assert tm.label() == "04:00" and abs(tm.progress() - 0.2) < 1e-9
    tm.toggle(); c.t = 500                       # paused: time stands still
    assert tm.label() == "04:00"
    tm.add(1)
    assert tm.label() == "05:00"
    tm.toggle(); c.t = 500 + 300
    assert tm.done() and not tm.done()           # fires exactly once
    assert tm.label() == "00:00" and not tm.running
    tm.toggle()                                  # starting again after it finished restarts it
    assert tm.running and tm.remaining() == 300


def test_now_playing_is_read_and_named_plainly():
    out = json.dumps({"title": "Blinding Lights", "artist": "The Weeknd", "app": "Spotify.exe", "playing": True})
    assert tools.parse_now_playing(out) == {"title": "Blinding Lights", "artist": "The Weeknd", "app": "Spotify", "playing": True}
    assert tools.parse_now_playing("{}") == {} and tools.parse_now_playing("") == {} and tools.parse_now_playing("junk") == {}
    chrome = tools.parse_now_playing(json.dumps({"title": "lofi", "app": "Chrome", "playing": False}))
    assert chrome["app"] == "Chrome" and chrome["playing"] is False


def test_music_is_polled_in_the_background_and_not_too_often():
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"title": "Song", "playing": True}), "")
    np = tools.NowPlaying(every=3.0, run=run)
    np.poll(now=100.0)
    for _ in range(50):
        if np.info:
            break
        time.sleep(0.01)
    np.poll(now=101.0)                            # too soon: not asked again
    assert len(calls) == 1 and np.info["title"] == "Song"
    assert "now_playing.ps1" in calls[0][-1] and "-NoProfile" in calls[0]
    np.pressed("play_pause")
    assert np.info["playing"] is False            # the button shows the change at once


def test_system_stats_are_sampled_at_most_once_a_second():
    st = tools.Stats(every=1.0)
    assert st.sample(now=10.0)
    assert not st.sample(now=10.5)
    assert st.sample(now=11.2)
    assert 0.0 <= st.cpu <= 100.0 and 0.0 < st.mem <= 100.0
    assert st.down.endswith("/s") and len(st.history) == 40


def test_screenshots_go_to_pictures_with_a_readable_name(tmp_path):
    p = tools.screenshot_path(dt.datetime(2026, 11, 13, 11, 2, 5), folder=tmp_path / "Screenshots")
    assert p.name == "VERONICA 2026-11-13 11.02.05.png" and p.parent.is_dir()


def test_media_keys_only_known_ones(monkeypatch):
    pressed = []
    monkeypatch.setattr(tools.ctypes, "windll",
                        type("W", (), {"user32": type("U", (), {"keybd_event": staticmethod(lambda *a: pressed.append(a[0]))})()})())
    tools.media_key("next"); tools.media_key("format_disk")
    assert pressed == [0xB0, 0xB0]                # key down + key up, nothing for unknown names


def test_the_island_may_only_ask_for_these():
    assert set(COMMANDS) == {"stop", "mic_off", "mic_on", "timer_done",
                             "phone_text", "phone_text_file", "phone_image", "phone_file"}


def test_the_mic_button_mutes_her_and_the_heartbeat_remembers_it():
    from veronica.ui.cli import CLIApp
    app = CLIApp.__new__(CLIApp)
    app.voice_mode, app._status = True, (lambda m: None)
    app.island = IslandLink.__new__(IslandLink)
    app.island.mic_on = True
    app._island = lambda state: None
    app._island_command("mic_off")
    assert app._mic_muted and app.island.mic_on is False
    app.stt = object()
    assert app._get_input() == ""                 # muted: she doesn't listen for anything
    app._island_command("mic_on")
    assert not app._mic_muted and app.island.mic_on is True


def test_keep_awake_asks_windows_and_can_let_go(monkeypatch):
    seen = []
    monkeypatch.setattr(tools.ctypes, "windll", type("W", (), {"kernel32": type("K", (), {
        "SetThreadExecutionState": staticmethod(lambda f: (seen.append(f), 1)[1])})()})())
    assert tools.keep_awake(True) and tools.keep_awake(False)
    assert seen[0] == tools.ES_CONTINUOUS | tools.ES_SYSTEM_REQUIRED | tools.ES_DISPLAY_REQUIRED
    assert seen[1] == tools.ES_CONTINUOUS
