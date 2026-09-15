"""
Screen-change detection for VERONICA.

Provides configurable strategies for detecting significant changes in screen
state, supporting different observation modes:
- idle: only detect major structural changes (window switches, new windows)
- active: detect significant changes in elements and content
- waiting: very sensitive, detect small changes (for verification after actions)
- action: detect changes immediately after an action
- error: detect any deviation from expected state

Uses fingerprint-based comparison with weighted change analysis.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Callable, Any
from enum import Enum

from veronica.logging_setup import get_logger
from veronica.vision.screen_state import ScreenState

logger = get_logger("vision.change_detection")


class ChangeStrategy(str, Enum):
    """Configurable screen-change detection strategies."""
    IDLE = "idle"        # Only detect major structural changes
    ACTIVE = "active"    # Significant element/content changes
    WAITING = "waiting"  # Very sensitive - small changes
    ACTION = "action"    # Changes immediately after action execution
    ERROR = "error"      # Any deviation from expected


class ChangeDetectionConfig:
    """Configuration for change detection behavior."""

    def __init__(
        self,
        strategy: ChangeStrategy = ChangeStrategy.ACTIVE,
        threshold: float = 0.3,
        max_time_ms: float = 5000.0,
        check_interval_ms: float = 500.0,
    ):
        self.strategy = strategy
        self.threshold = threshold
        self.max_time_ms = max_time_ms
        self.check_interval_ms = check_interval_ms


class ChangeDetector:
    """Detects significant changes between screen states."""

    def __init__(self, config: Optional[ChangeDetectionConfig] = None):
        self.config = config or ChangeDetectionConfig()
        self.previous_state: Optional[ScreenState] = None
        self.change_history: List[Dict[str, Any]] = []

    def update_reference(self, state: ScreenState) -> None:
        """Update the reference state for future comparisons."""
        self.previous_state = state

    def has_changed(self, current: ScreenState, threshold: Optional[float] = None) -> bool:
        """Check if screen has changed significantly from previous reference.

        Args:
            current: Current screen state to compare
            threshold: Optional override for change threshold

        Returns:
            True if significant change detected; False if no reference state exists
            (UNKNOWN - cannot verify change without a reference)
        """
        if self.previous_state is None:
            logger.debug("No previous state - cannot verify change (UNKNOWN)")
            return False  # UNKNOWN != SUCCESS - never treat missing reference as proof of change

        # Use strategy-based threshold if not overridden
        if threshold is not None:
            effective_threshold = threshold
        else:
            effective_threshold = self._get_strategy_threshold()

        has_changed = current.has_changed_significantly(
            self.previous_state, effective_threshold
        )

        if has_changed:
            changes = current._analyze_changes(self.previous_state)
            self.change_history.append({
                "timestamp": time.time(),
                "score": changes.get("score", 0.0),
                "details": changes.get("details", []),
                "threshold": effective_threshold,
            })

        return has_changed

    def _get_strategy_threshold(self) -> float:
        """Get threshold based on current strategy."""
        thresholds = {
            ChangeStrategy.IDLE: 0.5,
            ChangeStrategy.ACTIVE: 0.3,
            ChangeStrategy.WAITING: 0.1,
            ChangeStrategy.ACTION: 0.2,
            ChangeStrategy.ERROR: 0.05,
        }
        return thresholds.get(self.config.strategy, self.config.threshold)

    def wait_for_change(
        self,
        observation_callback: Callable[[], ScreenState],
        timeout_ms: Optional[float] = None,
    ) -> Optional[ScreenState]:
        """Wait for a significant change from current reference.

        Args:
            observation_callback: Function that returns current ScreenState
            timeout_ms: Maximum time to wait (defaults to config.max_time_ms)

        Returns:
            ScreenState when change detected, or None if timeout
        """
        timeout = timeout_ms or self.config.max_time_ms
        interval = self.config.check_interval_ms / 1000.0  # Convert to seconds
        start_time = time.time()

        while True:
            current = observation_callback()
            if self.has_changed(current):
                return current

            # Check timeout
            elapsed = (time.time() - start_time) * 1000
            if elapsed >= timeout:
                return None

            time.sleep(interval)


def create_change_detector(
    strategy: ChangeStrategy = ChangeStrategy.ACTIVE
) -> ChangeDetector:
    return ChangeDetector(ChangeDetectionConfig(strategy=strategy))
