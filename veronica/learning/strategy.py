"""
Strategy evolution — deterministic, evidence-based behavioral approach tracking.

A Strategy represents a reusable behavioral approach (e.g. "unknown_file_lookup").
It accumulates evidence from Experience records, tracks its state (OBSERVED → PROMISING
→ VALIDATED → DEGRADED → PROMISING), and can be compared against competing strategies.

Design rules:
  * Evidence accumulation: successes + recoveries = positive evidence; failures = negative.
  * No fine-tuning, no RL, no autonomous code modification.
  * Strategy promotion/demotion is deterministic based on evidence counts and trends.
  * All transitions are reversible via new evidence.
  * Safety-sensitive strategies never become unrestricted (enforced by policy layer).
  * Conflicts resolve by priority: safety → explicit instruction → current context →
    validated strategy → weaker historical evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from veronica.learning.experience import Experience, ExperienceOutcome
from veronica.learning.store import ExperienceStore
from veronica.learning.metrics import EvidenceThresholds, LearningMetrics
from veronica.learning.policy import SAFETY_SENSITIVE_TOOLS
from veronica.logging_setup import get_logger

logger = get_logger("learning.strategy")


# --- strategy states -------------------------------------------------------------

class StrategyState:
    OBSERVED = "observed"
    PROMISING = "promising"
    VALIDATED = "validated"
    DEGRADED = "degraded"


# --- strategy model -------------------------------------------------------------

@dataclass
class StrategyEvidence:
    """Evidence accumulated for a single strategy from experience records.

    ``recent_*`` counts reflect only the most recent observations (a staleness-aware
    window), so chronically good strategies can still be demoted for *recent*
    performance while a single recent failure never destroys long-term trust.
    """
    successes: int = 0
    failures: int = 0
    recoveries: int = 0
    total_samples: int = 0
    total_duration: float = 0.0
    last_success_timestamp: str = ""
    last_failure_timestamp: str = ""
    recent_successes: int = 0          # successes in the recency window
    recent_recoveries: int = 0         # recoveries in the recency window
    recent_failures: int = 0           # failures in the recency window

    @property
    def recent_positive(self) -> int:
        return self.recent_successes + self.recent_recoveries

    @property
    def recent_success_rate(self) -> float:
        denom = self.recent_successes + self.recent_recoveries + self.recent_failures
        return (self.recent_successes + self.recent_recoveries) / denom if denom > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "successes": self.successes,
            "failures": self.failures,
            "recoveries": self.recoveries,
            "total_samples": self.total_samples,
            "total_duration": round(self.total_duration, 3),
            "last_success_timestamp": self.last_success_timestamp,
            "last_failure_timestamp": self.last_failure_timestamp,
            "recent_successes": self.recent_successes,
            "recent_recoveries": self.recent_recoveries,
            "recent_failures": self.recent_failures,
            "recent_success_rate": round(self.recent_success_rate, 4),
        }


@dataclass
class StrategyInfo:
    """Complete description of a strategy's state, evidence, and status."""
    name: str
    state: str = StrategyState.OBSERVED
    evidence: StrategyEvidence = field(default_factory=StrategyEvidence)
    confidence: float = 0.5
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self.state,
            "evidence": self.evidence.to_dict(),
            "confidence": round(self.confidence, 4),
            "reason": self.reason,
        }


# --- evolution engine ------------------------------------------------------------

