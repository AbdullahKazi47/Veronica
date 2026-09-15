"""
P1 target-specific computer-action verification tests.

Verifies the TargetStateVerifier abstraction and its integration into the
computer-use loop:

- TargetReference captures stable identity BEFORE the action and signs it with
  sha256 (never Python's built-in hash()).
- Verification outcomes are structured: VERIFIED / UNKNOWN / FAILED / BLOCKED.
- Evidence ranking: target-specific identity > owning-window context >
  existence/location > bounding-box consistency > target state change >
  global focus (supporting only) > generic screen change (never sufficient).
- Loop integration: correct phase mapping, correct order (coordinate
  validation -> PermissionGate -> physical action -> verification), and NO
  real mouse/keyboard actions (all hardware is mocked).

Safety invariants exercised here:
- PyAutoGUI returning successfully can never alone produce SUCCESS.
- Generic screen change can never alone produce SUCCESS.
- Global focus can never alone produce SUCCESS.
- UNKNOWN is never auto-promoted to SUCCESS.
- Sensitive element values / typed content are never serialized.
"""
from __future__ import annotations

import json
import platform
import time

import pytest

from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.vision.computer_controller import ComputerController
from veronica.vision.computer_use import (
    ComputerUseLoop,
    ComputerAction,
    LoopPhase,
    LoopResult,
    create_computer_use_loop,
)
from veronica.vision.coordinate_safety import (
    CoordinateSafetyValidator,
    ScreenBoundsProvider,
)
from veronica.vision.screen_state import ScreenState, UIElement, WindowInfo
from veronica.vision.target_verification import (
    TargetStateVerifier,
    TargetReference,
    VerificationResult,
    ActionVerificationStatus,
    create_target_state_verifier,
)

# ----------------------------------------------------------------------
# Deterministic fakes — no real hardware ever touched.
# ----------------------------------------------------------------------


class _FixedBoundsProvider(ScreenBoundsProvider):
    """Deterministic virtual-desktop bounds for tests."""

    def __init__(self, bounds=(0, 0, 1920, 1080)):
        self.bounds = bounds

    def get_virtual_desktop_bounds(self):
        return self.bounds


class _FakePyAutoGUI:
    """Records calls instead of driving real hardware."""

    FAILSAFE = True

    def __init__(self):
        self.clicks = []
        self.moves = []
        self.types = []

    def click(self, x, y, clicks=1, button="left"):
        self.clicks.append((x, y, clicks, button))

    def moveTo(self, x, y, duration=0.5):
        self.moves.append((x, y))

    def write(self, text, interval=0.0):
        self.types.append(text)

    def press(self, *a, **k):
        pass

    def hotkey(self, *a, **k):
        pass

    def scroll(self, *a, **k):
        pass

    def dragTo(self, *a, **k):
        pass


class _FakePerception:
    """Returns a scripted sequence of ScreenStates, one per get_current_state."""

    def __init__(self, states):
        self._states = list(states)
        self._i = 0
        self.calls = 0

    def get_current_state(self):
        self.calls += 1
        if self._i < len(self._states):
            state = self._states[self._i]
            self._i += 1
            return state
        return self._states[-1]


class _FakeDefense:
    """Pass-through screen-content defense."""

    def filter_perception_state(self, state):
        return state


class _RaisingController:
    """Controller stub that raises on every action (execution-failure tests)."""

    def __init__(self, gate):
        self.permission_gate = gate

    def click_element(self, *a, **k):
        raise RuntimeError("simulated execution failure")

    def click_by_name(self, *a, **k):
        raise RuntimeError("simulated execution failure")

    def click_at(self, *a, **k):
        raise RuntimeError("simulated execution failure")


# ----------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------


def _win(window_id, title="App", process_name="app.exe", process_id=1):
    return WindowInfo(
        window_id=window_id,
        title=title,
        process_name=process_name,
        process_id=process_id,
        bounding_box={"x": 0, "y": 0, "width": 800, "height": 600},
        is_active=False,
        is_visible=True,
    )


