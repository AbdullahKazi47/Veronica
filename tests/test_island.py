"""The island under the camera: what VERONICA tells it, and that she is never left invisible."""
from __future__ import annotations

import json
import socket

import pytest

from veronica.ui import island, island_link
from veronica.ui.island_link import IslandLink


class FakeProc:
    def __init__(self, code=None):
        self.returncode = code

    def poll(self):
        return self.returncode


@pytest.fixture
def listener():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(1.0)
    yield s
    s.close()


def _link(listener, procs):
    spawned = []

    def spawn(cmd, **kw):
        spawned.append(cmd)
        return procs.pop(0)
    link = IslandLink(lambda: (500.0, 60.0), spawn=spawn)
    link._launch()
    link.port = listener.getsockname()[1]          # send to the test instead of a real island
    return link, spawned


def _messages(listener, n):
    out = []
    for _ in range(n):
        out.append(json.loads(listener.recv(2048).decode()))
    return out


def test_states_are_sent_once_each(listener):
    link, spawned = _link(listener, [FakeProc()])
    assert "--hide" in spawned[0] and "veronica.ui.island" in spawned[0]
    link.set("listening"); link.set("listening"); link.set("thinking"); link.set("nonsense")
    assert [m["s"] for m in _messages(listener, 2)] == ["listening", "thinking"]


def test_a_clean_goodbye_tells_the_island_to_leave_quietly(listener):
    link, _ = _link(listener, [FakeProc()])
    link.close()
    assert _messages(listener, 1) == [{"s": "quit"}]


def test_a_dead_island_is_restarted_then_the_terminal_comes_back(listener, monkeypatch):
    shown = []
    link, spawned = _link(listener, [FakeProc(1), FakeProc(1), FakeProc(1), FakeProc(1)])
    monkeypatch.setattr(link, "show_terminal", lambda: shown.append(True))
    for _ in range(4):
        link._keep_alive()
    assert len(spawned) == 4 and shown == [True]      # 3 restarts, then she shows her window


def test_the_waveform_follows_the_voice_against_the_room():
    quiet, normal, loud = (island.level_to_height(x, 60.0) for x in (50.0, 300.0, 3000.0))
    assert quiet == 0.0 and 0.0 < normal < loud == 1.0
    assert island.level_to_height(300.0, 600.0) == 0.0     # a noisy room doesn't look like speech


def test_colours_never_hit_the_transparent_key():
    assert island._mix("#000000", "#ffffff", 0.5) == "#808080"
    assert island._mix(island.KEY, island.KEY, 0.3) != island.KEY


def test_off_unless_turned_on():
    from veronica.config import Config
    assert Config().voice.island is False
