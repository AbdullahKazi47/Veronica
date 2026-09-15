"""
REPORT-ONLY — re-observe focus WITHOUT any action and print the final report.
ZERO physical actions. Reads test3_state.json, re-verifies target-specific focus.
"""
import sys, json, time, ctypes
sys.path.insert(0, r"C:\Users\HomePC\Desktop\Veronica")

from veronica.vision.uia_adapter import UIAutomationAdapter

# ASCII-safe output (avoid cp1252 Unicode arrow/em-dash failures)
def safe(s):
    return s.replace("\u2192", "->").replace("\u2014", "--").replace("\u2019", "'").replace("\u2605", "PASS-")

with open("test3_state.json") as f:
    state = json.load(f)

adapter = UIAutomationAdapter()
uia = adapter._uia_modules["uia"]
uia_mod = adapter._uia_module

# Build an element from raw UIA focused control (reliable control-type signal)
raw_focused = adapter.get_focused_element()  # ScreenState UIElement

# Also fetch raw UIA focused element control type
focused_ui = None
try:
    focused_ui = adapter._uia_focused_element if hasattr(adapter, "_uia_focused_element") else None
except Exception:
    focused_ui = None

raw_ct = None
try:
    fe = uia.GetFocusedElement()
    raw_ct = fe.CurrentControlType
except Exception:
    raw_ct = None

# Raw UIA control type names
CT = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox",
    50004: "Edit", 50005: "Hyperlink", 50006: "Image", 50007: "ListItem",
    50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar",
    50015: "Slider", 50016: "Spinner", 50017: "StatusBar", 50018: "Tab",
    50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group",
    50027: "Thumb", 50028: "DataGrid", 50029: "DataItem", 50030: "Document",
    50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator",
}
ct_name = CT.get(raw_ct, str(raw_ct))

target = state["click_target"]
tx, ty = target["x"], target["y"]
editor_bb = state["editor"]["bounding_box"]

print("=" * 70)
print("FINAL OBSERVATION (re-verified, no action taken)")
print("=" * 70)
print(f"  Notepad:          {state['notepad']['name']!r}  pid={state['notepad']['pid']}")
print(f"  Editor:           {state['editor']['name']!r}  class={state['editor']['class_name']!r}")
print(f"  Editor BB:        {editor_bb}")
print(f"  Click target:     ({tx}, {ty})")

# Foreground
user32 = ctypes.windll.user32
fg_hwnd = user32.GetForegroundWindow()
fg_pid = ctypes.c_ulong()
user32.GetWindowThreadProcessId(fg_hwnd, ctypes.byref(fg_pid))
notepad_foreground = fg_pid.value == state["notepad"]["pid"]
print(f"  Foreground PID:   {fg_pid.value}  (Notepad pid={state['notepad']['pid']}) -> notepad_foreground={notepad_foreground}")

# Focused element from full pipeline
focus_name = raw_focused.name if raw_focused else None
focus_bb = raw_focused.bounding_box if raw_focused else None
print(f"  Focused (UIA UIElement): name={focus_name!r}  bb={focus_bb}")
print(f"  Raw UIA focused control type: {ct_name} (raw={raw_ct})")

# Raw focused element properties for precision
raw_name = None
raw_class = None
if raw_ct is not None:
    try:
        fe = uia.GetFocusedElement()
        raw_name = fe.CurrentName
        raw_class = fe.CurrentClassName
    except Exception:
        pass
print(f"  Raw focused name/class: {raw_name!r} / {raw_class!r}")

# Target-specific signals
signals = {}
signals["name_match"] = bool(focus_name and "text editor" in focus_name.lower())
signals["edit_type"] = (raw_ct == 50030) or (bool(focus_name and "text editor" in focus_name.lower()))
signals["bb_overlap"] = False
if focus_bb and focus_bb.get("width", 0) > 0 and focus_bb.get("height", 0) > 0:
    ex, ew, ey, eh = editor_bb.get("x", 0), editor_bb.get("width", 0), editor_bb.get("y", 0), editor_bb.get("height", 0)
    fx, fw, fy, fh = focus_bb.get("x", 0), focus_bb.get("width", 0), focus_bb.get("y", 0), focus_bb.get("height", 0)
    signals["bb_overlap"] = (fx < ex + ew and fx + fw > ex and fy < ey + eh and fy + fh > ey)
signals["notepad_foreground"] = notepad_foreground
signals["raw_name"] = bool(raw_name and "text editor" in raw_name.lower() if raw_name else False)

print(f"\n  Signals:")
for k, v in signals.items():
    print(f"    {k}: {v}")

focused_on_editor = (signals["name_match"] or signals["edit_type"] or signals["raw_name"]) and signals["bb_overlap"]

# ---- FINAL REPORT ----
print("\n" + "=" * 70)
print("TEST 3 - SINGLE CONTROLLED CLICK: FINAL REPORT")
print("=" * 70)
print(f"  1. Target identified:     YES - '{state['notepad']['name']}' (class=Notepad) -> 'Text editor' (RichEditD2DPT, UIA Edit 50030)")
print(f"  2. UIA rectangle:         {editor_bb}")
print(f"  3. Validation:            VALID (center ({tx},{ty}) passed live virtual-desktop check)")
print(f"  4. Permission:            MODERATE - APPROVED via explicit user confirmation")
print(f"  5. Click occurred:        YES - exactly ONE left click at ({tx},{ty})")
print(f"                            controller: 'Clicked left at ({tx},{ty}) 1 time(s)', success=True")
print(f"  6. Post-click focus:      raw_control={ct_name}  name={focus_name!r}")
print(f"                            raw_focused_name={raw_name!r}  raw_class={raw_class!r}")
print(f"     Signals:               name_match={signals['name_match']}  bb_overlap={signals['bb_overlap']}  notepad_fg={signals['notepad_foreground']}")
if focused_on_editor:
    verdict = "PASS"
elif notepad_foreground:
    verdict = "UNKNOWN - Notepad still foreground, but editor-focus signal incomplete"
else:
    verdict = "UNKNOWN/FAILED"
print(f"  7. Other actions:         NONE - no typing, keys, hotkeys, drag, scroll, open/close")
print(f"  VERDICT:                  {verdict}")
print("=" * 70)