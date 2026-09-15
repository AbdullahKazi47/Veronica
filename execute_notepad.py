"""
PHASE B — Execute the approved single click and verify target-specific focus.

Flow: load validated state → re-validate → PermissionGate (explicit approval
recorded from the user) → exactly ONE PyAutoGUI left click → observe →
target-specific verification (Notepad editor focus, not generic screen change).
"""
import sys, json, time, traceback, ctypes
sys.path.insert(0, r"C:\Users\HomePC\Desktop\Veronica")

from veronica.vision.uia_adapter import UIAutomationAdapter
from veronica.vision.coordinate_safety import CoordinateSafetyValidator, PyAutoGUIScreenBoundsProvider
from veronica.vision.computer_controller import ComputerController, create_computer_controller
from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.vision.screen_state import UIElement

APPROVAL_GIVEN = True  # User explicitly approved via Claude session confirmation.
APPROVAL_AUDIT = "Explicit user approval obtained in session: 'Yes — approve the click'"

print("=" * 70)
print("PHASE B — EXECUTE ONE CLICK + TARGET-SPECIFIC VERIFICATION")
print("=" * 70)

# ------------------------------------------------------------------
# 1. LOAD THE VALIDATED STATE FROM PHASE A
# ------------------------------------------------------------------
with open("test3_state.json") as f:
    state = json.load(f)

if state.get("status") != "VALIDATED":
    print(f"    FATAL: state is '{state.get('status')}' not VALIDATED. Blocked.")
    sys.exit(1)

target = state["click_target"]
tx, ty = target["x"], target["y"]
print(f"\n[1] LOADED VALIDATED TARGET: ({tx}, {ty})")
print(f"    Editor: {state['editor']['name']!r} class={state['editor']['class_name']!r}")
print(f"    Notepad: {state['notepad']['name']!r} pid={state['notepad']['pid']}")

# ------------------------------------------------------------------
# 2. RE-VALIDATE COORDINATES (defense in depth — reject if stale/invalid)
# ------------------------------------------------------------------
print("\n[2] RE-VALIDATION (fresh)")
validator = CoordinateSafetyValidator(PyAutoGUIScreenBoundsProvider())
outcome = validator.validate_point(tx, ty, "Notepad Text editor center")
print(f"    Result: {outcome.result.value}")
if not outcome.is_valid:
    print(f"    BLOCKED: re-validation failed ({outcome.result.value}). No click.")
    sys.exit(1)
print(f"    OK: point ({tx}, {ty}) valid against live virtual desktop bounds.")

# ------------------------------------------------------------------
# 3. PERMISSIONGATE — MODERATE via explicit user approval
# ------------------------------------------------------------------
print("\n[3] PERMISSIONGATE (MODERATE)")

def confirmation_channel(description, level):
    """PermissionGate confirmation channel.
    Returns the user's already-obtained explicit decision for THIS action."""
    print(f"    [gate] confirm_callback invoked: level={level.name} desc={description!r}")
    print(f"    [gate] decision: {APPROVAL_GIVEN} ({APPROVAL_AUDIT})")
    return APPROVAL_GIVEN

gate = PermissionGate(threshold=PermissionLevel.MODERATE, confirm_callback=confirmation_channel)
decision = gate.check(
    "computer_use_click",
    PermissionLevel.MODERATE,
    "Single left click at ({}, {}) inside Notepad Text editor".format(tx, ty),
)
print(f"    Decision: allowed={decision.allowed}  reason={decision.reason!r}")

if not decision.allowed:
    print("    BLOCKED: permission not granted. No click.")
    sys.exit(1)

# ------------------------------------------------------------------
# 4. BUILD CONTROLLER WITH THE REAL COORDINATE VALIDATOR + GATE
# ------------------------------------------------------------------
controller = create_computer_controller(
    permission_gate=gate,
    coordinate_validator=validator,
    screen_bounds_provider=PyAutoGUIScreenBoundsProvider(),
)
print(f"\n[4] CONTROLLER available={controller.is_available()}")

if not controller.is_available():
    print("    BLOCKED: computer control unavailable (pyautogui missing).")
    sys.exit(1)

# ------------------------------------------------------------------
# 5. BEFORE-STATE: focused element
# ------------------------------------------------------------------
print("\n[5] BEFORE-STATE (focus)")
adapter = UIAutomationAdapter()
before_focused = adapter.get_focused_element()
before_focus_name = before_focused.name if before_focused else None
before_focus_bb = before_focused.bounding_box if before_focused else None
print(f"    Focused element BEFORE: name={before_focus_name!r} bb={before_focus_bb}")

# ------------------------------------------------------------------
# 6. EXECUTE EXACTLY ONE LEFT CLICK
# ------------------------------------------------------------------
print(f"\n[6] EXECUTE: ONE left click at ({tx}, {ty})")
try:
    result = controller.click_at(
        tx, ty,
        button="left",
        clicks=1,
        permission_level=PermissionLevel.MODERATE,
    )
    print(f"    Controller result: success={result.success}")
    print(f"    Message: {result.message!r}")
