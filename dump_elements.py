import sys
sys.path.insert(0, r"C:\Users\HomePC\Desktop\Veronica")
from veronica.vision.uia_adapter import UIAutomationAdapter

adapter = UIAutomationAdapter()
elements = adapter.get_desktop_elements()
print(f"Total elements: {len(elements)}")
for i, el in enumerate(elements):
    print(f"[{i}] name={el.name!r} control_type={el.control_type!r} bb={el.bounding_box} auto_id={el.automation_id!r} value={getattr(el, 'value', None)!r}")
    if el.children:
        for j, child in enumerate(el.children):
            print(f"    [{j}] name={child.name!r} control_type={child.control_type!r} bb={child.bounding_box} auto_id={child.automation_id!r} value={getattr(child, 'value', None)!r}")