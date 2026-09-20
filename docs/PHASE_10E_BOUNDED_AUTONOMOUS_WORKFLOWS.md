# Phase 10E — Bounded Autonomous Multi-Step Workflows

Purpose: Execute bounded sequences of routine, non-destructive steps toward a user-defined goal. Each step is independently proposed, validated, authorized, executed, verified, and budget-checked. No blanket authorization; no vision authorization; autonomy disabled by default preserved; no Phase 10F.

## Components (reused from 10C/10D)
- `AutonomousExecutionPath` (10C authorization + loop integration)
- `ExecutionAuthorization` (proposal-bound; never vision-derived)
- `AutonomousExecutionBudget` (10D session-scoped budget shared across steps)
- `AutonomyPolicy` (9C; default disabled)
- `PermissionGate` (sole authority; preserved)
- `ComputerUseLoop` + verification (unchanged)
- Existing recovery/replan (feeds into existing closed-loop path)

## Workflow State Model
- `AutonomousWorkflow`: workflow_id, goal, steps, current_step_index, completed_steps, budget, cancellation_state, escalation_state, status.
- `WorkflowStep`: step_id, description, proposal (independent), authorization (independent), result, verification_status, status.

State transitions:
- PENDING → RUNNING → (VERIFIED → next / UNKNOWN → ESCALATED / BLOCKED / CANCELLED / CONFIRM → WAITING_CONFIRMATION / FAILED → recovery/replan)

## Safety Rules (preserved)
- Autonomy disabled by default (`AutonomyPolicy(enabled=False)`).
- Every step: proposal → validation → policy → authorization (proposal-bound) → budget check → execution → verification.
- Shared `AutonomousExecutionBudget` never reset per step.
- `UNKNOWN` never advances; `FAILED` uses existing recovery (new proposal + new authorization + budget continues).
- `CONFIRM` pauses and uses `PermissionGate`; rejection stops (`BLOCKED`, not downgraded).
- `BLOCKED` stops permanently; cancellation stops and prevents automatic retry/replan/next-step execution.
- Vision/perception never authorizes.
- `BLOCKED_ACTION_TYPES` preserved; no destructive autonomy.

## Key APIs
- `execute_workflow(workflow, *, cancellation_token=None, path=None, budget=None)` — bounded multi-step execution.
- `run_step(workflow, step, *, cancellation_token=None, path=None)` — per-step authorization + execution + verification.
- `handle_recovery(workflow, failed_step, *, cancellation_token=None, path=None)` — creates new proposal + new authorization; consumes same shared budget; does not replace existing recovery mechanism.

## Files
- Created: `veronica/vision/autonomous_workflow.py`, `tests/test_phase_10e_bounded_workflows.py`, `docs/PHASE_10E_BOUNDED_AUTONOMOUS_WORKFLOWS.md`, `docs/PHASE_10E_COMPLETION_REPORT.md`
- Modified (minimal): `veronica/vision/autonomous_execution.py` (no core changes), `veronica/vision/__init__.py` (exports)

## Tests (29/29 pass)
- Two-step AUTO workflow; order; unique proposal/auth; all verified = COMPLETED.
- UNKNOWN stops; FAILED invokes recovery; new proposal/auth; shared budget continues.
- CONFIRM pauses; approval resumes only that step; rejection stops.
- BLOCKED stops; authorization isolation; budget exhaustion; cancellation (before/between/during recovery).
- Cancelled cannot become SUCCESS; vision non-authority; disabled default; destructive blocked; gate sole authority; no blanket workflow authorization.
- 10C regression (18/18); 10D regression (25/25).

No 10F started; no 24/7; no unrestricted autonomy; no new vision/memory/model-routing.
