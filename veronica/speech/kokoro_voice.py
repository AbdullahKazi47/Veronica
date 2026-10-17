"""
Kokoro (github.com/hexgrad/kokoro, Apache-2.0) via kokoro-onnx: a natural neural voice that
runs on this PC. No internet, no key, no monthly limit, and no waiting on a cloud service:
a short sentence is ready in about a second on this laptop's CPU.

Model files (data/models/kokoro/, from the kokoro-onnx releases):
  kokoro-v1.0.onnx  ~310 MB (full precision)   voices-v1.0.bin  ~27 MB
On this laptop the full model makes a sentence in about 0.6x its spoken length; the
smaller int8 file was about 1.6x (slower than speaking), so the full one is preferred.
"""
from __future__ import annotations

import threading
import wave
from pathlib import Path
from typing import Optional

from veronica.logging_setup import get_logger

logger = get_logger("speech.kokoro")

# kokoro-onnx's phonemizer warns "words count mismatch" on most sentences; it is harmless
# and would print into the clean terminal.
import logging as _logging  # noqa: E402
_logging.getLogger("phonemizer").setLevel(_logging.ERROR)

MODEL_FILES = ("kokoro-v1.0.onnx", "kokoro-v1.0.int8.onnx")   # fastest first
VOICES_FILE = "voices-v1.0.bin"


def _model(folder: Path) -> Optional[Path]:
    return next((folder / m for m in MODEL_FILES if (folder / m).exists()), None)


_LOCK = threading.Lock()
_ENGINES: dict[str, object] = {}


def model_dir(config=None) -> Path:
    return config.resolve_path("data/models/kokoro") if config is not None and hasattr(config, "resolve_path") \
        else Path("data/models/kokoro")


def installed(folder: Path) -> bool:
    try:
        import kokoro_onnx  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return _model(folder) is not None and (folder / VOICES_FILE).exists()


def _engine(folder: Path):
    key = str(folder)
    with _LOCK:
        if key not in _ENGINES:
            from kokoro_onnx import Kokoro
            _ENGINES[key] = Kokoro(str(_model(folder)), str(folder / VOICES_FILE))
        return _ENGINES[key]


def voices(folder: Path) -> list[str]:
    """Voice names in the voices file, e.g. 'bf_emma' (b = British, a = American; f/m)."""
    try:
        return sorted(_engine(folder).get_voices())
    except Exception:  # noqa: BLE001
        return []


def lang_for(voice: str) -> str:
    return "en-gb" if voice.startswith("b") else "en-us"


def synthesize(folder: Path, text: str, voice: str, out: Path, speed: float = 1.0) -> Optional[Path]:
    """Write one WAV clip. None on failure."""
    try:
        import numpy as np
        samples, rate = _engine(folder).create(text, voice=voice, speed=speed, lang=lang_for(voice))
        pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        tmp = out.with_suffix(".part")
        with wave.open(str(tmp), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(pcm)
        tmp.replace(out)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("Kokoro voice failed (%s).", e)
        return None
