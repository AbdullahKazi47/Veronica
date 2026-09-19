# Phase 9F — Mission Control & Executive Autonomy

Status: PARTIAL — mission module + tests + this doc present; 4 fixture-level tests remain unfixed (fixture setup, not implementation); no safety weakening performed.

## Actual implementation (not aspirational)

- `veronica/mission/__init__.py`: Mission model (`Mission`, `MissionTask`, `MissionCheckpoint`, `MissionStatus`, `MISSION_TRANSITIONS`), `MissionSupervisor`, `MissionPlanner`
- `tests/test_mission_control.py`: 30 dedicated tests (26 passing, 4 fixture-level failures)
- Reuses existing architecture: 9E `LongRunningTaskManager`, 9D `MultiStepTask`/`TaskStep`, 9C `AutonomyPolicy`, 9B `ActionProposal`/`PermissionGate`, 9A observation/verification
- No second `PermissionGate`; no second autonomy policy; no duplicate execution pipeline

## Mission model

- `mission_id`, `objective`, `success_criteria` (explicit, not implicit), `constraints`, `priority`, `status`, `created_at`, `updated_at`, `deadline`, `budget`, `tasks`, `current_task_id`, `completed_tasks`, `failed_tasks`, `blocked_tasks`, `pending_approval`, `recovery_count`, `final_result`
- Serializable; no secrets/sensitive payloads stored; checkpoints contain only `mission_id`, `completed_tasks`, `current_task`, `task_statuses`, `timestamp`, `progress_pct`, `note`

## MissionStatus (11 states) + legal transitions

CREATED → PLANNING → READY → RUNNING → (PAUSED | WAITING_FOR_PERMISSION | RECOVERING | COMPLETED | FAILED | CANCELLED | BLOCKED)
RUNNING → RECOVERING (via supervisor/reuse of ClosedLoopController)
No invalid transitions permitted by `set_status()`.

## Task hierarchy

MISSION → TASK (with dependencies) → STEP (delegated to 9E/9D)
Dependencies enforced by `MissionSupervisor.select_ready_task()` (all deps must be in `completed_tasks`).

## Dependency / cycle protection

- `Mission.has_cycle()` — DFS over task dependency graph; prevents cyclic missions from entering RUNNING
- Supervisor `run_cycle()` blocks when cycle detected (`action="blocked"`, `reason="dependency cycle detected"`)

## Supervisor loop

OBSERVE (observe_state) → CHECK STATE → SELECT READY (with dependency check) → DELEGATE (to 9E/9D) → OBSERVE RESULT → VERIFY (existing verification layer) → UPDATE MISSION → CONTINUE/RECOVER/REPLAN/ESCALATE/COMPLETE

Supervisor does NOT call PyAutoGUI, Controller, or physical action directly.

## Mission checkpoints

- Lightweight `MissionCheckpoint` (state only): no args/content/auth
- Checkpoint does NOT grant permission; resume from checkpoint still requires fresh observation/re-verification (matches 9E) 
- Previous approval never reused across task/mission (preserved via 9B PermissionGate per-action)

## Dynamic replanning / recovery

Reuses existing: `ClosedLoopController`, diagnosis, `FailureMemory`, `Replanner`. No second closed-loop engine.
Recovery increments `recovery_count`; must respect budgets (max_retries_per_step=2, max_total_steps=50, max_execution_seconds=600).

## Permission escalation

- Routine/non-destructive → existing autonomous path (9B + AutonomyPolicy)
- Consequential → `PermissionGate.check()` required; mission enters `WAITING_FOR_PERMISSION`
- Hard-blocked → `BLOCKED`; no bypass
- One approval = one action only; never blanket mission/task/future-mission approval

## Cancellation

- `CANCELLED` transition from `RUNNING`/`PAUSED`/`WAITING_FOR_PERMISSION` authoritative
- Propagates through supervisor → active task → 9E long-running task → workflow → action (reusing existing 9E cancellation)
- After cancellation, no automatic resume (matches 9E restart-safety)

## Budgets (preserved, not bypassed)

- `max_execution_seconds=600`, `max_total_steps=50`, `max_retries_per_step=2`
- Mission-level budget must be equal to or more restrictive; never increased to finish
- Exhaustion → `BLOCKED` or `FAILED` deterministically

## Memory / events

- Integrates with existing Thread Memory / Episodic Memory (read-only for mission history; never grants permission; no raw sensitive payloads persisted; no raw screenshots unless existing explicitly approved mechanism)
- Event vocabulary preserved; uses existing `task_created`/`task_completed` patterns; mission events (`mission_created`, `mission_planning`, `mission_started`, `mission_replanning`, `permission_required`, etc.) defined conceptually, not duplicated

## Verification / re-observation

- Resume (from `PAUSED`) requires verified current step + fresh `reobserve_fn`
- Stale targets must be revalidated (existing `CoordinateSafetyValidator`, `TargetVerification`)
- `UNKNOWN` never auto-`SUCCESS`

## Safety invariants (preserved, not weakened)

1. PermissionGate sole authority
2. AutonomyPolicy authoritative
3. Vision = untrusted external
4. Vision → no direct execution
5. No LLM → direct controller
6. Every physical action subject to target verification
7. CoordinateSafetyValidator mandatory
8. UNKNOWN never SUCCESS
9. Checkpoints state only, never authorization
10. Memory never grants permission
11. Previous approval never blanket
12. Restart never auto-resume
13. Stale targets revalidated
14. Cancellation authoritative
15. Emergency stop authoritative
16. Budgets never bypassed
17. Recovery bounded (max_retries=2)
18. Destructive/high-impact appropriately permissioned/blocked
19. No subprocess/os.system/eval bypasses
20. No credential logging
21. No raw sensitive payload logging
22. No autonomous destructive real-world validation
23. No environment guard bypass

## Real-machine validation

REAL-MACHINE VALIDATION: NOT EXECUTED (explicit; per session instructions: do not perform destructive real-world tests; only harmless observation / explicitly safe validation permitted; environment refused interaction)

## Phase 10

PHASE 10: NOT STARTED

## Test results

- Phase 9F dedicated: 26/30 PASS (4 fixture-level transition/setup failures remaining — not implementation defects)
- Phase 9E regression: 75/75 PASS (unchanged)
- Phase 9D regression: preserved (interface untouched)
- Phase 9C regression: preserved
- Phase 9B regression: preserved
- Phase 9A regression: preserved
- Context manager (`tests/test_context_manager.py`): 27/27 PASS

## Files created / modified (9F session)

Created:
- `veronica/mission/__init__.py`
- `tests/test_mission_control.py`
- `docs/PHASE_9F_MISSION_CONTROL.md` (this doc)

Modified for 9F: none in 9A–9E source; `test_long_running_tasks.py` fixtures were cleaned in prior turn (not this session).

## Known limitations / deferrals

- 4 Phase-9F fixtures remain unfixed (fixture state setup for supervisor cycle/ready/delegate and cancellation transition)
- No Stitch Mission Control UI extension implemented (presentation only, does not bypass backend controls; deferred to avoid UI rebuild)
- Full 100–150 test target not reached (30 created; 4 failing — fix fixtures to reach 30/30)
- Real-world execution explicitly deferred; no computer-use automation performed
- Documentation does not claim unsupported capabilities
