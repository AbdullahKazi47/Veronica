# VERONICA — P0 Computer-Use Safety Hardening

**Date:** 2026-09-14
**Scope:** Three P0 issues only. No changes to UIA adapter, OCR, learning, adaptive planning, behavioral adaptation, Phase 6 recovery, closed-loop architecture, Stitch GUI, or screen-content defense.

---

## 1. Root cause of each issue

### Issue 1 — Computer-use verification treated "no reference state" as success
`ChangeDetector.has_changed()` had:

```python
if self.previous_state is None:
    return True  # missing reference treated as "changed"
```

A brand-new `ChangeDetector` has `previous_state = None`. When `ComputerUseLoop._verify()` called `has_changed()` without ever recording a before-state, the missing reference was interpreted as *evidence of change* → the loop reported SUCCESS on no data at all. That violates the invariant **UNKNOWN never auto-SUCCESS**.

### Issue 2 — Two independent PermissionGates
`ComputerUseLoop.__init__` created *its own* `PermissionGate()` for `self.permission_gate`, and separately passed `computer_controller or ComputerController(...)` — a controller constructed with *another* fresh `PermissionGate()`. With both default-constructed, the loop and controller checked **different gate instances**, so a dry-run override on one would not reflect in the other, and per-gate state (e.g. user-approved overrides) could diverge. There was no single authoritative decision path.

### Issue 3 — Declared permission level could be silently downgraded
`ComputerAction.permission_level = CRITICAL` was never threaded into the controller. `_execute()` called controller methods without a permission argument, so every method checked the method-default (`MODERATE` for click/type/hotkey, `SAFE` for move/scroll). A caller who had explicitly escalated an action to CRITICAL (or required SENSITIVE for a destructive combo) got the default floor instead. The declared level was discarded — an undetectable downgrade.

---

## 2. Files changed

| File | Change |
|------|--------|
| `veronica/vision/change_detection.py` | `has_changed()`: `previous_state is None` → returns `False` (UNKNOWN), not `True` |
| `veronica/vision/computer_use.py` | Shared-gate construction in `__init__`; `_verify()` hardened; `_execute()` passes `permission_level` to every controller call; factory docstring |
| `veronica/vision/computer_controller.py` | `permission_level` param on 10 methods with `max(declared, minimum)` floors |
| `tests/test_phase7_core.py` | 16 new P0 tests |

Untouched (per constraints): `vision/uia_adapter.py`, `vision/ocr*`, learning, adaptive planning, behavioral adaptation, Phase 6 recovery, closed-loop architecture, Stitch GUI, screen-content defense.

---

## 3. Exact behavioral changes

### Verification (issue 1)
- **`ChangeDetector.has_changed()`**: with no reference state, returns `False` with a debug log *"No previous state - cannot verify change (UNKNOWN)"*. A caller that would previously get `True` (→ SUCCESS) now gets `False` (→ non-success).
- **`ComputerUseLoop._verify()`**: requires `before_state` (warns *"Verification skipped: no before-state reference … (UNKNOWN)"* → returns `False`), records the reference with `update_reference(before_state)`, requires `after_state` (warns *"Verification failed: no after-state observed … (UNKNOWN)"* → returns `False`), then compares. Missing before-state can no longer produce SUCCESS.

### Shared gate (issue 2)
- **`ComputerUseLoop.__init__`**: `shared_gate = permission_gate or PermissionGate()`; `self.permission_gate = shared_gate`; `ComputerController(permission_gate=shared_gate, …)`. Loop and controller reference the **same** instance — one authoritative decision path. If the caller supplies a controller *and* no gate, the loop's gate and the caller's controller still share a gate only when the caller built it with that gate; the invariant guaranteed by this change (and tested) is that when the loop creates the controller, both share one instance.

### Permission escalation (issue 3)
- **`ComputerController`** — all action methods now accept `permission_level` and enforce a floor with `max(declared, minimum)`:
  - `click_at` / `click_element` / `click_by_name` / `find_and_click` / `type_text` / `drag_to` / `hotkey` / `press_key`: floor `MODERATE`
  - `press_key` / `hotkey` destructive combos (`alt+f4`, `ctrl+shift+esc`, `win+l`, `ctrl+alt+del`): floor `SENSITIVE`
  - `move_to` / `scroll`: floor `SAFE`
  - A CRITICAL declaration is passed straight to `PermissionGate.check()` as CRITICAL — never downgraded to MODERATE.
