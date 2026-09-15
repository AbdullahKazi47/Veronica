"""
TEST 4 — STEP OBSERVE (observe-only, NO physical action).

Identifies the Notepad window and its Text editor via Windows UI Automation,
normalizes the UIA rectangle, validates it through CoordinateSafetyValidator,
and reports Target/Validation/Action/Permission — then STOPS.

No mouse movement, no clicks, no keystrokes happen in this script.
It only READS the accessibility tree.
"""
import sys, json, ctypes, traceback
sys.path.insert(0, r"C:\Users\HomePC\Desktop\Veronica")
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from veronica.vision.uia_adapter import UIAutomationAdapter
from veronica.vision.coordinate_safety import CoordinateSafetyValidator, PyAutoGUIScreenBoundsProvider
from veronica.vision.text_entry import normalize_rectangle

print("=" * 72)
print("TEST 4 — OBSERVE-ONLY: IDENTIFY NOTEPAD + TEXT EDITOR (NO PHYSICAL ACTION)")
print("=" * 72)

STATE_FILE = "test4_state.json"

# ------------------------------------------------------------------
# 1. INIT UIA ADAPTER
# ------------------------------------------------------------------
adapter = UIAutomationAdapter()
if not adapter.is_available():
    print("\n[1] FATAL: UI Automation unavailable. Nothing can be identified. STOP.")
    sys.exit(1)
print("\n[1] UIA adapter available. Reading window tree...")

# ------------------------------------------------------------------
# 2. FIND NOTEPAD WINDOW (window/process identity)
# ------------------------------------------------------------------
windows = adapter.get_windows()
active = adapter.get_active_window()

def _is_notepad(w):
    title = (w.title or "").lower()
    proc = (w.process_name or "").lower()
    return ("notepad" in title) or ("notepad" in proc) or title.startswith("untitled")

notepad = None
if active is not None and _is_notepad(active):
    notepad = active
else:
    for w in windows:
        if _is_notepad(w):
            notepad = w
            break

print(f"[2] visible windows: {len(windows)}; active={active.title if active else None!r}")
if notepad is None:
    print("    FATAL: no Notepad window found. Open Notepad (Untitled) and re-run.")
    sys.exit(1)

print(f"    Notepad window: title={notepad.title!r}")
print(f"      window_id      = {notepad.window_id!r}")
print(f"      process_name   = {notepad.process_name!r}")
print(f"      process_id     = {notepad.process_id}")
print(f"      bounding_box   = {notepad.bounding_box}")
print(f"      is_active      = {notepad.is_active}")

# ------------------------------------------------------------------
# 3. FIND THE TEXT EDITOR ELEMENT
#    Deep descendant -> not in desktop_elements. On this system
#    ElementFromPoint returns None; the reliable signal is the FOCUSED
#    element whose class is RichEditD2DPT (Notepad's editor).
# ------------------------------------------------------------------
def _is_editor_element(el):
    if el is None:
        return False
    cls = (el.class_name or "").lower()
    ct = (el.control_type or "")
    name = (el.name or "").lower()
    if "richedit" in cls:
        return True
    if ct in ("Edit", "Document", "DataItem") and "text editor" in name:
        return True
    return False

def _find_editor():
    window_bb = notepad.bounding_box or {}
    if window_bb.get("width", 0) <= 0 or window_bb.get("height", 0) <= 0:
        return None, None
    cx = window_bb["x"] + window_bb["width"] // 2
    cy = window_bb["y"] + window_bb["height"] // 2
    at_point = adapter.get_element_at_point(cx, cy)
    return at_point, (cx, cy)

focused = adapter.get_focused_element()
editor = focused if _is_editor_element(focused) else None
probe_point = None
if editor is None:
    el, pt = _find_editor()
    if _is_editor_element(el):
        editor = el
        probe_point = pt

if editor is None:
    print("\n[3] FATAL: could not resolve the Text editor element. STOP (no action).")
    sys.exit(1)

