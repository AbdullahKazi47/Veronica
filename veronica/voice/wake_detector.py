"""Phase 11C/11E — Wake-word provider abstraction."""
from __future__ import annotations

from abc import ABC, abstractmethod
import os

import numpy as np


class WakeWordDetector(ABC):
    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def detect(self, frame: bytes) -> bool: ...


class OpenWakeWordDetector(WakeWordDetector):
    """Real local openWakeWord detector using a verified custom ONNX model."""

    def __init__(
        self,
        phrases: list[str] | None = None,
        model_path: str | None = None,
        threshold: float = 0.5,
    ):
        self.phrases = phrases or ["Veronica", "V"]
        self.running = False
        self._model = None
        self.model_path = model_path
        self.threshold = threshold
        self._adapter = None
        self._model_name = "veronica_wake_model"

        try:
            import openwakeword as ow
            self._adapter = ow
        except ImportError:
            self._adapter = None

    def start(self) -> None:
        self.running = False
        self._model = None

        if self._adapter is None:
            return

        if not self.model_path or not os.path.isfile(self.model_path):
            return

        try:
            from openwakeword.model import Model

            self._model = Model(
                wakeword_models=[self.model_path],
                inference_framework="onnx",
            )

            self.running = True
        except Exception:
            self._model = None
            self.running = False

    def stop(self) -> None:
        self.running = False
        self._model = None

    def detect(self, frame: bytes) -> bool:
        if not self.running or self._model is None:
            return False

        if not frame:
            return False

        try:
            audio = np.frombuffer(frame, dtype=np.int16)

            if audio.size == 0:
                return False

            predictions = self._model.predict(audio)

            score = float(predictions.get(self._model_name, 0.0))

            return score >= self.threshold

        except Exception:
            return False


class MockWakeDetector(WakeWordDetector):
    """Mock / offline-fallback; never generates fake detections."""

    def __init__(self, phrases: list[str] | None = None):
        self.phrases = phrases or ["Veronica", "V"]
        self.running = False

    def start(self):
        self.running = True

    def stop(self):
        self.running = False

    def detect(self, frame: bytes) -> bool:
        return False