def _button_element(bb=None, element_id="target-1"):
    return UIElement(
        element_id=element_id,
        name="OK",
        control_type="Button",
        bounding_box=bb or {"x": 100, "y": 100, "width": 60, "height": 30},
        is_enabled=True,
        is_visible=True,
        value=None,
        automation_id="btn_ok",
        class_name="Button",
    )


def _state(active=None, windows=None, elements=None, focused=None, ts=100.0):
    return ScreenState(
        timestamp=ts,
        active_window=active,
        visible_windows=windows or ([] if active is None else [active]),
        desktop_elements=elements or [],
        focused_element=focused,
    )


def _allowing_gate():
    return PermissionGate(
        threshold=PermissionLevel.SAFE,
        confirm_callback=lambda *a, **k: True,
        dry_run=False,
    )


def _denying_gate():
    return PermissionGate(
        threshold=PermissionLevel.SAFE,
        confirm_callback=lambda *a, **k: False,
        dry_run=False,
    )


def _make_loop(
    initial,
    final,
    gate=None,
    controller=None,
    screen_bounds=(0, 0, 1920, 1080),
):
    """Build a ComputerUseLoop wired to scripted perception + mocked hardware."""
    gate = gate or _allowing_gate()
    if controller is None:
        controller = ComputerController(
            permission_gate=gate,
            screen_bounds_provider=_FixedBoundsProvider(screen_bounds),
        )
    # Unconditionally swap in recording hardware — no real mouse/keyboard.
    controller._pyautogui = _FakePyAutoGUI()
    loop = ComputerUseLoop(
        perception_layer=_FakePerception([initial, final]),
        computer_controller=controller,
        permission_gate=gate,
        screen_defense=_FakeDefense(),
    )
    return loop, controller


# ----------------------------------------------------------------------
# TargetReference identity (sha256, stable, no sensitive data)
# ----------------------------------------------------------------------


def test_target_reference_identity_signature_sha256():
    """Identity signatures use sha256 (64 hex chars), never built-in hash()."""
    ref = TargetReference(
        element_id="e1",
        target_name="OK",
        control_type="Button",
        automation_id="btn_ok",
        class_name="Button",
    )
    sig = ref.identity_signature()
    assert isinstance(sig, str)
    assert len(sig) == 64
    # Deterministic: equal identities produce equal signatures
    ref2 = TargetReference(
        element_id="e1",
        target_name="OK",
        control_type="Button",
        automation_id="btn_ok",
        class_name="Button",
    )
    assert ref.identity_signature() == ref2.identity_signature()
    # Different identity -> different signature
    ref3 = TargetReference(
        element_id="e1",
        target_name="Cancel",
        control_type="Button",
        automation_id="btn_cancel",
        class_name="Button",
    )
    assert ref3.identity_signature() != ref2.identity_signature()


def test_target_reference_to_dict_excludes_values():
    """TargetReference serialization never stores element value/text content."""
    ref = TargetReference(
        element_id="e1",
        target_name="Password field",
        control_type="Edit",
        automation_id="pwd_1",
        class_name="Edit",
        bounding_box={"x": 0, "y": 0, "width": 10, "height": 10},
    )
    d = ref.to_dict()
    payload = json.dumps(d)
    assert "value" not in payload
    assert ref.__dataclass_fields__  # sanity: dataclass present


# ----------------------------------------------------------------------
# Verifier: VERIFIED outcomes
# ----------------------------------------------------------------------


