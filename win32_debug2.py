#!/usr/bin/env python
# -*- coding: utf-8 -*-
import win32gui
import win32process

# Test IsWindowVisible directly
notepad_hwnd = None
all_windows = []

def enum_callback(hwnd, _):
    global notepad_hwnd
    try:
        title = win32gui.GetWindowText(hwnd)
        class_name = win32gui.GetClassName(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        rect = win32gui.GetWindowRect(hwnd)

        # Test IsWindowVisible
        try:
            visible = win32gui.IsWindowVisible(hwnd)
        except Exception as e:
            visible = f"ERROR: {e}"

        # Test IsIconic (minimized)
        try:
            minimized = win32gui.IsIconic(hwnd)
        except Exception as e:
            minimized = f"ERROR: {e}"

        all_windows.append({
            'hwnd': hwnd,
            'title': title,
            'class': class_name,
            'pid': pid,
            'rect': rect,
            'minimized': minimized,
            'visible': visible
        })

        if 'notepad' in title.lower() or 'notepad' in class_name.lower():
            notepad_hwnd = hwnd
            print(f'NOTEPAD CANDIDATE: HWND={hwnd} PID={pid} Title="{title}" Class="{class_name}" Vis={visible} Min={minimized} Rect={rect}')
    except Exception as e:
        pass
    return True

win32gui.EnumWindows(enum_callback, None)
print(f'\nTotal windows: {len(all_windows)}')

# Check specific notepad pid
print('\nChecking all windows for PID 25820:')
for w in all_windows:
    if w['pid'] == 25820:
        print(f'  HWND={w["hwnd"]} Title="{w["title"]}" Class="{w["class"]}" Vis={w["visible"]} Min={w["minimized"]} Rect={w["rect"]}')

# Also check by title
print('\nChecking all windows for "notepad" in title/class:')
for w in all_windows:
    if 'notepad' in w['title'].lower() or 'notepad' in w['class'].lower():
        print(f'  HWND={w["hwnd"]} PID={w["pid"]} Title="{w["title"]}" Class="{w["class"]}" Vis={w["visible"]} Min={w["minimized"]} Rect={w["rect"]}')

# Check visible count
vis_true = [w for w in all_windows if w.get('visible') is True]
print(f'\nWindows with visible=True: {len(vis_true)}')
for w in vis_true[:20]:
    print(f'  HWND={w["hwnd"]} PID={w["pid"]} Title="{w["title"][:50]}" Class="{w["class"]}"')

# Check if the issue is that windows are not visible due to being in a different session/desktop
print('\n--- Testing direct HWND access for Notepad PID 25820 ---')
# We know from UIA that Notepad is at PID 25820, let's see if we can find its HWND
# by enumerating all windows of that process
try:
    import psutil
    proc = psutil.Process(25820)
    print(f'Process: {proc.name()}, exe: {proc.exe()}')

    # Get threads and their window handles
    # We need a different approach - use EnumThreadWindows
    import win32api
    # This is tricky - let's try to find window by walking process threads
    threads = proc.threads()
    print(f'Threads: {len(threads)}')
    for t in threads[:5]:
        print(f'  Thread ID: {t.id}')
except Exception as e:
    print(f'Thread enum failed: {e}')

# Try to find window by process ID using a different method
# EnumWindows should find it if it's on the same desktop
print('\n--- Summary ---')
print(f'UIA found Notepad (from earlier diagnostic): PID=25820, Title="Untitled - Notepad"')
print(f'Win32 EnumWindows found {len(all_windows)} total windows')
print(f'Win32 visible=True count: {len(vis_true)}')
print(f'Notepad found in Win32 enum: {notepad_hwnd is not None}')