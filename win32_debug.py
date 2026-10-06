#!/usr/bin/env python
# -*- coding: utf-8 -*-
import win32gui
import win32process
import win32api

print("win32gui version:", win32gui.__version__ if hasattr(win32gui, '__version__') else 'unknown')
print("Platform:", win32api.GetVersionEx() if hasattr(win32api, 'GetVersionEx') else 'unknown')

# Test basic calls
try:
    desktop = win32gui.GetDesktopWindow()
    print(f"GetDesktopWindow: {desktop}")
except Exception as e:
    print(f"GetDesktopWindow failed: {e}")

try:
    progman = win32gui.FindWindow(None, "Program Manager")
    print(f"FindWindow Program Manager: {progman}")
except Exception as e:
    print(f"FindWindow failed: {e}")

# Try EnumWindows with different approach
print("\nTrying EnumWindows...")
count = [0]
def enum_callback(hwnd, _):
    count[0] += 1
    if count[0] <= 5:
        try:
            title = win32gui.GetWindowText(hwnd)
            class_name = win32gui.GetClassName(hwnd)
            visible = win32gui.IsWindowVisible(hwnd)
            print(f"  [{count[0]}] HWND={hwnd} Title=\"{title}\" Class=\"{class_name}\" Vis={visible}")
        except Exception as e:
            print(f"  [{count[0]}] HWND={hwnd} ERROR: {e}")
    return True

result = win32gui.EnumWindows(enum_callback, None)
print(f"EnumWindows returned: {result}")
print(f"Total callbacks: {count[0]}")

# Also try GetTopWindow / GetWindow chain
print("\nTrying GetTopWindow chain...")
try:
    hwnd = win32gui.GetTopWindow(0)
    idx = 0
    while hwnd and idx < 10:
        try:
            title = win32gui.GetWindowText(hwnd)
            class_name = win32gui.GetClassName(hwnd)
            visible = win32gui.IsWindowVisible(hwnd)
            print(f"  [{idx}] HWND={hwnd} Title=\"{title}\" Class=\"{class_name}\" Vis={visible}")
        except Exception as e:
            print(f"  [{idx}] HWND={hwnd} ERROR: {e}")
        hwnd = win32gui.GetWindow(hwnd, win32gui.GW_HWNDNEXT)
        idx += 1
except Exception as e:
    print(f"GetTopWindow chain failed: {e}")