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
    assert set(COMMANDS) == {"stop", "mic_off", "mic_on", "quit",
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


def test_go_to_sleep_inside_a_conversation_closes_her_not_just_the_conversation():
    """Seen in the log three times: 'you can go to sleep now' only ended the conversation."""
    import time as _time
    from types import SimpleNamespace
    from veronica.ui.cli import CLIApp, quits_veronica
    app = CLIApp.__new__(CLIApp)
    said = []
    app.config = SimpleNamespace(voice=SimpleNamespace(conversation_idle_timeout_s=300, follow_up_seconds=8, conversation_mode=True),
                                 assistant=SimpleNamespace(name="VERONICA"))
    app._follow_up_until = _time.monotonic() + 60
    app._stop = SimpleNamespace(is_set=lambda: False)
    app._status = lambda m: None
    app._say = said.append
    app._island = lambda s: None
    app._listen = lambda timeout_s=6: SimpleNamespace(success=True, text="Okay, Veronica, you can go to sleep now.", error="")
    app._accept = lambda result, open_conversation: result.text
    app.orchestrator = SimpleNamespace(tools=None)
    text = app._conversation_turn()
    assert quits_veronica(text)                       # handed up to the main loop, which closes her
    assert said == []                                 # not answered with "Anytime." and left running


def test_the_power_button_says_goodbye_and_exits(monkeypatch):
    import threading
    from veronica.ui.cli import CLIApp
    app = CLIApp.__new__(CLIApp)
    said, exited = [], []
    app._status = lambda m: None
    app._say = said.append
    app._stop = threading.Event()
    app.island = None
    app.wake_gate = None
    monkeypatch.setattr("veronica.main._exit_now", lambda code=0: exited.append(code))
    monkeypatch.setattr("veronica.ui.cli.time.sleep", lambda s: None)
    app._island_command("quit")
    for t in threading.enumerate():
        if t.name == "power-off":
            t.join(5)
    assert said == ["Goodbye, boss."] and exited == [0] and app.quit_requested and app._stop.is_set()
