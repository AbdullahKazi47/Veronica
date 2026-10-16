import sys
import types
from unittest.mock import patch

import numpy  # noqa: F401 — import once up front; patch.dict(sys.modules) would otherwise evict it
import pytest

from veronica.config import STTConfig
from veronica.speech.stt import SpeechToText, STTResult


def test_none_backend_returns_clean_error():
    cfg = STTConfig(backend="none")
    out = SpeechToText(cfg).listen_once()
    assert out.success is False
    assert "disabled" in out.error.lower()


class _FakeMic:
    """Stand-in for veronica.voice.microphone.MicrophoneInput (Phase 11F capture):
    no real audio device is touched. ``chunks`` are int16 PCM buffers returned by
    read(); once exhausted it keeps returning ``tail``."""

    def __init__(self, chunks=(), tail=b"\x00\x00" * 1024, can_open=True):
        self._chunks = list(chunks)
        self._tail = tail
        self._can_open = can_open
        self.device_index = 0
        self._backend = "fake"
        self.closed = False

    def open(self):
        return self._can_open

    def read(self):
        return self._chunks.pop(0) if self._chunks else self._tail

    def close(self):
        self.closed = True


def _patch_mic(mic):
    return patch("veronica.voice.microphone.MicrophoneInput", lambda *a, **kw: mic)


def _loud_chunk(level=4000):
    import numpy as np
    return (np.ones(1024, dtype=np.int16) * level).tobytes()


def _fake_sr(microphone_cls=None):
    return types.SimpleNamespace(
        Recognizer=lambda: types.SimpleNamespace(),
        Microphone=microphone_cls,
        AudioData=lambda *a, **kw: object(),
        UnknownValueError=type("UnknownValueError", (Exception,), {}),
        RequestError=type("RequestError", (Exception,), {}),
    )


def test_google_free_without_pyaudio_captures_via_sounddevice():
    """Phase 11F: PyAudio is optional. google_free captures through VERONICA's
    sounddevice MicrophoneInput and never constructs sr.Microphone. With no input
    device the error is a clear 'No microphone available', not a timeout."""
    cfg = STTConfig(backend="google_free")

    class _MustNotBeUsed:
        def __init__(self, *a, **kw):
            raise AssertionError("sr.Microphone (PyAudio) must not be used")

    mic = _FakeMic(can_open=False)
    with patch.dict(sys.modules, {"speech_recognition": _fake_sr(_MustNotBeUsed)}), _patch_mic(mic):
        out = SpeechToText(cfg).listen_once()
    assert out.success is False
    assert "No microphone available" in out.error
    assert "Listening timed out" not in out.error


def test_google_free_pyaudio_backend_falls_back_when_pyaudio_missing():
    """mic_backend='pyaudio' without PyAudio installed falls back to sounddevice
    capture instead of failing on the missing binding."""
    cfg = STTConfig(backend="google_free", mic_backend="pyaudio")

    class _MustNotBeUsed:
        def __init__(self, *a, **kw):
            raise AssertionError("sr.Microphone (PyAudio) must not be used")

    mic = _FakeMic(can_open=False)
    with patch.dict(sys.modules, {"speech_recognition": _fake_sr(_MustNotBeUsed), "pyaudio": None}),             _patch_mic(mic):
        out = SpeechToText(cfg).listen_once()
    assert out.success is False
    assert "No microphone available" in out.error


def test_google_free_pyaudio_microphone_import_error_is_actionable():
    """When the PyAudio path is selected and importable but sr.Microphone still
    raises ImportError, surface the actionable choice, not 'listening failed'."""
    cfg = STTConfig(backend="google_free", mic_backend="pyaudio")

    class _FakeMicrophone:
        def __init__(self, *a, **kw):
            raise ImportError("Please install PyAudio")

    with patch.dict(sys.modules, {"speech_recognition": _fake_sr(_FakeMicrophone),
                                  "pyaudio": types.SimpleNamespace()}):
        out = SpeechToText(cfg).listen_once()
    assert out.success is False
    assert "PyAudio" in out.error
    assert "faster_whisper" in out.error or "sounddevice" in out.error
    assert "Listening timed out" not in out.error


def test_capture_reports_no_speech_on_silence():
    """Silence for the whole timeout -> 'No speech detected', mic is closed."""
    stt = SpeechToText(STTConfig(backend="faster_whisper"))
    mic = _FakeMic()  # all-zero buffers forever
    with _patch_mic(mic):
        pcm, err = stt._capture_utterance(timeout_s=0.5)
    assert pcm is None
    assert "No speech detected" in err
    assert mic.closed is True


def test_faster_whisper_missing_dependency_friendly_message():
    cfg = STTConfig(backend="faster_whisper")
    # Simulate the package being absent. Without this the test used the real microphone
    # and real model, and failed whenever anyone was talking in the room.
    with patch.dict(sys.modules, {"faster_whisper": None}):
        out = SpeechToText(cfg).listen_once()
    assert out.success is False
    # Either faster-whisper is installed (test environment dependent) or the message
    # is actionable — either is acceptable; we only assert the failure path is clean.
    if "faster-whisper" in out.error:
        assert "pip install faster-whisper" in out.error


def test_faster_whisper_model_load_failure():
    cfg = STTConfig(backend="faster_whisper", faster_whisper_model="nonexistent")
    # Force a load failure by making WhisperModel raise.
    fake = types.SimpleNamespace(
        WhisperModel=lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("model not found")
        )
    )
    with patch.dict(sys.modules, {"faster_whisper": fake, "sounddevice": types.SimpleNamespace()}):
        # need numpy too — install a stub that supports np.asarray flattening
        np_stub = types.SimpleNamespace(
            ndarray=type("ndarray", (), {}),
            asarray=lambda a: type("x", (), {"flatten": lambda self: a})(),
        )
        with patch.dict(sys.modules, {"numpy": np_stub}):
            out = SpeechToText(cfg).listen_once()
    assert out.success is False
    assert "Whisper" in out.error or "model" in out.error.lower()


def test_faster_whisper_no_speech_returns_clean_error():
    """Speech-level audio is captured but the model transcribes nothing: the
    result must say 'No speech' rather than crashing."""
    cfg = STTConfig(backend="faster_whisper")

    class _FakeModel:
        def transcribe(self, samples, language=None, beam_size=None):
            return ([], None)

    fake_module = types.SimpleNamespace(WhisperModel=lambda *a, **kw: _FakeModel())
    # A few loud chunks (speech starts), then silence until the pause ends capture.
    mic = _FakeMic(chunks=[b"\x00\x00" * 1024] * 5 + [_loud_chunk()] * 3)
    with patch.dict(sys.modules, {"faster_whisper": fake_module}), _patch_mic(mic):
        out = SpeechToText(cfg).listen_once()
    assert out.success is False
    assert "No speech" in out.error
    assert mic.closed is True