def test_verifier_verified_with_strong_identity_even_when_focus_elsewhere():
    """Strong target identity + window context -> VERIFIED even if focus moves."""
    verifier = create_target_state_verifier()
    ref = TargetReference(
        element_id="target-1",
        target_name="OK",
        control_type="Button",
        automation_id="btn_ok",
        class_name="Button",
        owning_process_id=1,
        owning_window_id="w1",
        owning_window_title="App",
        bounding_box={"x": 100, "y": 100, "width": 60, "height": 30},
    )
    after = _state(
        active=_win("w1"),
        windows=[_win("w1")],
        elements=[_button_element()],
        focused=UIElement(
            element_id="other",
            name="Untitled - Notepad",
            control_type="Edit",
            bounding_box={"x": 0, "y": 0, "width": 400, "height": 300},
            is_enabled=True,
            is_visible=True,
            automation_id="txt_editor",
            class_name="RichEditD2DPT",
        ),
        ts=101.0,
    )
    result = verifier.verify(
        ComputerAction(action_type="click", target_name="OK"),
        ref, after, executed=True,
    )
    assert result.status == ActionVerificationStatus.VERIFIED
    assert result.is_verified is True
    assert result.is_success is True


def test_verifier_partial_identity_with_focus_and_window_verifies():
    """Partial identity + focus on target + owning window present -> VERIFIED."""
    verifier = create_target_state_verifier()
    # Reference knows only automation_id + class_name (partial identity signals)
    ref = TargetReference(
        automation_id="btn_ok",
        class_name="Button",
        owning_process_id=1,
        owning_window_id="w1",
        owning_window_title="App",
        bounding_box={"x": 100, "y": 100, "width": 60, "height": 30},
    )
    located = UIElement(
        element_id="e2",
        name="OK",
        control_type="Button",
        bounding_box={"x": 100, "y": 100, "width": 60, "height": 30},
        is_enabled=True,
        is_visible=True,
        automation_id="btn_ok",
        class_name=None,  # class unknown on candidate -> no hard conflict
    )
    after = _state(
        active=_win("w1"),
        windows=[_win("w1")],
        elements=[located],
        focused=located,
        ts=101.0,
    )
    result = verifier.verify(
        ComputerAction(action_type="click", target_name="OK"),
        ref, after, executed=True,
    )
    assert result.status == ActionVerificationStatus.VERIFIED


def test_verifier_small_bounding_box_change_still_verified():
    """Minor rectangle shift with preserved identity -> VERIFIED."""
    verifier = create_target_state_verifier()
    ref = TargetReference(
        element_id="target-1",
        target_name="OK",
        control_type="Button",
        automation_id="btn_ok",
        class_name="Button",
        owning_process_id=1,
        owning_window_id="w1",
        owning_window_title="App",
        bounding_box={"x": 100, "y": 100, "width": 60, "height": 30},
    )
    moved = _button_element(bb={"x": 110, "y": 100, "width": 60, "height": 30})
    after = _state(
        active=_win("w1"), windows=[_win("w1")], elements=[moved], ts=101.0,
    )
    result = verifier.verify(
        ComputerAction(action_type="click", target_name="OK"),
        ref, after, executed=True,
    )
    assert result.status == ActionVerificationStatus.VERIFIED


# ----------------------------------------------------------------------
# Verifier: UNKNOWN outcomes
# ----------------------------------------------------------------------


def test_verifier_target_disappears_returns_unknown():
    """Target not re-located + owning window present -> UNKNOWN, never SUCCESS."""
    verifier = create_target_state_verifier()
    ref = TargetReference(
        element_id="target-1",
        target_name="OK",
        control_type="Button",
        automation_id="btn_ok",
        class_name="Button",
        owning_process_id=1,
        owning_window_id="w1",
        owning_window_title="App",
        bounding_box={"x": 100, "y": 100, "width": 60, "height": 30},
    )
    after = _state(active=_win("w1"), windows=[_win("w1")], ts=101.0)
    result = verifier.verify(
        ComputerAction(action_type="click", target_name="OK"),
        ref, after, executed=True,
    )
    assert result.status == ActionVerificationStatus.UNKNOWN
    assert result.is_verified is False
    assert result.is_success is False


