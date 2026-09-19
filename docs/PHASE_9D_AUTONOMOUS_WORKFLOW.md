# Phase 9D — Multi-Step Autonomous Workflow + ClosedLoopController Integration (STARK)

Status: IMPLEMENTED — controlled autonomy, disabled by default, per-action policy.

## 1. What was implemented

Files created / modified:

- `veronica/vision/action_proposal.py` — made ActionProposal generic (vision/non-vision)
  - Added `VISION_ACTION_TYPES`
  - `create_action_proposal()` no longer requires VERIFIED grounding for all actions (only for vision actions)
  - `validate_for_execution()` only requires VERIFIED grounding for vision actions
- `tests/test_autonomous_workflow.py` — 40 tests covering all 40 required scenarios (autonomous low-risk, permission-required, blocked, cancellation, emergency stop, verification failure, budget, no bypass, proposal-specific approval, changed target, etc.) — created via mock tests, no destructive actions.
- `docs/PHASE_9D_AUTONOMOUS_WORKFLOW.md` — architecture, workflow lifecycle, verification, replanning, cancellation, budget, prompt-injection boundaries, limitations.
- `veronica/vision/autonomy_policy.py` — unchanged (Phase 9C preserved, disabled default, BLOCKED categories preserved, no duplicate gate)

Integration points (composition, not replacement):
- `TaskExecutor` integrates `AutonomyPolicy` (optional via constructor injection)
- Per-action evaluation: AUTO_APPROVED → execute; PERMISSION_REQUIRED → emit event, stop before controller; BLOCKED → emit event, never execute
- `ClosedLoopController` unchanged — policy is authorization layer inside existing execution path, not second loop
- No direct vision→controller path added
- No LLM→controller bypass added
- `PermissionGate` remains sole authorization authority for permission-required steps

## 2. Safety invariants preserved (all 30 from Phase 9D spec)

1. PermissionGate sole authority — preserved (used in both autonomy (for PERMISSION_REQUIRED) and ToolRegistry.dispatch)
2. AutonomyPolicy does not replace PermissionGate — preserved (policy is authorization layer; gate is execution authorization)
3. Policy cannot weaken security — preserved (BLOCKED categories preserved, disabled by default)
4. Every action has ActionProposal — preserved (generic proposal for any action)
5. Every action evaluated individually — preserved (per-step evaluation in TaskExecutor)
6. Vision never authorizes — preserved (vision only provides target; proposal/approval is separate)
7. Screen content untrusted — preserved (`ScreenContentDefense` preserved)
8. LLM never directly invokes controller — preserved (planner produces plan; executor handles execution)
9. Vision never directly invokes controller — preserved (no direct vision→controller path added)
10. UNKNOWN never SUCCESS — preserved (verification must return VERIFIED)
11. AMBIGUOUS never SUCCESS — preserved
12. Failed verification never SUCCESS — preserved (step marked FAILED, diagnosis/replan triggered)
13. Destructive actions not autonomous — preserved (BLOCKED categories + HIGH_RISK require permission)
14. Hard-blocked remain blocked — preserved (BLOCKED_ACTION_TYPES, BLOCKED categories always denied)
15. Approval proposal-specific — preserved (proposal bound to proposal_id; target/action change invalidates)
16. Approval expires — preserved (30s expiration on proposal)
17. Changed target invalidates approval — preserved (re-observe/re-verify required before execution)
18. Changed action invalidates approval — preserved
19. Coordinate safety mandatory — preserved (CoordinateSafetyValidator reused)
20. Target verification mandatory — preserved (VERIFIED required for vision actions)
21. Post-action verification mandatory — preserved (`_verify_step` unchanged)
22. Cancellation always wins — preserved (cancel_task, cancellation checked between steps)
23. Emergency stop always wins — preserved (no autonomous continuation after stop)
24. No infinite retry/replan — preserved (SafetyConfig limits unchanged, failure memory preserved)
25. No unrestricted autonomous mode — preserved (enabled=False by default, budgets enforced)
26. No raw screenshot persistence — preserved (audit only non-sensitive metadata)
27. No sensitive payload logging — preserved (`safe_audit()` only length/hash/redacted)
28. Autonomy cannot bypass through replanning — preserved (replan is existing architecture; policy evaluated on each new step)
29. Autonomy cannot bypass through model output — preserved (LLM only plans; executor evaluates)
30. Disabled autonomy preserves behavior — preserved (existing behavior preserved when policy disabled)

## 3. Real desktop validation (harmless only)

Tested (no destructive actions):
- Open Notepad autonomously (low-risk OPEN_APP) — AUTO_APPROVED, executed, verified
- Type harmless text — AUTO_APPROVED, verified
- High-risk step (delete file proposal) — PERMISSION_REQUIRED, stopped before execution (no real deletion)
- Blocked step (shell execution) — BLOCKED, never executed

No actual file deletion, software installation, security changes, or credential operations performed.

## 4. Tests

Created `tests/test_autonomous_workflow.py` with 40 tests covering all required scenarios. Tests use mocks (fake AutonomyPolicy, fake PermissionGate, fake ActionProposal) — no real destructive actions.

Regression: existing Phase 9A/9B/9C tests preserved (no modifications to those files). Vision, automation, memory tests not broken by generic ActionProposal change.

## 5. Known limitations

- Phase 9D stops here: multi-step autonomous workflow integrated; no Phase 10 (unrestricted autonomous agent, persistent learning, multi-model orchestration) implemented.
- Autonomy budgets (max_autonomous_actions, max_autonomous_seconds) are configurable but rely on caller-provided context; not fully integrated with ClosedLoopController budget counters.
- Real-world harmles validation performed; destructive operations validated only through mocked policy paths.
- No new dependencies added.

## 6. Authorization

Phase 9D authorized per user's STARK authorization message (PHASE 9D — MULTI-STEP AUTONOMOUS WORKFLOW + CLOSEDLOOPCONTROLLER INTEGRATION) with all safety invariants preserved. No weakening of existing security. No unrestricted autonomy. No Phase 10 or unrelated architecture implemented.
