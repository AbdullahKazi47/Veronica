# Phase 10A: Mission → LongRunningTask Execution Integration

## Overview

Phase 10A bridges the architectural gap between **Phase 9F (Mission Control)** and **Phase 9E (Long-Running Task Management)**. Before this phase, `MissionSupervisor` could select ready tasks and return a "delegate" action, but it did not actually connect to the existing `LongRunningTaskManager` for execution. This phase implements the real integrated execution chain:

```
Mission → MissionSupervisor → LongRunningTaskManager → TaskExecutor → ClosedLoopController → AutonomyPolicy → PermissionGate → Action → Verification → Mission state update
```

## What Changed

### 1. `MissionTask.steps` Format Changed

**Before (Phase 9F):**
```python
steps: List[str] = field(default_factory=list)  # List of step descriptions
```

**After (Phase 10A):**
```python
steps: List[Dict[str, Any]] = field(default_factory=list)  # Executable step dicts
```

Each step dict now contains:
- `description`: Human-readable description of the step
- `tool`: Tool name to invoke
- `args`: Arguments for the tool

This enables the `LongRunningTaskManager` to directly execute mission tasks without translation.

### 2. `MissionSupervisor` Constructor Changed

**Before:**
```python
def __init__(self, mission: Mission, planner: "MissionPlanner"):
```

**After:**
```python
def __init__(self, mission: Mission, long_running_task_manager: "LongRunningTaskManager"):
```

The supervisor now receives the `LongRunningTaskManager` as a required dependency and uses it for actual task execution.

### 3. New Integration Methods in `MissionSupervisor`

| Method | Purpose |
|--------|---------|
| `_task_to_mission_task` | Maps long-running task IDs to mission task IDs |
| `_map_long_task_result_to_mission()` | Propagates long-running task status back to mission tasks |
| `_get_next_step_for_long_task()` | Retrieves next executable step from long-running task |
| `run_cycle()` | Now creates and delegates to `LongRunningTaskManager.create_task()` |

### 4. `MissionPlanner.plan()` Creates Executable Steps

The planner now produces `MissionTask` objects with properly formatted step dicts containing `tool` and `args` fields that the `LongRunningTaskManager` can execute directly.

## Integration Flow

### Mission Execution Lifecycle

1. **Planning**: `MissionPlanner.plan()` creates a `Mission` with `MissionTask` objects containing executable step dicts
2. **Ready State**: Mission transitions to `RUNNING` status
3. **Supervisor Cycle**: `MissionSupervisor.run_cycle()` called
4. **Task Selection**: Supervisor selects ready task via `select_ready_task()`
5. **Delegation**: Supervisor calls `LongRunningTaskManager.create_task(goal, steps)` with the mission task's steps
6. **Mapping**: Supervisor maps `long_task_id` → `mission_task_id` in `_task_to_mission_task`
7. **Execution**: External loop executes the long-running task via `TaskExecutor`
8. **State Propagation**: Supervisor calls `_map_long_task_result_to_mission()` to sync status
9. **Completion**: Mission task marked `completed`/`verified` when long-running task completes
10. **Next Cycle**: Supervisor selects next ready task (respecting dependencies)

### State Mapping

| Long-Running Task Status | Mission Task Status | Mission Status |
|--------------------------|---------------------|----------------|
| `completed` | `completed` + `verified=True` | `RUNNING` (continues) |
| `failed` | `failed` | `FAILED` |
| `waiting_for_permission` | `waiting_for_permission` | `WAITING_FOR_PERMISSION` |
| `paused` | `paused` | `PAUSED` |
| `recovering` | `recovering` | `RECOVERING` |
| `cancelled` | `cancelled` | `CANCELLED` |
| `blocked` | `blocked` | `BLOCKED` |
| `running` | `running` | `RUNNING` |

## Safety Invariants Preserved

All Phase 9A–9E safety invariants are maintained:

| Invariant | How Preserved |
|-----------|---------------|
| **PermissionGate sole authority** | `LongRunningTaskManager.wait_for_permission()` uses `PermissionGate.check()`; supervisor never bypasses |
| **UNKNOWN never auto-SUCCESS** | Mission task only marked `verified=True` when long-running task status is `completed` (which requires verification) |
| **MissionSupervisor cannot directly execute tools** | Supervisor only delegates via `LongRunningTaskManager.create_task()`; no tool dispatch code in supervisor |
| **9E resume verification preserved** | `LongRunningTaskManager.resume_task()` still requires `reobserve_fn` returning `True` |
| **Checkpoints lightweight, no auth** | `TaskCheckpoint` unchanged — only metadata (step_index, verified, timestamp, note) |
| **Cancellation propagates** | `LongRunningTaskManager.cancel_task()` delegates to `TaskExecutor.cancel_task()` |
| **Budgets preserved** | `LongRunningTaskManager.check_budget()` uses `SafetyConfig` limits (600s, 50 steps, 2 retries) |

