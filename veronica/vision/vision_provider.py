"""
VisionProvider: abstract base class for visual perception models.

Vision models analyze visual screen information. They are PERCEPTION components,
NOT the Brain. They never execute tools, click, type, authorize actions, or
modify permissions.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any

from veronica.vision.screen_state import ScreenState
from veronica.vision.visual_analysis import VisualAnalysis, DetectedElement, DetectedTextRegion


class VisionProviderError(Exception):
    """Base exception for vision provider failures."""
    pass


class VisionTimeoutError(VisionProviderError):
    """Raised when visual analysis exceeds the specified timeout."""
    pass


class VisionCancellationError(VisionProviderError):
    """Raised when visual analysis is cancelled before completion."""
    pass


class CancellationToken:
    """Thread-safe cooperative cancellation token."""

    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()


class VisionProvider(ABC):
    """
    Abstract base interface for visual perception models.

    Must be thread-safe for calls to `analyze()`.
    """

    @abstractmethod
    def is_available(self) -> bool:
        """Check if the vision provider is available and ready for inference."""
        pass

    @abstractmethod
    def analyze(
        self,
        image: bytes,
        screen_state: Optional[ScreenState] = None,
        prompt: Optional[str] = None,
        timeout: Optional[float] = None,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> VisualAnalysis:
        """
        Analyze an image and return structured visual understanding.

        Args:
            image: Preprocessed/defended screenshot image bytes.
            screen_state: Optional ScreenState for structural context.
            prompt: Optional guiding text/focus for the vision model.
            timeout: Maximum seconds to wait for analysis.
            cancellation_token: Optional token for cooperative cancellation.

        Returns:
            VisualAnalysis structured record.

        Raises:
            VisionTimeoutError: If inference times out.
            VisionCancellationError: If inference is cancelled.
            VisionProviderError: On any other inference error.
        """
        pass

    def close(self) -> None:
        """Release any resources held by the provider."""
        pass


class StubVisionProvider(VisionProvider):
    """
    Deterministic stub vision provider for tests and offline validation.

    Does not require any external LLM, GPU, or network.
    """

    def __init__(
        self,
        summary: str = "Visual display shows desktop with active window.",
        confidence: float = 0.9,
        delay: float = 0.0,
        fail_with: Optional[Exception] = None,
        elements: Optional[List[DetectedElement]] = None,
        texts: Optional[List[DetectedTextRegion]] = None,
        available: bool = True,
        model_name: str = "stub-vision-v1",
    ) -> None:
        self.summary = summary
        self.confidence = confidence
        self.delay = delay
        self.fail_with = fail_with
        self.elements = elements or []
        self.texts = texts or []
        self._available = available
        self.model_name = model_name
        self.call_count = 0

    def is_available(self) -> bool:
        return self._available

    def analyze(
        self,
        image: bytes,
        screen_state: Optional[ScreenState] = None,
        prompt: Optional[str] = None,
        timeout: Optional[float] = None,
        cancellation_token: Optional[CancellationToken] = None,
    ) -> VisualAnalysis:
        self.call_count += 1
        start_time = time.time()

        if cancellation_token and cancellation_token.is_cancelled:
            raise VisionCancellationError("Analysis cancelled before execution.")

        if self.delay > 0:
            step = 0.01
            elapsed = 0.0
            while elapsed < self.delay:
                if cancellation_token and cancellation_token.is_cancelled:
                    raise VisionCancellationError("Analysis cancelled during execution.")
                if timeout and elapsed >= timeout:
                    raise VisionTimeoutError(f"Analysis timed out after {timeout}s.")
                time.sleep(min(step, self.delay - elapsed))
                elapsed += step

        if self.fail_with is not None:
            raise self.fail_with

        win_title = None
        if screen_state and screen_state.active_window:
            win_title = screen_state.active_window.title

        latency = (time.time() - start_time) * 1000.0

        return VisualAnalysis(
            summary=self.summary,
            active_window_title=win_title,
            detected_elements=list(self.elements),
            detected_text=list(self.texts),
            visual_state="stable",
            confidence=self.confidence,
            timestamp=time.time(),
            provider="stub",
            model=self.model_name,
            latency_ms=latency,
        )
