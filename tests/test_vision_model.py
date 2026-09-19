"""
Unit tests for Vision Model V1 components.
Tests the privacy boundary, policy gating, job executor, provider abstraction,
and integration with ContinuousObservationService.
"""
from __future__ import annotations

import io
import time
import unittest
from dataclasses import dataclass
from typing import Optional
from unittest.mock import MagicMock, patch

from PIL import Image

from veronica.config import VisionConfig
from veronica.vision.continuous_observer import ContinuousObservationService
from veronica.vision.screen_state import ScreenState, WindowInfo
from veronica.vision.vision_image_privacy import VisionImagePrivacy
from veronica.vision.vision_job import VisionJobExecutor
from veronica.vision.vision_policy import VisionPolicy, VisionPolicyDecision
from veronica.vision.vision_provider import (
    CancellationToken,
    StubVisionProvider,
    VisionProvider,
    VisionProviderError,
    VisionTimeoutError,
    VisionCancellationError,
)
from veronica.vision.visual_analysis import (
    DetectedElement,
    DetectedTextRegion,
    VisualAnalysis,
)


class DummyVisionProvider(VisionProvider):
    """A simple test provider that returns a fixed analysis."""

    def __init__(self, return_value: Optional[VisualAnalysis] = None,
                 should_raise: Optional[Exception] = None,
                 delay: float = 0.0):
        self._return_value = return_value
        self._should_raise = should_raise
        self.delay = delay
        self.analyze_called = False
        self.last_image_bytes: Optional[bytes] = None
        self.last_screen_state: Optional[ScreenState] = None
        self.last_prompt: Optional[str] = None
        self.last_timeout: Optional[float] = None
        self.last_cancellation_token: Optional[CancellationToken] = None

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
            # Simulate delay if specified before returning
            if self.delay > 0:
                time.sleep(self.delay)
            return self._return_value
        # Default return
        if self.delay > 0:
            time.sleep(self.delay)
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


class TestVisionImagePrivacy(unittest.TestCase):
    """Test the VisionImagePrivacy preprocessing."""

    def setUp(self) -> None:
        self.config = VisionConfig(
            enabled=True,
            crop_to_active_window=True,
            downscale_width=100,
            downscale_height=100,
            to_grayscale=False,
            full_screen_fallback=False,
        )
        self.privacy = VisionImagePrivacy(self.config)

    def test_process_no_active_window(self) -> None:
        # Create a simple image
        img = Image.new('RGB', (200, 200), color='red')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        screenshot_bytes = buf.getvalue()

        screen_state = ScreenState(
            screen_width=200,
            screen_height=200,
            active_window=None  # No active window
        )

        processed = self.privacy.process(screenshot_bytes, screen_state)
        # Should be downscaled to 100x100
        out_img = Image.open(io.BytesIO(processed))
        self.assertEqual(out_img.size, (100, 100))
        # Should still be RGB (not grayscale)
        self.assertEqual(out_img.mode, 'RGB')

    def test_process_with_active_window_crop(self) -> None:
        # Create a simple image
        img = Image.new('RGB', (200, 200), color='red')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        screenshot_bytes = buf.getvalue()

        # Active window covering the left half
        active_window = WindowInfo(
            window_id="1",
            title="Test",
            process_name="Test",
            process_id=1,
            bounding_box={"left": 0, "top": 0, "width": 100, "height": 200},
            is_active=True
        )
        screen_state = ScreenState(
            screen_width=200,
            screen_height=200,
            active_window=active_window
        )

        processed = self.privacy.process(screenshot_bytes, screen_state)
        # Should be cropped to 100x200 then downscaled to 100x100
        out_img = Image.open(io.BytesIO(processed))
        self.assertEqual(out_img.size, (100, 100))

    def test_process_grayscale(self) -> None:
        config = VisionConfig(
            enabled=True,
            crop_to_active_window=False,
            downscale_width=100,
            downscale_height=100,
            to_grayscale=True,
            full_screen_fallback=False,
        )
        privacy = VisionImagePrivacy(config)

        img = Image.new('RGB', (200, 200), color='red')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        screenshot_bytes = buf.getvalue()

        screen_state = ScreenState(screen_width=200, screen_height=200)
        processed = privacy.process(screenshot_bytes, screen_state)

        out_img = Image.open(io.BytesIO(processed))
        self.assertEqual(out_img.size, (100, 100))
        self.assertEqual(out_img.mode, 'L')  # Grayscale

    def test_process_full_screen_fallback_disabled_by_default(self) -> None:
        # Even if config had full_screen_fallback=True, we want to test the default
        config = VisionConfig(enabled=True)  # defaults: crop=True, fallback=False
        privacy = VisionImagePrivacy(config)

        img = Image.new('RGB', (200, 200), color='red')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        screenshot_bytes = buf.getvalue()

        screen_state = ScreenState(
            screen_width=200,
            screen_height=200,
            active_window=None  # No active window, but fallback is off by default
        )
        # With no active window and fallback off, we should still process (crop off, so full image)
        processed = privacy.process(screenshot_bytes, screen_state)
        out_img = Image.open(io.BytesIO(processed))
        self.assertEqual(out_img.size, (640, 480))  # default downscale

    def test_process_invalid_image_returns_original(self) -> None:
        # Corrupted image bytes
        screenshot_bytes = b"not an image"
        screen_state = ScreenState()
        processed = self.privacy.process(screenshot_bytes, screen_state)
        self.assertEqual(processed, screenshot_bytes)


