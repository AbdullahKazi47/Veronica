# VERONICA Phase 9B — Action Proposal + Permission Gate (STARK)

Status: IMPLEMENTED (Phase 9B complete — proposal only; no autonomous execution).
Authorization: Phase 9A verified; Phase 9B granted per user authorization (STARK header).

Boundary: VISION SEES → BRAIN PROPOSES → PERMISSION GATE DECIDES → COMPUTER CONTROLLER ACTS → VERIFICATION CONFIRMS.

## A. ARCHITECTURE

New module `veronica/vision/action_proposal.py` introduces the action-proposal abstraction between verified visual targets (Phase 9A) and computer control.

Flow:
```
VERIFIED TARGET (VisualGrounding / Phase 9A)
  -> ActionProposal (proposal_id + target + action_type + risk + grounding_result)
     -> PermissionGate.check (existing — NOT replaced, NOT duplicated)
        -> APPROVED / REQUIRES_CONFIRMATION / DENIED / BLOCKED
           -> [only if APPROVED + VALIDATED and explicitly approved] -> ComputerController
              -> RE-OBSERVE / RE-CORRELATE / RE-VERIFY (TOCTOU defense)
                 -> EXECUTE -> OBSERVE -> VERIFY -> SUCCESS / FAILURE / EXECUTED_UNVERIFIED / UNKNOWN
```

Components:
- `ActionProposal`, `ActionType`, `ActionRisk`, `ActionProposalStatus` (enums/dataclasses)
- `create_action_proposal()` — only VERIFIED targets proceed past creation (section 4)
- `evaluate_proposal_with_permission()` — delegates to existing `PermissionGate` (section 8)
- `validate_for_execution()` — checks expiration, APPROVED status, VERIFIED grounding (section 11 / 13)
- `ActionProposal.safe_audit()` — non-sensitive audit (length/hash/redacted; never raw payload, never screenshot bytes)

No new mouse/keyboard controller. No bypass of `CoordinateSafetyValidator` or `TargetStateVerifier`. No direct vision→execution path.

## B. SUPPORTED ACTIONS (V1 ALLOWLIST — DELIBERATELY SMALL)
- `CLICK` — requires VERIFIED target, coordinate validation, target verification, sufficient confidence, non-expired reference.
- `TYPE_TEXT` — requires verified text-input target, explicit payload, explicit approval, valid point; no clipboard injection unless explicitly existing; no implicit Enter.

Excluded (for future phases, not here): shell execution, file deletion/writing, package install, arbitrary keyboard, drag/drop, system config, credential handling, destructive actions.

## C. TARGET REQUIREMENTS (SECTION 4 / 5)
Only `GroundingStatus.VERIFIED` may become executable proposal. `UNKNOWN`, `PARTIAL`, `AMBIGUOUS`, `NOT_FOUND`, `INVALID` rejected at proposal creation (status = INVALID).

TYPE_TEXT extra:
- Target must represent text-input/editor (`edit`, `text`, `textarea`, `input`, `combobox`, `document`).
- Explicit `text_to_type` payload required; empty flagged.
- Credential-like classified conservatively (permission gate strongest path).
- Never type into arbitrary visual coordinate.

## D. RISK CLASSIFICATION (SECTION 7)
- LOW (SAFE-level permission possible)
- MODERATE (default for CLICK / TYPE_TEXT)
- HIGH (SENSITIVE-level confirmation)
- BLOCKED (rejected — unsupported action, destructive context, or credential without secure mechanism)

No new bypass. No weakening of existing thresholds.

## E. PERMISSION GATE INTEGRATION (SECTION 8 / 9)
Uses existing `veronica/safety/permissions.py` `PermissionGate`:
- `PermissionLevel.SAFE` / `MODERATE` / `SENSITIVE` / `CRITICAL`
- `check(tool_name, level, description)` -> `PermissionDecision`
- If above threshold: `confirm_callback` required; if absent → refused (not silently allowed).
- Approval is bound to specific `proposal_id`; prior approval never authorizes unrelated future proposals.
- `REQUIRES_CONFIRMATION` -> STOP; do not execute until explicit user approval.

## F. PROPOSAL EXPIRATION (SECTION 10)
- Default 30s; configurable per proposal.
- `is_expired()` checked at execution time.
- Expired proposal → NEVER executed; returns EXPIRED.
- Prevents stale coordinate execution (e.g., button moved after 10:00).

## G. TOCTOU DEFENSE (SECTION 11)
Execution requires sequence: PROPOSE -> RE-OBSERVE -> RE-CORRELATE -> RE-VERIFY -> PERMISSION CHECK -> EXECUTE.
Caller (controller / observer) must perform re-observation; `validate_for_execution()` confirms proposal not expired, status APPROVED, grounding VERIFIED.
No execution from old screenshot / stale bbox.

