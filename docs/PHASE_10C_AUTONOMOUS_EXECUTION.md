# Phase 10C: Risk-Based Autonomous Execution

## Purpose

Integrate existing Phase 9C `AutonomyPolicy`, 9B `PermissionGate`, 9A observation/verification, and the `ComputerController` / `ComputerUseLoop` into a controlled autonomous execution path — without redesigning architecture, duplicating safety systems, or creating unrestricted autonomy.

## What Was Implemented

- `veronica/vision/autonomous_execution.py` — `AutonomousExecutionPath`
- `tests/test_phase_10c_autonomous_execution.py` — 18 focused tests (all pass)
- `docs/PHASE_10C_AUTONOMOUS_EXECUTION.md` — this document

No changes to:
- `autonomy_policy.py` (reused 9C; disabled by default preserved)
- `permissions.py` (`PermissionGate` remains sole authority)
- `action_proposal.py` (proposal validation preserved)
- `computer_controller.py` (coordinate safety preserved; no second controller)
- `computer_use.py` (loop / verification reused; no duplicate recovery)

## AUTO / CONFIRM / BLOCKED Paths

### AUTO (`AutonomyDecision.AUTO_APPROVED`)
1. Receive `ActionProposal`
2. Validate proposal (not expired, not invalid)
3. Verify target (existing `TargetStateVerifier` / `CoordinateSafetyValidator`)
4. `AutonomyPolicy.evaluate()` returns `AUTO_APPROVED` (only for verified low-risk)
5. Execute through existing `ComputerController` / `ComputerUseLoop`
6. Post-action verification (`VerificationResult`) — `VERIFIED` vs `UNKNOWN` preserved
7. Return result distinguishing `EXECUTED_VERIFIED` vs `EXECUTED_UNVERIFIED`

### CONFIRM (`PERMISSION_REQUIRED`)
1. Validation + verification (same as AUTO)
2. Policy returns `PERMISSION_REQUIRED`
3. Do NOT execute; request user permission via existing `PermissionGate`
4. Only after `PermissionGate.check()` allows:
   - Re-validate proposal (must still be valid)
   - Execute
   - Verify
5. If rejected: return `PERMISSION_REQUIRED` / safe denial; no execution

### BLOCKED (`BLOCKED`)
1. Policy evaluates blocked categories (`BLOCKED_ACTION_TYPES`: `SHELL_EXECUTION`, `DESTRUCTIVE_SYSTEM`, etc.)
2. Never execute; never ask permission; never downgrade to AUTO
3. Return `BLOCKED` with audit-safe reason (no sensitive payload exposure)

## Safety Invariants (All Preserved)

- One `AutonomyPolicy` (no V2)
- One `PermissionGate` (no second gate)
- `AutonomyPolicy.enabled = False` by default (legacy/manual behavior unchanged)
- Hard `BLOCKED_ACTION_TYPES` intact; never autonomous
- `PermissionGate` remains final authorization authority
- `ActionProposal` not bypassed; target verification not skipped; coordinate safety preserved
- `ComputerController` uses existing `CoordinateSafetyValidator` before gate
- Vision / `PerceptionLayer` does NOT authorize execution; vision data remains untrusted
- `VerificationResult` never promotes `UNKNOWN` to `VERIFIED` / `SUCCESS`
- Failed verification does not silently continue
- Cancellation respects `loop.cancel()`; no competing mechanism
- Sensitive payloads (TYPE_TEXT) not logged; audit excludes `text`, `password`, `secret`

## Verification Behavior

- `VerificationResult.status` mapped to `VERIFIED` / `UNKNOWN` / `FAILED`
- `EXECUTED_VERIFIED` only when loop reports verified
- `EXECUTED_UNVERIFIED` when executed but not verified (defensive — never promoted)
- `FAILED` when execution fails; `BLOCKED` when policy blocks; `CANCELLED` when cancelled; `PERMISSION_REQUIRED` when gate denies

## What Was NOT Implemented (Per Directive)

- Phase 10D (mission persistence)
- Phase 10E (mission event streaming / SSE)
- Phase 10F (multi-mission coordination)
- Unrestricted autonomous multi-action workflows
- Autonomous destructive actions
- Any architecture redesign

## Limitations

- `AutonomousExecutionPath` relies on `ComputerUseLoop` / `ComputerController`; real-world execution depends on `pyautogui` availability.
- Multi-step autonomous workflows remain out of scope for 10C; only individual proposal execution.
- The `execute_proposal` method builds a `ComputerAction` from proposal fields — this mapping is minimal and adequate for the 10C directive but could be expanded in future phases.

## Test Results

```
Phase 10C: 18 passed, 0 failed, 0 skipped
Full suite: see separate run output (no regressions from 10C)
```
