import time
from veronica.vision.vision_job import VisionJobExecutor
from veronica.vision.screen_state import ScreenState
from veronica.vision.visual_analysis import VisualAnalysis, DetectedElement, DetectedTextRegion
from veronica.vision.vision_provider import VisionProvider, CancellationToken
from unittest.mock import MagicMock

class DummyVisionProvider(VisionProvider):
    """A simple test provider that returns a fixed analysis."""

    def __init__(self, return_value: VisualAnalysis = None,
                 should_raise: Exception = None):
        self._return_value = return_value
        self._should_raise = should_raise
        self.analyze_called = False
        self.last_image_bytes = None
        self.last_screen_state = None
        self.last_prompt = None
        self.last_timeout = None
        self.last_cancellation_token = None

    def analyze(self, image: bytes, screen_state: ScreenState, prompt: str,
                timeout: float, cancellation_token: Optional[CancellationToken] = None) -> VisualAnalysis:
        self.analyze_called = True
        self.last_image_bytes = image
        self.last_screen_state = screen_state
        self.last_prompt = prompt
        self.last_timeout = timeout
        self.last_cancellation_token = cancellation_token

        if self._should_raise:
            raise self._should_raise
        if self._return_value is not None:
            return self._return_value
        # Default return
        return VisualAnalysis(
            summary="dummy summary",
            detected_elements=[],
            detected_text=[],
            visual_state="dummy",
            confidence=0.8,
            latency_ms=10.0
        )

    def is_available(self) -> bool:
        return True

    def close(self) -> None:
        pass

provider = DummyVisionProvider(return_value=VisualAnalysis(
    summary="test",
    detected_elements=[],
    detected_text=[],
    visual_state="test",
    confidence=0.9,
    latency_ms=10.0
))
executor = VisionJobExecutor(provider, timeout_seconds=1.0)

print("First submit")
future1 = executor.submit(b"fake", None, "prompt")
print(f"future1: {future1}")
if future1 is not None:
    print("Waiting for future1 result...")
    result = future1.result(timeout=2.0)
    print(f"Result: {result}")
else:
    print("First submit returned None!")

print("\nSecond submit (while first running)")
future2 = executor.submit(b"fake2", None, "prompt")
print(f"future2: {future2}")

# Clean up
executor.shutdown(wait=True)
print("Done")
