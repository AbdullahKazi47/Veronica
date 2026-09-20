# Phase 10D — Autonomous Execution Limits & Safe Escalation

## Purpose
Phase 10D introduces bounded execution limits around the Phase 10C autonomous execution path (ExecutionAuthorization → ComputerUseLoop) without duplicating or replacing any core architecture component.

The limits ensure that when autonomous execution is eventually enabled, VERONICA cannot run indefinitely, execute uncontrolled numbers of actions, or continue after uncertainty or failure.

## Execution Budget
- `AutonomousExecutionBudget` (new): bounded session-scoped budget.
- Fields: `max_actions`, `max_duration_seconds`, `actions_used`, `started_at`, `cancelled`.
- Scope: per session/task; not global; not stored across restarts.
- Configured with safe defaults (`max_actions=10`, `max_duration_seconds=120`).
- Can be passed via `context={"budget": budget}` to `execute_proposal()`.

## Action Limit
Before execution: budget check (cancelled, action count, duration, proposal valid).
- If `max_actions` reached: STOP; return `FAILED` with `decision="BLOCKED"` and audit `budget_exceeded: action_count`.
- Action is recorded (`record_action()`) only when execution proceeds (or attempts).

## Duration Limit
- `is_duration_budget_exceeded()` checks `time.time() - started_at`.
- If exceeded: STOP; return `FAILED` with budget exceeded audit.

## Cancellation
- Respects existing `AutonomousExecutionPath.cancel()` (sets `_cancelled` and calls `loop.cancel()`).
- Budget state also carries `cancelled`; budget check stops new actions when cancelled.
- After cancellation: retry blocked; recovery after cancellation blocked by cancellation guard.

## Timeout / Failure Escalation
- Execution exceptions preserved as `FAILED`; budget exceeded after failure reported separately.
- No silent retry; budget limits prevent indefinite retries.

## Verification Escalation
- `UNKNOWN` verification never promoted to `SUCCESS` (existing invariant preserved).
- `FAILED` verification enters recovery/escalation (existing loop result mapped to `FAILED` / `BLOCKED` / `PERMISSION_REQUIRED` accordingly).
- Budget layer does not alter verification logic; it only limits how many attempts occur.

## Human Escalation
- Budget exceeded -> `FAILED` / `BLOCKED` result (clear escalation state).
- Timeout -> same.
- Repeated failure -> budget stops further attempts.
- Verification unknown -> preserved as unverified (existing); budget stops further attempts.
- Blocked action -> `BLOCKED`; never reaches loop.
- CONFIRM / PERMISSION_REQUIRED -> existing `PermissionGate` path preserved.
- Authorization invalid / expired -> preserved by proposal validation + authorization binding.
- Cancellation -> `CANCELLED`; no further action.

## Safety Invariants Preserved
- `PermissionGate` remains sole human authority (no second gate).
- `AutonomyPolicy` remains single policy (9C-v1); default `enabled=False`; unchanged.
- `ExecutionAuthorization` preserved (proposal-bound, never vision-derived).
- `ActionProposal` validated before budget check; proposal binding preserved.
- `TargetStateVerifier` / `CoordinateSafetyValidator` preserved (loop performs verification).
- `ComputerUseLoop` unchanged (authorization mechanism preserved).
- `BLOCKED_ACTION_TYPES` intact.
- Screen content never authorizes (unchanged).
- Unknown never becomes SUCCESS.
- Autonomy disabled by default preserved.

## Configuration
- `AutonomousExecutionBudget` has safe defaults (`max_actions=10`, `max_duration_seconds=120`).
- No changes to `autonomy_policy.py`, `permissions.py`, `action_proposal.py`, `computer_use.py`, `coordinate_safety.py`, `target_verification.py`.
- Budget parameters can be overridden per session via `context`.

## Tests
- `tests/test_phase_10d_autonomous_limits.py`: 25 pass.
- Covers: budget enforcement (action + duration), budget scoped, cancellation (prevents next + retry), timeout, failure escalation, repeated failure stops, UNKNOWN not SUCCESS, FAILED enters recovery, CONFIRM reaches gate, BLOCKED never executes, expired proposal, proposal binding, vision not authority, disabled safe, 10C/10B regression, computer-use preserved.

## Real-Desktop Validation
- Not performed; safe bounded action only possible with temporary enabled policy + safe budget, restored afterward.
- Directive: do not enable unrestricted autonomy; only harmless bounded validation if needed.
- Not required for Phase 10D since budget is a conservative limit layer (prevents harm, does not enable new execution paths).

## Known Limitations
- Budget is session-scoped; not persisted across restarts.
- Budget does not replace existing ClosedLoopController / recovery mechanism; it limits autonomous attempts.
- No new vision / memory / model-routing / 10E / 10F components.
- Budget defaults are conservative; can be adjusted per task via context.

## Confirmations
- 10E / 10F NOT started.
- Autonomy disabled by default (`AutonomyPolicy.enabled=False`).
- Existing 10C authorization architecture preserved (ExecutionAuthorization + loop integration unchanged).
- No destructive actions performed.
