"""Wake-gated voice input (`python -m veronica.main --wake`)."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np

from veronica.config import Config
from veronica.ui.cli import CLIApp
from veronica.voice.wake_detector import MockWakeDetector, OpenWakeWordDetector
from veronica.voice.wake_gate import FRAME_SAMPLES, WakeGate

FRAME = np.zeros(FRAME_SAMPLES, np.int16).tobytes()


class FakeMic:
    def __init__(self, frames=None, opens=True):
        self.frames = list(frames) if frames is not None else None
        self.opens = opens
        self.opened = self.closed = False

    def open(self):
        self.opened = True
        return self.opens

    def read(self):
        if self.frames is None:
            return FRAME
        return self.frames.pop(0) if self.frames else b""

    def close(self):
        self.closed = True


class FakeDetector(MockWakeDetector):
    """Fires on the Nth frame (never, if fire_on is None)."""

    def __init__(self, fire_on=None):
        super().__init__()
        self.fire_on, self.seen, self.resets = fire_on, 0, 0

    def detect(self, frame):
        self.seen += 1
        return self.fire_on is not None and self.seen >= self.fire_on

    def reset(self):
        self.resets += 1


def gate(mic, det):
    return WakeGate(Config(), mic_factory=lambda: mic, detector=det)


def test_wait_returns_true_on_detection_and_releases_mic():
    mic, det = FakeMic(), FakeDetector(fire_on=3)
    g = gate(mic, det)
    assert g.available
    assert g.wait(timeout_s=5) is True
    assert det.seen == 3 and det.resets == 1
    assert mic.closed  # STT must be able to open the same device next


def test_wait_times_out_without_detection_and_never_fakes_one():
    mic = FakeMic()
    g = gate(mic, FakeDetector(fire_on=None))
    assert g.wait(timeout_s=0.05) is False
    assert mic.closed


def test_mic_that_will_not_open_reports_reason():
    g = gate(FakeMic(opens=False), FakeDetector(fire_on=1))
    assert g.wait(timeout_s=1) is False
    assert "microphone" in g.reason


def test_mic_that_stops_delivering_audio_gives_up():
    mic = FakeMic(frames=[])  # every read is empty
    g = gate(mic, FakeDetector(fire_on=1))
    assert g.wait(timeout_s=5) is False
    assert "stopped" in g.reason and mic.closed


def test_missing_model_is_unavailable_with_reason(tmp_path):
    cfg = Config()
    cfg.voice.wake_model_path = str(tmp_path / "missing.onnx")
    det = OpenWakeWordDetector(model_path=str(tmp_path / "missing.onnx"))
    g = WakeGate(cfg, mic_factory=FakeMic, detector=det)
    assert not g.available
    assert g.wait(timeout_s=0.01) is False
    assert "missing.onnx" in g.reason or "not installed" in g.reason


def test_gate_uses_stt_microphone_and_configured_threshold(monkeypatch):
    import veronica.voice.microphone as microphone
    # Hermetic: don't depend on which devices this machine has right now.
    monkeypatch.setattr(microphone, "resolve_input_device", lambda name, index: index)
    cfg = Config()
    cfg.stt.mic_device_index = 7
    cfg.voice.wake_threshold = 0.42
    g = WakeGate(cfg)
    assert g.device_index == 7
    assert g.detector.threshold == 0.42


# --- CLI integration (CLIApp.__init__ builds TTS/STT, so wire a bare instance) ---

def bare_cli(wake_gate, stt_text="what time is it"):
    cli = CLIApp.__new__(CLIApp)
    cli.config = Config()
    cli.voice_mode = True
    cli.wake_gate = wake_gate
    cli._capturing = threading.Event()
    calls = []

    def listen_once(timeout_s=6):
        calls.append("stt")
        return SimpleNamespace(success=True, text=stt_text, error=None)

    cli.stt = SimpleNamespace(listen_once=listen_once)
    cli._beep = lambda: calls.append("beep")
    return cli, calls


def test_cli_waits_for_wake_word_before_listening():
    g = gate(FakeMic(), FakeDetector(fire_on=2))
    cli, calls = bare_cli(g)
    assert cli._get_input() == "what time is it"
    assert calls == ["beep", "stt"]


def test_cli_does_not_listen_when_wake_word_not_heard(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    g = gate(FakeMic(opens=False), FakeDetector(fire_on=1))
    cli, calls = bare_cli(g)
    assert cli._get_input() == ""
    assert calls == []  # STT never ran, so nothing reaches the brain


def test_cli_without_wake_gate_listens_directly():
    cli, calls = bare_cli(None)
    assert cli._get_input() == "what time is it"
    assert calls == ["stt"]


def test_unavailable_gate_falls_back_to_plain_voice(capsys):
    cli = CLIApp.__new__(CLIApp)
    cli.config = Config()
    unavailable = SimpleNamespace(available=False, reason="wake model not found at X")
    assert cli._make_wake_gate(unavailable) is None
    assert "wake model not found at X" in capsys.readouterr().out


class ScoredDetector(MockWakeDetector):
    """Replays a list of per-frame scores; fires when a score reaches the threshold."""

    def __init__(self, scores, threshold=0.5):
        super().__init__()
        self.scores, self.threshold, self.last_score = list(scores), threshold, 0.0

    def detect(self, frame):
        self.last_score = self.scores.pop(0) if self.scores else 0.0
        return self.last_score >= self.threshold


def test_near_miss_reported_once_per_utterance_with_peak(monkeypatch):
    import veronica.voice.wake_gate as wg
    clock = iter(i * 0.08 for i in range(10_000))   # each frame = 80 ms of fake time
    monkeypatch.setattr(wg.time, "monotonic", lambda: next(clock))
    # utterance peaking at 0.31, then ~2 s of silence, then the real wake word
    scores = [0.0, 0.2, 0.31, 0.25, 0.1] + [0.0] * 25 + [0.9]
    reports = []
    g = gate(FakeMic(), ScoredDetector(scores))
    assert g.wait(timeout_s=100, on_near_miss=reports.append) is True
    assert reports == [0.31]


def test_silence_produces_no_near_miss_reports():
    reports = []
    g = gate(FakeMic(), ScoredDetector([0.01] * 40))
    assert g.wait(timeout_s=0.3, on_near_miss=reports.append) is False
    assert reports == []
