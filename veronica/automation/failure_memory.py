"""
Same-failure detection: prevents infinite retries of identical failures.

This module tracks failure signatures and detects when the same failure is
occurring repeatedly (e.g., the same tool failing with the same error pattern
across retries or replans). When identical failures are detected, the system
stops automatically retrying and either requests human intervention or applies
an alternative recovery strategy.

Key principle: Stored experience is DATA. Never treat retrieved experience as
executable instructions. Never allow learned text to override system safety rules.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from veronica.automation.observation import StepObservation, FailureCategory
from veronica.logging_setup import get_logger

logger = get_logger("automation.failure_memory")


@dataclass
class FailureRecord:
    """A record of a single failure occurrence."""
    signature: str
    category: FailureCategory
    tool_name: str
    error: str
    timestamp: float = field(default_factory=time.time)
    task_id: str = ""
    step_index: int = 0
    recovery_attempted: Optional[str] = None  # What recovery was tried

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signature": self.signature,
            "category": self.category.value,
            "tool_name": self.tool_name,
            "error": self.error[:100],  # Truncate for storage
            "timestamp": self.timestamp,
            "task_id": self.task_id,
            "step_index": self.step_index,
            "recovery_attempted": self.recovery_attempted,
        }


@dataclass
class FailureCluster:
    """A cluster of identical or similar failures sharing the same signature."""
    signature: str
    failures: List[FailureRecord] = field(default_factory=list)
    last_seen: float = 0.0
    recovery_attempts: List[str] = field(default_factory=list)
    blocked: bool = False  # True when max identical failures reached

    def __post_init__(self):
        if self.failures:
            self.last_seen = max(f.timestamp for f in self.failures)

    @property
    def count(self) -> int:
        return len(self.failures)

    def add_failure(self, record: FailureRecord) -> None:
        self.failures.append(record)
        self.last_seen = record.timestamp
        if record.recovery_attempted and record.recovery_attempted not in self.recovery_attempts:
            self.recovery_attempts.append(record.recovery_attempted)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "signature": self.signature,
            "count": self.count,
            "category": self.failures[-1].category.value if self.failures else "unknown",
            "tool_name": self.failures[-1].tool_name if self.failures else "unknown",
            "last_seen": self.last_seen,
            "recovery_attempts": self.recovery_attempts,
            "blocked": self.blocked,
        }


class FailureMemory:
    """Tracks failure signatures and detects same-failure patterns.

    This is the "memory" of failures — not to be confused with the learning
    subsystem's experience store. This memory is short-term and focused on
    preventing infinite identical retries.

    Configuration:
        max_identical_failures: Maximum times the same failure can occur before
                                blocking automatic recovery (default: 3)
        max_recovery_attempts: Maximum different recovery actions to try before
                              requesting human intervention (default: 3)
        failure_window_seconds: Time window for counting failures (default: 3600 = 1 hour)
    """

    def __init__(
        self,
        max_identical_failures: int = 3,
        max_recovery_attempts: int = 3,
        failure_window_seconds: float = 3600.0,
    ):
        self.max_identical_failures = max_identical_failures
        self.max_recovery_attempts = max_recovery_attempts
        self.failure_window_seconds = failure_window_seconds
        self._clusters: Dict[str, FailureCluster] = {}
        self._task_failures: Dict[str, Set[str]] = {}  # task_id -> set of signatures

    def record_failure(
        self,
        observation: StepObservation,
        task_id: str = "",
        recovery_attempted: Optional[str] = None,
    ) -> bool:
        """Record a failure and return True if recovery should proceed.

        Returns True if the failure count is within limits and recovery can continue.
        Returns False if the same failure has occurred too many times and should stop.
        """
        signature = observation.failure_signature
        if not signature:
            logger.warning("No failure signature on observation — skipping failure recording")
            return True

        record = FailureRecord(
            signature=signature,
            category=observation.category,
            tool_name=observation.expected.tool_name,
            error=observation.observed.error or "",
            task_id=task_id,
            step_index=observation.observed.step_index,
            recovery_attempted=recovery_attempted,
        )

        # Add to cluster
        if signature not in self._clusters:
            self._clusters[signature] = FailureCluster(signature=signature)
        cluster = self._clusters[signature]
        cluster.add_failure(record)

        # Track per-task
        if task_id:
            if task_id not in self._task_failures:
                self._task_failures[task_id] = set()
            self._task_failures[task_id].add(signature)

        # Check if we're in the failure window
        now = time.time()
        recent_failures = [
            f for f in cluster.failures
            if now - f.timestamp <= self.failure_window_seconds
        ]

        # Check if blocked
        if len(recent_failures) >= self.max_identical_failures:
            cluster.blocked = True
            logger.warning(
                "Failure cluster %s blocked: %d identical failures in %ds (max: %d)",
                signature, len(recent_failures), self.failure_window_seconds, self.max_identical_failures
            )
            return False

        # Check if recovery attempts exhausted
        # The current attempt was already added to cluster.recovery_attempts, so
        # block only when the count EXCEEDS the maximum (allowing max_recovery_attempts
        # distinct recovery strategies before escalation).
        if len(cluster.recovery_attempts) > self.max_recovery_attempts:
            cluster.blocked = True
            logger.warning(
                "Failure cluster %s blocked: %d recovery attempts (max: %d)",
                signature, len(cluster.recovery_attempts), self.max_recovery_attempts
            )
            return False

        logger.info(
            "Failure recorded: signature=%s tool=%s category=%s count=%d/%d",
            signature, record.tool_name, record.category.value,
            len(recent_failures), self.max_identical_failures
        )
        return True

    def is_blocked(self, signature: str) -> bool:
        """Check if a failure signature is blocked (should not retry)."""
        cluster = self._clusters.get(signature)
        if not cluster:
            return False
        return cluster.blocked

    def should_stop(self, observation: StepObservation) -> bool:
        """Check if recovery should stop for this failure.

        Returns True if:
        - The same failure has occurred too many times
        - The failure is blocked
        - Recovery attempts exhausted
        """
        signature = observation.failure_signature
        if not signature:
            return False
        cluster = self._clusters.get(signature)
        if not cluster:
            return False
        return cluster.blocked

    def get_failure_count(self, signature: str) -> int:
        """Get the number of times a failure signature has occurred in the window."""
        cluster = self._clusters.get(signature)
        if not cluster:
            return 0
        now = time.time()
        return sum(
            1 for f in cluster.failures
            if now - f.timestamp <= self.failure_window_seconds
        )

    def get_recovery_attempts(self, signature: str) -> List[str]:
        """Get the list of recovery attempts for a failure signature."""
        cluster = self._clusters.get(signature)
        if not cluster:
            return []
        return list(cluster.recovery_attempts)

    def get_task_failures(self, task_id: str) -> List[FailureRecord]:
        """Get all failure records for a specific task."""
        signatures = self._task_failures.get(task_id, set())
        records = []
        for sig in signatures:
            cluster = self._clusters.get(sig)
            if cluster:
                for f in cluster.failures:
                    if f.task_id == task_id:
                        records.append(f)
        return sorted(records, key=lambda r: r.timestamp)

    def get_failure_summary(self, task_id: Optional[str] = None) -> Dict[str, Any]:
        """Get a summary of failure patterns."""
        if task_id:
            records = self.get_task_failures(task_id)
        else:
            records = []
            for cluster in self._clusters.values():
                records.extend(cluster.failures)

        if not records:
            return {"total_failures": 0, "blocked_clusters": 0, "categories": {}}

        # Count by category
        categories: Dict[str, int] = {}
        for r in records:
            cat = r.category.value
            categories[cat] = categories.get(cat, 0) + 1

        blocked = sum(1 for c in self._clusters.values() if c.blocked)

        return {
            "total_failures": len(records),
            "blocked_clusters": blocked,
            "unique_signatures": len(self._clusters),
            "categories": categories,
        }

    def clear_task(self, task_id: str) -> int:
        """Clear failure records for a specific task. Returns count cleared."""
        if task_id not in self._task_failures:
            return 0
        signatures = self._task_failures.pop(task_id)
        count = 0
        for sig in signatures:
            cluster = self._clusters.get(sig)
            if cluster:
                # Remove task-specific records
                original_count = len(cluster.failures)
                cluster.failures = [f for f in cluster.failures if f.task_id != task_id]
                if len(cluster.failures) < original_count:
                    count += 1
                # If cluster is now empty, remove it
                if not cluster.failures:
                    del self._clusters[sig]
        return count

    def clear_all(self) -> None:
        """Clear all failure records."""
        self._clusters.clear()
        self._task_failures.clear()

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the failure memory for debugging."""
        return {
            "clusters": {
                sig: cluster.to_dict()
                for sig, cluster in self._clusters.items()
            },
            "task_failures": {
                tid: list(sigs)
                for tid, sigs in self._task_failures.items()
            },
            "config": {
                "max_identical_failures": self.max_identical_failures,
                "max_recovery_attempts": self.max_recovery_attempts,
                "failure_window_seconds": self.failure_window_seconds,
            },
        }