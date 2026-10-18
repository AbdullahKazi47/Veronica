"""Instant "Mm-hm." while the model thinks, so she never feels slow to answer.

The cloud model takes seconds before its first word (3-9 s measured on 2026-10-18). The
moment you finish talking she plays a short clip in her own voice, made once and kept
in the voice cache. WAV clips are held in memory and played by winsound, which starts in
~1 ms on this PC (her usual player, MCI, takes ~150 ms to open a file). Commands she runs herself
("open YouTube", "volume 60") answer fast anyway and get no clip.
Settings: voice.quick_ack and voice.quick_ack_phrases in config/config.yaml.
"""
from __future__ import annotations

import random
import threading
from pathlib import Path
from typing import Optional

from veronica.logging_setup import get_logger

logger = get_logger("voice.quick_ack")

PHRASES = ("Mm-hm.", "One sec.", "Let me see.", "Okay.", "Sure, one moment.")


class QuickAck:
    def __init__(self, tts, phrases=PHRASES) -> None:
        self.tts = tts
        self.phrases = [p for p in phrases if p and p.strip()] or list(PHRASES)
        self._clips: dict[str, Path] = {}
        self._last: Optional[str] = None

    def prepare(self) -> threading.Thread:
        """Make the clips in the background at start-up (instant after the first run:
        they come from the voice cache)."""
        def go() -> None:
            for p in self.phrases:
                try:
                    path = self.tts._voice_file(p)
                    if path is not None and Path(path).exists():
                        self._clips[p] = Path(path)
                        if Path(path).suffix.lower() == ".wav":
                            _wav_bytes(Path(path))      # into memory now, not when needed
                except Exception:  # noqa: BLE001 - no clip, no acknowledgement; never fatal
                    logger.debug("Couldn't make the clip %r.", p, exc_info=True)
        t = threading.Thread(target=go, name="quick-ack", daemon=True)
        t.start()
        return t

    def pick(self) -> Optional[Path]:
        """A ready clip, never the same one twice in a row. None while none are ready."""
        ready = [p for p in self._clips if p != self._last] or list(self._clips)
        if not ready:
            return None
        self._last = random.choice(ready)
        return self._clips[self._last]


def play(clip: Path) -> None:
    """Play a clip to the end, as fast to start as Windows allows."""
    clip = Path(clip)
    if clip.suffix.lower() == ".wav":
        try:
            import winsound
            winsound.PlaySound(_wav_bytes(clip), winsound.SND_MEMORY | winsound.SND_NODEFAULT)
            return
        except Exception:  # noqa: BLE001 - not Windows, or an odd WAV: the usual player
            pass
    from veronica.speech.tts import play_interruptible
    play_interruptible(clip, threading.Event())


_wav_cache: dict[Path, bytes] = {}


def _wav_bytes(clip: Path) -> bytes:
    if clip not in _wav_cache:
        _wav_cache[clip] = clip.read_bytes()
    return _wav_cache[clip]
