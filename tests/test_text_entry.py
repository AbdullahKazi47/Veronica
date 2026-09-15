"""TEST 4 — 17 deterministic mocked tests for text-entry verification.

All tests use fakes/mocks; NO real mouse/keyboard activity during
execution. Each test covers exactly one of the enumerated scenarios.
"""
from __future__ import annotations

import json
import pytest
from typing import Optional, List
from dataclasses import dataclass, field

from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.vision.computer_controller import ComputerController
from veronica.vision.computer_use import ComputerAction, ComputerUseLoop
from veronica.vision.coordinate_safety import ScreenBoundsProvider
from veronica.vision.screen_state import ScreenState, UIElement, WindowInfo
from veronica.vision.target_verification import (
    TargetReference,
    TargetStateVerifier,
    ActionVerificationStatus,
    create_target_state_verifier,
)
from veronica.vision.text_entry import (
    TextStateVerifier,
    TextVerificationResult,
    normalize_rectangle,
    EXPECTED_TEST_TEXT,
)


# ────────────────────────────────────────────────────────────────────────────
# Deterministic test fakes
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class _FakeWindow:
    window_id: str = "w-notepad"
    title: str = "Untitled - Notepad"
    process_name: str = "notepad.exe"
    process_id: int = 101
    bounding_box: dict = field(default_factory=lambda: {"x": 50, "y": 50, "width": 800, "height": 600})
    is_active: bool = True
    is_visible: bool = True
    is_minimized: bool = False
    is_maximized: bool = False


def _editor_element(value: Optional[str] = None,
                    name: str = "Text editor",
                    class_name: str = "RichEditD2DPT",
                    bbox: Optional[dict] = None) -> UIElement:
    return UIElement(
        element_id="ed-1",
        name=name,
        control_type="Edit",
        bounding_box=bbox or {"x": 60, "y": 90, "width": 780, "height": 520},
        is_enabled=True,
        is_visible=True,
        value=value,
        automation_id="15",
        class_name=class_name,
    )


def _make_state(editor: Optional[UIElement] = None,
                focused: Optional[UIElement] = None) -> ScreenState:
    win = _FakeWindow()
    return ScreenState(
        timestamp=100.0,
        active_window=win,
        visible_windows=[win],
        desktop_elements=[editor] if editor else [],
        focused_element=focused,
    )


def _editor_reference(editor: Optional[UIElement] = None) -> TargetReference:
    e = editor or _editor_element()
    return TargetReference(
        element_id=e.element_id,
        target_name=e.name,
        control_type=e.control_type,
        automation_id=e.automation_id,
        class_name=e.class_name,
        owning_process_id=101,
        owning_window_id="w-notepad",
        owning_window_title="Untitled - Notepad",
        bounding_box=dict(e.bounding_box),
        target_point=(450, 350),
    )


class _FixedBoundsProvider(ScreenBoundsProvider):
    def __init__(self, bounds=(0, 0, 1920, 1080)):
        self.bounds = bounds
    def get_virtual_desktop_bounds(self):
        return self.bounds


class _FakePyAutoGUI:
    FAILSAFE = True
    def __init__(self):
        self.clicks: list = []
        self.moves: list = []
        self.types: list = []
        self.presses: list = []
        self.hotkeys: list = []
    def click(self, x, y, clicks=1, button="left"):
        self.clicks.append((x, y, clicks, button))
    def moveTo(self, x, y, duration=0.5):
        self.moves.append((x, y))
    def write(self, text, interval=0.0):
        self.types.append(text)
    def press(self, *a, **k):
        self.presses.append(a)
    def hotkey(self, *a, **k):
        self.hotkeys.append(a)
    def scroll(self, *a, **k):
        pass
    def dragTo(self, *a, **k):
        pass


class _FakePerception:
    def __init__(self, states):
        self._states = list(states)
        self._i = 0
    def get_current_state(self):
        s = self._states[min(self._i, len(self._states)-1)]
        self._i += 1
        return s


class _FakeDefense:
    def filter_perception_state(self, state):
        return state


def _gate(confirm_return: bool = True) -> PermissionGate:
    return PermissionGate(
        threshold=PermissionLevel.SAFE,
        confirm_callback=lambda *a, **k: confirm_return,
        dry_run=False,
    )


