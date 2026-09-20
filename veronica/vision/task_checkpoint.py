"""
Phase 10F — Task Checkpoint for Long-Running Autonomous Tasks.

A checkpoint captures the state of a LongRunningTask for persistence and recovery.
It does NOT contain reusable execution authority (e.g., Authorization objects).
It is safe to persist and restore.

"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
import time

from veronica.vision.autonomous_workflow import AutonomousWorkflow, WorkflowStatus


@dataclass
class TaskCheckpoint:
    """Immutable snapshot of a LongRunningTask's state for recovery.

    Fields are chosen to restore task progress without reusing authority.
    All execution authority (Authorization) must be re-acquired fresh upon restore.
    """

    task_id: str
    goal: str
    # The workflow state at the point of checkpointing (excluding authorization)
    workflow_state: Dict[str, Any] = field(default_factory=dict)
    # Index of the next step to run (0-based)
    current_step_index: int = 0
    # List of step IDs that have been VERIFIED and completed
    completed_steps: List[str] = field(default_factory=list)
    # The result of the last executed step (if any)
    last_step_result: Optional[Dict[str, Any]] = None
    # Status of the task at checkpoint
    task_status: str = "UNKNOWN"
    # Timestamp when the checkpoint was created
    timestamp: float = field(default_factory=time.time)
    # Metadata about budget consumption (not the budget object itself)
    budget_consumed: Dict[str, Any] = field(default_factory=dict)
    # Wait state if the task was waiting (e.g., for a condition)
    wait_state: Optional[Dict[str, Any]] = None
    # Recovery/replan history (counts, not data that grants authority)
    recovery_count: int = 0
    replan_count: int = 0
    # Any additional safe metadata
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a dictionary for storage (e.g., JSON)."""
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "workflow_state": self.workflow_state,
            "current_step_index": self.current_step_index,
            "completed_steps": self.completed_steps,
            "last_step_result": self.last_step_result,
            "task_status": self.task_status,
            "timestamp": self.timestamp,
            "budget_consumed": self.budget_consumed,
            "wait_state": self.wait_state,
            "recovery_count": self.recovery_count,
            "replan_count": self.replan_count,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskCheckpoint":
        """Restore from a dictionary (e.g., after JSON load)."""
        return cls(
            task_id=data["task_id"],
            goal=data["goal"],
            workflow_state=data.get("workflow_state", {}),
            current_step_index=data.get("current_step_index", 0),
            completed_steps=data.get("completed_steps", []),
            last_step_result=data.get("last_step_result"),
            task_status=data.get("task_status", "UNKNOWN"),
            timestamp=data.get("timestamp", time.time()),
            budget_consumed=data.get("budget_consumed", {}),
            wait_state=data.get("wait_state"),
            recovery_count=data.get("recovery_count", 0),
            replan_count=data.get("replan_count", 0),
            metadata=data.get("metadata", {}),
        )