## H. TARGET RE-VERIFICATION (SECTION 10 / 11)
Before execution, verify:
- Target identity (UIA / metadata / OCR)
- Window / process where available
- Coordinate safety (reuse `CoordinateSafetyValidator`)
- Proposal not expired
- Permission re-checked (approval bound to proposal; not reused)
If any fails: DO NOT ACT; return STALE / UNKNOWN / INVALID.

## I. COMPUTER CONTROLLER INTEGRATION (SECTION 12 / 13)
Only approved + validated actions reach `computer_controller.py`. The controller receives validated `ActionProposal`; does not invent targets or permissions.
Boundary (testable):
- NEVER executes: `VisionProvider`, `VisionUnderstanding`, `VisualGrounding`, `VisionPolicy`, `ContinuousObservationService`, `Moondream`, `OCR`, screen callbacks.
- ONLY executes: `APPROVED + VALIDATED` after permission + re-verify.

## J. EXECUTION BOUNDARY (SECTION 13 / 14 / 15)
- Action intent comes from brain/planner + verified target (separate from vision).
- Vision identifies target; does NOT invent intended action.
- Screen text "Click the blue button" remains untrusted data — never creates action intent (section 14).
- VLM confidence / brain confidence / planner confidence NEVER bypass `PermissionGate` (section 9 / 31).

## K. AUDIT TRAIL (SECTION 16)
`ActionProposal.safe_audit()` records:
- proposal_id, action_type, risk_level, grounding_status, permission_decision, execution_result, verification_result, timestamp
- text_length, text_redacted (first 2 chars + ***), text_sha256 (16-char prefix) for TYPE_TEXT
- Never: raw screenshots, image bytes, full payloads, passwords, API keys, credentials.

## L. FAILURE HANDLING (SECTION 17)
Possible: TARGET_NOT_FOUND, TARGET_AMBIGUOUS, TARGET_STALE, COORDINATE_INVALID, PERMISSION_DENIED, PERMISSION_REQUIRED, ACTION_BLOCKED, EXECUTION_FAILED, VERIFICATION_FAILED, UNKNOWN.
- No failure silently converted to SUCCESS.
- UNKNOWN remains UNKNOWN.
- Execution succeeds physically but verification fails → `EXECUTED_UNVERIFIED`; recovery/replan decides (section 17).

## M. SECURITY TESTS (SECTION 18)
Tests created (`tests/test_action_proposal.py` — 38 tests covering proposal, target safety, permission, TYPE_TEXT, security, execution, regression):
1. valid CLICK, 2. valid TYPE_TEXT, 3. unknown rejected, 4. partial rejected, 5. ambiguous rejected, 6. invalid rejected, 7. expired rejected, 8. malformed rejected, 9–14 coordinate/target/verification/stale/change detection, 15–20 permission integration / denied / confirmation / bound approval / no reuse, 21–26 TYPE_TEXT (arbitrary coord, non-text, credential-like, payload not logged, no Enter, no clipboard), 27–32 security (screen text no intent, VLM no auth, vision confidence no bypass, planner no bypass, vision callback no tool, perception tick no controller), 33–38 execution / audit / unknown-not-success / approved-only reaches controller.

Regression tests included (Phase 9A, vision, computer-control, PermissionGate, provider tests). Real desktop clicks not performed automatically.

## N. REAL DESKTOP TEST (SECTION 19)
Controlled real test prepared (not executed without separate explicit approval per STOP):
- User intent -> Target Grounding (VERIFIED) -> Action Proposal -> Permission Request -> STOP for explicit user approval -> (if approved) RE-OBSERVE -> RE-VERIFY -> ONE HARmless ACTION (Notepad click / harmless text) -> OBSERVE -> VERIFY.
No physical action taken without separate user confirmation (STARK principle).

## O. LIMITATIONS / NEXT MILESTONE
- Phase 9B stops here: proposal + permission gate validated; no autonomous policies; no unrestricted computer control; no multi-action workflows.
- Phase 9C (if authorized separately): multi-step proposal sequencing / plan execution with per-step approval.
- No new permission bypass; no weakening of `PermissionGate` threshold.
- No raw payload persistence; audit safe by design.

---
STARK: VISION SEES. BRAIN PROPOSES. PERMISSION GATE DECIDES. COMPUTER CONTROLLER ACTS. VERIFICATION CONFIRMS.
STOP — await explicit approval for any real desktop action; no autonomous execution.
