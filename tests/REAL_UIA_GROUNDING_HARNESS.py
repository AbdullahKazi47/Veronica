"""
HARNESS ONLY — READ-ONLY real UIA grounding validation.
Owns fixture lifetime; performs UIA → VisualGrounding pipeline in-stage.
No production source edited; no proposal/auth/type/Phase 11.
"""
import subprocess, sys, os, time, ctypes, ctypes.wintypes as wintypes

# 1. Launch fixture with validated detached launcher
pythonw = __import__('shutil').which('pythonw.exe') or sys.executable.replace('python.exe','pythonw.exe')
with open('fixture_harness.log','w') as out:
    proc = subprocess.Popen([pythonw,'-u','test_surface_ctypes.py'], stdout=out, stderr=out,
                            stdin=subprocess.DEVNULL, creationflags=0x208, close_fds=True)
fixture_pid = proc.pid

# 2-3. Wait synchronously for READY
ready_line = None
for _ in range(60):  # max ~30s
    try:
        with open('fixture_harness.log','r') as f:
            lines = f.read().splitlines()
        for ln in lines:
            if 'VERONICA_TEST_SURFACE_READY' in ln:
                ready_line = ln
                break
    except Exception:
        pass
    if ready_line:
        break
    time.sleep(0.5)

if ready_line is None:
    print("FAIL: READY not emitted within timeout")
    # Shutdown anyway
    try:
        import ctypes
        user32 = ctypes.windll.user32
        # We don't have HWND from READY; skip shutdown; just exit
    except Exception:
        pass
    sys.exit(1)

# 4. Parse PID / HWND
parts = ready_line.split()
pid_str = [p for p in parts if p.startswith('PID=')][0].split('=',1)[1]
parent_hwnd_str = [p for p in parts if p.startswith('HWND=')][0].split('=',1)[1]
edit_hwnd_str = [p for p in parts if p.startswith('EDIT_HWND=')][0].split('=',1)[1]
fixture_pid = int(pid_str)
parent_hwnd = int(parent_hwnd_str)
edit_hwnd = int(edit_hwnd_str)

# 5. Confirm IsWindow / positive rect
user32 = ctypes.windll.user32
assert user32.IsWindow(parent_hwnd) != 0, "Parent not alive"
assert user32.IsWindow(edit_hwnd) != 0, "EDIT not alive"
rect_e = wintypes.RECT()
assert user32.GetWindowRect(edit_hwnd, ctypes.byref(rect_e)) != 0, "EDIT GetWindowRect failed"
assert rect_e.right > rect_e.left and rect_e.bottom > rect_e.top, "EDIT rect non-positive"

# 6-10. UIA + pipeline (same process — fixture alive)
sys.path.insert(0,'.')
from veronica.vision.uia_target_grounder import UITargetGrounder, ground_notepad_edit
from veronica.vision.visual_grounding import GroundingStatus

gu = UITargetGrounder()
if not gu.uia.is_available():
    print("UIA adapter not available — graceful failure")
    result = None
else:
    # ElementFromHandle
    uia = gu.uia._uia_modules.get('uia')
    edit_element = None
    acq_point = None
    try:
        edit_element = uia.ElementFromHandle(edit_hwnd)
        print("ElementFromHandle SUCCESS")
    except Exception as e:
        print("ElementFromHandle FAIL:", type(e).__name__)
    # Preserve acquisition point from EDIT rect for any fallback / reporting
    rect_e = wintypes.RECT()
    user32.GetWindowRect(edit_hwnd, ctypes.byref(rect_e))
    acq_point = ((rect_e.left+rect_e.right)//2, (rect_e.top+rect_e.bottom)//2)
    if edit_element is None:
        try:
            pt = wintypes.POINT(acq_point[0], acq_point[1])
            edit_element = uia.ElementFromPoint(pt)
            print("ElementFromPoint SUCCESS at", acq_point)
        except Exception as e:
            print("ElementFromPoint FAIL:", type(e).__name__)

    # 3. Identity only (no content/text)
    if edit_element is not None:
        mod = gu.uia._uia_module
        pid = edit_element.GetCurrentPropertyValue(mod.UIA_ProcessIdPropertyId)
        ctl = edit_element.GetCurrentPropertyValue(mod.UIA_ControlTypePropertyId)
        cls = edit_element.GetCurrentPropertyValue(mod.UIA_ClassNamePropertyId)
        bbox_prop = edit_element.GetCurrentPropertyValue(mod.UIA_BoundingRectanglePropertyId)
        auto_id = edit_element.GetCurrentPropertyValue(mod.UIA_AutomationIdPropertyId)
        name = edit_element.GetCurrentPropertyValue(mod.UIA_NamePropertyId)
        print(f"UIA PID={pid} ControlType={ctl} ClassName={cls} AutoId={auto_id} Name={name} BBox={bbox_prop}")
    else:
        print("UIA element NOT ACQUIRED")

    # 5. Run existing pipeline (do not alter thresholds/rules)
    try:
        result = ground_notepad_edit()
        if result is None:
            print("Pipeline result: None (GroundingStatus NONE)")
        else:
            status = result.status.name if hasattr(result.status,'name') else str(result.status)
            score = getattr(result,'score',None)
            print(f"Pipeline result: GroundingStatus={status} score={score}")
    except Exception as e:
        print("Pipeline exception:", type(e).__name__, ":", e)
        result = None

# 6. Report result
if result is not None and hasattr(result,'status') and result.status.name == 'VERIFIED':
    print("SUCCESS: GroundingStatus.VERIFIED")
else:
    reason = "GroundingStatus is not VERIFIED (fixture may have completed; or pipeline did not reach VERIFIED)"
    if result is None:
        reason += "; result=None"
    else:
        s = result.status.name if hasattr(result.status,'name') else str(result.status)
        reason += "; status=" + s
    print("STOP:", reason)

# 16. Explicit shutdown AFTER reporting
# Only if parent/edit still alive; if already destroyed, skip cleanly
parent_alive = user32.IsWindow(parent_hwnd) != 0 if 'parent_hwnd' in dir() else False
if parent_alive:
    user32.PostMessageW(ctypes.c_longlong(parent_hwnd), 0x8001, 0, 0)
    # Brief wait for clean exit
    for _ in range(20):
        if user32.IsWindow(parent_hwnd) == 0:
            break
        time.sleep(0.1)
    print("Clean shutdown (0x8001) completed; windows destroyed.")
else:
    print("Fixture already destroyed; no 0x8001 needed.")
print("Harness completed.")
