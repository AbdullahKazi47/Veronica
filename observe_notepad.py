"""
PHASE A — Observe, identify, validate.
No actions performed. Read-only UIA traversal + CoordinateSafetyValidator.
Outputs a JSON state file for Phase B if validation succeeds.
"""
import sys, json, time, traceback
sys.path.insert(0, r"C:\Users\HomePC\Desktop\Veronica")

from veronica.vision.uia_adapter import UIAutomationAdapter
from veronica.vision.coordinate_safety import CoordinateSafetyValidator, PyAutoGUIScreenBoundsProvider, ValidationResult

print("=" * 70)
print("PHASE A — DESKTOP OBSERVATION & VALIDATION")
print("=" * 70)

# ------------------------------------------------------------------
# 1. INITIALIZE UIA
# ------------------------------------------------------------------
adapter = UIAutomationAdapter()
print(f"\n[1] UIA AVAILABLE: {adapter.is_available()}")
if not adapter.is_available():
    print("    FATAL: UIA unavailable. Cannot proceed.")
    sys.exit(1)

uia = adapter._uia_modules["uia"]
uia_mod = adapter._uia_module

# ------------------------------------------------------------------
# 2. IDENTIFY NOTEPAD
# ------------------------------------------------------------------
print("\n[2] IDENTIFYING NOTEPAD")
cond_window = uia.CreatePropertyCondition(
    uia_mod.UIA_ControlTypePropertyId, uia_mod.UIA_WindowControlTypeId
)
all_windows = adapter._root_element.FindAll(uia_mod.TreeScope_Children, cond_window)
print(f"    Top-level windows found: {all_windows.Length}")

notepad_info = None
for i in range(all_windows.Length):
    win = all_windows.GetElement(i)
    name = win.CurrentName or ""
    pid = win.CurrentProcessId
    class_name = win.CurrentClassName or ""
    if "Notepad" in name or "notepad" in name.lower() or class_name == "Notepad":
        rect = win.GetCurrentPropertyValue(uia_mod.UIA_BoundingRectanglePropertyId)
        try:
            bb = {"x": int(rect[0]), "y": int(rect[1]),
                   "width": int(rect[2] - rect[0]), "height": int(rect[3] - rect[1])}
        except:
            bb = None
        notepad_info = {
            "name": name,
            "pid": pid,
            "class_name": class_name,
            "bounding_box": bb,
            "element": win,
        }
        print(f"    FOUND: name={name!r}  pid={pid}  class={class_name!r}  bb={bb}")
        break

if not notepad_info:
    print("    Notepad not found in top-level. Searching all windows...")
    true_cond = uia.CreateTrueCondition()
    all_desc = adapter._root_element.FindAll(uia_mod.TreeScope_Descendants, true_cond)
    for i in range(all_desc.Length):
        el = all_desc.GetElement(i)
        name = el.CurrentName or ""
        class_name = el.CurrentClassName or ""
        if "Notepad" in name and el.CurrentControlType == uia_mod.UIA_WindowControlTypeId:
            rect = el.GetCurrentPropertyValue(uia_mod.UIA_BoundingRectanglePropertyId)
            try:
                bb = {"x": int(rect[0]), "y": int(rect[1]),
                       "width": int(rect[2] - rect[0]), "height": int(rect[3] - rect[1])}
            except:
                bb = None
            notepad_info = {
                "name": name, "pid": el.CurrentProcessId,
                "class_name": class_name, "bounding_box": bb, "element": el,
            }
            print(f"    FOUND (descendant): name={name!r}  pid={el.CurrentProcessId}  class={class_name!r}  bb={bb}")
            break

if not notepad_info:
    print("    NOTEPAD NOT FOUND. Test blocked.")
    state = {"status": "BLOCKED", "reason": "Notepad not found"}
    with open("test3_state.json", "w") as f:
        json.dump(state, f, indent=2)
    sys.exit(1)

# 2b. Cross-validate with Win32
import ctypes
user32 = ctypes.windll.user32
foreground_hwnd = user32.GetForegroundWindow()
foreground_pid = ctypes.c_ulong()
user32.GetWindowThreadProcessId(foreground_hwnd, ctypes.byref(foreground_pid))
print(f"    Win32 foreground PID: {foreground_pid.value}  |  UIA Notepad PID: {notepad_info['pid']}")
if foreground_pid.value == notepad_info['pid']:
    print("    Win32 confirms: Notepad is the foreground window.")
else:
    print(f"    NOTE: Notepad is NOT the foreground window (foreground PID={foreground_pid.value})")