def test_verifier_large_relocation_no_focus_returns_unknown():
    """Identity preserved but target displaced far, no focus -> UNKNOWN."""
    verifier = create_target_state_verifier()
    ref = TargetReference(
        element_id="target-1",
        target_name="OK",
        control_type="Button",
        automation_id="btn_ok",
        class_name="Button",
        owning_process_id=1,
        owning_window_id="w1",
        owning_window_title="App",
        bounding_box={"x": 100, "y": 100, "width": 60, "height": 30},
    )
    relocated = _button_element(bb={"x": 1000, "y": 100, "width": 60, "height": 30})
    after = _state(
        active=_win("w1"), windows=[_win("w1")], elements=[relocated], ts=101.0,
    )
    result = verifier.verify(
        ComputerAction(action_type="click", target_name="OK"),
        ref, after, executed=True,
    )
    assert result.status == ActionVerificationStatus.UNKNOWN


def test_verifier_global_focus_alone_never_verifies():
    """Global focus on an unrelated element can never alone produce VERIFIED."""
    verifier = create_target_state_verifier()
    ref = TargetReference(
        target_name="OK",
        control_type="Button",
        automation_id="btn_ok",
        class_name="Button",
        owning_process_id=1,
        owning_window_id="w1",
        owning_window_title="App",
        bounding_box={"x": 100, "y": 100, "width": 60, "height": 30},
    )
    # Target is ABSENT from the desktop; only an unrelated focused element exists.
    unrelated_focused = UIElement(
        element_id="txt_1",
        name="Untitled - Notepad",
        control_type="Edit",
        bounding_box={"x": 0, "y": 0, "width": 400, "height": 300},
        is_enabled=True,
        is_visible=True,
        automation_id="txt_editor",
        class_name="RichEditD2DPT",
    )
    after = _state(
        active=_win("w1"),
        windows=[_win("w1")],
        elements=[],
        focused=unrelated_focused,
        ts=101.0,
    )
    result = verifier.verify(
        ComputerAction(action_type="click", target_name="OK"),
        ref, after, executed=True,
    )
    assert result.status == ActionVerificationStatus.UNKNOWN
    assert result.is_verified is False


def test_verifier_no_reference_is_unknown():
    """Missing pre-action reference -> UNKNOWN (never SUCCESS)."""
    verifier = create_target_state_verifier()
    after = _state(active=_win("w1"), windows=[_win("w1")], ts=101.0)
    result = verifier.verify(
        ComputerAction(action_type="click", target_name="OK"),
        None, after, executed=True,
    )
    assert result.status == ActionVerificationStatus.UNKNOWN


# ----------------------------------------------------------------------
# Verifier: FAILED outcomes
# ----------------------------------------------------------------------


def test_verifier_identity_conflict_at_location_fails():
    """A different control occupying the target location -> FAILED."""
    verifier = create_target_state_verifier()
    ref = TargetReference(
        target_name="OK",
        control_type="Button",
        automation_id="btn_ok",
        class_name="Button",
        owning_process_id=1,
        owning_window_id="w1",
        owning_window_title="App",
        bounding_box={"x": 100, "y": 100, "width": 60, "height": 30},
        target_point=(100, 100),
    )
    # A different control sits exactly where the target was.
    imposter = UIElement(
        element_id="other-9",
        name="Untitled - Notepad",
        control_type="Edit",
        bounding_box={"x": 50, "y": 50, "width": 200, "height": 200},
        is_enabled=True,
        is_visible=True,
        automation_id="txt_editor",
        class_name="RichEditD2DPT",
    )
    after = _state(
        active=_win("w1"), windows=[_win("w1")], elements=[imposter], ts=101.0,
    )
    result = verifier.verify(
        ComputerAction(action_type="click", target_name="OK"),
        ref, after, executed=True,
    )
    assert result.status == ActionVerificationStatus.FAILED
    assert result.is_verified is False


