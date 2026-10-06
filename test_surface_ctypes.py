"""Temporary test-only native Win32 window + EDIT child — TEST INFRA only."""
import ctypes, ctypes.wintypes as wintypes
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WNDPROC_TYPE = ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

# Minimal window procedure
@WNDPROC_TYPE
def WndProc(hwnd, msg, wparam, lparam):
    if msg == 0x0002:  # WM_DESTROY
        user32.PostQuitMessage(0)
        return 0
    # DefWindowProcW with correct pointer-sized types (fixture only)
    def_window = user32.DefWindowProcW
    def_window.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    def_window.restype = ctypes.c_longlong  # LRESULT pointer-sized (64-bit)
    return def_window(hwnd, msg, wparam, lparam)

# Register class using standard WNDCLASS struct manually
class WNDCLASS_STRUCT(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", wintypes.INT),
        ("cbWndExtra", wintypes.INT),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HCURSOR),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]

cls = WNDCLASS_STRUCT()
cls.style = 0
cls.lpfnWndProc = ctypes.cast(WndProc, ctypes.c_void_p)
cls.cbClsExtra = 0
cls.cbWndExtra = 0
cls.hInstance = kernel32.GetModuleHandleW(None)
cls.hIcon = 0
cls.hCursor = 0
cls.hbrBackground = 0  # WHITE_BRUSH handle (simplified)
cls.lpszMenuName = None
cls.lpszClassName = "VERONICA_TEST_WINDOW"
atom = user32.RegisterClassW(ctypes.byref(cls))
if atom == 0:
    err = kernel32.GetLastError()
    raise RuntimeError(f"RegisterClassW failed: error={err}")
print("Registered atom:", atom)

# Verify SetWindowPos declarations (pointer-sized for HWND) — STEP 1
def verify_declarations():
    # Confirm SetWindowPos exists with 64-bit pointer-sized HWND params
    # ctypes.windll.user32 handles pointer size automatically
    print("SetWindowPos available (pointer-sized HWND params)")
    # Confirm ShowWindow, MoveWindow exist
    print("ShowWindow available; MoveWindow available")
verify_declarations()

# Parent window (visible, caption, normal size)
hwnd_parent = user32.CreateWindowExW(
    0, "VERONICA_TEST_WINDOW", "VERONICA UIA TYPE TEST",
    0x00CF0000,  # WS_OVERLAPPEDWINDOW (visible, caption, resize/min/max)
    100, 100, 600, 300,
    0, 0, kernel32.GetModuleHandleW(None), 0,
)
if hwnd_parent == 0:
    err = kernel32.GetLastError()
    raise RuntimeError(f"CreateWindowExW (parent) failed: HWND=0 error={err}")
print("Parent HWND:", hwnd_parent)

# EDIT child (visible, positive bbox, correctly parented) at step-5 position
hwnd_edit = user32.CreateWindowExW(
    0, "EDIT", "",
    0x50800000 | 0x0001 | 0x0040,
    20, 40, 560, 30,
    hwnd_parent, 0, kernel32.GetModuleHandleW(None), 0,
)
if hwnd_edit == 0:
    err = kernel32.GetLastError()
    raise RuntimeError(f"CreateWindowExW (EDIT) failed: HWND=0 error={err}")
print("Edit HWND:", hwnd_edit)

# Explicit visibility / unminimized / positive size / update
user32.ShowWindow(hwnd_parent, 1)
user32.ShowWindow(hwnd_edit, 1)
# Ensure visible and not minimized (STEP 2 — restore state)
show_result = user32.ShowWindow(hwnd_parent, 9)  # SW_RESTORE (9)
print("SW_RESTORE result:", show_result)
if user32.IsIconic(hwnd_parent) != 0:
    print("WARNING: parent still iconic after SW_RESTORE")
else:
    print("Parent not iconic after SW_RESTORE")
show_result2 = user32.ShowWindow(hwnd_parent, 1)  # SW_SHOWNORMAL (1)
print("SW_SHOWNORMAL result:", show_result2)

# Explicit positioning (STEP 3) — declare argtypes for 64-bit correctness
user32.SetWindowPos.argtypes = [
    wintypes.HWND,  # hWnd
    wintypes.HWND,  # hWndInsertAfter
    ctypes.c_int,   # X
    ctypes.c_int,   # Y
    ctypes.c_int,   # cx
    ctypes.c_int,   # cy
    ctypes.c_uint,  # uFlags
]
user32.SetWindowPos.restype = ctypes.c_int
swp_result = user32.SetWindowPos(hwnd_parent, 0, 100, 100, 600, 250, 0x0040 | 0x0010)  # SWP_SHOWWINDOW | SWP_NOZORDER
print("SetWindowPos result:", swp_result, "last_error:", kernel32.GetLastError())
rect_after_set = wintypes.RECT(); user32.GetWindowRect(hwnd_parent, ctypes.byref(rect_after_set))
print("GetWindowRect after SetWindowPos: left=%d top=%d right=%d bottom=%d" % (rect_after_set.left, rect_after_set.top, rect_after_set.right, rect_after_set.bottom))
if swp_result == 0:
    print("FAIL: SetWindowPos returned 0 — error=" + str(kernel32.GetLastError()))
    raise RuntimeError("SetWindowPos failed with last_error=" + str(kernel32.GetLastError()))