# 2c. Process name check
try:
    kernel32 = ctypes.windll.kernel32
    h_proc = kernel32.OpenProcess(0x1000, False, notepad_info['pid'])
    if h_proc:
        buf = ctypes.create_unicode_buffer(260)
        size = ctypes.c_ulong(260)
        psapi = ctypes.windll.psapi
        psapi.GetModuleBaseNameW(h_proc, None, buf, size)
        proc_name = buf.value
        kernel32.CloseHandle(h_proc)
        print(f"    Process image: {proc_name!r}")
except Exception as e:
    print(f"    Process check failed: {e}")

# ------------------------------------------------------------------
# 3. FIND TEXT EDITOR
# ------------------------------------------------------------------
print("\n[3] FINDING TEXT EDITOR")
edit_element = None
edit_bb = None
edit_name = None
edit_class = None

if notepad_info["element"]:
    notepad_el = notepad_info["element"]
    true_cond = uia.CreateTrueCondition()
    descendants = notepad_el.FindAll(uia_mod.TreeScope_Subtree, true_cond)
    print(f"    Notepad subtree elements: {descendants.Length}")

    for j in range(descendants.Length):
        ch = descendants.GetElement(j)
        ctype = ch.CurrentControlType
        cname = ch.CurrentName or ""
        cclass = ch.CurrentClassName or ""
        if ctype == 50030:
            rect = ch.GetCurrentPropertyValue(uia_mod.UIA_BoundingRectanglePropertyId)
            try:
                ch_bb = {"x": int(rect[0]), "y": int(rect[1]),
                          "width": int(rect[2] - rect[0]), "height": int(rect[3] - rect[1])}
            except:
                ch_bb = None
            print(f"    Edit candidate: name={cname!r}  class={cclass!r}  bb={ch_bb}")
            if "Text editor" in cname or edit_element is None:
                edit_element = ch
                edit_bb = ch_bb
                edit_name = cname
                edit_class = cclass
                if "Text editor" in cname:
                    break

if not edit_element:
    print("    TEXT EDITOR NOT FOUND. Test blocked.")
    state = {"status": "BLOCKED", "reason": "Text editor not found in Notepad subtree"}
    with open("test3_state.json", "w") as f:
        json.dump(state, f, indent=2)
    sys.exit(1)

print(f"    SELECTED EDIT: name={edit_name!r}  class={edit_class!r}  bb={edit_bb}")

# ------------------------------------------------------------------
# 4. COORDINATE VALIDATION
# ------------------------------------------------------------------
print("\n[4] COORDINATE VALIDATION")
validator = CoordinateSafetyValidator(PyAutoGUIScreenBoundsProvider())
outcome = validator.validate_rectangle(edit_bb, "Notepad Text editor")
print(f"    Result: {outcome.result.value}")
print(f"    Message: {outcome.message}")
print(f"    Is valid: {outcome.is_valid}")
if outcome.validated:
    print(f"    Validated rect: x={outcome.validated.x}, y={outcome.validated.y}, "
          f"w={outcome.validated.width}, h={outcome.validated.height}")
    center = outcome.validated.center
    print(f"    Center: ({center[0]}, {center[1]})")

if not outcome.is_valid:
    print(f"\n    VALIDATION FAILED: {outcome.result.value}")
    print(f"    Action: NO CLICK")
    state = {"status": "BLOCKED", "reason": "validation failed", "validation": outcome.to_dict()}
    with open("test3_state.json", "w") as f:
        json.dump(state, f, indent=2)
    sys.exit(1)

# ------------------------------------------------------------------
# 5. SAVE STATE FOR PHASE B
# ------------------------------------------------------------------
click_x, click_y = outcome.validated.center

state = {
    "status": "VALIDATED",
    "notepad": {
        "name": notepad_info["name"],
        "pid": notepad_info["pid"],
        "class_name": notepad_info["class_name"],
    },
    "editor": {
        "name": edit_name,
        "class_name": edit_class,
        "bounding_box": edit_bb,
    },
    "validated_rect": outcome.validated.to_dict(),
    "click_target": {"x": click_x, "y": click_y},
    "validation": outcome.to_dict(),
    "permission_level": "MODERATE",
    "timestamp": time.time(),
}

with open("test3_state.json", "w") as f:
    json.dump(state, f, indent=2)

# ------------------------------------------------------------------
# 6. ACTION PREVIEW
# ------------------------------------------------------------------
print("\n" + "=" * 70)
print("ACTION PREVIEW")
print("=" * 70)
print(f"  Target:       Notepad -> Text editor")
print(f"  Editor name:  {edit_name!r}")
print(f"  Editor class: {edit_class!r}")
print(f"  UIA rect:     {edit_bb}")
print(f"  Validation:   {outcome.result.value}")
print(f"  Center:       ({click_x}, {click_y})")
print(f"  Action:       left click x 1")
print(f"  Permission:   MODERATE")
print(f"  Execution:    AWAITING EXPLICIT USER APPROVAL")
print("=" * 70)
print("\nState saved to test3_state.json -- ready for Phase B.")
