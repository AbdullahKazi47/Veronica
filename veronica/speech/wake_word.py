"""
A lightweight trigger listener, offered as a cheaper alternative to always running full
cloud STT. Two modes:
  - "push_to_talk": trivial, default — the UI decides when to listen (press a key).
  - "energy_spike": a generalization of hectorg2211/jarvis's double-clap detector — flags
    a loud, sudden spike in mic input above ambient RMS, so VERONICA can wake without
    a heavyweight always-on wake-word model or paid API.

This module intentionally does NOT try to do full wake-word phrase detection (e.g.
"hey VERONICA") with a neural model — that is a good, well-scoped future upgrade
(e.g. via `openwakeword` or Porcupine) and is left as a clean extension point rather
than half-implemented here.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from veronica.config import WakeWordConfig
from veronica.logging_setup import get_logger

logger = get_logger("speech.wake_word")


class WakeWordListener:
    def __init__(self, config: WakeWordConfig) -> None:
        self.config = config

    def wait_for_trigger(self, on_trigger: Optional[Callable[[], None]] = None, timeout_s: float = 30.0) -> bool:
        """Block until a trigger is detected (or timeout). Returns True if triggered."""
        if not self.config.enabled or self.config.mode == "push_to_talk":
            # Push-to-talk: the UI layer is responsible for calling listen() directly;
            # this listener has nothing to do.
            return True

        try:
            import numpy as np
            import sounddevice as sd
        except ImportError:
            logger.warning("energy_spike wake word needs `pip install sounddevice numpy`; falling back to push-to-talk.")
            return True

        block_ms = 100
        sample_rate = 16000
        ambient_rms = 0.01
        deadline = time.time() + timeout_s

        def rms(block) -> float:
            return float(np.sqrt(np.mean(block.astype(np.float64) ** 2)) + 1e-9)

        try:
            with sd.InputStream(samplerate=sample_rate, channels=1, dtype="int16") as stream:
                # calibrate briefly against ambient noise
                calib, _ = stream.read(int(sample_rate * 0.5))
                ambient_rms = max(rms(calib), 1.0)

                while time.time() < deadline:
                    block, _ = stream.read(int(sample_rate * block_ms / 1000))
                    if rms(block) > ambient_rms * self.config.spike_ratio:
                        logger.info("Wake trigger detected (energy spike).")
                        if on_trigger:
                            on_trigger()
                        return True
        except Exception:
            logger.exception("Wake word listener failed; falling back to push-to-talk.")
            return True

        return False