## Testing

### Phase 10A Tests (in `tests/test_mission_control.py`)

All 30 tests pass, covering:
- Mission model fields and lifecycle
- Task dependencies and cycle detection
- Supervisor cycle (blocked by cycles, delegates ready tasks)
- Planner creates executable steps (no execution)
- Checkpoints are lightweight
- Success criteria preserved
- Recovery count increments
- Permission isolation (checkpoints don't grant permission)
- Budget fields exist
- Cancellation transitions
- Memory no secrets
- Cyclic missions blocked
- Reporting structure
- Safety invariants (UNKNOWN never SUCCESS)
- Regression compatibility (9A–9E interfaces untouched)

### Phase 9E Regression (75 tests)

All 75 Phase 9E tests pass without modification, confirming:
- `LongRunningTaskManager` unchanged
- `TaskExecutor` unchanged  
- `PermissionGate` unchanged
- Checkpoint behavior unchanged
- Pause/resume with re-verification unchanged
- Permission-wait state unchanged
- Failure recovery unchanged
- Budgets unchanged
- Restart safety unchanged

### Full Suite

All 944 tests pass (1 skipped).

## Usage Example

```python
from veronica.mission import MissionPlanner, MissionSupervisor, MissionStatus
from veronica.automation.long_running_task import LongRunningTaskManager
from veronica.automation.task_executor import TaskExecutor
from veronica.tools.registry import ToolRegistry
from veronica.safety.permissions import PermissionGate, PermissionLevel, PermissionDecision

# Setup dependencies (typically done in main.py wiring)
tool_registry = ToolRegistry()
executor = TaskExecutor(tool_registry)
gate = PermissionGate(PermissionLevel.SAFE, lambda *_: PermissionDecision(True, "auto-approve for test"))
manager = LongRunningTaskManager(executor, gate=gate)

# Create mission via planner
planner = MissionPlanner()
mission = planner.plan(
    objective="Create a report file",
    constraints=["safe"],
    criteria=["file_created", "content_verified"]
)

# Setup supervisor with LongRunningTaskManager
supervisor = MissionSupervisor(mission, manager)

# Run mission
mission.set_status(MissionStatus.PLANNING)
mission.set_status(MissionStatus.READY)
mission.set_status(MissionStatus.RUNNING)

while mission.status == MissionStatus.RUNNING:
    result = supervisor.run_cycle()
    print(result)
    
    if result["action"] == "delegate":
        # Execute the delegated long-running task
        long_task_id = result["long_task_id"]
        long_task = executor.tasks[long_task_id]
        executor.execute_task(long_task_id)
        
        # Sync mission state
        supervisor._map_long_task_result_to_mission(long_task)
    
    elif result["action"] == "complete":
        break
    elif result["action"] in ("blocked", "wait"):
        # Handle blocking/waiting
        break

print(f"Mission final status: {mission.status}")
print(f"Completed tasks: {mission.completed_tasks}")
```

## Files Modified

| File | Changes |
|------|---------|
| `veronica/mission/__init__.py` | Major rewrite: `MissionTask.steps` format, `MissionSupervisor` integration with `LongRunningTaskManager`, state mapping, planner executable steps |
| `tests/test_mission_control.py` | Updated test fixtures to provide `LongRunningTaskManager` with `FakeToolRegistry` |

## Files NOT Modified (Preserved)

- `veronica/automation/long_running_task.py` — Phase 9E intact
- `veronica/automation/task_executor.py` — Phase 9D intact
- `veronica/automation/closed_loop.py` — Phase 9C intact
- `veronica/safety/permissions.py` — Phase 9B intact
- `veronica/automation/planner.py` — Phase 9D planner intact

## Compatibility

- **No breaking changes** to Phase 9E interfaces
- **No new public APIs** added to 9A–9E modules
- **MissionSupervisor constructor signature changed** — callers must now pass `LongRunningTaskManager` (see `main.py` wiring)
- **MissionTask.steps format changed** — internal format only; planner output matches new format

## Next Steps (Future Phases)

- Phase 10B: Mission persistence (save/load mission state)
- Phase 10C: Multi-mission coordination
- Phase 10D: Mission-level budget enforcement
- Phase 10E: Mission event streaming / SSE