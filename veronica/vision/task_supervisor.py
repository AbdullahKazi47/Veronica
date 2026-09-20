"""
Phase 10F — Task Supervisor (Orchestration Layer Above 10E).

Delegates multi-step execution to Phase 10E (AutonomousWorkflow).
Manages task lifecycle (create, start, pause, resume, cancel, checkpoint, restore).
Does NOT replace 10E workflow execution logic.
Does NOT persist reusable execution authority (never stores Authorization objects).
Preserves all 10C/10D/10E safety invariants.
"""
from __future__ import annotations

import time
import uuid
from enum import Enum, auto
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, List

from veronica.vision.autonomous_workflow import (
    AutonomousWorkflow, WorkflowStep, WorkflowStatus, StepStatus,
    execute_workflow, run_step,
)
from veronica.vision.autonomous_budget import AutonomousExecutionBudget
from veronica.vision.autonomy_policy import AutonomyPolicy
from veronica.vision.task_checkpoint import TaskCheckpoint
from veronica.vision.task_wait import WaitCondition, WaitStatus, evaluate_wait
from veronica.logging_setup import get_logger

logger = get_logger("vision.task_supervisor")


class TaskStatus(str, Enum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    PAUSED = "PAUSED"
    RECOVERING = "RECOVERING"
    REPLANNING = "REPLANNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


@dataclass
class LongRunningTask:
    task_id: str
    goal: str
    workflow: AutonomousWorkflow
    status: TaskStatus = TaskStatus.CREATED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # Checkpoint is optional; if present it is a state snapshot (no authorization)
    checkpoint: Optional[TaskCheckpoint] = None
    budget: Optional[AutonomousExecutionBudget] = None
    cancellation_requested: bool = False
    pause_reason: Optional[str] = None
    escalation_reason: Optional[str] = None
    attempts: int = 0
    # Metadata for events/logging
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.budget is None:
            # If no budget provided, inherit from the workflow
            self.budget = self.workflow.budget or AutonomousExecutionBudget()
            if self.workflow.budget is not self.budget:
                self.workflow.budget = self.budget


class TaskSupervisor:
    """Orchestrates long-running bounded autonomous tasks using Phase 10E."""

    def __init__(self, autonomy_policy: Optional[AutonomyPolicy] = None):
        # Reuse existing policy; never replace
        self.policy = autonomy_policy or AutonomyPolicy(enabled=False)
        self.tasks: Dict[str, LongRunningTask] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def create_task(
        self,
        goal: str,
        workflow: AutonomousWorkflow,
        budget: Optional[AutonomousExecutionBudget] = None,
    ) -> LongRunningTask:
        task_id = str(uuid.uuid4())
        if budget is None:
            budget = workflow.budget or AutonomousExecutionBudget()
        if workflow.budget is None:
            workflow.budget = budget
        task = LongRunningTask(
            task_id=task_id,
            goal=goal,
            workflow=workflow,
            budget=budget,
        )
        self.tasks[task_id] = task
        task.status = TaskStatus.CREATED
        task.updated_at = time.time()
        logger.info("TaskSupervisor CREATED task=%s goal=%s", task_id, goal)
        return task

    def start_task(
        self,
        task: LongRunningTask,
        *,
        cancellation_token: Optional[Any] = None,
    ) -> LongRunningTask:
        task.attempts += 1
        if task.cancellation_requested or (cancellation_token is not None and getattr(cancellation_token, "cancelled", False)):
            task.status = TaskStatus.CANCELLED
            task.updated_at = time.time()
            logger.info("TaskSupervisor START cancelled task=%s", task.task_id)
            return task
        # Delegate execution to Phase 10E workflow
        task.status = TaskStatus.RUNNING
        task.updated_at = time.time()
        result = execute_workflow(
            task.workflow,
            cancellation_token=cancellation_token,
            budget=task.budget,
        )
        # Update task from workflow result
        task.workflow = result
        # Derive task status from workflow
        if result.status == WorkflowStatus.COMPLETED:
            task.status = TaskStatus.COMPLETED
        elif result.status == WorkflowStatus.CANCELLED:
            task.status = TaskStatus.CANCELLED
            task.cancellation_requested = True
        elif result.status == WorkflowStatus.BLOCKED:
            task.status = TaskStatus.FAILED
            task.escalation_reason = result.escalation_state
        elif result.status == WorkflowStatus.ESCALATED:
            task.status = TaskStatus.ESCALATED
            task.escalation_reason = result.escalation_state
        elif result.status == WorkflowStatus.PARTIAL:
            task.status = TaskStatus.PARTIAL
        elif result.status == WorkflowStatus.WAITING_CONFIRMATION:
            task.status = TaskStatus.WAITING
        else:
            task.status = TaskStatus.RUNNING  # Still in progress
        task.updated_at = time.time()
        logger.info(
            "TaskSupervisor STARTED task=%s status=%s workflow=%s",
            task.task_id, task.status.value, result.status.value,
        )
        return task

    def pause_task(self, task: LongRunningTask, reason: str = "user request") -> LongRunningTask:
        task.pause_reason = reason
        task.status = TaskStatus.PAUSED
        task.updated_at = time.time()
        # Checkpoint the current workflow state (no authorization stored)
        try:
            task.checkpoint = self.checkpoint_task(task)
        except Exception:
            # Defensive: checkpoint failure must not break the task
            logger.warning("TaskSupervisor checkpoint failed during pause task=%s", task.task_id)
        logger.info("TaskSupervisor PAUSED task=%s reason=%s", task.task_id, reason)
        return task

    def resume_task(
        self,
        task: LongRunningTask,
        *,
        cancellation_token: Optional[Any] = None,
    ) -> LongRunningTask:
        # Clear pause state
        task.pause_reason = None
        task.status = TaskStatus.RUNNING
        task.updated_at = time.time()
        # If a checkpoint exists, reset workflow state (not authorization)
        if task.checkpoint is not None:
            try:
                self._reset_workflow_from_checkpoint(task.workflow, task.checkpoint)
            except Exception:
                logger.warning("TaskSupervisor restore failed task=%s", task.task_id)
        # After reset, execution must go through new proposal/auth/budget again
        # Delegate to start_task (which runs execute_workflow)
        return self.start_task(task, cancellation_token=cancellation_token)

    def cancel_task(self, task: LongRunningTask, reason: Optional[str] = None) -> LongRunningTask:
        task.cancellation_requested = True
        task.status = TaskStatus.CANCELLED
        task.escalation_reason = reason or "user cancellation"
        task.updated_at = time.time()
        # Propagate cancellation to workflow (use existing workflow cancellation mechanism)
        try:
            if hasattr(task.workflow, "cancel"):
                task.workflow.cancel()
            else:
                # Defensive: if cancel method missing, set cancellation state directly
                task.workflow.cancellation_state = True
        except Exception:
            pass
        # Create checkpoint of cancelled state (optional, safe)
        try:
            task.checkpoint = self.checkpoint_task(task)
        except Exception:
            pass
        logger.info("TaskSupervisor CANCELLED task=%s reason=%s", task.task_id, reason)
        return task

    def run_task(
        self,
        task: LongRunningTask,
        *,
        cancellation_token: Optional[Any] = None,
    ) -> LongRunningTask:
        # If task is paused, resume first
        if task.status == TaskStatus.PAUSED:
            task = self.resume_task(task, cancellation_token=cancellation_token)
        elif task.status not in (TaskStatus.RUNNING, TaskStatus.CREATED, TaskStatus.RECOVERING, TaskStatus.REPLANNING):
            # If already completed/failed/cancelled, don't re-run
            return task
        # Start / continue execution
        return self.start_task(task, cancellation_token=cancellation_token)

    # ------------------------------------------------------------------
    # Checkpointing
    # ------------------------------------------------------------------

    def checkpoint_task(self, task: LongRunningTask) -> TaskCheckpoint:
        # Build state from workflow (safe fields only)
        workflow_state = {}
        try:
            workflow_state = {
                "workflow_id": task.workflow.workflow_id,
                "goal": task.workflow.goal,
                "status": task.workflow.status.value if hasattr(task.workflow.status, "value") else str(task.workflow.status),
                "current_step_index": task.workflow.current_step_index,
                "completed_steps": list(task.workflow.completed_steps),
                "current_result": task.workflow.current_result.to_dict() if task.workflow.current_result else None,
                "budget_state": task.workflow.budget.budget_state() if task.workflow.budget else {},
                "cancellation_state": task.workflow.cancellation_state,
                "escalation_state": task.workflow.escalation_state,
            }
        except Exception:
            workflow_state = {}
        budget_consumed = {}
        try:
            if task.budget is not None:
                budget_consumed = task.budget.budget_state()
        except Exception:
            pass
        checkpoint = TaskCheckpoint(
            task_id=task.task_id,
            goal=task.goal,
            workflow_state=workflow_state,
            current_step_index=task.workflow.current_step_index,
            completed_steps=list(task.workflow.completed_steps),
            last_step_result=task.workflow.current_result.to_dict() if task.workflow.current_result else None,
            task_status=task.status.value if hasattr(task.status, "value") else str(task.status),
            timestamp=time.time(),
            budget_consumed=budget_consumed,
            recovery_count=task.attempts,
            metadata={
                "pause_reason": task.pause_reason,
                "escalation_reason": task.escalation_reason,
            },
        )
        return checkpoint

    def _reset_workflow_from_checkpoint(
        self,
        workflow: AutonomousWorkflow,
        checkpoint: TaskCheckpoint,
    ) -> None:
        """Reset workflow progress from checkpoint (no authorization restored)."""
        try:
            workflow.current_step_index = checkpoint.current_step_index
            workflow.completed_steps = list(checkpoint.completed_steps)
            workflow.status = WorkflowStatus.PENDING
            # Preserve existing steps; only progress resets
        except Exception:
            pass

    def restore_task(self, checkpoint: TaskCheckpoint) -> AutonomousWorkflow:
        # Restore workflow from checkpoint state (not authorization)
        # Create a new workflow with the saved state; authorization is NOT restored.
        # Note: we must preserve steps; the checkpoint only stores state, not the full step list.
        # For this bounded implementation, we restore using the workflow state and assume
        # the caller will provide the original steps if needed (the supervisor's design
        # does not require full step reproduction from checkpoint alone).
        from veronica.vision.autonomous_workflow import WorkflowStatus
        # We reconstruct with at least one placeholder step if none exist in state
        # (This is safe because the checkpoint is only for progress tracking, not execution.)
        steps = []
        # If the original workflow had steps, they should be provided separately.
        # For safe restoration, we accept the workflow state and create with minimal steps.
        restored_workflow = AutonomousWorkflow(
            workflow_id=checkpoint.workflow_state.get("workflow_id", "restored"),
            goal=checkpoint.goal,
            steps=steps,
        )
        # Restore index and completed steps from checkpoint
        restored_workflow.current_step_index = checkpoint.current_step_index
        restored_workflow.completed_steps = list(checkpoint.completed_steps)
        # Restore status from checkpoint if valid
        try:
            restored_workflow.status = WorkflowStatus(checkpoint.workflow_state.get("status", "PENDING"))
        except Exception:
            restored_workflow.status = WorkflowStatus.PENDING
        # Budget is preserved separately (not restored into authorization)
        return restored_workflow

    # ------------------------------------------------------------------
    # Waiting
    # ------------------------------------------------------------------

    def run_wait(
        self,
        task: LongRunningTask,
        condition: WaitCondition,
        *,
        cancellation_token: Optional[Any] = None,
    ) -> LongRunningTask:
        if task.cancellation_requested or (cancellation_token is not None and getattr(cancellation_token, "cancelled", False)):
            task.status = TaskStatus.CANCELLED
            task.updated_at = time.time()
            return task
        # Set task to WAITING
        task.status = TaskStatus.WAITING
        task.updated_at = time.time()
        # Evaluate bounded wait
        attempts = 0
        wait_start = time.time()
        while True:
            status, attempts, elapsed = evaluate_wait(
                condition,
                cancellation_token=cancellation_token,
                start_time=wait_start,
                attempts=attempts,
            )
            if status == WaitStatus.CONDITION_MET:
                task.status = TaskStatus.RUNNING
                task.updated_at = time.time()
                logger.info("TaskSupervisor WAIT_MET task=%s condition=%s", task.task_id, condition.name)
                return task
            elif status == WaitStatus.CANCELLED:
                task.status = TaskStatus.CANCELLED
                task.cancellation_requested = True
                task.updated_at = time.time()
                logger.info("TaskSupervisor WAIT_CANCELLED task=%s", task.task_id)
                return task
            elif status == WaitStatus.TIMEOUT:
                task.status = TaskStatus.ESCALATED
                task.escalation_reason = f"wait timeout for condition={condition.name}"
                task.updated_at = time.time()
                logger.info("TaskSupervisor WAIT_TIMEOUT task=%s condition=%s", task.task_id, condition.name)
                return task
            elif status == WaitStatus.WAITING:
                # Continue polling; do not block forever because of max_attempts/max_duration
                # Sleep for interval
                time.sleep(condition.interval_seconds)
                # If timeout exceeded after sleep, loop will exit next iteration
            else:
                # Defensive
                task.status = TaskStatus.PAUSED
                return task

    # ------------------------------------------------------------------
    # Recovery / Replanning assistance
    # ------------------------------------------------------------------

    def trigger_recovery(self, task: LongRunningTask, reason: str = "step failure") -> LongRunningTask:
        task.status = TaskStatus.RECOVERING
        task.updated_at = time.time()
        # Delegate to 10E workflow recovery (existing mechanism)
        # We don't reproduce recovery logic; we set state so that resume/run uses it.
        logger.info("TaskSupervisor RECOVERY task=%s reason=%s", task.task_id, reason)
        return task

    def trigger_replan(self, task: LongRunningTask, reason: str = "environment changed") -> LongRunningTask:
        task.status = TaskStatus.REPLANNING
        task.updated_at = time.time()
        # Replan is a bounded operation; it creates new workflow/steps when resumed.
        # We do not replace the 10E workflow directly here; the supervisor just notes the state.
        logger.info("TaskSupervisor REPLAN task=%s reason=%s", task.task_id, reason)
        return task