def _editor_to_state(editor: UIElement,
                     focused: Optional[UIElement] = None) -> ScreenState:
    """Return a state with editor in desktop_elements for TargetStateVerifier lookup."""
    win = _FakeWindow()
    return ScreenState(
        timestamp=101.0,
        active_window=win,
        visible_windows=[win],
        desktop_elements=[editor],
        focused_element=focused or editor,
    )


# ────────────────────────────────────────────────────────────────────────────
# Test 1: Text-entry permission approved
# ────────────────────────────────────────────────────────────────────────────


def test_text_entry_permission_approved():
    gate = _gate(True)
    ctrl = ComputerController(
        permission_gate=gate,
        screen_bounds_provider=_FixedBoundsProvider(),
    )
    ctrl._pyautogui = _FakePyAutoGUI()
    pre = _make_state(focused=_editor_element())
    post = _editor_to_state(_editor_element(value=EXPECTED_TEST_TEXT))

    loop = ComputerUseLoop(
        perception_layer=_FakePerception([pre, post]),
        computer_controller=ctrl,
        permission_gate=gate,
        screen_defense=_FakeDefense(),
    )
    result = loop.execute_action(
        ComputerAction(
            action_type="type",
            text=EXPECTED_TEST_TEXT,
            target_name="Text editor",
            description="test",
            permission_level=PermissionLevel.MODERATE,
        )
    )
    assert result.success is True
    assert result.verification is not None
    assert result.verification.is_verified is True
    assert ctrl._pyautogui.types == [EXPECTED_TEST_TEXT]


# ────────────────────────────────────────────────────────────────────────────
# Test 2: Text-entry permission denied
# ────────────────────────────────────────────────────────────────────────────


def test_text_entry_permission_denied():
    gate = _gate(False)
    ctrl = ComputerController(
        permission_gate=gate,
        screen_bounds_provider=_FixedBoundsProvider(),
    )
    ctrl._pyautogui = _FakePyAutoGUI()
    pre = _make_state()
    loop = ComputerUseLoop(
        perception_layer=_FakePerception([pre, pre]),
        computer_controller=ctrl,
        permission_gate=gate,
        screen_defense=_FakeDefense(),
    )
    result = loop.execute_action(
        ComputerAction(
            action_type="type",
            text=EXPECTED_TEST_TEXT,
            description="test",
            permission_level=PermissionLevel.MODERATE,
        )
    )
    assert result.success is False
    assert result.phase_reached.value == "permission"
    assert ctrl._pyautogui.types == []  # no physical typing


# ────────────────────────────────────────────────────────────────────────────
# Test 3: Invalid target coordinates block typing
# ────────────────────────────────────────────────────────────────────────────


def test_invalid_target_coordinates_block_typing():
    gate = _gate(True)
    ctrl = ComputerController(
        permission_gate=gate,
        screen_bounds_provider=_FixedBoundsProvider(),
    )
    ctrl._pyautogui = _FakePyAutoGUI()
    editor = _editor_element(bbox={"x": -50000, "y": 0, "width": 10, "height": 10})
    bad_state = ScreenState(
        timestamp=100.0,
        active_window=_FakeWindow(),
        visible_windows=[_FakeWindow()],
        desktop_elements=[editor],
    )
    loop = ComputerUseLoop(
        perception_layer=_FakePerception([bad_state, bad_state]),
        computer_controller=ctrl,
        permission_gate=gate,
        screen_defense=_FakeDefense(),
    )
    result = loop.execute_action(
        ComputerAction(
            action_type="click",
            target_element=editor,
            permission_level=PermissionLevel.MODERATE,
        )
    )
    # Coordinate validation fails before any typing action occurs
    assert result.success is False
    assert ctrl._pyautogui.types == []


# ────────────────────────────────────────────────────────────────────────────
# Test 4: Target disappears before typing → UNKNOWN
# ────────────────────────────────────────────────────────────────────────────


