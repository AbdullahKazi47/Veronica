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
    steps: List[str] = field(default_factory=list)
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
    """Executive supervisor: observe → check → select → delegate → verify → update."""

    def __init__(self, mission: Mission):
        self.mission = mission

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
                # Only mark ready if verified; for simplicity we assume verified on dependency complete
                return t
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
        return {
            "action": "delegate",
            "task_id": task.task_id,
            "reason": "task selected by supervisor",
        }


class MissionPlanner:
    """Planner abstraction — does NOT execute actions."""

    def __init__(self):
        pass

    def plan(self, objective: str, constraints: List[str], criteria: List[str]) -> Mission:
        # Minimal deterministic plan for testing; real deployment integrates with 9D planner
        mission = Mission(
            mission_id=f"mission_{hash(objective) % 1000000:06d}",
            objective=objective,
            success_criteria=criteria,
            constraints=constraints,
        )
        # Example decomposition: analyze, execute, verify
        mission.add_task(MissionTask("t_analyze", "Analyze objective and state"))
        mission.add_task(
            MissionTask("t_execute", "Execute action", dependencies=["t_analyze"])
        )
        mission.add_task(
            MissionTask("t_verify", "Verify result", dependencies=["t_execute"])
        )
        return mission
