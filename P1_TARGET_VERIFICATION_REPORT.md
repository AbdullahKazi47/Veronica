# VERONICA — P1 Target-Specific Computer-Action Verification

**Date:** 2026-09-15
**Scope:** Target-specific verification hardening of the computer-use loop. No real desktop
action was performed during development; all tests use mocked pyautogui + scripted perception.
No weakening of `PermissionGate`; `CoordinateSafetyValidator` unchanged; UNKNOWN is never
auto-promoted to SUCCESS.

---

## 1. Problem

The pre-P1 loop used `ChangeDetector` (generic screen diff) as the *success* criterion for every
computer action. That conflates "the screen changed somewhere" with "the intended target was
acted upon", and it cannot tell a verified target from a moved one or a disappeared one. P1
introduces a reusable, target-specific verification layer.

## 2. New verification states

`ActionVerificationStatus` (values are the stable Stitch/log contract):

| Status  | Value  | Meaning |
|---------|--------|---------|
| `ACTION_EXECUTED`  | `action_executed`  | ran, but not established (internal label) |
| `ACTION_VERIFIED`  | `verification_confirmed` | target-specific evidence confirms |
| `ACTION_UNKNOWN`   | `verification_unknown` | ran; evidence insufficient |
| `ACTION_FAILED`    | `action_failed` | execution raised, or wrong-target evidence |
| `ACTION_BLOCKED`   | `action_blocked` | never reached physical execution |

## 3. Architecture added

**`veronica/vision/target_verification.py`** (new): `TargetReference` (frozen dataclass),
`VerificationResult`, `TargetStateVerifier`, factory `create_target_state_verifier()`.

- **`TargetReference`** captures identity BEFORE the action: control type, automation id,
  class name, element id, semantic name, owning process/window/title, bounding box, target
  point. `identity_signature()` = **sha256** of identity fields (excludes `element_id` and
  bounding box; never Python's built-in `hash()`); `to_dict()` excludes element value/text.
- **`TargetStateVerifier`** re-locates the target AFTER the action (custom locator →
  recursive identity search → automation-id exact match → coordinate containment → focused
  element) and scores evidence:
  control_type 0.30, automation_id 0.25, class_name 0.15, element_id 0.15, name 0.15;
  STRONG ≥ 0.8, PARTIAL ≥ 0.5; large displacement default 0.75.
  Ranking: target-specific identity > owning-window context > existence/location >
  bounding-box consistency > target state change; **global focus and generic screen change
  are supporting evidence only — never sufficient alone**.
- `executed=False` → BLOCKED; missing reference/after-state/location → UNKNOWN;
  identity conflict or owning-window disappearance → FAILED.

**Integration** (`veronica/vision/computer_use.py`):
- `execute_action` order unchanged and invariant:
  coordinate validation → `PermissionGate` → physical action → target verification.
- Captures `self._target_reference = self._capture_reference(action, initial_state)` just
  BEFORE `_execute` (after UNDERSTAND resolves `target_element`).
- `loop.success = verification.is_verified`; phase `SUCCESS` if verified, else `VERIFY`
  (recovery may then decide). `LoopResult.verification` carries the `VerificationResult`.
- Permission-denied → BLOCKED; execution failure/exception → FAILED (verification never
  supersedes execution).
- Legacy `ChangeDetector`-based `_verify` (bool) preserved verbatim for backward-compatible
  tests; it is **not** used for success determination.
- Events: `action_verification_{status}` plus `verification_status` on `action_completed`.
  `veronica/ui/stitch/bridge.py` publishes an `action_verification` SSE with
  `[action_type, target, status]`.

**Exports**: `veronica/vision/__init__.py` adds `TargetStateVerifier`, `TargetReference`,
`VerificationResult`, `ActionVerificationStatus`, `create_target_state_verifier`.

## 4. Files changed

| File | Change |
|------|--------|
| `veronica/vision/target_verification.py` | NEW — verifier, reference, result, statuses |
| `veronica/vision/computer_use.py` | Reconstructed to merge legacy public API (ComputerAction, LoopResult.to_dict, cancel/reset_cancel, legacy bool `_verify`) with shared-gate/coordinate-validator (P0) and P1 target verification; reference captured before execute; success from `_verify_target`; BLOCKED/FAILED verification states on denial/failure; `_verify` sets `config.strategy` (original contract) |
| `veronica/vision/__init__.py` | Export P1 symbols |
| `veronica/ui/stitch/bridge.py` | `action_verification` SSE; `verification_status` on `action_completed` |
| `tests/test_target_verification.py` | NEW — 23 tests |

Untouched (per constraints): `vision/screen_content_defense.py`, `vision/coordinate_safety.py`,
`vision/computer_controller.py`, `vision/uia_adapter.py`, learning/planning/adaptation,
Phase 6 `closed_loop.py`, Stitch GUI internals.

## 5. Tests added (`tests/test_target_verification.py`, 23)

sha256 identity; serialization excludes values; strong-identity VERIFIED despite focus
elsewhere; partial identity + focus + window VERIFIED; small rect change VERIFIED; target
disappears → UNKNOWN; large relocation without focus → UNKNOWN; global-focus-only → UNKNOWN
never success; missing reference → UNKNOWN; identity conflict at location → FAILED; owning
window changed → FAILED; not executed → BLOCKED; end-to-end verified click success (exact
recorded click, no real hardware); generic screen change alone not success; execution
exception → FAILED; permission denied → BLOCKED with no physical action; cancelled → BLOCKED;
invalid coordinates blocked before any physical action; sensitive element value never in
`LoopResult.to_dict()` / `VerificationResult.to_dict()`; Phase 6 semantics (VERIFIED→SUCCESS,
UNKNOWN→VERIFY+recovery); phase mapping.

## 6. Test totals

Full suite **473 passed, 1 skipped** (450 pre-P1 + 23 new), verified **3× consecutive**
2026-09-15 (109 s / 33.8 s / 33.9 s). No regressions; the 1 skip is the pre-existing
documented platform-gated skip.

## 7. Remaining limitations

- Verification strength scales with the identity signals the perception layer actually
  produces; if UIA yields only a name, that is STRONG-on-one-signal (normalized score) rather
  than multi-signal strong.
- Coordinate-only references (no owning window observed before the action) can fall back to
  UNKNOWN rather than FAILED on a disappeared target — safe, but less decisive.
- No changes to real-device behavior were exercised (development was mock-only, per task
  constraints); the next step (typing / autonomous computer use) remains gated on this report
  and was NOT attempted.