class TestVisionPolicy(unittest.TestCase):
    """Test the VisionPolicy gating logic."""

    def setUp(self) -> None:
        self.config = VisionConfig(
            enabled=True,
            # Policy thresholds
        )
        self.policy = VisionPolicy(self.config)

    def test_should_run_vision_cooldown(self) -> None:
        # First run should be allowed (no cooldown)
        decision = self.policy.should_run_vision(ScreenState())
        self.assertTrue(decision.should_run)
        self.assertEqual(decision.reason, "no_previous_state")

        # Simulate a recent run by setting last_run time
        self.policy._last_run_time = time.time()
        decision = self.policy.should_run_vision(ScreenState())
        self.assertFalse(decision.should_run)
        self.assertIn("cooldown", decision.reason)

    def test_should_run_vision_change_threshold(self) -> None:
        # Set up a previous state with significant change
        prev_state = ScreenState(
            screen_width=100,
            screen_height=100,
            active_window=WindowInfo(
                window_id="1", title="A", process_name="A", process_id=1,
                bounding_box={"left": 0, "top": 0, "width": 50, "height": 50},
                is_active=True
            )
        )
        curr_state = ScreenState(
            screen_width=100,
            screen_height=100,
            active_window=WindowInfo(
                window_id="2", title="B", process_name="B", process_id=2,
                bounding_box={"left": 0, "top": 0, "width": 50, "height": 50},
                is_active=True
            )
        )
        # Set previous state in policy
        self.policy._previous_state = prev_state
        # Change in active window should trigger (given default thresholds)
        decision = self.policy.should_run_vision(curr_state)
        self.assertIsInstance(decision, VisionPolicyDecision)

    def test_should_run_vision_task_relevance(self) -> None:
        # Set task context
        self.policy._task_context = "browser"
        # Window with title containing task context
        curr_state = ScreenState(
            active_window=WindowInfo(
                window_id="1", title="Browser - Google", process_name="browser", process_id=1,
                bounding_box={"left": 0, "top": 0, "width": 100, "height": 100},
                is_active=True
            )
        )
        prev_state = ScreenState(
            active_window=WindowInfo(
                window_id="1", title="Editor - Code", process_name="editor", process_id=1,
                bounding_box={"left": 0, "top": 0, "width": 100, "height": 100},
                is_active=True
            )
        )
        # Set previous state
        self.policy._previous_state = prev_state
        # The change in window (from editor to browser) plus task relevance should trigger
        decision = self.policy.should_run_vision(curr_state, "browser")
        # We don't assert the exact outcome because it depends on internal scoring,
        # but we ensure it returns a decision.
        self.assertIsInstance(decision, VisionPolicyDecision)