def test_target_disappears_before_typing():
    gate = _gate(True)
    ctrl = ComputerController(
        permission_gate=gate,
        screen_bounds_provider=_FixedBoundsProvider(),
    )
    ctrl._pyautogui = _FakePyAutoGUI()
    pre = _make_state(focused=_editor_element())
    post = ScreenState(timestamp=101.0, active_window=_FakeWindow(), visible_windows=[], desktop_elements=[], focused_element=None)

    loop = ComputerUseLoop(
        perception_layer=_FakePerception([pre, post]),
        computer_controller=ctrl,
        permission_gate=gate,
        screen_defense=_FakeDefense(),
    )
    result = loop.execute_action(
        ComputerAction(
            action_type="type",
            text=EXPECTED_TEST_TEXT,
            description="test",
            permission_level=PermissionLevel.MODERATE,
        )
    )
    assert result.success is False
    assert result.verification is not None
    assert result.verification.status == ActionVerificationStatus.UNKNOWN


# ────────────────────────────────────────────────────────────────────────────
# Test 5: Text successfully verified → SUCCESS
# ────────────────────────────────────────────────────────────────────────────


def test_text_successfully_verified():
    gate = _gate(True)
    ctrl = ComputerController(permission_gate=gate, screen_bounds_provider=_FixedBoundsProvider())
    ctrl._pyautogui = _FakePyAutoGUI()
    editor = _editor_element()
    pre = _make_state(focused=editor)
    post = _editor_to_state(_editor_element(value=EXPECTED_TEST_TEXT))

    verifier = TextStateVerifier()
    ref = _editor_reference(editor)
    action = ComputerAction(action_type="type", text=EXPECTED_TEST_TEXT)
    tv = verifier.verify(action, ref, post, executed=True, expected_text=EXPECTED_TEST_TEXT)

    assert tv.status == "SUCCESS"
    assert tv.exact_match is True
    assert tv.observed_text == EXPECTED_TEST_TEXT


# ────────────────────────────────────────────────────────────────────────────
# Test 6: Text API succeeds but cannot verify → UNKNOWN
# ────────────────────────────────────────────────────────────────────────────


def test_text_api_succeeds_but_cannot_verify():
    gate = _gate(True)
    ctrl = ComputerController(permission_gate=gate, screen_bounds_provider=_FixedBoundsProvider())
    ctrl._pyautogui = _FakePyAutoGUI()
    # Simulate type_text succeeds but after-state editor is empty
    editor = _editor_element(value=None)
    pre = _make_state(focused=_editor_element())
    post = _editor_to_state(editor)

    verifier = TextStateVerifier()
    ref = _editor_reference(_editor_element())
    action = ComputerAction(action_type="type", text=EXPECTED_TEST_TEXT)
    tv = verifier.verify(action, ref, post, executed=True, expected_text=EXPECTED_TEST_TEXT)

    assert tv.status == "FAILED"
    assert tv.exact_match is False
    # to_dict must not leak raw document text in reasons or observed_text
    d = tv.to_dict()
    assert d["observed_text"] is None
    for reason in d["reasons"]:
        assert "value '" not in reason


# ────────────────────────────────────────────────────────────────────────────
# Test 7: Wrong editor contains the text → FAILED
# ────────────────────────────────────────────────────────────────────────────


def test_wrong_editor_contains_text():
    """A different editor (different identity) has the text → FAILED."""
    wrong_editor = UIElement(
        element_id="ed-99",
        name="Other editor",
        control_type="Edit",
        bounding_box={"x": 60, "y": 90, "width": 780, "height": 520},
        is_enabled=True, is_visible=True,
        value=EXPECTED_TEST_TEXT,
        automation_id="different-aut-id",
        class_name="WinFormsEdit",
    )
    gate = _gate(True)
    ctrl = ComputerController(permission_gate=gate, screen_bounds_provider=_FixedBoundsProvider())
    ctrl._pyautogui = _FakePyAutoGUI()
    ref = _editor_reference(_editor_element())
    post = ScreenState(
        timestamp=101.0,
        active_window=_FakeWindow(),
        visible_windows=[_FakeWindow()],
        desktop_elements=[wrong_editor],
        focused_element=wrong_editor,
    )
    verifier = TextStateVerifier()
    action = ComputerAction(action_type="type", text=EXPECTED_TEST_TEXT)
    tv = verifier.verify(action, ref, post, executed=True, expected_text=EXPECTED_TEST_TEXT)

    assert tv.status == "FAILED"
    assert tv.exact_match is False


