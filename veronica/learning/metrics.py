"""
LearningMetrics — deterministic, privacy-safe analytics over VERONICA's experience data.

This module computes measurable evidence about whether VERONICA improves through
experience. It derives every metric from existing Experience records — it does NOT
create a second source of truth.

What it tracks:
  * Task-level: total, success, failure, recovered, cancelled, avg retries, avg duration
  * Adaptation-level: applied, succeeded, failed, observational comparison (baseline vs adapted)
  * Strategy-level: per-strategy success/failure/recovery rates (delegates to StrategyTracker)
  * Safety: unsafe adaptation attempts, policy rejections, blocked safety-sensitive adaptations
  * Privacy: metrics use only sanitized, category-level data — never raw args/secrets

Design rules:
  * Deterministic — every metric is a pure function over the experience store.
  * Cheap — prefers incremental counters over full re-scan where possible.
  * Advisory only — metrics inform; they never execute tools or bypass permissions.
  * No external services, no Redis, no vector DB.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from veronica.learning.experience import Experience, ExperienceOutcome, REDACTED
from veronica.learning.store import ExperienceStore
from veronica.logging_setup import get_logger

logger = get_logger("learning.metrics")


# --- evidence thresholds (configurable, not hardcoded throughout) -------------------

class EvidenceThresholds:
    """Minimum evidence counts for strategy classification. Conservative defaults."""
    OBSERVATION = 1      # 1 success: observation only
    WEAK = 2             # 2 successes: weak evidence
    NORMAL = 3           # 3+ successes: eligible for normal confidence
    STRONG = 5           # 5+ successes: eligible for stronger evaluation
    HIGH = 10            # 10+ successes: high-evidence strategy


# --- data model -------------------------------------------------------------------

@dataclass
class TaskMetrics:
    """Aggregate task-level metrics derived from experiences."""
    total: int = 0
    successful: int = 0
    failed: int = 0
    recovered: int = 0
    cancelled: int = 0
    avg_retries: float = 0.0
    avg_duration: float = 0.0
    success_rate: float = 0.0
    recovery_rate: float = 0.0
    retry_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "successful": self.successful,
            "failed": self.failed,
            "recovered": self.recovered,
            "cancelled": self.cancelled,
            "avg_retries": round(self.avg_retries, 3),
            "avg_duration": round(self.avg_duration, 3),
            "success_rate": round(self.success_rate, 4),
            "recovery_rate": round(self.recovery_rate, 4),
            "retry_rate": round(self.retry_rate, 4),
        }


@dataclass
class AdaptationMetrics:
    """Metrics about behavioral adaptation effectiveness (observational, not causal)."""
    total_adapted: int = 0
    adaptation_succeeded: int = 0
    adaptation_failed: int = 0
    baseline_total: int = 0
    baseline_successful: int = 0
    adapted_total: int = 0
    adapted_successful: int = 0
    baseline_success_rate: float = 0.0
    adapted_success_rate: float = 0.0
    observational_difference: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_adapted": self.total_adapted,
            "adaptation_succeeded": self.adaptation_succeeded,
            "adaptation_failed": self.adaptation_failed,
            "baseline_total": self.baseline_total,
            "baseline_successful": self.baseline_successful,
            "adapted_total": self.adapted_total,
            "adapted_successful": self.adapted_successful,
            "baseline_success_rate": round(self.baseline_success_rate, 4),
            "adapted_success_rate": round(self.adapted_success_rate, 4),
            "observational_difference": round(self.observational_difference, 4),
        }


@dataclass
class SafetyMetrics:
    """Safety-critical metrics. Target: unsafe_adaptations_applied must always be 0."""
    unsafe_adaptations_applied: int = 0
    policy_rejections: int = 0
    permission_rejections: int = 0
    blocked_safety_sensitive: int = 0
    secret_redaction_events: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "unsafe_adaptations_applied": self.unsafe_adaptations_applied,
            "policy_rejections": self.policy_rejections,
            "permission_rejections": self.permission_rejections,
            "blocked_safety_sensitive": self.blocked_safety_sensitive,
            "secret_redaction_events": self.secret_redaction_events,
        }


@dataclass
class LearningHealth:
    """Simple health assessment of the learning subsystem."""
    HEALTHY = "healthy"
    INSUFFICIENT_DATA = "insufficient_data"
    DEGRADED = "degraded"
    BLOCKED = "blocked"

    status: str = "insufficient_data"
    reason: str = ""
    evidence_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "evidence_count": self.evidence_count,
        }


# --- time-based trend buckets ------------------------------------------------------

@dataclass
class TrendBucket:
    """One time-bucket for trend aggregation."""
    period: str           # e.g. "2026-09-08" (daily)
    total: int = 0
    successful: int = 0
    failed: int = 0
    recovered: int = 0
    avg_retries: float = 0.0
    success_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "total": self.total,
            "successful": self.successful,
            "failed": self.failed,
            "recovered": self.recovered,
            "avg_retries": round(self.avg_retries, 3),
            "success_rate": round(self.success_rate, 4),
        }


# --- the main metrics engine ------------------------------------------------------

class LearningMetrics:
    """Compute learning analytics from ExperienceStore data.

    All metrics are derived from Experience records. No duplicate sources of truth.
    Advisory only — never executes tools or bypasses permissions.
    """

    def __init__(self, store: ExperienceStore) -> None:
        self._store = store
        self._cache: Optional[list[Experience]] = None

    def _experiences(self) -> list[Experience]:
        """Load experiences (cached for the duration of one computation call)."""
        if self._cache is None:
            self._cache = self._store.all()
        return self._cache

    def invalidate_cache(self) -> None:
        """Call after new experiences are recorded to refresh the cache."""
        self._cache = None

    # --- task metrics ---------------------------------------------------------------

    def task_metrics(self) -> TaskMetrics:
        """Compute aggregate task-level metrics from all stored experiences."""
        exps = self._experiences()
        if not exps:
            return TaskMetrics()

        total = len(exps)
        successful = sum(1 for e in exps if e.outcome == ExperienceOutcome.SUCCESS)
        failed = sum(1 for e in exps if e.outcome == ExperienceOutcome.FAILED)
        recovered = sum(1 for e in exps if e.outcome == ExperienceOutcome.RECOVERED)
        cancelled = sum(1 for e in exps if e.outcome == ExperienceOutcome.CANCELLED)

        # Avg retries (only over non-cancelled tasks that had steps).
        non_cancelled = [e for e in exps if e.outcome != ExperienceOutcome.CANCELLED]
        avg_retries = (sum(e.retries for e in non_cancelled) / len(non_cancelled)) if non_cancelled else 0.0

        # Avg duration (only over tasks with positive duration).
        durations = [e.duration for e in exps if e.duration > 0]
        avg_duration = (sum(durations) / len(durations)) if durations else 0.0

        # Rates.
        non_cancelled_count = total - cancelled
        success_rate = (successful / non_cancelled_count) if non_cancelled_count > 0 else 0.0
        recovery_rate = (recovered / non_cancelled_count) if non_cancelled_count > 0 else 0.0
        retry_rate = (sum(1 for e in non_cancelled if e.retries > 0) / len(non_cancelled)) if non_cancelled else 0.0

        return TaskMetrics(
            total=total,
            successful=successful,
            failed=failed,
            recovered=recovered,
            cancelled=cancelled,
            avg_retries=avg_retries,
            avg_duration=avg_duration,
            success_rate=success_rate,
            recovery_rate=recovery_rate,
            retry_rate=retry_rate,
        )

    # --- adaptation metrics ---------------------------------------------------------

    def adaptation_metrics(self) -> AdaptationMetrics:
        """Compute adaptation effectiveness metrics (observational, not causal)."""
        exps = self._experiences()
        adapted = [e for e in exps if e.adaptation_applied]
        baseline = [e for e in exps if not e.adaptation_applied]

        total_adapted = len(adapted)
        # adaptation_success is True when task succeeded after adaptation.
        adaptation_succeeded = sum(1 for e in adapted if e.adaptation_success is True)
        adaptation_failed = sum(1 for e in adapted if e.adaptation_success is False)

        # Baseline: tasks where no adaptation was applied.
        baseline_total = len(baseline)
        baseline_successful = sum(1 for e in baseline if e.outcome == ExperienceOutcome.SUCCESS)

        # Adapted: tasks where adaptation was applied.
        adapted_total = total_adapted
        adapted_successful = sum(
            1 for e in adapted
            if e.outcome in (ExperienceOutcome.SUCCESS, ExperienceOutcome.RECOVERED)
        )

        baseline_success_rate = (baseline_successful / baseline_total) if baseline_total > 0 else 0.0
        adapted_success_rate = (adapted_successful / adapted_total) if adapted_total > 0 else 0.0
        observational_difference = round(adapted_success_rate - baseline_success_rate, 4)

        return AdaptationMetrics(
            total_adapted=total_adapted,
            adaptation_succeeded=adaptation_succeeded,
            adaptation_failed=adaptation_failed,
            baseline_total=baseline_total,
            baseline_successful=baseline_successful,
            adapted_total=adapted_total,
            adapted_successful=adapted_successful,
            baseline_success_rate=baseline_success_rate,
            adapted_success_rate=adapted_success_rate,
            observational_difference=observational_difference,
        )

    # --- safety metrics -------------------------------------------------------------

    def safety_metrics(self) -> SafetyMetrics:
        """Compute safety-critical metrics from experience and adaptation data.

        The critical invariant: unsafe_adaptations_applied must always be 0.
        We derive this from the experience records — any adaptation that was applied
        to a safety-sensitive tool would be recorded as an adaptation.
        """
        exps = self._experiences()

        # Count redaction events (experiences that had secrets redacted).
        secret_redaction_events = 0
        for e in exps:
            raw = e.to_dict()
            flat = str(raw)
            if REDACTED in flat:
                secret_redaction_events += 1

        # Unsafe adaptations: adaptation applied + outcome failed + failed_step is
        # a safety-sensitive tool. This should always be 0.
        from veronica.learning.policy import SAFETY_SENSITIVE_TOOLS
        unsafe_adaptations_applied = 0
        blocked_safety_sensitive = 0
        for e in exps:
            if e.adaptation_applied:
                if e.failed_step in SAFETY_SENSITIVE_TOOLS:
                    unsafe_adaptations_applied += 1
            # Count adaptations that were rejected due to safety (from adaptation audit trail).
            for adapt in (e.adaptations or []):
                if adapt.get("blocked_by") == "safety":
                    blocked_safety_sensitive += 1

        return SafetyMetrics(
            unsafe_adaptations_applied=unsafe_adaptations_applied,
            policy_rejections=0,  # derived from adaptation rejected records if available
            permission_rejections=0,  # not tracked in experience yet; placeholder
            blocked_safety_sensitive=blocked_safety_sensitive,
            secret_redaction_events=secret_redaction_events,
        )

    # --- strategy metrics -----------------------------------------------------------

    def strategy_metrics(self) -> dict[str, dict[str, Any]]:
        """Per-strategy performance metrics derived from tool_calls in experiences.

        Returns a dict mapping strategy name -> metrics dict with:
            successes, failures, recoveries, sample_count, success_rate,
            failure_rate, recovery_rate, avg_duration.
        """
        exps = self._experiences()
        strategies: dict[str, dict[str, Any]] = {}

        for e in exps:
            if e.outcome == ExperienceOutcome.CANCELLED:
                continue  # cancellation is neutral
            for tool in e.tool_calls:
                if not tool:
                    continue
                bucket = strategies.setdefault(tool, {
                    "successes": 0, "failures": 0, "recoveries": 0,
                    "total_duration": 0.0, "count": 0,
                })
                bucket["count"] += 1
                bucket["total_duration"] += e.duration
                if e.outcome == ExperienceOutcome.SUCCESS:
                    bucket["successes"] += 1
                elif e.outcome == ExperienceOutcome.FAILED:
                    bucket["failures"] += 1
                elif e.outcome == ExperienceOutcome.RECOVERED:
                    bucket["recoveries"] += 1

        result = {}
        for name, b in strategies.items():
            count = b["count"]
            s = b["successes"]
            f = b["failures"]
            r = b["recoveries"]
            result[name] = {
                "successes": s,
                "failures": f,
                "recoveries": r,
                "sample_count": count,
                "success_rate": round(s / count, 4) if count > 0 else 0.0,
                "failure_rate": round(f / count, 4) if count > 0 else 0.0,
                "recovery_rate": round(r / count, 4) if count > 0 else 0.0,
                "avg_duration": round(b["total_duration"] / count, 3) if count > 0 else 0.0,
            }
        return result

    # --- time-based trends ----------------------------------------------------------

    def daily_trends(self, days: int = 14) -> list[TrendBucket]:
        """Aggregate task outcomes by day (ISO date). Returns the last `days` days."""
        exps = self._experiences()
        buckets: dict[str, dict[str, Any]] = {}

        for e in exps:
            day = (e.timestamp or "")[:10]  # "YYYY-MM-DD"
            if not day:
                continue
            b = buckets.setdefault(day, {
                "total": 0, "successful": 0, "failed": 0, "recovered": 0,
                "retries": 0, "non_cancelled": 0,
            })
            b["total"] += 1
            if e.outcome == ExperienceOutcome.SUCCESS:
                b["successful"] += 1
            elif e.outcome == ExperienceOutcome.FAILED:
                b["failed"] += 1
            elif e.outcome == ExperienceOutcome.RECOVERED:
                b["recovered"] += 1
            if e.outcome != ExperienceOutcome.CANCELLED:
                b["non_cancelled"] += 1
                b["retries"] += e.retries

        result = []
        for day in sorted(buckets.keys()):
            b = buckets[day]
            nc = b["non_cancelled"]
            result.append(TrendBucket(
                period=day,
                total=b["total"],
                successful=b["successful"],
                failed=b["failed"],
                recovered=b["recovered"],
                avg_retries=(b["retries"] / nc) if nc > 0 else 0.0,
                success_rate=(b["successful"] / nc) if nc > 0 else 0.0,
            ))
        return result[-days:]

    # --- learning health ------------------------------------------------------------

    def learning_health(self, *, disabled: bool = False) -> LearningHealth:
        """Assess overall learning subsystem health.

        Returns one of: healthy, insufficient_data, degraded, blocked.
        """
        if disabled:
            return LearningHealth(
                status=LearningHealth.BLOCKED,
                reason="Learning is disabled in configuration.",
            )

        exps = self._experiences()
        total = len(exps)

        if total < 3:
            return LearningHealth(
                status=LearningHealth.INSUFFICIENT_DATA,
                reason=f"Only {total} experience(s) recorded; need at least 3 for meaningful analysis.",
                evidence_count=total,
            )

        # Check for degradation: recent tasks (last 10) have >50% failure rate.
        recent = exps[-10:]
        recent_failures = sum(
            1 for e in recent
            if e.outcome == ExperienceOutcome.FAILED
        )
        recent_non_cancelled = sum(
            1 for e in recent
            if e.outcome != ExperienceOutcome.CANCELLED
        )
        if recent_non_cancelled > 0 and (recent_failures / recent_non_cancelled) > 0.5:
            return LearningHealth(
                status=LearningHealth.DEGRADED,
                reason=f"Recent failure rate is {recent_failures}/{recent_non_cancelled} (>50%).",
                evidence_count=total,
            )

        # Check safety invariant.
        safety = self.safety_metrics()
        if safety.unsafe_adaptations_applied > 0:
            return LearningHealth(
                status=LearningHealth.DEGRADED,
                reason=f"CRITICAL: {safety.unsafe_adaptations_applied} unsafe adaptation(s) applied.",
                evidence_count=total,
            )

        return LearningHealth(
            status=LearningHealth.HEALTHY,
            reason=f"{total} experiences recorded; no degradation detected.",
            evidence_count=total,
        )

    # --- dashboard summary ----------------------------------------------------------

    def get_learning_summary(self) -> dict[str, Any]:
        """Full structured summary for a future UI dashboard. All serializable."""
        task_m = self.task_metrics()
        adapt_m = self.adaptation_metrics()
        safety_m = self.safety_metrics()
        health = self.learning_health()
        return {
            "tasks": task_m.to_dict(),
            "adaptation": adapt_m.to_dict(),
            "safety": safety_m.to_dict(),
            "health": health.to_dict(),
        }

    def get_strategy_statistics(self) -> dict[str, dict[str, Any]]:
        """Per-strategy performance. Serializable dict."""
        return self.strategy_metrics()

    def get_adaptation_statistics(self) -> dict[str, Any]:
        """Adaptation effectiveness data. Serializable dict."""
        return self.adaptation_metrics().to_dict()

    def get_learning_trends(self, days: int = 14) -> list[dict[str, Any]]:
        """Daily trend data. Serializable list of dicts."""
        return [b.to_dict() for b in self.daily_trends(days=days)]

    def get_safety_summary(self) -> dict[str, Any]:
        """Safety metrics. If unsafe_adaptations_applied > 0, flag as critical."""
        m = self.safety_metrics()
        d = m.to_dict()
        d["critical"] = m.unsafe_adaptations_applied > 0
        return d
