# Phase 10F — Long-Running, Resumable Autonomous Tasks

## Overview

Phase 10F adds support for long-running, resumable autonomous tasks to Veronica. It builds upon the existing Phase 10C (AUTO authorization via ExecutionAuthorization, loop integration), Phase 10D (budget, cancellation, escalation), and Phase 10E (bounded multi-step workflows) without redesigning the core safety mechanisms.

The key addition is a task supervisor layer that orchestrates long-running tasks with the ability to pause, resume, checkpoint, and recover while preserving all existing safety invariants (permission gate, authorization isolation, budget non-reset, blocked actions, etc.).

## Design Goals

- **Resumability**: Tasks can be paused and later resumed from the same workflow state (current step index, completed steps).
- **Checkpointing**: Safe snapshots of task progress that do not contain reusable execution authority (authorization objects).
- **Bounded Waiting**: Support for waiting on external conditions with bounded attempts and duration.
- **Recovery/Replanning**: Integration with existing 10E recovery mechanisms.
- **Budget Persistence**: The same budget object is used across pause/resume/recovery cycles (no reset).
- **No Authorization Reuse**: Checkpoints do not store authorization; fresh authorization is required on resume.
- **No Unrestricted Autonomy**: Autonomy remains disabled by default; no new vision authorization mechanism; no blanket task/workflow authorization.

## Key Components

### 1. `LongRunningTask` (in `autonomous_task.py`)
   - A simple data class that associates a task ID, goal, workflow, and runtime status.
   - Includes fields for checkpoint, budget, cancellation requested, pause reason, escalation reason, attempts, and metadata.

### 2. `TaskSupervisor` (in `task_supervisor.py`)
   - Orchestrates the lifecycle of a `LongRunningTask`.
   - Methods:
     - `create_task`: Creates a task with a given goal and workflow.
     - `start_task`: Delegates execution to the Phase 10E `execute_workflow` function.
     - `pause_task`: Pauses the task and creates a checkpoint (without authorization).
     - `resume_task`: Clears pause state, resets workflow progress from checkpoint (if any), and delegates to `start_task`.
     - `cancel_task`: Cancels the task and propagates cancellation to the workflow.
     - `run_task`: Convenience method that resumes if paused, otherwise starts.
     - `run_wait`: Executes a bounded wait condition (from `task_wait.py`).
     - `trigger_recovery` / `trigger_replan`: Sets task state for recovery/replanning (delegates to existing 10E mechanisms).
   - Checkpointing and restoration:
     - `checkpoint_task`: Creates a `TaskCheckpoint` from the current workflow state (step index, completed steps, budget state, etc.) but **does not** include authorization.
     - `restore_task`: Creates a new `AutonomousWorkflow` from a checkpoint (with at least one placeholder step) and restores the workflow progress (index, completed steps). The supervisor then resets the given workflow's progress in place during resume (to avoid creating new workflow objects each time).

### 3. `TaskCheckpoint` (in `task_checkpoint.py`)
   - An immutable snapshot of a `LongRunningTask`'s state for persistence and recovery.
   - Intentionally excludes any authorization-related fields to prevent reuse of execution authority.
   - Contains:
     - `task_id`, `goal`
     - `workflow_state`: A dictionary with workflow ID, goal, status, current step index, completed steps, current result, budget state, cancellation state, escalation state.
     - `current_step_index`, `completed_steps`
     - `last_step_result`
     - `task_status`
     - `timestamp`
     - `budget_consumed`: A dictionary of budget state (not the budget object itself).
     - `wait_state`: Optional wait condition state.
     - `recovery_count`, `replan_count`
     - `metadata`: For pause reason, escalation reason, etc.

### 4. `WaitCondition` and `evaluate_wait` (in `task_wait.py`)
   - Defines a bounded wait condition with:
     - `name`, `description`
     - `check`: A function that returns True when the condition is met.
     - `max_attempts`: Maximum number of polling attempts.
     - `interval_seconds`: Seconds to wait between attempts.
     - `max_duration_seconds`: Maximum total duration in seconds.
     - `context`: Optional additional data (does not include authorization).
   - `evaluate_wait` returns a tuple of `(WaitStatus, attempts, elapsed)` and respects cancellation tokens.

### 5. Reuse of Existing Phases
   - Phase 10C: `AutonomousExecutionPath`, `ExecutionAuthorization`, `ComputerUseLoop`, `PermissionGate`.
   - Phase 10D: `AutonomousExecutionBudget` (action count, duration, cancellation).
   - Phase 10E: `AutonomousWorkflow`, `WorkflowStep`, per-step proposal and authorization, verification, recovery.
   - Thread/Episodic Memory: Unchanged.

## Safety Preservation

All safety mechanisms from prior phases are preserved and not weakened:

- **PermissionGate**: Remains the sole authority for execution; not bypassed.
- **AutonomyPolicy**: Default `enabled=False`; uses the same 9C-v1 policy.
- **ExecutionAuthorization**: Always proposal-bound; never reused across steps or workflows.
- **Budget**: Never reset on resume/recovery/replan; same object reference maintained.
- **Blocked Actions**: `BLOCKED_ACTION_TYPES` preserved (e.g., `SHELL_EXECUTION` remains blocked).
- **Checkpoint Safety**: No authorization, no secrets, no sensitive text stored.
- **Verification**: Never skipped; verification required for step advancement.
- **No Vision Authorization**: No new mechanism for vision to grant authorization.
- **No Unrestricted Autonomy**: No 24/7 agent; no destructive actions; autonomy disabled by default.

## Files Created or Modified

### New Files
- `veronica/vision/autonomous_task.py` (exports `LongRunningTask`, re-exports from `task_supervisor` for convenience)
- `veronica/vision/task_supervisor.py` (TaskSupervisor and LongRunningTask implementation)
- `veronica/vision/task_checkpoint.py` (TaskCheckpoint)
- `veronica/vision/task_wait.py` (WaitCondition, WaitStatus, evaluate_wait)
- `tests/test_phase_10f_long_running_tasks.py` (46 unit tests)
- `docs/PHASE_10F_LONG_RUNNING_TASKS.md` (this document)
- `docs/PHASE_10F_COMPLETION_REPORT.md` (see below)

### No Changes to Existing Files
- The design reuses existing 10C/10D/10E components without modification, except for the budget's `start()` method being called by the supervisor when needed (which is safe and does not alter the budget class).

## Test Results

- **Phase 10F Tests**: 46/46 PASS
- **Phase 10E Regression**: 29/29 PASS (unchanged)
- **Phase 10D Regression**: 25/25 PASS (unchanged)
- **Phase 10C Regression**: 18/18 PASS (unchanged)

## Real-Desktop Validation

Phase 10F does not introduce new real-desktop execution paths; it delegates to the existing Phase 10E `AutonomousExecutionPath` and `ComputerUseLoop`. Therefore, any real-desktop validation would be identical to that of Phase 10E (which was blocked due to lack of real interactive desktop session, not due to safety or design issues).

## Conclusion

Phase 10F successfully adds long-running, resumable autonomous tasks to Veronica while preserving all safety invariants and reusing existing mechanisms. The design ensures that:
- Checkpoints are safe to persist and restore (no authorization).
- Budgets are not reset across pauses/resumes.
- Waiting is bounded and cancellable.
- The supervisor does not replace the 10E workflow execution logic; it delegates to it.
- No new vision authorization or blanket task authorization is introduced.
- Autonomy remains disabled by default.