# ────────────────────────────────────────────────────────────────────────────
# Test 8: Global focus differs but correct editor has expected text → SUCCESS
# ────────────────────────────────────────────────────────────────────────────


def test_global_focus_differs_but_correct_editor_has_text():
    """Focus is on a different element, but the correct editor holds the expected text."""
    editor = _editor_element(value=EXPECTED_TEST_TEXT)
    other_button = UIElement(
        element_id="btn-2", name="Other", control_type="Button",
        bounding_box={"x": 60, "y": 600, "width": 100, "height": 30},
        is_enabled=True, is_visible=True,
    )
    state = ScreenState(
        timestamp=101.0,
        active_window=_FakeWindow(),
        visible_windows=[_FakeWindow()],
        desktop_elements=[editor, other_button],
        focused_element=other_button,  # focus elsewhere
    )
    verifier = TextStateVerifier()
    ref = _editor_reference(editor)
    action = ComputerAction(action_type="type", text=EXPECTED_TEST_TEXT)
    tv = verifier.verify(action, ref, state, executed=True, expected_text=EXPECTED_TEST_TEXT)

    assert tv.status == "SUCCESS"
    assert tv.exact_match is True


# ────────────────────────────────────────────────────────────────────────────
# Test 9: Generic screen change does not imply success
# ────────────────────────────────────────────────────────────────────────────


def test_generic_screen_change_not_success():
    """A screen change where the expected text is NOT present yields FAILED/UNKNOWN, not SUCCESS."""
    editor = _editor_element(value="some other text")  # changed, but wrong text
    state = ScreenState(
        timestamp=101.0,
        active_window=_FakeWindow(),
        visible_windows=[_FakeWindow()],
        desktop_elements=[editor],
        focused_element=editor,
    )
    verifier = TextStateVerifier()
    ref = _editor_reference(editor)
    action = ComputerAction(action_type="type", text=EXPECTED_TEST_TEXT)
    tv = verifier.verify(action, ref, state, executed=True, expected_text=EXPECTED_TEST_TEXT)

    assert tv.status == "FAILED"
    assert tv.exact_match is False


# ────────────────────────────────────────────────────────────────────────────
# Test 10: No clipboard usage during text entry
# ────────────────────────────────────────────────────────────────────────────


def test_no_clipboard_usage():
    """The FakePyAutoGUI records no hotkey calls for clipboard operations."""
    gate = _gate(True)
    ctrl = ComputerController(permission_gate=gate, screen_bounds_provider=_FixedBoundsProvider())
    ctrl._pyautogui = _FakePyAutoGUI()
    pre = _make_state(focused=_editor_element())
    post = _editor_to_state(_editor_element(value=EXPECTED_TEST_TEXT))

    loop = ComputerUseLoop(
        perception_layer=_FakePerception([pre, post]),
        computer_controller=ctrl,
        permission_gate=gate,
        screen_defense=_FakeDefense(),
    )
    loop.execute_action(
        ComputerAction(
            action_type="type",
            text=EXPECTED_TEST_TEXT,
            description="test",
            permission_level=PermissionLevel.MODERATE,
        )
    )
    assert ctrl._pyautogui.hotkeys == []  # no ctrl+c/ctrl+v


# ────────────────────────────────────────────────────────────────────────────
# Test 11: No Enter key press after typing
# ────────────────────────────────────────────────────────────────────────────


def test_no_enter_after_typing():
    gate = _gate(True)
    ctrl = ComputerController(permission_gate=gate, screen_bounds_provider=_FixedBoundsProvider())
    ctrl._pyautogui = _FakePyAutoGUI()
    pre = _make_state(focused=_editor_element())
    post = _editor_to_state(_editor_element(value=EXPECTED_TEST_TEXT))

    loop = ComputerUseLoop(
        perception_layer=_FakePerception([pre, post]),
        computer_controller=ctrl,
        permission_gate=gate,
        screen_defense=_FakeDefense(),
    )
    loop.execute_action(
        ComputerAction(
            action_type="type",
            text=EXPECTED_TEST_TEXT,
            description="test",
            permission_level=PermissionLevel.MODERATE,
        )
    )
    assert ctrl._pyautogui.presses == []  # no press_key calls
    assert ctrl._pyautogui.types == [EXPECTED_TEST_TEXT]  # only the one type call