except Exception as e:
    print(f"    EXCEPTION: {e}")
    traceback.print_exc()
    sys.exit(1)

click_occurred = result.success
if not click_occurred:
    print("    CLICK DID NOT OCCUR (controller refused).")
    sys.exit(1)

# ------------------------------------------------------------------
# 7. AFTER-STATE: observe + target-specific verification
# ------------------------------------------------------------------
time.sleep(1.5)
print("\n[7] AFTER-STATE (target-specific verification)")

uia = adapter._uia_modules["uia"]
uia_mod = adapter._uia_module

# 7a. Notepad still present?
after_windows = adapter.get_windows()
after_notepad = None
for w in after_windows:
    if w.title and "Notepad" in w.title:
        after_notepad = w
        break
print(f"    Notepad after: present={after_notepad is not None}  title={after_notepad.title if after_notepad else None!r}")

# 7b. Foreground check via Win32
user32 = ctypes.windll.user32
foreground_hwnd = user32.GetForegroundWindow()
fg_pid = ctypes.c_ulong()
user32.GetWindowThreadProcessId(foreground_hwnd, ctypes.byref(fg_pid))
print(f"    Foreground PID after: {fg_pid.value}  (Notepad PID: {state['notepad']['pid']})")
notepad_is_foreground = fg_pid.value == state["notepad"]["pid"]

# 7c. Focused element AFTER — is it the Text editor?
after_focused = adapter.get_focused_element()
after_focus_name = after_focused.name if after_focused else None
after_focus_bb = after_focused.bounding_box if after_focused else None
after_focus_ct = str(after_focused.control_type).lower() if after_focused else None
print(f"    Focused element AFTER: name={after_focus_name!r} bb={after_focus_bb} type={after_focus_ct!r}")

editor_bb = state["editor"]["bounding_box"]  # {x, y, width, height} from re-observed UIA

# 7d. Compute target-specific verification signals
signals = {}

# Signal A: focused element name is the editor
signals["name_match"] = bool(after_focus_name and "text editor" in after_focus_name.lower())

# Signal B: focused element is an Edit control
signals["edit_type"] = bool(after_focus_ct and "edit" in after_focus_ct)

# Signal C: focused element bounding box overlaps the editor rectangle
signals["bb_overlap"] = False
if after_focus_bb and after_focus_bb.get("width", 0) > 0 and after_focus_bb.get("height", 0) > 0:
    fb = after_focus_bb
    ex, ew, ey, eh = editor_bb.get("x", 0), editor_bb.get("width", 0), editor_bb.get("y", 0), editor_bb.get("height", 0)
    fx, fw, fy, fh = fb.get("x", 0), fb.get("width", 0), fb.get("y", 0), fb.get("height", 0)
    signals["bb_overlap"] = (fx < ex + ew and fx + fw > ex and fy < ey + eh and fy + fh > ey)

# Signal D: Notepad is the foreground window (click did not escape)
signals["notepad_foreground"] = notepad_is_foreground

print(f"\n    Verification signals:")
print(f"      A. focused name is 'Text editor':        {signals['name_match']}")
print(f"      B. focused type is Edit:                  {signals['edit_type']}")
print(f"      C. focused BB overlaps editor rect:       {signals['bb_overlap']}")
print(f"      D. Notepad still foreground:              {signals['notepad_foreground']}")

# A click-inside focused the editor if (A) and (C), or (B) and (C).
# We never treat generic 'screen changed' as success — these are editor-specific.
focused_on_editor = (signals["name_match"] or signals["edit_type"]) and signals["bb_overlap"]

# ------------------------------------------------------------------
# 8. REPORT
# ------------------------------------------------------------------
print("\n" + "=" * 70)
print("TEST 3 — SINGLE CONTROLLED CLICK: FINAL REPORT")
print("=" * 70)
print(f"  1. Target identified:       YES — '{state['notepad']['name']}' (class=Notepad) → 'Text editor' (RichEditD2DPT, Edit)")
print(f"  2. UIA rectangle:           {editor_bb}")
print(f"  3. Validation:              VALID (center ({tx},{ty}) within live virtual desktop)")
print(f"  4. Permission:              MODERATE — APPROVED via explicit user confirmation")
print(f"  5. Click occurred:          {'YES — exactly one left click' if click_occurred else 'NO'}")
print(f"  6. Post-click focus:        name={after_focus_name!r}  editor_focused={focused_on_editor}")
print(f"     - notepad foreground:    {signals['notepad_foreground']}")
print(f"     - before focus:          {before_focus_name!r}")
if focused_on_editor:
    verdict = "PASS"
elif notepad_is_foreground:
    verdict = "UNKNOWN — Notepad still foreground but editor-focus signal incomplete"
else:
    verdict = "UNKNOWN/FAILED"
print(f"  7. Other physical actions:  NONE (type/key/hotkey/drag/scroll/open/close: 0)")
print(f"  VERDICT:                    {verdict}")
print("=" * 70)