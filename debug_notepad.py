import sys
sys.path.insert(0, r"C:\Users\HomePC\Desktop\Veronica")
from veronica.vision.uia_adapter import UIAutomationAdapter

adapter = UIAutomationAdapter()
print("UIA available:", adapter.is_available())

notepad_title = "Untitled - Notepad"
windows = adapter.get_windows()
notepad_win = None
for w in windows:
    if w.title == notepad_title:
        notepad_win = w
        break
if notepad_win:
    print("Found Notepad window:", notepad_win.title, "pid", notepad_win.process_id, "bb", notepad_win.bounding_box)
    # Try to get the UIA element for this window via automation ID? Not directly.
    # Instead, get desktop elements and see if any element's bounding box matches the window and is a Window control type.
    elements = adapter.get_desktop_elements()
    print("Total desktop elements:", len(elements))
    window_elems = [e for e in elements if e.control_type.lower() == "window" and e.name == notepad_title]
    print("Matching window elements by name/control:", len(window_elems))
    for we in window_elems:
        print("  Window element:", we.name, we.control_type, we.bounding_box, we.automation_id)
        # Now walk its children recursively
        def walk(el, depth=0):
            indent = "  " * depth
            print(f"{indent}{el.name} ({el.control_type}) bb={el.bounding_box} auto_id={el.automation_id} value={getattr(el, 'value', None)!r}")
            for child in (el.children or []):
                walk(child, depth+1)
        walk(we)
else:
    print("Notepad window not found")
    for w in windows[:10]:
        print(w.title, w.process_name)