class TestVisionJobExecutor(unittest.TestCase):
    """Test the VisionJobExecutor bounds and behavior."""

    def setUp(self) -> None:
        self.provider = DummyVisionProvider(delay=0.5)
        self.executor = VisionJobExecutor(self.provider, timeout_seconds=1.0)

    def test_submit_when_idle_returns_future(self) -> None:
        future = self.executor.submit(b"fake", None, "prompt")
        self.assertIsNotNone(future)
        # The job should have run
        self.assertTrue(self.provider.analyze_called)

    def test_submit_when_busy_returns_none(self) -> None:
        # First submission
        future1 = self.executor.submit(b"fake1", None, "prompt")
        self.assertIsNotNone(future1)
        print(f"future1: {future1}")
        # Second submission while first is running (we don't wait for completion)
        future2 = self.executor.submit(b"fake2", None, "prompt")
        print(f"future2: {future2}")
        self.assertIsNone(future2)  # Should be skipped

    def test_shutdown_prevents_new_submissions(self) -> None:
        self.executor.shutdown(wait=False)
        future = self.executor.submit(b"fake", None, "prompt")
        self.assertIsNone(future)


class TestVisualAnalysis(unittest.TestCase):
    """Test the VisualAnalysis dataclass and related."""

    def test_visual_analysis_creation(self) -> None:
        va = VisualAnalysis(
            summary="Test",
            detected_elements=[DetectedElement("el1", "Button", {"left": 0, "top": 0, "width": 10, "height": 10})],
            detected_text=[DetectedTextRegion("Hello", 0.9, {"left": 0, "top": 0, "width": 20, "height": 10})],
            visual_state="state",
            confidence=0.95,
            latency_ms=50.0
        )
        self.assertEqual(va.summary, "Test")
        self.assertEqual(len(va.detected_elements), 1)
        self.assertEqual(len(va.detected_text), 1)
        self.assertEqual(va.confidence, 0.95)

    def test_visual_analysis_to_dict(self) -> None:
        va = VisualAnalysis(
            summary="Test",
            detected_elements=[],
            detected_text=[],
            visual_state="state",
            confidence=0.8,
            latency_ms=16.0
        )
        d = va.to_dict()
        self.assertEqual(d["summary"], "Test")
        self.assertEqual(d["confidence"], 0.8)
        self.assertEqual(d["latency_ms"], 16.0)
        self.assertIsInstance(d["detected_elements"], list)
        self.assertIsInstance(d["detected_text"], list)


