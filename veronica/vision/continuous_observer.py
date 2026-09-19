"""
Continuous Screen Perception V1: ContinuousObservationService.

Maintains continuous desktop understanding through background observation.
OBSERVATION ONLY — no autonomous actions. Disabled by default (opt-in via config).

Pipeline: DESKTOP → CONTINUOUS OBSERVER → CAPTURE FRAME → UIA/SCREEN/OCR →
STATE NORMALIZATION → CHANGE DETECTION → IMPORTANCE FILTER → CONTEXT ENGINE → BRAIN
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
from concurrent.futures import Future

from veronica.logging_setup import get_logger
from veronica.vision.change_detection import ChangeDetector, ChangeDetectionConfig, ChangeStrategy
from veronica.vision.perception import PerceptionLayer, create_perception_layer
from veronica.vision.screen_content_defense import ScreenContentDefense, create_screen_content_defense
from veronica.vision.screen_state import ScreenState
from veronica.vision.understanding import VisionUnderstanding
from veronica.vision.vision_provider import VisionProvider, VisionTimeoutError, VisionCancellationError, CancellationToken
from veronica.vision.vision_image_privacy import VisionImagePrivacy
from veronica.vision.vision_policy import VisionPolicy, VisionPolicyDecision
from veronica.vision.vision_job import VisionJobExecutor
from veronica.config import VisionConfig

logger = get_logger("vision.continuous_observer")


class ObservationStatus(str, Enum):
    """Status of the continuous observation service."""
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    ERROR = "error"


@dataclass
class ObservationConfig:
    """Configuration for continuous observation service."""
    enabled: bool = False
    interval_seconds: float = 2.0
    change_strategy: str = "ACTIVE"  # IDLE, ACTIVE, WAITING, ACTION, ERROR
    max_queue_size: int = 50
    max_recent_events: int = 100
    task_aware_enabled: bool = True
    screen_capture_enabled: bool = True
    uia_enabled: bool = True
    ocr_enabled: bool = False  # OCR is heavy; opt-in
    graceful_degradation: bool = True


@dataclass
class PerceptionEvent:
    """A single perception frame with change detection and importance scoring."""
    frame_id: str
    timestamp: float
    state: ScreenState
    understanding: VisionUnderstanding
    change_detected: bool
    change_score: float
    change_details: Dict[str, Any]
    importance_score: float
    importance_factors: Dict[str, float]

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization/SSE."""
        return {
            "frame_id": self.frame_id,
            "timestamp": self.timestamp,
            "state": self.state.to_dict(),
            "understanding": self.understanding.to_dict(),
            "change_detected": self.change_detected,
            "change_score": self.change_score,
            "change_details": self.change_details,
            "importance_score": self.importance_score,
            "importance_factors": self.importance_factors,
        }


