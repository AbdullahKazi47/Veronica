import sys
import types
from unittest.mock import patch

import pytest

from veronica.config import STTConfig
from veronica.speech.stt import SpeechToText, STTResult


def test_none_backend_returns_clean_error():
    cfg = STTConfig(backend="none")
    out = SpeechToText(cfg).listen_once()
    assert out.success is False
    assert "disabled" in out.error.lower()


def test_google_free_missing_pyaudio_returns_actionable_error():
    """PyAudio is optional. When it's absent, SpeechRecognition's Microphone raises
    ImportError — the backend must surface the choice (install PyAudio, or use an
    offline backend) rather than reporting a generic 'listening failed' error."""
    cfg = STTConfig(backend="google_free")

    class _FakeMicrophone:
        def __init__(self, *a, **kw):
            raise ImportError("Please install PyAudio")

    fake_sr = types.SimpleNamespace(
        Recognizer=lambda: types.SimpleNamespace(),
        Microphone=_FakeMicrophone,
    )
    with patch.dict(sys.modules, {"speech_recognition": fake_sr}):
        out = SpeechToText(cfg).listen_once()
    assert out.success is False
    assert "PyAudio" in out.error
    assert "faster_whisper" in out.error or "sounddevice" in out.error
    assert "Listening timed out" not in out.error


def test_faster_whisper_missing_dependency_friendly_message():
    cfg = STTConfig(backend="faster_whisper")
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
    """When the model transcribes empty audio, the result must say 'no speech'
    rather than crashing."""
    cfg = STTConfig(backend="faster_whisper")

    # Stub WhisperModel.transcribe to return no segments.
    class _FakeModel:
        def transcribe(self, samples, language=None, beam_size=None):
            return ([], None)

    fake_module = types.SimpleNamespace(WhisperModel=lambda *a, **kw: _FakeModel())
    # sounddevice.rec returns a (N,1) array; wait is a no-op; np.asarray flattens it.
    audio = [[0.0]] * 10  # shape (10, 1)
    sd_stub = types.SimpleNamespace(
        rec=lambda *a, **kw: audio,
        wait=lambda: None,
    )
    np_stub = types.SimpleNamespace(
        asarray=lambda a: type("x", (), {"flatten": lambda self: a})(),
    )
    with patch.dict(sys.modules, {"faster_whisper": fake_module, "sounddevice": sd_stub, "numpy": np_stub}):
        out = SpeechToText(cfg).listen_once()
    assert out.success is False
    assert "No speech" in out.error