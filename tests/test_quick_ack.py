"""Instant "Mm-hm." while the model thinks."""
import threading
import time
from types import SimpleNamespace

from veronica.voice.quick_ack import QuickAck


class FakeTTS:
    def __init__(self, folder):
        self.folder, self.made = folder, []

    def _voice_file(self, text):
        self.made.append(text)
        p = self.folder / f"{len(self.made)}.wav"
        p.write_bytes(b"RIFF")
        return p


def test_clips_are_made_once_and_never_repeat_back_to_back(tmp_path):
    ack = QuickAck(FakeTTS(tmp_path), ["Mm-hm.", "One sec."])
    ack.prepare().join()
    picks = [ack.pick() for _ in range(6)]
    assert all(p is not None for p in picks)
    assert all(a != b for a, b in zip(picks, picks[1:]))


def test_nothing_ready_means_no_clip(tmp_path):
    assert QuickAck(FakeTTS(tmp_path), ["Mm-hm."]).pick() is None


def _app(fast_match):
    from veronica.ui.cli import CLIApp
    app = CLIApp.__new__(CLIApp)
    app._speak_lock = threading.RLock()
    app.hub = None
    app.orchestrator = SimpleNamespace(tools=SimpleNamespace(find_fast_match=lambda t: fast_match))
    app.quick_ack = SimpleNamespace(pick=lambda: "clip.wav")
    return app


def test_acknowledges_a_question_and_reply_waits_for_it(monkeypatch):
    played = []
    import veronica.voice.quick_ack as qa
    monkeypatch.setattr(qa, "play", lambda clip: (time.sleep(0.1), played.append(clip)))
    app = _app(fast_match=None)
    t = app._acknowledge("what's the weather in Pune?")
    with app._speak_lock:              # the reply: only after the clip has played
        assert played == ["clip.wav"]
    t.join()


def test_no_acknowledgement_for_commands_she_runs_herself(monkeypatch):
    import veronica.voice.quick_ack as qa
    monkeypatch.setattr(qa, "play", lambda clip: (_ for _ in ()).throw(AssertionError))
    assert _app(fast_match=object())._acknowledge("open YouTube") is None
