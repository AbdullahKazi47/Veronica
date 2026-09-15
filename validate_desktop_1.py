"""
VERONICA — Real Desktop Validation Test 1 (OBSERVE-ONLY)

Non-destructive read of the current desktop via VERONICA's own UIA perception
stack. Does NOT type, click, navigate, open/close apps, run shell commands, or
modify any file. Reads UI Automation properties only.
"""
import sys
import time
sys.path.insert(0, r"C:\Users\HomePC\Desktop\Veronica")

from veronica.vision.uia_adapter import UIAutomationAdapter
from veronica.vision.perception import PerceptionLayer

adapter = UIAutomationAdapter()
print(f"[1] UIA adapter initialized: is_available={adapter.is_available()}")
if not adapter.is_available():
    sys.exit("UIA unavailable - cannot observe")

print("\n=== [2] ACTIVE WINDOW (get_active_window / PID-matching) ===")
active = adapter.get_active_window()
if active:
    print(f"  title        : {active.title!r}")
    print(f"  process      : {active.process_name!r}  (pid={active.process_id})")
    print(f"  bounding_box : {active.bounding_box}")
    print(f"  is_active    : {active.is_active}  is_visible: {active.is_visible}")
else:
    print("  None")

print("\n=== [3] WINDOW LIST (get_windows) ===")
windows = adapter.get_windows()
print(f"  total windows visible to UIA: {len(windows)}")
for i, w in enumerate(windows):
    bb = w.bounding_box or {}
    print(f"  [{i}] {w.title!r}  | proc={w.process_name!r} pid={w.process_id} | bb=({bb.get('x')},{bb.get('y')} {bb.get('width')}x{bb.get('height')})")

# Locate Notepad by process name
notepad_window = next((w for w in windows
                       if w.process_name and "notepad" in w.process_name.lower()), None)

print("\n=== [4] NOTEPAD TARGET ===")
if not notepad_window:
    print("  NOT FOUND by process name 'notepad'. Candidates above are authoritative.")
else:
    print(f"  matched window  : {notepad_window.title!r}")
    print(f"  process         : {notepad_window.process_name!r} (pid={notepad_window.process_id})")
    wclass = getattr(notepad_window, "window_class", None) or getattr(notepad_window, "class_name", None)
    print(f"  class           : {wclass!r}")
    print(f"  bounding box    : {notepad_window.bounding_box}")

    print("\n  --- editor element search (control_type = 'edit' under Notepad) ---")
    desktop_elements = adapter.get_desktop_elements()
    print(f"  desktop-level element count: {len(desktop_elements)}")

    # Collect edit controls across the desktop tree (read-only traversal).
    editors = []
    for el in desktop_elements:
        _stack = [el]
        while _stack:
            n = _stack.pop()
            if n.control_type and "edit" in n.control_type.lower():
                editors.append(n)
            _stack.extend(n.children or [])

    # Dedup by bounding box and require a real, visible box
    def box_inside_window(eb, wb):
        if not eb or not wb:
            return False
        ex, ey = eb.get("x", 0), eb.get("y", 0)
        if (eb.get("width", 0) <= 0) or (eb.get("height", 0) <= 0):
            return False
        # element left/top >= window left/top, and overlaps window bounds
        return ex >= wb.get("x", -1e9) - 50 and ey >= wb.get("y", -1e9) - 50 \
           and ex <= wb.get("x", 1e9) + wb.get("width", 1e9) + 50 \
           and ey <= wb.get("y", 1e9) + wb.get("height", 1e9) + 50

    wbb = notepad_window.bounding_box or {}
    seen, unique = set(), []
    for e in editors:
        b = e.bounding_box or {}
        if not box_inside_window(b, wbb):
            continue
        key = (b.get("x"), b.get("y"), b.get("width"), b.get("height"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    unique = unique[:10]
    print(f"  edit elements within Notepad bounds: {len(unique)}")
    for i, e in enumerate(unique):
        b = e.bounding_box or {}
        print(f"    [{i}] name={e.name!r}  type={e.control_type} | "
              f"value={str(e.value)[:40]!r} | auto_id={e.automation_id!r} | "
              f"class={e.class_name!r} | bb=({b.get('x')},{b.get('y')} {b.get('width')}x{b.get('height')}) "
              f"| visible={e.is_visible} enabled={e.is_enabled}")
    print("  NOTE: element is the file view / text surface of Notepad.")

print("\n=== [5] FOCUSED ELEMENT (get_focused_element) ===")
focused = adapter.get_focused_element()
if focused:
    b = focused.bounding_box or {}
    print(f"  name={focused.name!r} type={focused.control_type} "
          f"bb=({b.get('x')},{b.get('y')} {b.get('width')}x{b.get('height')})")
else:
    print("  None")

print("\n=== [6] SEMANTIC PATH (PerceptionLayer.get_current_state) ===")
perception = PerceptionLayer()
state = perception.get_current_state()
print(f"  sources      : {perception.get_perception_sources()}")
if state:
    print(f"  active_window: {state.active_window!r}")
    print(f"  fps/timestamp: {state.timestamp if hasattr(state,'timestamp') else 'n/a'}")
    print(f"  fingerprint  : {str(state.get_fingerprint())[:24]}...")
    print(f"  total elements in semantics: {len(state.desktop_elements)}")
    print(f"  text regions : {len(state.text_regions) if hasattr(state,'text_regions') else 'n/a'}")
else:
    print("  state = None")

print("\n[7] OBSERVE-ONLY: no input was injected (type/click/nav all skipped).")