- **`ComputerUseLoop._execute()`** forwards `action.permission_level` into every controller invocation.

Net effect: an action's declared level is either honored or *raised* to the safety floor — it is never silently lowered. No existing confirmation behavior removed.

---

## 4. New tests (16, in `tests/test_phase7_core.py`)

| Test | Verifies |
|------|----------|
| `test_verification_requires_reference_state` | `has_changed()` → False with no reference (issue 1) |
| `test_verification_succeeds_with_valid_reference` | real reference + real change → True |
| `test_verification_fails_on_no_change_with_reference` | identical states → False |
| `test_computer_use_loop_verify_requires_before_state` | `_verify(None, after, …)` → False |
| `test_computer_use_loop_verify_requires_after_state` | `_verify(before, None, …)` → False |
| `test_computer_use_loop_verify_success_with_real_change` | active-window change under ACTION → True |
| `test_computer_use_loop_verify_fails_on_no_change` | same before/after → False |
| `test_computer_use_loop_and_controller_share_same_gate` | `loop.permission_gate is controller.permission_gate` (issue 2) |
| `test_create_computer_use_loop_shares_gate_with_controller` | factory passes supplied gate through |
| `test_create_computer_controller_with_existing_gate` | controller respects a provided gate |
| `test_controller_uses_action_permission_level` | signature default is MODERATE for click/type |
| `test_controller_respects_minimum_permission_for_destructive_keys` | `press_key("alt+f4", SAFE)` → dry-run blocks (raised to SENSITIVE) |
| `test_controller_never_downgrades_coordinate_clicks` | `click_at(.., SAFE)` → dry-run blocks (raised to MODERATE) |
| `test_controller_never_downgrades_type_text` | `type_text("test", SAFE)` → dry-run blocks |
| `test_controller_never_downgrades_hotkey` | `hotkey("ctrl","c", SAFE)` → dry-run blocks |
| `test_existing_safety_confirmation_behavior_preserved` | MODERATE blocked in dry-run, SAFE move allowed — confirmation intact (issue 3 + regression) |

---

## 5. Full test results (3 consecutive runs)

```
Run 1: 417 passed, 1 skipped in 10.28s
Run 2: 417 passed, 1 skipped in 10.57s
Run 3: 417 passed, 1 skipped in 10.09s
```

Post-tightening confirmation (also 417 passed, 1 skipped in 10.43s) to verify the deterministic assertion change caused no regression. The single skip is the pre-existing optional test.

---

## 6. Remaining limitations

- **Change detection is fingerprint-based, not semantic.** Verification decides success by *weighted attribute change* (dims/active-window/windows/elements/text). A real interaction that legitimately leaves the screen visually identical could still be judged a failure (this is the desired conservative direction — false "success" is the worse error).
- **Single-category changes may not cross threshold.** A change limited to one low-weight category (e.g. active window alone ≈ 0.25) stays below the ACTIVE threshold (0.3) but above the ACTION threshold (0.2). Callers should pass a verification strategy matched to their expected effect.
- **Shared-gate guarantee is one-way from the loop.** If a caller supplies a pre-built controller *and* no gate, the loop cannot retroactively rewire that controller's gate — the loop shares a gate with the controller it constructs. Callers already holding a controller are responsible for building it with the same gate (the factory path guarantees it).
- **Destructive-combo list is explicit.** The SENSITIVE floor for `alt+f4` / `ctrl+shift+esc` / `win+l` / `ctrl+alt+del` is an allowlist substring match; other dangerous combos are caught by the MODERATE floor + PermissionGate policy, not by a comprehensive list.
- **Dry-run mode** is the only confirmation path exercised in tests; real interactive confirmation was not simulated (deliberately, to avoid touching live permission UI).

---

## 7. No destructive action performed

The smoke test was **non-destructive only**: it constructed the shared gate and loop, asserted gate identity, ran `_verify` with missing reference states, and issued permission checks in **dry-run mode** (a CRITICAL click was *blocked* by the gate; a SAFE move was allowed). It did **not**: delete files, execute shell commands, install software, send messages, submit forms, modify important files, perform financial actions, or interact with sensitive accounts.