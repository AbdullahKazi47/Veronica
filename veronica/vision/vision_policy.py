"""
VisionPolicy: multi-factor gate for deciding when to run vision analysis.

Implements the revised architecture's policy with:
- Change detection gating (structural diff > threshold)
- Importance scoring (heuristic, no LLM)
- Cooldown intervals (minimum time between analyses)
- Task relevance (optional, via prompt/task context)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any

from veronica.vision.screen_state import ScreenState
from veronica.logging_setup import get_logger

logger = get_logger("vision.policy")


@dataclass
class VisionPolicyConfig:
    """Configuration for VisionPolicy gating factors."""
    # Change detection: minimum structural change to trigger analysis
    change_threshold: float = 0.1
    # Importance scoring: minimum importance to trigger analysis
    importance_threshold: float = 0.3
    # Cooldown: minimum seconds between vision analyses
    cooldown_seconds: float = 5.0
    # Task relevance: whether to consider task/prompt relevance
    consider_task_relevance: bool = True
    # Task relevance threshold: minimum task relevance to trigger analysis
    task_relevance_threshold: float = 0.4


@dataclass
class VisionPolicyDecision:
    """Result of evaluating whether to run vision analysis."""
    should_run: bool
    reason: str
    change_score: float = 0.0
    importance_score: float = 0.0
    task_relevance: float = 0.0
    timestamp: float = field(default_factory=time.time)


class VisionPolicy:
    """
    Multi-factor gate for vision analysis execution.

    Factors:
    1. Change detection: compares current ScreenState to previous
    2. Importance scoring: heuristic based on UI density, text, etc.
    3. Cooldown: minimum time between analyses
    4. Task relevance: optional, based on current task/prompt

    The policy is stateless except for tracking last_run_time.
    """

    def __init__(self, config: Optional[VisionPolicyConfig] = None) -> None:
        # Accept either VisionPolicyConfig or VisionConfig (which now has same fields)
        if config is None:
            self.config = VisionPolicyConfig()
        elif isinstance(config, VisionPolicyConfig):
            self.config = config
        else:
            # Assume it's a VisionConfig with the same field names
            self.config = VisionPolicyConfig(
                change_threshold=getattr(config, "change_threshold", 0.1),
                importance_threshold=getattr(config, "importance_threshold", 0.3),
                cooldown_seconds=getattr(config, "cooldown_seconds", 5.0),
                consider_task_relevance=getattr(config, "consider_task_relevance", True),
                task_relevance_threshold=getattr(config, "task_relevance_threshold", 0.4),
            )
        self._last_run_time: float = 0.0
        self._previous_state: Optional[ScreenState] = None

    def should_run_vision(
        self,
        current_state: ScreenState,
        task_context: Optional[str] = None,
    ) -> VisionPolicyDecision:
        """
        Evaluate whether vision analysis should run now.

        Args:
            current_state: Current screen state from perception layer.
            task_context: Optional task description or prompt for relevance.

        Returns:
            VisionPolicyDecision with should_run and reasoning.
        """
        now = time.time()

        # Factor 1: Cooldown check (skip if no previous run)
        if self._last_run_time > 0:
            time_since_last = now - self._last_run_time
            if time_since_last < self.config.cooldown_seconds:
                return VisionPolicyDecision(
                    should_run=False,
                    reason=f"cooldown active ({time_since_last:.1f}s < {self.config.cooldown_seconds}s)",
                    timestamp=now,
                )
        else:
            # First run: no previous state
            return VisionPolicyDecision(
                should_run=True,
                reason="no_previous_state",
                timestamp=now,
            )

        # Factor 2: Change detection (structural diff)
        change_score = self._compute_change_score(current_state)
        if change_score < self.config.change_threshold:
            return VisionPolicyDecision(
                should_run=False,
                reason=f"Insufficient change ({change_score:.3f} < {self.config.change_threshold})",
                change_score=change_score,
                timestamp=now,
            )

        # Factor 3: Importance scoring
        importance_score = self._compute_importance_score(current_state)
        if importance_score < self.config.importance_threshold:
            return VisionPolicyDecision(
                should_run=False,
                reason=f"Insufficient importance ({importance_score:.3f} < {self.config.importance_threshold})",
                change_score=change_score,
                importance_score=importance_score,
                timestamp=now,
            )

        # Factor 4: Task relevance (if enabled)
        task_relevance = 0.5  # default neutral
        if self.config.consider_task_relevance and task_context:
            task_relevance = self._compute_task_relevance(current_state, task_context)
            if task_relevance < self.config.task_relevance_threshold:
                return VisionPolicyDecision(
                    should_run=False,
                    reason=f"Insufficient task relevance ({task_relevance:.3f} < {self.config.task_relevance_threshold})",
                    change_score=change_score,
                    importance_score=importance_score,
                    task_relevance=task_relevance,
                    timestamp=now,
                )

        # All factors passed - update state and allow vision
        self._last_run_time = now
        self._previous_state = current_state
        return VisionPolicyDecision(
            should_run=True,
            reason="All policy factors satisfied",
            change_score=change_score,
            importance_score=importance_score,
            task_relevance=task_relevance,
            timestamp=now,
        )

    def _compute_change_score(self, current_state: ScreenState) -> float:
        """
        Compute structural change score between current and previous state.
        Returns 0.0-1.0 where higher means more change.
        """
        if self._previous_state is None:
            return 1.0  # First frame always triggers

        try:
            # Simple heuristic: compare active window and visible elements
            change = 0.0
            factors = 0

            # Active window change
            prev_win = self._previous_state.active_window
            curr_win = current_state.active_window
            if prev_win and curr_win:
                if prev_win.window_id != curr_win.window_id:
                    change += 0.4  # Window switch is significant
                elif prev_win.title != curr_win.title:
                    change += 0.2
                elif prev_win.process_name != curr_win.process_name:
                    change += 0.2
                factors += 1

            # Visible windows count change
            prev_count = len(self._previous_state.visible_windows)
            curr_count = len(current_state.visible_windows)
            if prev_count != curr_count:
                change += 0.2
            factors += 1

            # Element count change (rough)
            prev_elems = len(self._previous_state.desktop_elements)
            curr_elems = len(current_state.desktop_elements)
            if prev_elems != 0 or curr_elems != 0:
                elem_change = abs(prev_elems - curr_elems) / max(prev_elems, curr_elems, 1)
                change += elem_change * 0.2
            factors += 1

            # Text region change (rough)
            prev_texts = len(self._previous_state.text_regions)
            curr_texts = len(current_state.text_regions)
            if prev_texts != 0 or curr_texts != 0:
                text_change = abs(prev_texts - curr_texts) / max(prev_texts, curr_texts, 1)
                change += text_change * 0.2
            factors += 1

            # Normalize by number of factors considered
            if factors > 0:
                change = change / factors
            return min(1.0, max(0.0, change))
        except Exception as exc:
            logger.debug("Change score computation failed: %s", exc)
            return 0.5  # Unknown -> moderate change to be safe

    def _compute_importance_score(self, state: ScreenState) -> float:
        """
        Compute heuristic importance score based on screen content.
        Returns 0.0-1.0 where higher means more important to analyze.
        """
        try:
            score = 0.0
            # Base importance from having content
            if state.desktop_elements or state.text_regions:
                score += 0.3

            # Active window importance
            if state.active_window:
                score += 0.2
                # Applications that typically need more attention
                important_procs = {
                    "code.exe", "devenv.exe", "notepad.exe", "word.exe", "excel.exe",
                    "chrome.exe", "firefox.exe", "edge.exe", "outlook.exe", "teams.exe",
                    "slack.exe", "discord.exe", "zoom.exe", "powershell.exe", "cmd.exe"
                }
                if state.active_window.process_name.lower() in important_procs:
                    score += 0.2

            # Text density (more text = potentially more important)
            text_count = len(state.text_regions)
            if text_count > 0:
                score += min(0.3, text_count * 0.05)  # Cap at 0.3

            # Interactive elements (buttons, inputs, etc.)
            interactive_types = {"Button", "Edit", "ComboBox", "List", "TreeItem"}
            interactive_count = sum(
                1 for el in state.desktop_elements
                if el.control_type in interactive_types
            )
            if interactive_count > 0:
                score += min(0.2, interactive_count * 0.05)  # Cap at 0.2

            return min(1.0, max(0.0, score))
        except Exception as exc:
            logger.debug("Importance score computation failed: %s", exc)
            return 0.5  # Unknown -> moderate importance

    def _compute_task_relevance(self, state: ScreenState, task_context: str) -> float:
        """
        Compute relevance between screen state and task context.
        Simple keyword matching for V1; can be enhanced later.
        Returns 0.0-1.0 where higher means more relevant.
        """
        try:
            if not task_context:
                return 0.5

            task_lower = task_context.lower()
            score = 0.0
            matches = 0

            # Check active window title for task keywords
            if state.active_window and state.active_window.title:
                title_lower = state.active_window.title.lower()
                # Simple word overlap
                task_words = set(task_lower.split())
                title_words = set(title_lower.split())
                if task_words & title_words:
                    overlap = len(task_words & title_words)
                    score += overlap * 0.2
                    matches += 1

            # Check text regions for task keywords
            for text_region in state.text_regions:
                text_lower = text_region.text.lower()
                text_words = set(text_lower.split())
                if task_words & text_words:
                    overlap = len(task_words & text_words)
                    score += overlap * 0.1
                    matches += 1
                    break  # Only need first match for efficiency

            # Check element names for task keywords
            for element in state.desktop_elements:
                name_lower = element.name.lower()
                name_words = set(name_lower.split())
                if task_words & name_words:
                    overlap = len(task_words & name_words)
                    score += overlap * 0.1
                    matches += 1
                    break

            # Normalize by number of checks (but cap reasonable)
            if matches > 0:
                # Average score per match, but don't let it get too low
                score = max(score, 0.3)
            else:
                score = 0.1  # Low but not zero relevance

            return min(1.0, max(0.0, score))
        except Exception as exc:
            logger.debug("Task relevance computation failed: %s", exc)
            return 0.5  # Unknown -> neutral relevance