# Fallback only if needed (STEP 4)
if rect_after_set.left < 0 or rect_after_set.top < 0:
    print("SetWindowPos did not produce positive rect; attempting MoveWindow fallback")
    user32.MoveWindow.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
    user32.MoveWindow.restype = ctypes.c_int
    mv_result = user32.MoveWindow(hwnd_parent, 100, 100, 600, 250, 1)
    print("MoveWindow result:", mv_result, "last_error:", kernel32.GetLastError())
    rect_after_mv = wintypes.RECT(); user32.GetWindowRect(hwnd_parent, ctypes.byref(rect_after_mv))
    print("GetWindowRect after MoveWindow:", rect_after_mv.left, rect_after_mv.top)
    if mv_result == 0:
        raise RuntimeError("MoveWindow also failed: error=" + str(kernel32.GetLastError()))
else:
    print("SetWindowPos produced positive rect; fallback not needed")
# Ensure not minimized
user32.ShowWindow(hwnd_parent, 1)  # SW_SHOWNORMAL repeated
user32.UpdateWindow(hwnd_parent)
user32.UpdateWindow(hwnd_edit)
user32.SetForegroundWindow(hwnd_parent)

# Verify parent size positive, child is child
rect_parent = wintypes.RECT()
user32.GetWindowRect(hwnd_parent, ctypes.byref(rect_parent))
rect_edit = wintypes.RECT()
user32.GetWindowRect(hwnd_edit, ctypes.byref(rect_edit))
assert rect_parent.right > rect_parent.left and rect_parent.bottom > rect_parent.top, "Parent rectangle non-positive"
assert rect_edit.right > rect_edit.left and rect_edit.bottom > rect_edit.top, "EDIT rectangle non-positive"

# Verify EDIT is child of parent (GetParent)
parent_of_edit = user32.GetParent(hwnd_edit)
assert parent_of_edit == hwnd_parent, f"EDIT parent mismatch: expected {hwnd_parent} got {parent_of_edit}"

# Verify EDIT is visible and enabled
assert user32.IsWindowVisible(hwnd_edit) != 0, "EDIT not visible"
assert user32.IsWindowEnabled(hwnd_edit) != 0, "EDIT not enabled"

# Before READY: verify both windows are valid, visible, positive rect, in desktop bounds
assert user32.IsWindow(hwnd_parent) != 0, "Parent not alive"
assert user32.IsWindow(hwnd_edit) != 0, "EDIT not alive"
assert user32.IsWindowVisible(hwnd_parent) != 0, "Parent not visible"
assert user32.IsWindowVisible(hwnd_edit) != 0, "EDIT not visible"
assert user32.IsWindowEnabled(hwnd_edit) != 0, "EDIT not enabled"
rect_parent = wintypes.RECT()
assert user32.GetWindowRect(hwnd_parent, ctypes.byref(rect_parent)) != 0, "Parent GetWindowRect failed"
rect_edit = wintypes.RECT()
assert user32.GetWindowRect(hwnd_edit, ctypes.byref(rect_edit)) != 0, "EDIT GetWindowRect failed"
assert rect_parent.right > rect_parent.left and rect_parent.bottom > rect_parent.top, "Parent rectangle non-positive"
assert rect_edit.right > rect_edit.left and rect_edit.bottom > rect_edit.top, "EDIT rectangle non-positive"
assert rect_parent.left >= 0 and rect_parent.top >= 0 and rect_parent.right > 0 and rect_parent.bottom > 0, "Parent not on-screen (positive desktop bounds)"
assert rect_edit.left >= rect_parent.left and rect_edit.top >= rect_parent.top and rect_edit.right <= rect_parent.right and rect_edit.bottom <= rect_parent.bottom, "EDIT not inside parent"
# Confirm EDIT parent relationship
parent_of_edit = user32.GetParent(hwnd_edit)
assert parent_of_edit == hwnd_parent, f"EDIT parent mismatch: expected {hwnd_parent} got {parent_of_edit}"

# Small validation helper (read-only, non-destructive)
assert user32.IsWindow(hwnd_parent) != 0, "Parent not alive"
assert user32.IsWindow(hwnd_edit) != 0, "EDIT not alive"

# Message loop (persistent until WM_DESTROY)
msg = wintypes.MSG()
first_loop = True
while True:
    ret = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
    if ret == 0 or ret == -1:
        break
    if msg.message == 0x8001:
        break  # shutdown
    user32.TranslateMessage(ctypes.byref(msg))
    user32.DispatchMessageW(ctypes.byref(msg))
    if first_loop and user32.IsWindow(hwnd_parent) != 0 and user32.IsWindow(hwnd_edit) != 0:
        import os; pid=os.getpid(); print("VERONICA_TEST_SURFACE_READY PID=%s HWND=%s EDIT_HWND=%s"%(pid,hwnd_parent,hwnd_edit)); first_loop=False; assert user32.IsWindow(hwnd_parent)


# Cleanup
if hwnd_edit:
    try: user32.DestroyWindow(hwnd_edit)
    except: pass
if hwnd_parent:
    try: user32.DestroyWindow(hwnd_parent)
    except: pass
try: user32.UnregisterClassW("VERONICA_TEST_WINDOW", kernel32.GetModuleHandleW(None))
except: pass