def test_verifier_owning_window_changed_fails():
    """Target found but its owning window disappeared -> FAILED."""
    verifier = create_target_state_verifier()
    ref = TargetReference(
        element_id="target-1",
        target_name="OK",
        control_type="Button",
        automation_id="btn_ok",
        class_name="Button",
        owning_process_id=1,
        owning_window_id="w1",
        owning_window_title="App",
        bounding_box={"x": 100, "y": 100, "width": 60, "height": 30},
    )
    w2 = _win("w2", title="Calculator", process_name="calc.exe", process_id=20)
    after = _state(
        active=w2,
        windows=[w2],
        elements=[_button_element()],
        ts=101.0,
    )
    result = verifier.verify(
        ComputerAction(action_type="click", target_name="OK"),
        ref, after, executed=True,
    )
    assert result.status == ActionVerificationStatus.FAILED


def test_verifier_not_executed_is_blocked():
    """executed=False must produce BLOCKED, never VERIFIED."""
    verifier = create_target_state_verifier()
    ref = TargetReference(
        target_name="OK", control_type="Button",
        automation_id="btn_ok", class_name="Button",
    )
    after = _state(active=_win("w1"), windows=[_win("w1")], ts=101.0)
    result = verifier.verify(
        ComputerAction(action_type="click", target_name="OK"),
        ref, after, executed=False,
    )
    assert result.status == ActionVerificationStatus.BLOCKED
    assert result.is_verified is False
    assert result.is_success is False


# ----------------------------------------------------------------------
# Loop integration: VERIFIED target click (no real hardware)
# ----------------------------------------------------------------------


def test_loop_verified_target_click_is_success():
    """A semantically targeted click that stays verified -> SUCCESS."""
    element = _button_element()
    initial = _state(active=_win("w1"), windows=[_win("w1")], elements=[element])
    final = _state(active=_win("w1"), windows=[_win("w1")], elements=[element], ts=101.0)

    loop, controller = _make_loop(initial, final)
    action = ComputerAction(
        action_type="click",
        target_element=element,
        description="click OK",
    )

    result = loop.execute_action(action)
    assert result.success is True
    assert result.phase_reached == LoopPhase.SUCCESS
    assert result.verification is not None
    assert result.verification.status == ActionVerificationStatus.VERIFIED
    assert result.recovery_suggested is False
    # Exactly one physical click at the validated center (150, 150 center of 100..160?)
    # Center of {"x":100,"y":100,"width":60,"height":30} is (130, 115).
    assert controller._pyautogui.clicks == [(130, 115, 1, "left")]


def test_loop_unverified_generic_change_is_not_success():
    """Execution ok but target inconclusive -> UNKNOWN, never auto-success."""
    # Initial state has no active window; reference is coordinates-only.
    initial = _state(ts=100.0)
    # Final state changed generically (new unrelated window + unrelated element),
    # but provides no target-specific evidence for the moved-to point.
    w2 = _win("w2", title="OtherApp", process_name="other.exe", process_id=20)
    unrelated = UIElement(
        element_id="u1",
        name="Sidebar",
        control_type="Text",
        bounding_box={"x": 500, "y": 500, "width": 100, "height": 40},
        is_enabled=True,
        is_visible=True,
    )
    final = _state(active=w2, windows=[w2], elements=[unrelated], ts=101.0)

    loop, controller = _make_loop(initial, final)
    action = ComputerAction(
        action_type="move",
        coordinates=(100, 100),
        description="move cursor",
    )

    result = loop.execute_action(action)
    assert result.success is False
    assert result.phase_reached == LoopPhase.VERIFY
    assert result.verification is not None
    assert result.verification.status == ActionVerificationStatus.UNKNOWN
    assert result.recovery_suggested is True
    # Physical action executed (recorded), but no SUCCESS derived from it.
    assert controller._pyautogui.moves == [(100, 100)]


# ----------------------------------------------------------------------
# Loop integration: FAILED / BLOCKED
# ----------------------------------------------------------------------


def test_loop_execution_exception_fails():
    """An exception during physical execution -> FAILED, not SUCCESS."""
    gate = _allowing_gate()
    loop = ComputerUseLoop(
        perception_layer=_FakePerception([_state(), _state(ts=101.0)]),
        computer_controller=_RaisingController(gate),
        permission_gate=gate,
        screen_defense=_FakeDefense(),
    )
    result = loop.execute_action(
        ComputerAction(action_type="click", target_name="OK", description="click")
    )
    assert result.success is False
    assert result.phase_reached == LoopPhase.FAILED
    assert "simulated execution failure" in result.error
    assert result.verification.status == ActionVerificationStatus.FAILED


