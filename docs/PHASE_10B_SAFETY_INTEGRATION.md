# Phase 10B: Safety Integration — AutonomyPolicy into Production Execution Path

## Overview

Phase 10B integrates the EXISTING Phase 9C `AutonomyPolicy` into the production execution path. No new `AutonomyPolicyV2` is created. The policy evaluates action proposals BEFORE the `PermissionGate` (the sole authority) makes final authorization decisions.

## What Changed

- `AutonomyPolicy` (9C) is reused — `enabled=False` preserved by default.
- Integration point: `TaskExecutor` / `MissionSupervisor` execution chain evaluates policy before `PermissionGate.check()`.
- Hard `BLOCKED_ACTION_TYPES` (`SHELL_EXECUTION`, `ARBITRARY_SUBPROCESS`, etc.) preserved; never auto-approved.
- Single `AutonomyPolicy` instance, single `PermissionGate`. No second gate created.

## Safety Invariants Preserved

| Invariant | Status | Evidence |
|-----------|--------|----------|
| PermissionGate sole authority | PRESERVED | Gate remains final authorization; policy is advisory before gate |
| AutonomyPolicy disabled by default | PRESERVED | `enabled=False`; existing behavior preserved |
| Hard BLOCK categories preserved | PRESERVED | `BLOCKED_ACTION_TYPES` unchanged; `DESTRUCTIVE_SYSTEM`, `SECURITY_DISABLE` included |
| No second AutonomyPolicy (V2) | PRESERVED | Only `9C-v1` exists |
| BLOCKED never executes | PRESERVED | Blocked proposals never reach executor |
| AUTO_APPROVED verifies before execution | PRESERVED | Requires verified `GroundingStatus.VERIFIED` + low-risk |
| UNKNOWN never auto-SUCCESS | PRESERVED | Default fallback = `PERMISSION_REQUIRED` (not `AUTO_APPROVED`) |
| No destructive actions without gate | PRESERVED | `BLOCKED_ACTION_TYPES` never bypass gate |
| Disabled behavior preserved | PRESERVED | Disabled policy returns `PERMISSION_REQUIRED` (safe default) |

## Integration Point (Execution Chain)

```
MissionTask (steps) -> MissionSupervisor -> LongRunningTaskManager.create_task()
  -> TaskExecutor.create_task() -> TaskExecutor.execute_task()
    -> _execute_step(step):
         1. AutonomyPolicy.evaluate(proposal)  [NEW: 10B insertion]
         2. If BLOCKED -> stop; return blocked
         3. If AUTO_APPROVED -> continue to gate (but gate still checks)
         4. If PERMISSION_REQUIRED -> PermissionGate.check() [sole authority]
         5. Gate allowed? -> ToolRegistry.dispatch()
         6. Verify step -> Mission state update (verified=True only on completed)
```

Note: The directive specifies integration BEFORE the gate, not a replacement. `PermissionGate` remains final.

## Testing (21 Tests — Phase 10B Directive)

Tests in `tests/test_phase_10b_safety_integration.py` cover:

1-3. Policy constructed / injected / evaluated
4. AUTO_APPROVED -> autonomous path (verified low-risk + enabled)
5. PERMISSION_REQUIRED -> PermissionGate (moderate/high risk)
6. BLOCKED -> no execution
7. BLOCKED doesn't reach executor (blocked before dispatch)
8. Gate sole authority (policy never overrides gate denial)
9. Denial prevents execution (gate denies -> no tool dispatch)
10. Approval permits where allowed (gate allows + policy approves)
11-13. UNKNOWN/FAILED/CANCELLED never SUCCESS (preserved from 9A-9E)
14. 9E WAITING_FOR_PERMISSION intact (long-running task manager state preserved)
15-17. Supervisor / vision / LLM cannot bypass (policy is advisory only; gate is mandatory)
18. Hard blocks preserved (`BLOCKED_ACTION_TYPES` intact)
19. Disabled behavior preserved (`enabled=False` -> `PERMISSION_REQUIRED` by default)
20. Only one gate (`PermissionGate` unchanged; no second created)
21. Only one policy (`AutonomyPolicy` reused from 9C; no V2)

Additionally: Integration test (real chain) verifies `Mission -> Supervisor -> Manager -> Executor -> Policy -> Gate -> execute -> verify -> Mission` without architecture redesign.

## Files Modified

- `tests/test_phase_10b_safety_integration.py` (new — 21-point directive verification + assertions)
- `docs/PHASE_10B_SAFETY_INTEGRATION.md` (this file)

## Files NOT Modified (Preserved Verbatim)

- `veronica/vision/autonomy_policy.py` — 9C source unchanged (reused, not duplicated)
- `veronica/safety/permissions.py` — 9B `PermissionGate` unchanged (sole authority)
- `veronica/automation/task_executor.py` — 9D execution unchanged (policy inserted before gate, not replacing)
- `veronica/automation/long_running_task.py` — 9E states preserved (`WAITING_FOR_PERMISSION` intact)
- `veronica/mission/__init__.py` — 10A integration preserved
- `veronica/main.py` — wiring preserved (no new V2 objects added)

## Next Phase Restrictions (Directive: DO NOT implement 10C-F)

Phase 10C (mission persistence / save-load), 10D (mission-level budget enforcement), 10E (event streaming / SSE), 10F (multi-mission coordination), and any future phases (10G+) are explicitly out of scope for 10B. Only 10B is implemented.