# ────────────────────────────────────────────────────────────────────────────
# Test 12: No additional key presses (only the one type_text)
# ────────────────────────────────────────────────────────────────────────────


def test_no_additional_key_presses():
    gate = _gate(True)
    ctrl = ComputerController(permission_gate=gate, screen_bounds_provider=_FixedBoundsProvider())
    ctrl._pyautogui = _FakePyAutoGUI()
    pre = _make_state(focused=_editor_element())
    post = _editor_to_state(_editor_element(value=EXPECTED_TEST_TEXT))

    loop = ComputerUseLoop(
        perception_layer=_FakePerception([pre, post]),
        computer_controller=ctrl,
        permission_gate=gate,
        screen_defense=_FakeDefense(),
    )
    loop.execute_action(
        ComputerAction(
            action_type="type",
            text=EXPECTED_TEST_TEXT,
            description="test",
            permission_level=PermissionLevel.MODERATE,
        )
    )
    assert len(ctrl._pyautogui.types) == 1, f"Expected exactly 1 type call, got {len(ctrl._pyautogui.types)}"
    assert ctrl._pyautogui.types[0] == EXPECTED_TEST_TEXT
    assert ctrl._pyautogui.presses == []
    assert ctrl._pyautogui.hotkeys == []


# ────────────────────────────────────────────────────────────────────────────
# Test 13: Sensitive UIA values not logged in serialized output
# ────────────────────────────────────────────────────────────────────────────


def test_sensitive_uia_values_not_logged():
    """Password-like values are never included in the serialized TextVerificationResult."""
    sensitive_editor = UIElement(
        element_id="pwd-1", name="Password", control_type="Edit",
        bounding_box={"x": 60, "y": 90, "width": 780, "height": 520},
        is_enabled=True, is_visible=True,
        value="super_secret_password_123",
        automation_id="pwd-box", class_name="Edit",
    )
    state = ScreenState(
        timestamp=101.0, active_window=_FakeWindow(),
        visible_windows=[_FakeWindow()],
        desktop_elements=[sensitive_editor],
    )
    verifier = TextStateVerifier()
    ref = TargetReference(
        element_id="pwd-1", target_name="Password", control_type="Edit",
        automation_id="pwd-box", class_name="Edit",
        owning_process_id=101, owning_window_id="w-notepad",
        owning_window_title="Untitled - Notepad",
        bounding_box=sensitive_editor.bounding_box,
    )
    action = ComputerAction(action_type="type", text=EXPECTED_TEST_TEXT)
    tv = verifier.verify(action, ref, state, executed=True, expected_text=EXPECTED_TEST_TEXT)

    d = tv.to_dict()
    serialized = json.dumps(d)
    assert "super_secret" not in serialized
    # observed_text is always redacted when it doesn't match the harmless test string
    assert d["observed_text"] is None


# ────────────────────────────────────────────────────────────────────────────
# Test 14: Existing PermissionGate remains mandatory
# ────────────────────────────────────────────────────────────────────────────


def test_permission_gate_remains_mandatory():
    """A TextStateVerifier.verify does not bypass the loop's permission gate."""
    gate = _gate(False)
    ctrl = ComputerController(permission_gate=gate, screen_bounds_provider=_FixedBoundsProvider())
    ctrl._pyautogui = _FakePyAutoGUI()
    pre = _make_state()
    loop = ComputerUseLoop(
        perception_layer=_FakePerception([pre, pre]),
        computer_controller=ctrl,
        permission_gate=gate,
        screen_defense=_FakeDefense(),
    )
    result = loop.execute_action(
        ComputerAction(
            action_type="type", text=EXPECTED_TEST_TEXT,
            description="test", permission_level=PermissionLevel.MODERATE,
        )
    )
    assert result.success is False
    assert result.phase_reached.value == "permission"
    assert ctrl._pyautogui.types == []
    assert loop.permission_gate is ctrl.permission_gate  # shared gate


