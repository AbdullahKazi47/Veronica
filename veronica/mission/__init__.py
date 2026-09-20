"""Phase 9F — Mission Control & Executive Autonomy.

Reuses (does NOT duplicate):
- 9E LongRunningTaskManager (execution)
- 9D MultiStepTask / TaskStep (step layer)
- 9C AutonomyPolicy
- 9B ActionProposal / PermissionGate / CoordinateSafetyValidator
- 9A visual grounding / observation
- closed_loop, failure_memory, replanner, event infrastructure

Mission = orchestration layer only. No direct physical actions.
Safety invariants preserved: PermissionGate sole authority; checkpoints
are state not authorization; resume requires fresh verification; UNKNOWN
never auto-SUCCESS; budgets never bypassed; no destructive real-world
execution in autonomous mode.

Phase 10A: MissionSupervisor now integrates with LongRunningTaskManager
for actual execution delegation. The supervisor receives the LongRunningTaskManager
as a dependency and uses it to create and execute long-running tasks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set
import time


class MissionStatus(str, Enum):
    CREATED = "created"
    PLANNING = "planning"
    READY = "ready"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_PERMISSION = "waiting_for_permission"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"


# Legal lifecycle transitions (defensive, explicit only)
MISSION_TRANSITIONS: Dict[str, Set[str]] = {
    MissionStatus.CREATED: {MissionStatus.PLANNING},
    MissionStatus.PLANNING: {MissionStatus.READY, MissionStatus.BLOCKED},
    MissionStatus.READY: {MissionStatus.RUNNING, MissionStatus.CANCELLED},
    MissionStatus.RUNNING: {
        MissionStatus.PAUSED,
        MissionStatus.WAITING_FOR_PERMISSION,
        MissionStatus.RECOVERING,
        MissionStatus.COMPLETED,
        MissionStatus.FAILED,
        MissionStatus.CANCELLED,
        MissionStatus.BLOCKED,
    },
    MissionStatus.PAUSED: {
        MissionStatus.RUNNING, MissionStatus.CANCELLED, MissionStatus.BLOCKED
    },
    MissionStatus.WAITING_FOR_PERMISSION: {
        MissionStatus.RUNNING,
        MissionStatus.CANCELLED,
        MissionStatus.BLOCKED,
    },
    MissionStatus.RECOVERING: {
        MissionStatus.RUNNING,
        MissionStatus.FAILED,
        MissionStatus.BLOCKED,
    },
    MissionStatus.COMPLETED: set(),
    MissionStatus.FAILED: set(),
    MissionStatus.CANCELLED: set(),
    MissionStatus.BLOCKED: {MissionStatus.PLANNING, MissionStatus.CANCELLED},
}


@dataclass
class MissionTask:
    task_id: str
    objective: str
    steps: List[Dict[str, Any]] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    status: str = "pending"
    verified: bool = False
    result: Optional[str] = None
    attempts: int = 0


@dataclass
class MissionCheckpoint:
    """Lightweight mission-level checkpoint (state, not authorization)."""
    mission_id: str
    timestamp: float = field(default_factory=time.time)
    completed_tasks: List[str] = field(default_factory=list)
    current_task: Optional[str] = None
    task_statuses: Dict[str, str] = field(default_factory=dict)
    progress_pct: float = 0.0
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "timestamp": self.timestamp,
            "completed_tasks": list(self.completed_tasks),
            "current_task": self.current_task,
            "task_statuses": dict(self.task_statuses),
            "progress_pct": self.progress_pct,
            "note": self.note,
        }


@dataclass
class Mission:
    mission_id: str
    objective: str
    success_criteria: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    priority: str = "normal"
    status: str = MissionStatus.CREATED
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    deadline: Optional[float] = None
    budget: Optional[Dict[str, Any]] = None
    tasks: List[MissionTask] = field(default_factory=list)
    current_task_id: Optional[str] = None
    completed_tasks: List[str] = field(default_factory=list)
    failed_tasks: List[str] = field(default_factory=list)
    blocked_tasks: List[str] = field(default_factory=list)
    pending_approval: List[str] = field(default_factory=list)
    recovery_count: int = 0
    final_result: Optional[str] = None
    checkpoints: List[MissionCheckpoint] = field(default_factory=list)

    def update_time(self) -> None:
        self.updated_at = time.time()

    def add_task(self, task: MissionTask) -> None:
        self.tasks.append(task)
        self.update_time()

    def set_status(self, new_status: str) -> bool:
        allowed = new_status in MISSION_TRANSITIONS.get(self.status, set())
        if allowed:
            self.status = new_status
            self.update_time()
        return allowed

    def has_cycle(self) -> bool:
        # Simple DFS for dependency cycles
        adj: Dict[str, List[str]] = {t.task_id: list(t.dependencies) for t in self.tasks}
        # Include pending/current as nodes too; build full set
        nodes = set(adj.keys())
        for t in self.tasks:
            nodes.add(t.task_id)
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {n: WHITE for n in nodes}

        def dfs(node: str) -> bool:
            color[node] = GRAY
            for neighbor in adj.get(node, []):
                if neighbor not in color:
                    color[neighbor] = WHITE
                if color[neighbor] == GRAY:
                    return True
                if color[neighbor] == WHITE:
                    if dfs(neighbor):
                        return True
            color[node] = BLACK
            return False

        for n in nodes:
            if color.get(n, WHITE) == WHITE:
                if dfs(n):
                    return True
        return False


class MissionSupervisor:
    """Executive supervisor: observe → check → select → delegate → verify → update.

    Phase 10A: Integrates with LongRunningTaskManager for actual task execution.
    The supervisor delegates ready mission tasks to the LongRunningTaskManager,
    which then uses the existing TaskExecutor and ClosedLoopController for
    verified execution with permission gating.
    """

    def __init__(
        self,
        mission: Mission,
        long_running_task_manager: "LongRunningTaskManager"
    ):
        self.mission = mission
        self.long_running_task_manager = long_running_task_manager
        self._task_to_mission_task: Dict[str, str] = {}  # Maps long-running task_id -> mission task_id

    def observe_state(self) -> Dict[str, Any]:
        return {
            "status": self.mission.status,
            "completed": len(self.mission.completed_tasks),
            "total": len(self.mission.tasks),
            "current_task": self.mission.current_task_id,
            "recovery_count": self.mission.recovery_count,
        }

    def select_ready_task(self) -> Optional[MissionTask]:
        completed_ids = set(self.mission.completed_tasks)
        for t in self.mission.tasks:
            if t.task_id in completed_ids or t.task_id in self.mission.failed_tasks:
                continue
            if t.status != "ready" and t.status != "pending":
                continue
            # All dependencies completed and verified?
            if all(dep in completed_ids for dep in t.dependencies):
                return t
        return None

    def _map_long_task_result_to_mission(self, long_task) -> None:
        """Update mission task status based on long-running task result."""
        mission_task_id = self._task_to_mission_task.get(long_task.task_id)
        if not mission_task_id:
            return

        mission_task = next((t for t in self.mission.tasks if t.task_id == mission_task_id), None)
        if not mission_task:
            return

        # Map long-running task status to mission task status
        if long_task.status == "completed":
            mission_task.status = "completed"
            mission_task.verified = True
            if mission_task_id not in self.mission.completed_tasks:
                self.mission.completed_tasks.append(mission_task_id)
            if mission_task_id in self.mission.failed_tasks:
                self.mission.failed_tasks.remove(mission_task_id)
            if mission_task_id in self.mission.blocked_tasks:
                self.mission.blocked_tasks.remove(mission_task_id)
            self.mission.set_status(MissionStatus.RUNNING)
        elif long_task.status == "failed":
            mission_task.status = "failed"
            if mission_task_id not in self.mission.failed_tasks:
                self.mission.failed_tasks.append(mission_task_id)
            if mission_task_id in self.mission.completed_tasks:
                self.mission.completed_tasks.remove(mission_task_id)
            self.mission.set_status(MissionStatus.FAILED)
        elif long_task.status == "waiting_for_permission":
            mission_task.status = "waiting_for_permission"
            if mission_task_id not in self.mission.pending_approval:
                self.mission.pending_approval.append(mission_task_id)
            self.mission.set_status(MissionStatus.WAITING_FOR_PERMISSION)
        elif long_task.status == "paused":
            mission_task.status = "paused"
            self.mission.set_status(MissionStatus.PAUSED)
        elif long_task.status == "recovering":
            mission_task.status = "recovering"
            self.mission.set_status(MissionStatus.RECOVERING)
        elif long_task.status == "cancelled":
            mission_task.status = "cancelled"
            self.mission.set_status(MissionStatus.CANCELLED)
        elif long_task.status == "blocked":
            mission_task.status = "blocked"
            if mission_task_id not in self.mission.blocked_tasks:
                self.mission.blocked_tasks.append(mission_task_id)
            self.mission.set_status(MissionStatus.BLOCKED)
        elif long_task.status == "running":
            mission_task.status = "running"

    def _get_next_step_for_long_task(self, mission_task: MissionTask) -> Optional[Dict[str, Any]]:
        """Get the next step to execute for a mission task from the long-running task."""
        long_task_id = None
        for ltid, mtid in self._task_to_mission_task.items():
            if mtid == mission_task.task_id:
                long_task_id = ltid
                break
        if not long_task_id:
            return None

        # The long-running task is managed by the LongRunningTaskManager
        # We need to get the task from the executor
        if self.long_running_task_manager.executor:
            long_task = self.long_running_task_manager.executor.tasks.get(long_task_id)
            if long_task and long_task.current_step < len(long_task.steps):
                step = long_task.steps[long_task.current_step]
                return {
                    "description": step.description,
                    "tool": step.tool_name,
                    "args": step.args,
                }
        return None

    def run_cycle(self) -> Dict[str, Any]:
        if self.mission.status not in {
            MissionStatus.RUNNING,
            MissionStatus.RECOVERING,
            MissionStatus.PLANNING,
            MissionStatus.READY,
        }:
            return {"action": "none", "reason": f"mission state {self.mission.status}"}

        # Dependency cycle guard
        if self.mission.has_cycle():
            self.mission.set_status(MissionStatus.BLOCKED)
            return {"action": "blocked", "reason": "dependency cycle detected"}

        # Check if current mission task has a corresponding long-running task that's still running
        current_mission_task_id = self.mission.current_task_id
        if current_mission_task_id:
            # Find the long-running task for the current mission task
            long_task_id = None
            for ltid, mtid in self._task_to_mission_task.items():
                if mtid == current_mission_task_id:
                    long_task_id = ltid
                    break

            if long_task_id and self.long_running_task_manager.executor:
                long_task = self.long_running_task_manager.executor.tasks.get(long_task_id)
                if long_task and long_task.status in ("running", "waiting_for_permission", "paused", "recovering"):
                    # The long-running task is still active; don't start a new one
                    self._map_long_task_result_to_mission(long_task)
                    return {"action": "wait", "reason": f"long-running task {long_task_id} still in progress"}

        task = self.select_ready_task()
        if task is None:
            # Nothing ready; check if everything completed
            if all(
                t.task_id in self.mission.completed_tasks for t in self.mission.tasks
            ):
                self.mission.set_status(MissionStatus.COMPLETED)
                return {"action": "complete", "reason": "all tasks completed"}
            return {"action": "wait", "reason": "no ready tasks"}

        self.mission.current_task_id = task.task_id
        task.status = "running"
        self.mission.update_time()

        # Delegate to LongRunningTaskManager for actual execution
        if self.long_running_task_manager is None:
            return {"action": "delegate", "task_id": task.task_id, "reason": "no long-running task manager available"}

        if self.long_running_task_manager.executor is None:
            return {"action": "delegate", "task_id": task.task_id, "reason": "no executor available in long-running task manager"}

        # Create a long-running task for this mission task
        # The mission task's steps are already in the format of step dicts
        steps = task.steps if task.steps else [{"description": task.objective, "tool": "", "args": {}}]

        try:
            long_task = self.long_running_task_manager.create_task(
                goal=task.objective,
                steps=steps,
            )

            # Map the long-running task to the mission task
            self._task_to_mission_task[long_task.task_id] = task.task_id

            # Start execution (this will run in the background if the executor supports it)
            # For now, we just return delegate and let the external loop handle execution
            # The actual execution happens when the external loop calls execute_task on the long-running task
            return {
                "action": "delegate",
                "task_id": task.task_id,
                "long_task_id": long_task.task_id,
                "reason": "task selected by supervisor, delegated to LongRunningTaskManager",
            }
        except Exception as e:
            return {"action": "delegate", "task_id": task.task_id, "reason": f"failed to create long-running task: {e}"}


class MissionPlanner:
    """Planner abstraction — does NOT execute actions."""

    def __init__(self):
        pass

    def plan(self, objective: str, constraints: List[str], criteria: List[str]) -> Mission:
        """Create a mission from a goal. Returns a Mission with tasks that have executable steps."""
        mission = Mission(
            mission_id=f"mission_{hash(objective) % 1000000:06d}",
            objective=objective,
            success_criteria=criteria,
            constraints=constraints,
        )
        # Example decomposition: analyze, execute, verify
        # Each step is a dict with description, tool, and args for the LongRunningTaskManager
        mission.add_task(MissionTask(
            "t_analyze",
            "Analyze objective and state",
            steps=[
                {"description": "Analyze the objective and current state", "tool": "read_file", "args": {"file_path": "dummy.txt"}}
            ]
        ))
        mission.add_task(MissionTask(
            "t_execute",
            "Execute action",
            dependencies=["t_analyze"],
            steps=[
                {"description": "Execute the main action", "tool": "write_file", "args": {"file_path": "output.txt", "content": "result"}}
            ]
        ))
        mission.add_task(MissionTask(
            "t_verify",
            "Verify result",
            dependencies=["t_execute"],
            steps=[
                {"description": "Verify the result", "tool": "read_file", "args": {"file_path": "output.txt"}}
            ]
        ))
        return mission

    def create_plan(self, objective: str, tools_schema: Dict[str, Any], llm: Any, learning_context: str = "") -> List[Any]:
        """Create a plan for the agentic path. This is called by the orchestrator.

        For Phase 10A, we keep this simple and return a basic plan structure.
        The actual planner in automation/planner.py should be used for real planning.
        """
        # This is a stub - the real planner is in automation.planner.MultiStepPlanner
        # We return a list of step objects that the orchestrator expects
        from veronica.automation.planner import TaskStep as PlannerTaskStep
        return [
            PlannerTaskStep("Analyze objective and state", "read_file", {"file_path": "dummy.txt"}),
            PlannerTaskStep("Execute action", "write_file", {"file_path": "output.txt", "content": "result"}),
            PlannerTaskStep("Verify result", "read_file", {"file_path": "output.txt"}),
        ]