def test_loop_permission_denied_blocks_before_execution():
    """Permission denial produces BLOCKED and NO physical action occurs."""
    gate = _denying_gate()
    element = _button_element()
    initial = _state(active=_win("w1"), windows=[_win("w1")], elements=[element])
    loop, controller = _make_loop(initial, _state(ts=101.0), gate=gate)

    result = loop.execute_action(
        ComputerAction(action_type="click", target_element=element, description="click OK")
    )
    assert result.success is False
    assert result.phase_reached == LoopPhase.PERMISSION
    assert result.verification is not None
    assert result.verification.status == ActionVerificationStatus.BLOCKED
    assert result.verification.executed is False
    assert controller._pyautogui.clicks == []


def test_loop_cancelled_blocks():
    """Cancellation produces BLOCKED before any observation or execution."""
    gate = _allowing_gate()
    loop = ComputerUseLoop(
        perception_layer=_FakePerception([_state()]),
        computer_controller=ComputerController(
            permission_gate=gate,
            screen_bounds_provider=_FixedBoundsProvider(),
        ),
        permission_gate=gate,
        screen_defense=_FakeDefense(),
    )
    loop.cancel()
    result = loop.execute_action(
        ComputerAction(action_type="click", description="click")
    )
    assert result.success is False
    assert result.phase_reached == LoopPhase.BLOCKED
    assert result.verification.status == ActionVerificationStatus.BLOCKED
    loop.reset_cancel()


def test_loop_invalid_coordinates_blocked_before_physical_execution():
    """Off-screen coordinates are rejected before any physical movement."""
    gate = _allowing_gate()
    # Controller uses a fix bounds provider; coordinates off the virtual desktop.
    controller = ComputerController(
        permission_gate=gate,
        screen_bounds_provider=_FixedBoundsProvider((0, 0, 1920, 1080)),
    )
    loop, ctrl = _make_loop(_state(), _state(ts=101.0), gate=gate, controller=controller)
    action = ComputerAction(
        action_type="click",
        coordinates=(-10000, -10000),
        description="click far off-screen",
    )
    result = loop.execute_action(action)
    assert result.success is False
    assert result.phase_reached == LoopPhase.EXECUTE
    assert "coordinate" in result.error.lower()
    # No physical action may have reached mocked hardware.
    assert ctrl._pyautogui.clicks == []
    assert ctrl._pyautogui.moves == []


# ----------------------------------------------------------------------
# Sensitive data is never serialized (P1 verification layer)
# ----------------------------------------------------------------------


def test_loop_serialization_excludes_element_value():
    """LoopResult serialization never exposes element value/text content."""
    secret = "supersecret-credential-7f3a"
    element = _button_element()
    element.value = secret  # element carries sensitive screen value
    initial = _state(active=_win("w1"), windows=[_win("w1")], elements=[element])
    final = _state(active=_win("w1"), windows=[_win("w1")], elements=[element], ts=101.0)

    loop, _ = _make_loop(initial, final)
    result = loop.execute_action(
        ComputerAction(action_type="click", target_element=element, description="click OK")
    )
    assert result.success is True
    payload = json.dumps(result.to_dict())
    assert secret not in payload
    # The verification layer's own serialization also excludes it.
    verify_payload = json.dumps(result.verification.to_dict())
    assert secret not in verify_payload