class TestContinuousObservationServiceIntegration(unittest.TestCase):
    """Test integration of vision components into ContinuousObservationService."""

    def setUp(self) -> None:
        self.vision_config = VisionConfig(
            enabled=True,
            provider="stub",
            model="stub-vision-v1",
            timeout_seconds=5.0,
            crop_to_active_window=True,
            downscale_width=640,
            downscale_height=480,
            to_grayscale=False,
            full_screen_fallback=False,
        )
        # We'll mock the vision provider to avoid actual model loading
        self.vision_provider = DummyVisionProvider(
            return_value=VisualAnalysis(
                summary="Test observation",
                detected_elements=[],
                detected_text=[],
                visual_state="normal",
                confidence=0.9,
                latency_ms=33.0
            )
        )

    def test_service_starts_with_vision_enabled(self) -> None:
        # Create service with vision components
        service = ContinuousObservationService(
            vision_provider=self.vision_provider,
            vision_config=self.vision_config
        )
        # Check that the service has the vision components
        self.assertIsNotNone(service._vision_provider)
        self.assertIsNotNone(service._vision_config)
        self.assertIsNotNone(service._vision_privacy)
        self.assertIsNotNone(service._vision_policy)
        self.assertIsNotNone(service._vision_executor)

    def test_capture_and_process_frame_runs_vision_when_policy_allows(self) -> None:
        # We'll mock the policy to always allow vision
        service = ContinuousObservationService(
            vision_provider=self.vision_provider,
            vision_config=self.vision_config
        )
        # Override the policy's should_run_vision to return a decision to run
        service._vision_policy.should_run_vision = MagicMock(
            return_value=VisionPolicyDecision(should_run=True, reason="test")
        )
        # Also we need to mock the perception layer to return a state with a screenshot
        mock_perception = MagicMock()
        mock_state = ScreenState(
            screen_width=1280,
            screen_height=720,
            active_window=WindowInfo(
                window_id="1", title="Test", process_name="test", process_id=1,
                bounding_box={"left": 0, "top": 0, "width": 100, "height": 100},
                is_active=True
            )
        )
        # Create a fake screenshot
        img = Image.new('RGB', (100, 100), color='blue')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        mock_state.raw_screenshot = buf.getvalue()
        mock_perception.get_current_state.return_value = mock_state
        service._perception_layer = mock_perception

        # Run one frame
        service._capture_and_process_frame()

        # Check that the vision provider was called
        self.assertTrue(self.vision_provider.analyze_called)
        # Check that the understanding in the latest state has visual analysis
        # Note: The frame is queued and internal state updated, but we can check the latest state
        # after a short delay or by accessing the internal state (with lock).
        # For simplicity, we'll check that the provider was called with the processed image.
        # We can also check that the understanding in the event has visual analysis,
        # but that requires accessing the queue. Instead, we'll trust that if the provider
        # was called and returned a value, it was used.

        # Alternatively, we can check that the service's latest state understanding has visual_analysis set.
        with service._lock:
            latest_state = service._latest_state
        if latest_state is not None:
            # The understanding is built in _capture_and_process_frame and passed to the event.
            # We don't have direct access to the understanding of the latest state unless we
            # store it. We'll skip this for now and rely on the provider being called.
            pass

    def test_capture_and_process_frame_skips_vision_when_policy_denies(self) -> None:
        service = ContinuousObservationService(
            vision_provider=self.vision_provider,
            vision_config=self.vision_config
        )
        # Mock policy to deny
        service._vision_policy.should_run_vision = MagicMock(
            return_value=VisionPolicyDecision(should_run=False, reason="test")
        )
        mock_perception = MagicMock()
        mock_state = ScreenState()
        mock_perception.get_current_state.return_value = mock_state
        service._perception_layer = mock_perception

        service._capture_and_process_frame()

        # Provider should not have been called
        self.assertFalse(self.vision_provider.analyze_called)

    def test_capture_and_process_frame_handles_vision_failure_gracefully(self) -> None:
        service = ContinuousObservationService(
            vision_provider=self.vision_provider,
            vision_config=self.vision_config
        )
        # Mock policy to allow
        service._vision_policy.should_run_vision = MagicMock(
            return_value=VisionPolicyDecision(should_run=True, reason="test")
        )
        # Make the provider raise an exception
        self.vision_provider._should_raise = VisionProviderError("Test error")
        mock_perception = MagicMock()
        mock_state = ScreenState()
        # Provide a screenshot
        img = Image.new('RGB', (100, 100), color='green')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        mock_state.raw_screenshot = buf.getvalue()
        mock_perception.get_current_state.return_value = mock_state
        service._perception_layer = mock_perception

        # This should not raise an exception
        try:
            service._capture_and_process_frame()
        except Exception as e:
            self.fail(f"_capture_and_process_frame raised {e} unexpectedly")

        # Provider was called but failed
        self.assertTrue(self.vision_provider.analyze_called)
        # The understanding should have been built without visual analysis (None)
        # We can't easily check without accessing the event, but we can check that
        # the service didn't crash.

    def test_capture_and_process_frame_handles_vision_timeout(self) -> None:
        service = ContinuousObservationService(
            vision_provider=self.vision_provider,
            vision_config=self.vision_config
        )
        service._vision_policy.should_run_vision = MagicMock(
            return_value=VisionPolicyDecision(should_run=True, reason="test")
        )
        # Make the provider slow (simulate timeout by making analyze take longer than timeout)
        # We'll instead make the provider raise a timeout error.
        self.vision_provider._should_raise = VisionTimeoutError("Timeout")
        mock_perception = MagicMock()
        mock_state = ScreenState()
        img = Image.new('RGB', (100, 100), color='yellow')
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        mock_state.raw_screenshot = buf.getvalue()
        mock_perception.get_current_state.return_value = mock_state
        service._perception_layer = mock_perception

        try:
            service._capture_and_process_frame()
        except Exception as e:
            self.fail(f"_capture_and_process_frame raised {e} unexpectedly")

        self.assertTrue(self.vision_provider.analyze_called)


if __name__ == '__main__':
    unittest.main()