"""
Talk over VERONICA: say "Veronica" while she's speaking and she stops mid-word to listen.

While a reply plays, this listens on a tap of the shared microphone and runs the same
wake-word model used to start a conversation. Using the wake word (rather than "any loud
sound") is deliberate: on laptop speakers the mic hears her own voice, and an energy
trigger would make her interrupt herself. Her own name is the one thing she never says
by accident — and for a sentence that does contain "Veronica", the listener stands down.
"""
from __future__ import annotations

import re
import threading
from typing import Callable, Optional

from veronica.logging_setup import get_logger

logger = get_logger("voice.barge_in")

FRAME_SAMPLES = 1280      # 80 ms, openWakeWord's step


class BargeInMonitor:
    def __init__(self, tap_factory: Callable[[int], object], detector, name: str = "VERONICA") -> None:
        self._tap_factory = tap_factory
        self.detector = detector
        self._name = re.compile(rf"\b{re.escape(name)}\b", re.I)
        self._armed = threading.Event()
        self._armed.set()
        self._done = threading.Event()
        self.triggered = False
        self._thread: Optional[threading.Thread] = None

    def arm_for(self, piece: str) -> None:
        """Called before each spoken sentence: stand down while she says her own name."""
        if self._name.search(piece or ""):
            self._armed.clear()
        else:
            self._armed.set()

    def start(self, stop: threading.Event) -> "BargeInMonitor":
        tap = self._tap_factory(FRAME_SAMPLES)
        if not tap.open():
            logger.info("Talk-over listener unavailable: the microphone tap didn't open.")
            return self
        try:
            self.detector.reset()
        except Exception:  # noqa: BLE001
            pass

        def watch() -> None:
            try:
                while not self._done.is_set() and not stop.is_set():
                    frame = tap.read()
                    if not frame:
                        continue
                    if not self._armed.is_set():
                        continue
                    if self.detector.detect(frame):
                        self.triggered = True
                        logger.info("Talked over: stopping speech.")
                        stop.set()
                        return
            except Exception:  # noqa: BLE001 - a listener failure must never break speech
                logger.exception("Talk-over listener failed.")
            finally:
                tap.close()
        self._thread = threading.Thread(target=watch, name="barge-in", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._done.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