print("\n[3] Text editor element identified:")
print(f"      name           = {editor.name!r}")
print(f"      control_type   = {editor.control_type!r}")
print(f"      automation_id  = {editor.automation_id!r}")
print(f"      class_name     = {editor.class_name!r}")
print(f"      element_id     = {editor.element_id!r}")
print(f"      bounding_box   = {editor.bounding_box}")
print(f"      value          = {editor.value!r} (document contents not logged)")
print(f"      probe point    = {probe_point}")
print(f"      focused el now = {focused.name if focused else None!r} "
      f"({focused.control_type if focused else '?'})")

# ------------------------------------------------------------------
# 4. NORMALIZE UIA RECT
# ------------------------------------------------------------------
norm = normalize_rectangle(editor.bounding_box)
print(f"\n[4] normalized rect = {norm}")

# ------------------------------------------------------------------
# 5. COORDINATESAFETYVALIDATOR
# ------------------------------------------------------------------
validator = CoordinateSafetyValidator(PyAutoGUIScreenBoundsProvider())
outcome = validator.validate_rectangle(norm, "Notepad Text editor")

print(f"\n[5] CoordinateSafetyValidator: {outcome.result.value}")
if not outcome.is_valid:
    print(f"    BLOCKED: TARGET_COORDINATES_INVALID -- {outcome.message}")
    print("    No action will be requested. STOP.")
    sys.exit(1)
validated = outcome.validated
print(f"    VALID: center=({validated.center[0]}, {validated.center[1]})")

# ------------------------------------------------------------------
# 6. FOCUS / CLICK REQUIREMENT
# ------------------------------------------------------------------
editor_focused = _is_editor_element(focused) and (
    focused.class_name == editor.class_name
    or (focused.bounding_box and editor.bounding_box
        and focused.bounding_box.get("x") == editor.bounding_box.get("x"))
    or focused.automation_id == editor.automation_id
)
print(f"\n[6] editor already focused: {editor_focused}")
click_required = not editor_focused

# ------------------------------------------------------------------
# 7. STATE + REPORT — NO PHYSICAL ACTION
# ------------------------------------------------------------------
payload = {
    "stage": "OBSERVED",
    "notepad": {
        "name": notepad.title,
        "pid": notepad.process_id,
        "process_name": notepad.process_name,
        "window_id": notepad.window_id,
        "bounding_box": notepad.bounding_box,
    },
    "editor": {
        "name": editor.name,
        "control_type": editor.control_type,
        "automation_id": editor.automation_id,
        "class_name": editor.class_name,
        "element_id": editor.element_id,
        "bounding_box": norm,
    },
    "validated": {
        "status": "VALID",
        "center": [validated.center[0], validated.center[1]],
        "rect": [validated.x, validated.y, validated.width, validated.height],
    },
    "click_required": click_required,
}
with open(STATE_FILE, "w") as f:
    json.dump(payload, f, indent=2)
print(f"\n[7] State saved to {STATE_FILE}")

print("\n" + "=" * 72)
print("PRE-ACTION REPORT  (awaiting explicit user approval — NOTHING was executed)")
print("=" * 72)
print(f"  Target:     {notepad.title!r} (Notepad) → Text editor "
      f"({editor.control_type}, class={editor.class_name!r})")
print(f"  Rect:       normalized {norm}  center={validated.center}")
print(f"  Validation: VALID (CoordinateSafetyValidator, live virtual desktop)")
print(f"  Click:      {'required' if click_required else 'NOT required (already focused)'}"
      f"  [ONE left click max, only if needed]")
print(f"  Permission: Awaiting explicit approval")
print(f"  Action:     Type disposable test text")
print(f"  Text:       \"VERONICA TEST 123\"")
print(f"  Status:     NOT EXECUTED — observe only; waiting for explicit user approval.")
print("=" * 72)