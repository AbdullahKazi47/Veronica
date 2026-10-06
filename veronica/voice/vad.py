"""Phase 11C — VAD component (separate; not a single amplitude check)."""
from __future__ import annotations


class VADDetector:
    """Lightweight offline VAD interface; no permanent mic storage by default."""
    def __init__(self, start_threshold: float = 0.02, end_threshold: float = 0.01,
                 silence_duration_s: float = 1.0, max_duration_s: float = 8.0,
                 min_duration_s: float = 0.3):
        self.start_threshold = start_threshold
        self.end_threshold = end_threshold
        self.silence_duration_s = silence_duration_s
        self.max_duration_s = max_duration_s
        self.min_duration_s = min_duration_s
        self.speech_started = False
        self.speech_ended = False

    def start(self) -> None:
        self.speech_started = False
        self.speech_ended = False

    def process(self, audio_frame: bytes) -> None:
        # Mock / offline method: no permanent storage; real backend unavailable = BLOCKED
        pass

    def reset(self) -> None:
        self.speech_started = False
        self.speech_ended = False
