"""
Supertonic (github.com/supertone-inc/supertonic): the voices behind FreeTTS's "AI Local
TTS" (Female 1-5 / Male 1-5 = F1-F5 / M1-M5), running on this PC. No internet, no key, no
limit, and about 0.3x real time here, twice as fast as Kokoro.

Model files: data/models/supertonic/ (~385 MB, downloaded from Hugging Face on first use
by the official `supertonic` package, or with scripts/get_voice_models.py). Weights are
OpenRAIL-M (use restrictions apply to harmful uses; personal use is fine), code MIT.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from veronica.logging_setup import get_logger

logger = get_logger("speech.supertonic")

VOICES = ("F1", "F2", "F3", "F4", "F5", "M1", "M2", "M3", "M4", "M5")
_LOCK = threading.Lock()
_ENGINES: dict[str, object] = {}
_STYLES: dict[tuple[str, str], object] = {}


def model_dir(config=None) -> Path:
    return config.resolve_path("data/models/supertonic") if config is not None and hasattr(config, "resolve_path") \
        else Path("data/models/supertonic")


def installed(folder: Path) -> bool:
    try:
        import supertonic  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return folder.exists() and any(folder.rglob("*.onnx"))


def _engine(folder: Path):
    key = str(folder)
    with _LOCK:
        if key not in _ENGINES:
            from supertonic import TTS
            # auto_download only when nothing is there yet; never re-fetch on every start
            _ENGINES[key] = TTS(model_dir=str(folder), auto_download=not installed(folder))
        return _ENGINES[key]


def _style(folder: Path, voice: str):
    key = (str(folder), voice)
    if key not in _STYLES:
        _STYLES[key] = _engine(folder).get_voice_style(voice_name=voice)
    return _STYLES[key]


_PLAIN = str.maketrans({"—": ", ", "–": ", ", "‘": "'", "’": "'", "“": '"', "”": '"',
                        "…": "...", " ": " ", "•": ", "})


def speakable(text: str) -> str:
    """Dashes, curly quotes and ellipses as plain punctuation; anything else the model
    can't read (emoji, stray control bytes) dropped, so one odd character never costs
    the whole sentence."""
    t = (text or "").translate(_PLAIN)
    return "".join(ch for ch in t if ch.isprintable() and (ord(ch) < 0x2000 or ch.isalpha())).strip()


def synthesize(folder: Path, text: str, voice: str, out: Path, speed: float = 1.05,
               steps: int = 6) -> Optional[Path]:
    """Write one WAV clip. None on failure. `steps` trades quality for speed (5-8)."""
    try:
        engine = _engine(folder)
        text = speakable(text)
        if not text:
            return None
        wav, _dur = engine.synthesize(text, voice_style=_style(folder, voice), total_steps=steps,
                                      speed=speed, lang="en")
        tmp = out.with_name(out.stem + ".part.wav")
        engine.save_audio(wav, str(tmp))
        tmp.replace(out)
        return out
    except Exception as e:  # noqa: BLE001
        logger.warning("Supertonic voice failed (%s).", e)
        return None
