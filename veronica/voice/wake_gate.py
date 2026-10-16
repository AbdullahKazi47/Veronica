"""Wake-word gate for the voice CLI: block until "Veronica" is heard, then hand over to STT.

The gate owns the microphone only while waiting and closes it before returning, so STT can
open the same device afterwards. It never fakes a detection: when openWakeWord or the
trained model is missing, `available` is False and `reason` says why. Hearing the wake word
grants no authority — the request that follows still goes through the normal brain path
(PermissionGate included).
"""
from __future__ import annotations

import time
from collections import deque
from typing import Callable, Optional

from veronica.config import Config
from veronica.voice.wake_detector import OpenWakeWordDetector, WakeWordDetector

FRAME_SAMPLES = 1280      # 80 ms at 16 kHz — openWakeWord's native step
SAMPLE_RATE = 16000
_MAX_EMPTY_READS = 50     # ~consecutive failed reads before treating the mic as gone
NEAR_MISS_FLOOR = 0.15    # scores from here up to the threshold are reported as near misses
_NEAR_MISS_GAP_S = 1.5    # one report per utterance, not one per 80 ms frame
_AMBIENT_FRAMES = 38      # ~3 s of room audio kept for the room-level estimate
_WARMUP_FRAMES = 7        # ~0.5 s


def _rms(frame: bytes) -> float:
    import numpy as np
    a = np.frombuffer(frame, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(a * a))) if a.size else 0.0


class WakeGate:
    def __init__(
        self,
        config: Config,
        mic_factory: Optional[Callable[[], object]] = None,
        detector: Optional[WakeWordDetector] = None,
    ) -> None:
        voice = config.voice
        self.model_path = str(config.resolve_path(voice.wake_model_path))
        # STT and the wake gate must use the same physical microphone (resolved by name).
        from veronica.voice.microphone import resolve_input_device
        fallback = config.stt.mic_device_index if config.stt.mic_device_index is not None else voice.mic_device_index
        self.device_index = resolve_input_device(getattr(config.stt, "mic_device_name", None), fallback)
        self._mic_factory = mic_factory or self._default_mic
        self.detector = detector or OpenWakeWordDetector(
            phrases=voice.wake_words, model_path=self.model_path, threshold=voice.wake_threshold)
        self.detector.start()
        self.available = bool(getattr(self.detector, "running", False))
        self.reason = "" if self.available else self._why_unavailable()
        self.ambient_rms: Optional[float] = None  # room level measured while waiting

    def _default_mic(self):
        from veronica.voice.microphone import MicrophoneInput
        return MicrophoneInput(device_index=self.device_index, chunk_size=FRAME_SAMPLES, rate=SAMPLE_RATE)

    def _why_unavailable(self) -> str:
        import os
        if getattr(self.detector, "_adapter", object()) is None:
            return "openwakeword is not installed (pip install openwakeword)"
        if not os.path.isfile(self.model_path):
            return f"wake model not found at {self.model_path}"
        return f"wake model failed to load: {self.model_path}"

    def wait(self, timeout_s: Optional[float] = None,
             on_near_miss: Optional[Callable[[float], None]] = None) -> bool:
        """Block until the wake word is detected (True), or timeout / mic failure (False).

        `on_near_miss(score)` is called once per utterance that sounded close to the wake
        word but scored under the threshold, so the user can tell "not heard" from
        "heard, not recognized"."""
        if not self.available:
            return False
        mic = self._mic_factory()
        if not mic.open():
            self.reason = "could not open the microphone"
            return False
        self.detector.reset()  # drop audio buffered from before this wait
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        empty = 0
        near_peak, near_last = 0.0, 0.0
        levels: deque[float] = deque(maxlen=_AMBIENT_FRAMES)
        skip = _WARMUP_FRAMES  # the device is near-silent right after opening
        try:
            while deadline is None or time.monotonic() < deadline:
                frame = mic.read()
                if not frame:
                    empty += 1
                    if empty >= _MAX_EMPTY_READS:
                        self.reason = "the microphone stopped delivering audio"
                        return False
                    time.sleep(0.01)
                    continue
                empty = 0
                if skip:
                    skip -= 1
                else:
                    levels.append(_rms(frame))
                if self.detector.detect(frame):
                    # Room level from the moments BEFORE the wake word, so STT doesn't
                    # calibrate on the user's own voice right after the beep. A low
                    # percentile keeps the "Veronica" itself out of the estimate.
                    if len(levels) >= 5:
                        ordered = sorted(levels)
                        self.ambient_rms = ordered[len(ordered) // 4]
                    return True
                if on_near_miss is not None:
                    score = float(getattr(self.detector, "last_score", 0.0))
                    now = time.monotonic()
                    if score >= NEAR_MISS_FLOOR:
                        near_peak, near_last = max(near_peak, score), now
                    elif near_peak and now - near_last >= _NEAR_MISS_GAP_S:
                        on_near_miss(near_peak)  # utterance over: report its best score once
                        near_peak = 0.0
            return False
        finally:
            mic.close()
