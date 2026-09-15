"""
LearningEngine — turns Experiences into structured learning signals.

For v1 this engine does NOT rewrite code, does NOT modify system behavior, and does NOT
feed the planner. Its job is analytic: read one Experience and extract what VERONICA can
*learn from* it, in a deterministic, structured form that a later phase (adaptive
planning) can consume.

Signals it can detect:
    strategy_success  — a tool/strategy ran and the task succeeded
    strategy_failure  — a tool/strategy ran and the task failed (or the step failed)
    recovery          — the first approach failed but a retry/recovery succeeded
    retry             — one or more retries were required
    cancellation      — the task was cancelled (user or system)
    user_correction   — explicit user feedback asking for different behavior

Every signal carries a ``confidence`` value from the ConfidenceTracker (evidence-based,
never a policy authority) and the id of the experience that produced it.

Safety guarantee: this engine only *observes* and *records*. It can never execute a tool,
bypass a permission gate, or change safety behavior.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from veronica.learning.confidence import ConfidenceTracker
from veronica.learning.experience import Experience, ExperienceOutcome
from veronica.logging_setup import get_logger

logger = get_logger("learning.learner")


class SignalType:
    STRATEGY_SUCCESS = "strategy_success"
    STRATEGY_FAILURE = "strategy_failure"
    RECOVERY = "recovery"
    RETRY = "retry"
    CANCELLATION = "cancellation"
    USER_CORRECTION = "user_correction"


@dataclass
class LessonSignal:
    """One structured, evidence-backed learning signal."""

    type: str
    subject: str                              # a tool/strategy/preference name
    evidence_experience_id: str
    confidence: float = 0.5
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "subject": self.subject,
            "evidence_experience_id": self.evidence_experience_id,
            "confidence": self.confidence,
            "context": self.context,
        }


class LearningEngine:
    def __init__(self, confidence: Optional[ConfidenceTracker] = None) -> None:
        self.confidence = confidence or ConfidenceTracker()
        self.signals: list[LessonSignal] = []
        self._max_signals = 2000

    # -- processing -----------------------------------------------------------------
    def process(self, experience: Experience) -> list[LessonSignal]:
        """Analyze one Experience, update confidence, and return the signals it yields.
        Deterministic; never raises (a bad record just yields no signals)."""
        signals: list[LessonSignal] = []
        try:
            outcome = experience.outcome or ExperienceOutcome.SUCCESS
            tool_calls = experience.tool_calls or []

            # Update evidence-based confidence per tool/strategy observed.
            for tool in tool_calls:
                if not tool:
                    continue
                if outcome == ExperienceOutcome.SUCCESS:
                    self.confidence.record(tool, True)
                    signals.append(self._signal(
                        SignalType.STRATEGY_SUCCESS, tool, experience,
                        {"outcome": outcome}))
                elif outcome == ExperienceOutcome.FAILED and tool == experience.failed_step:
                    self.confidence.record(tool, False)
                    signals.append(self._signal(
                        SignalType.STRATEGY_FAILURE, tool, experience,
                        {"outcome": outcome, "reason": experience.failure_reason}))

            # Recovery: initial approach failed but the task ultimately recovered.
            if outcome == ExperienceOutcome.RECOVERED:
                for tool in experience.recovery_actions:
                    if tool:
                        signals.append(self._signal(
                            SignalType.RECOVERY, tool, experience, {"outcome": outcome}))
                # Also credit the overall success path.
                for tool in tool_calls:
                    self.confidence.record(tool, True)

            # Retry signal: any retries were required (regardless of final outcome).
            if experience.retries > 0:
                signals.append(self._signal(
                    SignalType.RETRY, experience.failed_step or (tool_calls[-1] if tool_calls else ""),
                    experience, {"retries": experience.retries}))

            # Cancellation.
            if outcome == ExperienceOutcome.CANCELLED:
                signals.append(self._signal(
                    SignalType.CANCELLATION, "", experience, {"outcome": outcome}))

            # Explicit user correction (if attached to this experience).
            if experience.user_feedback:
                self._handle_user_feedback(experience, signals)

        except Exception:  # noqa: BLE001 — learning must never break execution
            logger.exception("LearningEngine.process failed for experience %s", getattr(experience, "experience_id", "?"))
            return []

        self._store(signals)
        return signals

    def _handle_user_feedback(self, experience: Experience, signals: list[LessonSignal]) -> None:
        fb = experience.user_feedback
        if not isinstance(fb, dict):
            return
        kind = fb.get("kind", "")
        subject = fb.get("subject", "") or ""
        correction_kinds = {"correction", "preference", "dont_do_that", "do_it_this_way"}
        if kind in correction_kinds:
            signals.append(self._signal(
                SignalType.USER_CORRECTION, subject, experience,
                {"feedback_kind": kind, "feedback_text": (fb.get("text") or "")[:120]}))

    def _signal(self, stype: str, subject: str, exp: Experience,
                context: dict[str, Any]) -> LessonSignal:
        conf = self.confidence.confidence(subject) if subject else 0.5
        return LessonSignal(
            type=stype,
            subject=subject,
            evidence_experience_id=exp.experience_id,
            confidence=conf,
            context=context,
        )

    def _store(self, signals: list[LessonSignal]) -> None:
        if not signals:
            return
        for s in signals:
            self.signals.append(s)
            logger.info("learning signal extracted type=%s subject=%r confidence=%s",
                        s.type, s.subject, s.confidence)
        if len(self.signals) > self._max_signals:
            self.signals = self.signals[-self._max_signals:]

    # -- queries --------------------------------------------------------------------
    def recent_signals(self, limit: int = 20) -> list[LessonSignal]:
        return self.signals[-limit:]

    def signals_of_type(self, stype: str) -> list[LessonSignal]:
        return [s for s in self.signals if s.type == stype]

    def best_strategies(self, top: int = 5) -> list[dict[str, Any]]:
        """Rank observed strategies by confidence (highest first). Advisory only —
        nothing here can alter what tools are actually permitted to run."""
        ranked = []
        for subject in self.confidence.subjects():
            ranked.append({
                "subject": subject,
                "confidence": self.confidence.confidence(subject),
                **self.confidence.stats(subject),
            })
        ranked.sort(key=lambda r: r["confidence"], reverse=True)
        return ranked[:top]
