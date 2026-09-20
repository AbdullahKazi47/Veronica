"""
Phase 10D — Autonomous Execution Budget & Safe Escalation.

Provides bounded execution limits around the existing autonomous
execution path (Phase 10C). Does NOT replace PermissionGate,
AutonomyPolicy, ActionProposal, ComputerUseLoop, or verification.

Bounded by design:
- Action count budget (max_actions)
- Duration budget (max_duration_seconds)
- Cancellation state (reuse existing cancel mechanism)
- Timeout / budget exceeded results integrate with existing
  ExecutionStatus / LoopPhase / VerificationResult.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional, Dict, Any

from veronica.logging_setup import get_logger

logger = get_logger("vision.autonomous_budget")


@dataclass
class AutonomousExecutionBudget:
    """Bounded execution budget scoped to a single autonomous session/task."""

    max_actions: int = 10
    max_duration_seconds: float = 120.0
    actions_used: int = 0
    started_at: Optional[float] = None
    cancelled: bool = False

    def start(self) -> None:
        if self.started_at is None:
            self.started_at = time.time()

    def cancel(self) -> None:
        self.cancelled = True

    def reset_cancel(self) -> None:
        self.cancelled = False

    def record_action(self) -> None:
        self.actions_used += 1

    def is_cancelled(self) -> bool:
        return self.cancelled

    def is_action_budget_exceeded(self) -> bool:
        return self.actions_used >= self.max_actions

    def is_duration_budget_exceeded(self) -> bool:
        if self.started_at is None:
            return False
        elapsed = time.time() - self.started_at
        return elapsed >= self.max_duration_seconds

    def budget_state(self) -> Dict[str, Any]:
        elapsed = (time.time() - self.started_at) if self.started_at is not None else 0.0
        return {
            "max_actions": self.max_actions,
            "max_duration_seconds": self.max_duration_seconds,
            "actions_used": self.actions_used,
            "started_at": self.started_at,
            "elapsed_seconds": elapsed,
            "cancelled": self.cancelled,
            "action_budget_exceeded": self.is_action_budget_exceeded(),
            "duration_budget_exceeded": self.is_duration_budget_exceeded(),
        }
