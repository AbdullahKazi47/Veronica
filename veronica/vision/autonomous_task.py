"""
Phase 10F — Long-Running Autonomous Task.

Reuses Phase 10C (ExecutionAuthorization / ComputerUseLoop),
Phase 10D (AutonomousExecutionBudget), Phase 10E (AutonomousWorkflow / per-step authorization),
and existing Thread/Episodic Memory.

Provides bounded long-running execution with:
- explicit state model (TaskStatus)
- checkpointing (TaskCheckpoint) that never stores authorization authority
- bounded waiting (WaitCondition / evaluate_wait)
- task-level limits (attempts, recovery limits, budget tracking)
- pause/resume/cancel lifecycle (TaskSupervisor)
- recovery/replan delegation (existing 10E/10C mechanisms)

No unrestricted autonomy.
No destructive autonomy.
No 24/7 agent.
Autonomy disabled by default preserved.
No new vision authorization mechanism.
"""
from __future__ import annotations

from veronica.vision.autonomous_workflow import AutonomousWorkflow
from veronica.vision.autonomous_budget import AutonomousExecutionBudget
from veronica.vision.autonomy_policy import AutonomyPolicy
from veronica.vision.task_supervisor import TaskSupervisor, LongRunningTask, TaskStatus
from veronica.vision.task_checkpoint import TaskCheckpoint
from veronica.vision.task_wait import WaitCondition, WaitStatus, evaluate_wait
from veronica.vision.task_supervisor import TaskSupervisor