class ImportanceFilter:
    """
    Heuristic importance scoring for perception events — no LLM calls.

    Scores factors:
    - Active window change (high weight)
    - Text region content change (medium weight)
    - UI element count delta (low weight)
    - Focused element change (medium weight)
    - Task context relevance (configurable bias)
    """

    # Weights for different change types (sum ≈ 1.0)
    WEIGHT_ACTIVE_WINDOW = 0.35
    WEIGHT_TEXT_CONTENT = 0.25
    WEIGHT_ELEMENT_COUNT = 0.15
    WEIGHT_FOCUSED_ELEMENT = 0.15
    WEIGHT_CURSOR_MOVEMENT = 0.10

    # Task context bonus (added when task context matches)
    TASK_CONTEXT_BONUS = 0.2

    def __init__(self, task_context: Optional[str] = None):
        self.task_context = task_context

    def set_task_context(self, context: Optional[str]) -> None:
        """Update the task context for relevance scoring."""
        self.task_context = context

    def score(
        self,
        current: ScreenState,
        previous: Optional[ScreenState],
        change_details: Dict[str, Any]
    ) -> tuple[float, Dict[str, float]]:
        """
        Compute importance score (0.0 to 1.0) and factor breakdown.

        Args:
            current: Current screen state
            previous: Previous screen state (None for first frame)
            change_details: Details from ChangeDetector

        Returns:
            (importance_score, factor_breakdown)
        """
        factors = {
            "active_window": 0.0,
            "text_content": 0.0,
            "element_count": 0.0,
            "focused_element": 0.0,
            "cursor_movement": 0.0,
            "task_context": 0.0,
        }

        if previous is None:
            # First frame - moderate importance to establish baseline
            return 0.5, factors

        # Active window change
        if change_details.get("active_window_changed"):
            factors["active_window"] = self.WEIGHT_ACTIVE_WINDOW
            # Bonus if the new window matches task context
            if self.task_context and current.active_window:
                if self._window_matches_task(current.active_window):
                    factors["task_context"] = self.TASK_CONTEXT_BONUS

        # Text content change
        if change_details.get("text_content_changed"):
            factors["text_content"] = self.WEIGHT_TEXT_CONTENT
            if self.task_context:
                # Check if changed text relates to task
                if self._text_matches_task(current, previous):
                    factors["task_context"] = max(factors["task_context"], self.TASK_CONTEXT_BONUS * 0.5)

        # Element count change
        if change_details.get("element_count_changed"):
            factors["element_count"] = self.WEIGHT_ELEMENT_COUNT

        # Focused element change
        if change_details.get("focused_element_changed"):
            factors["focused_element"] = self.WEIGHT_FOCUSED_ELEMENT

        # Cursor movement (minor factor)
        if current.cursor_position and previous.cursor_position:
            dx = current.cursor_position.get("x", 0) - previous.cursor_position.get("x", 0)
            dy = current.cursor_position.get("y", 0) - previous.cursor_position.get("y", 0)
            if dx * dx + dy * dy > 100:  # ~10 pixel threshold
                factors["cursor_movement"] = self.WEIGHT_CURSOR_MOVEMENT

        # Compute total score (capped at 1.0)
        total = sum(factors.values())
        return min(1.0, total), factors

    def _window_matches_task(self, window: Any) -> bool:
        """Check if window title/process matches task context."""
        if not self.task_context:
            return False
        task_lower = self.task_context.lower()
        title = getattr(window, "title", "").lower()
        process = getattr(window, "process_name", "").lower()
        return task_lower in title or task_lower in process

    def _text_matches_task(self, current: ScreenState, previous: ScreenState) -> bool:
        """Check if changed text regions relate to task context."""
        if not self.task_context:
            return False
        task_lower = self.task_context.lower()
        current_texts = {t.text.lower() for t in current.text_regions}
        previous_texts = {t.text.lower() for t in previous.text_regions}
        changed = current_texts - previous_texts
        return any(task_lower in text for text in changed)