def test_verification_result_to_dict_excludes_located_value():
    """VerificationResult.to_dict serializes the located element WITHOUT value."""
    verifier = create_target_state_verifier()
    secret = "hunter2-never-log-this"
    element = _button_element()
    element.value = secret
    ref = TargetReference(
        target_name="OK",
        control_type="Button",
        automation_id="btn_ok",
        class_name="Button",
        owning_process_id=1,
        owning_window_id="w1",
        owning_window_title="App",
        bounding_box={"x": 100, "y": 100, "width": 60, "height": 30},
    )
    after = _state(active=_win("w1"), windows=[_win("w1")], elements=[element], ts=101.0)
    result = verifier.verify(
        ComputerAction(action_type="click", target_name="OK"),
        ref, after, executed=True,
    )
    assert result.status == ActionVerificationStatus.VERIFIED
    d = result.to_dict()
    payload = json.dumps(d)
    assert secret not in payload
    assert "value" not in payload
    # Located element identity is present but without the sensitive value.
    assert d["located_identity"]["name"] == "OK"


# ----------------------------------------------------------------------
# Phase 6 closed-loop semantics preserved
# ----------------------------------------------------------------------


def test_verification_result_semantics_match_phase6():
    """Executed + VERIFIED -> SUCCESS; executed + UNKNOWN/FAILED -> not SUCCESS."""
    assert VerificationResult(
        ActionVerificationStatus.VERIFIED, 0.9, executed=True
    ).is_success is True
    assert VerificationResult(
        ActionVerificationStatus.UNKNOWN, 0.4, executed=True
    ).is_success is False
    assert VerificationResult(
        ActionVerificationStatus.FAILED, 0.3, executed=True
    ).is_success is False
    # Not executed can never be success, regardless of status.
    assert VerificationResult(
        ActionVerificationStatus.VERIFIED, 0.9, executed=False
    ).is_success is False
    assert VerificationResult(
        ActionVerificationStatus.BLOCKED, 0.0, executed=False
    ).is_success is False


def test_loop_maps_verified_to_success_and_unknown_to_recovery():
    """Loop phase mapping matches Phase 6: VERIFIED -> SUCCESS, UNKNOWN -> VERIFY."""
    element = _button_element()
    initial = _state(active=_win("w1"), windows=[_win("w1")], elements=[element])
    # Verified outcome (identity + window stable after action)
    final_ok = _state(active=_win("w1"), windows=[_win("w1")], elements=[element], ts=101.0)
    loop_ok, _ = _make_loop(initial, final_ok)
    ok = loop_ok.execute_action(
        ComputerAction(action_type="click", target_element=element, description="click OK")
    )
    assert ok.phase_reached == LoopPhase.SUCCESS
    assert ok.verification.is_success is True

    # Target relocated far (but still on-screen) -> UNKNOWN -> VERIFY phase.
    relocated = _button_element(bb={"x": 900, "y": 100, "width": 60, "height": 30})
    final_far = _state(active=_win("w1"), windows=[_win("w1")], elements=[relocated], ts=102.0)
    loop_far, _ = _make_loop(initial, final_far)
    far = loop_far.execute_action(
        ComputerAction(action_type="click", target_element=element, description="click OK")
    )
    assert far.phase_reached == LoopPhase.VERIFY
    assert far.success is False
    assert far.verification.status == ActionVerificationStatus.UNKNOWN
    assert far.recovery_suggested is True  # hand to Phase 6 recovery


# ----------------------------------------------------------------------
# Output contract
# ----------------------------------------------------------------------


def test_loop_result_to_dict_has_verification_and_unknown_never_success():
    """LoopResult.to_dict carries verification; UNKNOWN is never success."""
    element = _button_element()
    initial = _state(active=_win("w1"), windows=[_win("w1")], elements=[element])
    relocated = _button_element(bb={"x": 900, "y": 100, "width": 60, "height": 30})
    final_far = _state(active=_win("w1"), windows=[_win("w1")], elements=[relocated], ts=102.0)
    loop, _ = _make_loop(initial, final_far)
    result = loop.execute_action(
        ComputerAction(action_type="click", target_element=element, description="click OK")
    )
    d = result.to_dict()
    assert d["success"] is False
    assert d["phase_reached"] == "verify"
    assert d["verification"]["status"] == ActionVerificationStatus.UNKNOWN.value
    # UNKNOWN must never be reported as verified.
    assert d["verification"]["is_verified"] is False