# ────────────────────────────────────────────────────────────────────────────
# Test 15: Existing CoordinateSafetyValidator remains mandatory
# ────────────────────────────────────────────────────────────────────────────


def test_coordinate_safety_validator_remains_mandatory():
    gate = _gate(True)
    ctrl = ComputerController(permission_gate=gate, screen_bounds_provider=_FixedBoundsProvider())
    ctrl._pyautogui = _FakePyAutoGUI()
    # Element with zero-width bounding box
    bad_el = _editor_element(bbox={"x": 100, "y": 100, "width": 0, "height": 0})
    bad_state = ScreenState(timestamp=100.0, active_window=_FakeWindow(),
                            visible_windows=[_FakeWindow()], desktop_elements=[bad_el])
    loop = ComputerUseLoop(
        perception_layer=_FakePerception([bad_state, bad_state]),
        computer_controller=ctrl, permission_gate=gate, screen_defense=_FakeDefense(),
    )
    result = loop.execute_action(
        ComputerAction(action_type="click", target_element=bad_el,
                       permission_level=PermissionLevel.MODERATE)
    )
    # click_element validates rectangle BEFORE permission; width=0 → INVALID_RECTANGLE
    assert result.success is False
    assert ctrl._pyautogui.types == []


# ────────────────────────────────────────────────────────────────────────────
# Test 16: Existing TargetStateVerifier semantics intact
# ────────────────────────────────────────────────────────────────────────────


def test_target_state_verifier_semantics_intact():
    """P1 verifier's VERIFIED/UNKNOWN/FAILED/BLOCKED semantics are preserved."""
    verifier = create_target_state_verifier()
    ref = TargetReference(
        element_id="e1", target_name="OK", control_type="Button",
        automation_id="btn_ok", class_name="Button",
        owning_process_id=1, owning_window_id="w1",
        owning_window_title="App",
        bounding_box={"x": 100, "y": 100, "width": 60, "height": 30},
    )
    element = UIElement(
        element_id="e1", name="OK", control_type="Button",
        bounding_box={"x": 100, "y": 100, "width": 60, "height": 30},
        is_enabled=True, is_visible=True,
        automation_id="btn_ok", class_name="Button",
    )
    active = WindowInfo(window_id="w1", title="App", process_name="app.exe",
                        process_id=1, bounding_box={"x": 0, "y": 0, "width": 800, "height": 600},
                        is_active=True, is_visible=True)
    state = ScreenState(timestamp=101.0, active_window=active,
                        visible_windows=[active], desktop_elements=[element],
                        focused_element=element)
    r = verifier.verify(ref, ref, state, executed=True)
    assert r.status == ActionVerificationStatus.VERIFIED
    r2 = verifier.verify(ref, None, state, executed=True)
    assert r2.status == ActionVerificationStatus.UNKNOWN


# ────────────────────────────────────────────────────────────────────────────
# Test 17: Phase 6 recovery semantics intact (VERIFIED→SUCCESS, UNKNOWN→recovery_suggested)
# ────────────────────────────────────────────────────────────────────────────


def test_phase_6_recovery_semantics_intact():
    gate = _gate(True)
    ctrl = ComputerController(permission_gate=gate, screen_bounds_provider=_FixedBoundsProvider())
    ctrl._pyautogui = _FakePyAutoGUI()
    editor = _editor_element(value=EXPECTED_TEST_TEXT)
    pre = _make_state(focused=editor)
    post = _editor_to_state(editor)

    loop = ComputerUseLoop(
        perception_layer=_FakePerception([pre, post]),
        computer_controller=ctrl,
        permission_gate=gate,
        screen_defense=_FakeDefense(),
    )
    result = loop.execute_action(
        ComputerAction(
            action_type="type", text=EXPECTED_TEST_TEXT,
            target_name="Text editor",
            description="test", permission_level=PermissionLevel.MODERATE,
        )
    )
    # VERIFIED → success=True, phase=SUCCESS, recovery_suggested=False
    assert result.success is True
    assert result.phase_reached == "success"
    assert result.recovery_suggested is False