class ContinuousObservationService:
    """
    Continuous desktop observation service running in a background thread.

    Reuses existing vision components via composition:
    - PerceptionLayer for frame capture (UIA, screen capture, OCR)
    - ChangeDetector for change detection with configurable strategies
    - ScreenContentDefense for prompt-injection safety
    - ImportanceFilter for heuristic relevance scoring

    Event-driven: emits observation_frame, observation_error, observation_status_changed
    via a single callback matching the Stitch bridge pattern.
    """

    def __init__(
        self,
        config: Optional[ObservationConfig] = None,
        event_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        perception_layer: Optional[PerceptionLayer] = None,
        change_detector: Optional[ChangeDetector] = None,
        screen_content_defense: Optional[ScreenContentDefense] = None,
        importance_filter: Optional[ImportanceFilter] = None,
        vision_provider: Optional[VisionProvider] = None,
        vision_config: Optional[VisionConfig] = None,
    ):
        """
        Initialize the continuous observation service.

        Args:
            config: ObservationConfig (uses defaults if None)
            event_callback: Callback(signal, args) for SSE events
            perception_layer: Injected PerceptionLayer (created if None)
            change_detector: Injected ChangeDetector (created if None)
            screen_content_defense: Injected defense (created if None)
            importance_filter: Injected filter (created if None)
            vision_provider: Injected VisionProvider (created if None, only if vision enabled)
            vision_config: VisionConfig (used to wire vision pipeline if provider given)
        """
        self.config = config or ObservationConfig()
        self.event_callback = event_callback

        # Component dependencies (injected or created)
        self._perception_layer = perception_layer or create_perception_layer()
        self._change_detector = change_detector or ChangeDetector(
            ChangeDetectionConfig(
                strategy=ChangeStrategy[self.config.change_strategy],
            )
        )
        self._screen_content_defense = screen_content_defense or create_screen_content_defense()
        self._importance_filter = importance_filter or ImportanceFilter()

        # Vision components (if vision is enabled and provider given)
        self._vision_provider = vision_provider
        self._vision_config = vision_config
        self._vision_privacy = VisionImagePrivacy(vision_config) if vision_config and vision_config.enabled else None
        self._vision_policy = VisionPolicy(vision_config) if vision_config and vision_config.enabled else None
        self._vision_executor = VisionJobExecutor(vision_provider, timeout_seconds=vision_config.timeout_seconds) if vision_provider and vision_config and vision_config.enabled else None

        # Internal state
        self._status = ObservationStatus.STOPPED
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._frame_queue: queue.Queue = queue.Queue(maxsize=self.config.max_queue_size)
        self._recent_events: List[PerceptionEvent] = []
        self._latest_state: Optional[ScreenState] = None
        self._previous_state: Optional[ScreenState] = None
        self._frame_counter = 0
        self._error_count = 0
        self._last_error: Optional[str] = None
        self._lock = threading.Lock()

        # Task context for task-aware perception
        self._task_context: Optional[str] = None

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    @property
    def status(self) -> ObservationStatus:
        """Current service status."""
        with self._lock:
            return self._status

    @property
    def is_running(self) -> bool:
        """True if service is actively observing."""
        with self._lock:
            return self._status == ObservationStatus.RUNNING

    @property
    def is_paused(self) -> bool:
        """True if service is paused."""
        with self._lock:
            return self._status == ObservationStatus.PAUSED

    def get_latest_state(self) -> Optional[ScreenState]:
        """Get the most recent safe screen state."""
        with self._lock:
            return self._latest_state

    def get_recent_events(self, limit: Optional[int] = None) -> List[PerceptionEvent]:
        """Get recent perception events (most recent first)."""
        with self._lock:
            events = list(reversed(self._recent_events))
            if limit:
                return events[:limit]
            return events

    def get_status(self) -> Dict[str, Any]:
        """Get detailed service status for diagnostics."""
        with self._lock:
            return {
                "status": self._status.value,
                "is_running": self.is_running,
                "is_paused": self.is_paused,
                "frame_count": self._frame_counter,
                "error_count": self._error_count,
                "last_error": self._last_error,
                "queue_size": self._frame_queue.qsize(),
                "recent_events_count": len(self._recent_events),
                "has_latest_state": self._latest_state is not None,
                "task_context": self._task_context,
                "config": {
                    "interval_seconds": self.config.interval_seconds,
                    "change_strategy": self.config.change_strategy,
                    "max_queue_size": self.config.max_queue_size,
                    "max_recent_events": self.config.max_recent_events,
                    "task_aware_enabled": self.config.task_aware_enabled,
                    "screen_capture_enabled": self.config.screen_capture_enabled,
                    "uia_enabled": self.config.uia_enabled,
                    "ocr_enabled": self.config.ocr_enabled,
                },
            }

    def set_task_context(self, context: Optional[str]) -> None:
        """Set task context for task-aware importance scoring."""
        self._task_context = context
        if self.config.task_aware_enabled and self._importance_filter:
            self._importance_filter.set_task_context(context)
        logger.debug(f"Observation task context set: {context}")

    def start(self) -> bool:
        """
        Start the observation service.

        Returns:
            True if started successfully, False if already running or failed.
        """
        with self._lock:
            if self._status in (ObservationStatus.RUNNING, ObservationStatus.STARTING):
                logger.warning("Observation service already running")
                return False

            self._status = ObservationStatus.STARTING
            self._stop_event.clear()
            self._pause_event.clear()
            self._error_count = 0
            self._last_error = None

        try:
            # Start background worker
            self._worker_thread = threading.Thread(
                target=self._worker_loop,
                name="ContinuousObservationWorker",
                daemon=True,
            )
            self._worker_thread.start()

            # Wait briefly for worker to signal RUNNING or ERROR
            time.sleep(0.1)

            with self._lock:
                if self._status == ObservationStatus.ERROR:
                    logger.error(f"Observation service failed to start: {self._last_error}")
                    return False
                self._status = ObservationStatus.RUNNING

            self._emit("observation_status_changed", {"status": self._status.value})
            logger.info("Continuous observation service started")
            return True

        except Exception as e:
            with self._lock:
                self._status = ObservationStatus.ERROR
                self._last_error = str(e)
            logger.exception("Failed to start observation service")
            self._emit("observation_error", {"error": str(e), "phase": "start"})
            return False

    def stop(self) -> bool:
        """
        Stop the observation service.

        Returns:
            True if stopped, False if not running.
        """
        with self._lock:
            if self._status == ObservationStatus.STOPPED:
                return False

            self._status = ObservationStatus.STOPPED
            self._stop_event.set()
            self._pause_event.set()  # Unblock if paused

        # Wait for worker to finish
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=5.0)

        with self._lock:
            self._worker_thread = None

        self._emit("observation_status_changed", {"status": ObservationStatus.STOPPED.value})
        logger.info("Continuous observation service stopped")
        return True

    def pause(self) -> bool:
        """
        Pause observation (worker stays alive but stops capturing).

        Returns:
            True if paused, False if not running.
        """
        with self._lock:
            if self._status != ObservationStatus.RUNNING:
                return False
            self._status = ObservationStatus.PAUSED
            self._pause_event.set()

        self._emit("observation_status_changed", {"status": ObservationStatus.PAUSED.value})
        logger.info("Continuous observation service paused")
        return True

    def resume(self) -> bool:
        """
        Resume observation from paused state.

        Returns:
            True if resumed, False if not paused.
        """
        with self._lock:
            if self._status != ObservationStatus.PAUSED:
                return False
            self._status = ObservationStatus.RUNNING
            self._pause_event.clear()

        self._emit("observation_status_changed", {"status": ObservationStatus.RUNNING.value})
        logger.info("Continuous observation service resumed")
        return True

    # -------------------------------------------------------------------------
    # Worker loop
    # -------------------------------------------------------------------------

    def _worker_loop(self) -> None:
        """Background worker loop - captures and processes frames."""
        logger.debug("Observation worker started")

        try:
            # Initialize perception layer (no start/stop needed; get_current_state is direct)
            # The PerceptionLayer captures on demand via get_current_state()

            with self._lock:
                self._status = ObservationStatus.RUNNING

            while not self._stop_event.is_set():
                # Handle pause
                if self._pause_event.is_set():
                    time.sleep(0.1)
                    continue

                loop_start = time.time()

                try:
                    self._capture_and_process_frame()
                except Exception as e:
                    self._handle_worker_error(e, "frame_capture")

                # Maintain interval
                elapsed = time.time() - loop_start
                sleep_time = max(0, self.config.interval_seconds - elapsed)
                if sleep_time > 0:
                    time.sleep(sleep_time)

        except Exception as e:
            self._handle_worker_error(e, "worker_loop")
        finally:
            # No stop needed for PerceptionLayer (captures on demand)
            logger.debug("Observation worker stopped")

    def _capture_and_process_frame(self) -> None:
        """Capture a single frame and process through the pipeline."""
        # 1. Capture frame via PerceptionLayer (get_current_state captures on demand)
        raw_state = self._perception_layer.get_current_state()

        # 2. Apply screen content defense (safety filter)
        safe_state = self._screen_content_defense.filter_perception_state(raw_state)

        # 3. Vision analysis (if enabled and conditions met)
        visual_analysis = None
        if (self._vision_provider and self._vision_config and self._vision_config.enabled and
            self._vision_privacy and self._vision_policy and self._vision_executor):

            # Check if vision should run based on policy
            policy_decision = self._vision_policy.should_run_vision(
                safe_state, self._previous_state, self._task_context
            )

            if policy_decision.should_run:
                # Get screenshot for vision processing (from screen capture provider)
                screenshot_bytes = None
                if hasattr(self._perception_layer, 'screen_provider') and self._perception_layer.screen_provider:
                    screenshot_bytes = self._perception_layer.screen_provider.take_screenshot()

                # Apply privacy processing to screenshot
                if screenshot_bytes:
                    private_bytes = self._vision_privacy.process(
                        screenshot_bytes, safe_state
                    )

                    # Submit vision job (non-blocking — observer thread NEVER waits on slow VLM)
                    future = self._vision_executor.submit(
                        image_bytes=private_bytes,
                        screen_state=safe_state,
                        prompt="Describe what you see on the screen. Focus on UI elements, text content, and overall layout.",
                        cancellation_token=None
                    )
                    # Non-blocking: result delivered when complete; observer loop continues
                    # (future.result with timeout previously violated async contract — removed)

        # Build understanding for this frame (with optional visual analysis)
        understanding = VisionUnderstanding(safe_state, visual_analysis=visual_analysis)

        # 4. Change detection
        change_detected = False
        change_score = 0.0
        change_details = {}

        if self._previous_state is not None:
            change_detected = self._change_detector.has_changed(
                self._previous_state, safe_state
            )
            # Get detailed change analysis
            change_details = safe_state._analyze_changes(self._previous_state)
            change_score = change_details.get("score", 0.0)

        # 5. Importance filtering
        importance_score, importance_factors = self._importance_filter.score(
            safe_state, self._previous_state, change_details
        )

        # 6. Create perception event
        self._frame_counter += 1
        event = PerceptionEvent(
            frame_id=f"frame_{self._frame_counter:06d}_{uuid.uuid4().hex[:8]}",
            timestamp=time.time(),
            state=safe_state,
            understanding=understanding,
            change_detected=change_detected,
            change_score=change_score,
            change_details=change_details,
            importance_score=importance_score,
            importance_factors=importance_factors,
        )

        # 7. Queue event (backpressure: drop oldest if full)
        try:
            self._frame_queue.put_nowait(event)
        except queue.Full:
            # Drop oldest to make room (backpressure handling)
            try:
                self._frame_queue.get_nowait()
                self._frame_queue.put_nowait(event)
            except queue.Empty:
                pass  # Shouldn't happen, but defensive

        # 8. Update internal state
        with self._lock:
            self._latest_state = safe_state
            self._previous_state = safe_state
            self._recent_events.append(event)
            # Trim history
            if len(self._recent_events) > self.config.max_recent_events:
                self._recent_events = self._recent_events[-self.config.max_recent_events:]

        # 9. Emit event for SSE/GUI
        self._emit("observation_frame", event.to_dict())

    def _handle_worker_error(self, error: Exception, phase: str) -> None:
        """Handle errors in worker loop with graceful degradation."""
        self._error_count += 1
        self._last_error = f"{phase}: {error}"
        logger.warning(f"Observation worker error ({phase}): {error}")

        if not self.config.graceful_degradation:
            with self._lock:
                self._status = ObservationStatus.ERROR
            self._emit("observation_error", {"error": str(error), "phase": phase, "fatal": True})
            self._stop_event.set()
        else:
            # Continue with degraded operation
            self._emit("observation_error", {"error": str(error), "phase": phase, "fatal": False})

    def _emit(self, signal: str, args: Dict[str, Any]) -> None:
        """Emit event via callback if registered."""
        if self.event_callback:
            try:
                self.event_callback(signal, args)
            except Exception as e:
                logger.warning(f"Event callback failed for {signal}: {e}")


def create_continuous_observation_service(
    config: Optional[ObservationConfig] = None,
    event_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None,
    **kwargs,
) -> ContinuousObservationService:
    """Factory function for ContinuousObservationService."""
    return ContinuousObservationService(config=config, event_callback=event_callback, **kwargs)