class StrategyEvolution:
    """Determine which strategies exist and how they should evolve.

    Uses existing Experience data and the metrics subsystem. No separate source
    of truth — all evidence comes from the ExperienceStore.
    """

    # Evidence thresholds for promotion (configurable, conservative defaults).
    THRESHOLDS = EvidenceThresholds()

    # Recency window for detecting *recent* degradation. Only the last N non-cancelled
    # observations involving the strategy count toward recent_positive/recent_negative.
    RECENT_WINDOW = 5
    # Minimum recent failures needed to demote despite a strong history.
    RECENT_DEGRADE_FAILURES = 3

    def __init__(
        self,
        store: ExperienceStore,
        metrics: Optional[LearningMetrics] = None,
    ) -> None:
        self._store = store
        self._metrics = metrics
        self._cache: Optional[dict[str, StrategyInfo]] = None

    def invalidate_cache(self) -> None:
        self._cache = None

    # --- evidence accumulation ----------------------------------------------------

    def _accumulate_evidence(self, strategy_name: str, experiences: list[Experience]) -> StrategyEvidence:
        """Compute evidence for a strategy from experience records.

        Cancellation is neutral: it does not count toward successes, failures, or
        samples (a cancelled task decides nothing about a strategy's value).
        """
        ev = StrategyEvidence()
        counted = []
        for e in experiences:
            if strategy_name not in (e.tool_calls or []):
                continue
            if e.outcome == ExperienceOutcome.CANCELLED:
                continue  # neutral — never counted as failure or success
            counted.append(e)
            ev.total_samples += 1
            ev.total_duration += e.duration
            if e.outcome == ExperienceOutcome.SUCCESS:
                ev.successes += 1
                ev.last_success_timestamp = e.timestamp or ""
            elif e.outcome == ExperienceOutcome.FAILED:
                ev.failures += 1
                ev.last_failure_timestamp = e.timestamp or ""
            elif e.outcome == ExperienceOutcome.RECOVERED:
                # Recovery is moderate positive evidence — never equivalent to success.
                ev.recoveries += 1
                ev.last_success_timestamp = e.timestamp or ""
        # Recent window: the last RECENT_WINDOW counted observations by timestamp.
        counted.sort(key=lambda e: e.timestamp or "", reverse=True)
        for e in counted[: self.RECENT_WINDOW]:
            if e.outcome == ExperienceOutcome.SUCCESS:
                ev.recent_successes += 1
            elif e.outcome == ExperienceOutcome.FAILED:
                ev.recent_failures += 1
            elif e.outcome == ExperienceOutcome.RECOVERED:
                ev.recent_recoveries += 1
        return ev

    # --- state transition rules ---------------------------------------------------

    def _compute_state(self, evidence: StrategyEvidence) -> str:
        """Deterministic state transition based on evidence counts.

        Rules (checked in order):
          1. No counted evidence → OBSERVED.
          2. RECENT degradation: at least ``RECENT_DEGRADE_FAILURES`` failures in the
             recency window AND recent failures outnumber recent positives → DEGRADED.
             (A single failure, even on a strong strategy, never triggers this.)
          3. Cumulative degradation: failures >= 3 and exceed positives → DEGRADED.
          4. Strong repeated positives (>= STRONG threshold) → VALIDATED.
          5. Weak/moderate positives (>= WEAK threshold) → PROMISING.
          6. Anything else (1 observation, mixed) → OBSERVED.
        """
        total_positive = evidence.successes + evidence.recoveries
        total_negative = evidence.failures
        total_evidence = total_positive + total_negative

        if total_evidence == 0:
            return StrategyState.OBSERVED

        # Recent degradation wins over a long good history: it reflects *current* behavior.
        if (
            evidence.recent_failures >= self.RECENT_DEGRADE_FAILURES
            and evidence.recent_failures > evidence.recent_positive
        ):
            return StrategyState.DEGRADED

        # Cumulative degradation: failures dominate across all history.
        if total_negative >= 3 and total_negative > total_positive:
            return StrategyState.DEGRADED

        # Validated: at least STRONG successes or recoveries.
        if total_positive >= self.THRESHOLDS.STRONG:
            return StrategyState.VALIDATED

        # Promising: at least WEAK successes or recoveries.
        if total_positive >= self.THRESHOLDS.WEAK:
            return StrategyState.PROMISING

        return StrategyState.OBSERVED

    def _compute_reason(self, info: StrategyInfo, evidence: StrategyEvidence) -> str:
        s = info.state
        total_pos = evidence.successes + evidence.recoveries
        total_neg = evidence.failures
        if s == StrategyState.VALIDATED:
            return f"Validated: {total_pos} successes/recoveries with {evidence.total_samples} samples."
        elif s == StrategyState.PROMISING:
            return f"Promising: {total_pos} positive observations; {total_neg} failures."
        elif s == StrategyState.DEGRADED:
            return f"Degraded: {total_neg} failures exceed positive evidence ({total_pos})."
        else:
            return f"Observed: {evidence.total_samples} samples ({total_pos} positive, {total_neg} failures)."

    # --- public API ----------------------------------------------------------------

    def get_strategies(self) -> dict[str, StrategyInfo]:
        """Return all known strategies with their current state and evidence."""
        if self._cache is not None:
            return dict(self._cache)

        exps = self._store.all()
        # Find all strategy names from tool_calls.
        strategy_names = set()
        for e in exps:
            for t in (e.tool_calls or []):
                if t:
                    strategy_names.add(t)

        result = {}
        for name in sorted(strategy_names):
            # Filter experiences that involve this strategy.
            relevant = [e for e in exps if name in (e.tool_calls or [])]
            evidence = self._accumulate_evidence(name, relevant)

            # Confidence: derived from metrics if available, else basic rate.
            if self._metrics:
                stats = self._metrics.strategy_metrics().get(name, {})
                conf = stats.get("success_rate", 0.5)
            else:
                total_ev = evidence.successes + evidence.recoveries + evidence.failures
                conf = ((evidence.successes + evidence.recoveries) / total_ev) if total_ev > 0 else 0.5

            state = self._compute_state(evidence)
            info = StrategyInfo(
                name=name,
                state=state,
                evidence=evidence,
                confidence=conf,
                reason="",
            )
            info.reason = self._compute_reason(info, evidence)
            result[name] = info

        # If no strategies observed yet, return empty dict (not an error).
        self._cache = result
        return dict(result)

    def get_strategy(self, name: str) -> Optional[StrategyInfo]:
        """Get a single strategy by name."""
        strategies = self.get_strategies()
        return strategies.get(name)

    def evolve_strategy(self, name: str, new_experience: Experience) -> StrategyInfo:
        """Evolve a strategy based on a new experience record.

        Returns the updated StrategyInfo. Deterministic — always produces the
        same result for the same input data.
        """
        self.invalidate_cache()
        strategies = self.get_strategies()
        existing = strategies.get(name)

        # Recompute from full store (includes new experience)
        exps = self._store.all()
        relevant = [e for e in exps if name in (e.tool_calls or [])]
        evidence = self._accumulate_evidence(name, relevant)

        # Confidence: if metrics available, use strategy stats; else compute.
        if self._metrics:
            stats = self._metrics.strategy_metrics().get(name, {})
            conf = stats.get("success_rate", 0.5)
        else:
            total_ev = evidence.successes + evidence.recoveries + evidence.failures
            conf = ((evidence.successes + evidence.recoveries) / total_ev) if total_ev > 0 else 0.5

        state = self._compute_state(evidence)
        info = StrategyInfo(
            name=name,
            state=state,
            evidence=evidence,
            confidence=conf,
            reason="",
        )
        info.reason = self._compute_reason(info, evidence)
        return info

    def compare_strategies(self, names: list[str]) -> list[tuple[str, float]]:
        """Compare strategies by confidence (highest first). Returns list of (name, confidence)."""
        strategies = self.get_strategies()
        ranked = []
        for n in names:
            info = strategies.get(n)
            if info is not None:
                ranked.append((n, info.confidence))
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked

    def best_strategy(self, candidates: list[str]) -> Optional[str]:
        """Select the best strategy from a candidate list based on evidence."""
        ranked = self.compare_strategies(candidates)
        return ranked[0][0] if ranked else None

    def get_all_states(self) -> dict[str, str]:
        """Mapping strategy -> state for quick inspection."""
        return {name: info.state for name, info in self.get_strategies().items()}

    def get_promising_strategies(self) -> list[str]:
        """List strategies that are at least PROMISING."""
        return [
            name for name, info in self.get_strategies().items()
            if info.state in (StrategyState.PROMISING, StrategyState.VALIDATED)
        ]

    def get_validated_strategies(self) -> list[str]:
        return [
            name for name, info in self.get_strategies().items()
            if info.state == StrategyState.VALIDATED
        ]

    def get_degraded_strategies(self) -> list[str]:
        return [
            name for name, info in self.get_strategies().items()
            if info.state == StrategyState.DEGRADED
        ]

    def is_safe_for_unrestricted_use(self, name: str) -> bool:
        """A strategy is safe for unrestricted automatic behavior only if it has
        been validated with sufficient evidence AND is not a safety-sensitive operation.
        This check must be enforced at the wiring layer, not just here.
        """
        # Safety-sensitive strategies can never become unrestricted — regardless
        # of evidence. The policy layer enforces this; this is the deterministic
        # check that the adapter or planner can call.
        if name in SAFETY_SENSITIVE_TOOLS:
            return False
        info = self.get_strategy(name)
        if info is None:
            return False
        # Only VALIDATED with high evidence can be considered for unrestricted use.
        if info.state != StrategyState.VALIDATED:
            return False
        # Additional evidence threshold: at least 5 positive observations.
        positive = info.evidence.successes + info.evidence.recoveries
        return positive >= EvidenceThresholds.STRONG
