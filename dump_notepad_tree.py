import sys
sys.path.insert(0, r"C:\Users\HomePC\Desktop\Veronica")
from veronica.vision.uia_adapter import UIAutomationAdapter

adapter = UIAutomationAdapter()
uia = adapter._uia_modules["uia"]
uia_mod = adapter._uia_module

# Find the Notepad window element
condition = uia.CreatePropertyCondition(
    uia_mod.UIA_ControlTypePropertyId,
    uia_mod.UIA_WindowControlTypeId
)
all_windows = adapter._root_element.FindAll(uia_mod.TreeScope_Descendants, condition)
notepad_el = None
for i in range(all_windows.Length):
    el = all_windows.GetElement(i)
    if "Untitled - Notepad" in (el.CurrentName or ""):
        notepad_el = el
        break

if not notepad_el:
    print("Notepad window not found")
else:
    print(f"Found: {notepad_el.CurrentName!r} control_type={notepad_el.CurrentControlType}")
    rect = notepad_el.GetCurrentPropertyValue(uia_mod.UIA_BoundingRectanglePropertyId)
    print(f"  bb=({rect[0]},{rect[1]} {rect[2]-rect[0]}x{rect[3]-rect[1]})")

    # Get all descendants of Notepad window
    true_cond = uia.CreateTrueCondition()
    children = notepad_el.FindAll(uia_mod.TreeScope_Subtree, true_cond)
    print(f"  Total descendants: {children.Length}")
    for j in range(min(children.Length, 60)):
        c = children.GetElement(j)
        cname = c.CurrentName
        ctype = c.CurrentControlType
        try:
            cr = c.GetCurrentPropertyValue(uia_mod.UIA_BoundingRectanglePropertyId)
            cbb = {'x': int(cr[0]), 'y': int(cr[1]), 'w': int(cr[2]-cr[0]), 'h': int(cr[3]-cr[1])}
        except:
            cbb = {}
        # Get automation ID
        try:
            aid = c.CurrentAutomationId
        except:
            aid = None
        # Get class name
        try:
            cls = c.CurrentClassName
        except:
            cls = None
        print(f"  [{j}] name={cname!r:30} type={ctype:8} aid={aid!r:20} cls={cls!r:30} bb={cbb}")

        # If this is an Edit control, highlight it
        if ctype == 0xC354:  # UIA_EditControlTypeId
            print(f"    ^^^ THIS IS AN EDIT CONTROL ^^^")