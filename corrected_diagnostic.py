#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Corrected diagnostic - win32gui.IsZoomed doesn't exist, use IsIconic for minimized
and there's no IsZoomed equivalent - we'll skip that check.
"""
import win32gui
import win32process

all_windows = []
def enum_callback(hwnd, _):
    try:
        title = win32gui.GetWindowText(hwnd)
        class_name = win32gui.GetClassName(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        rect = win32gui.GetWindowRect(hwnd)

        # Test IsWindowVisible - returns int (1/0)
        visible_int = win32gui.IsWindowVisible(hwnd)
        visible = bool(visible_int)

        # Test IsIconic (minimized)
        minimized = bool(win32gui.IsIconic(hwnd))

        all_windows.append({
            'hwnd': hwnd,
            'title': title,
            'class': class_name,
            'pid': pid,
            'rect': rect,
            'minimized': minimized,
            'visible': visible,
            'visible_int': visible_int
        })
    except Exception as e:
        pass
    return True

win32gui.EnumWindows(enum_callback, None)
print(f'Total windows (all): {len(all_windows)}')

visible_windows = [w for w in all_windows if w.get('visible', False)]
print(f'Visible windows (bool): {len(visible_windows)}')

print('\nAll visible windows:')
for w in visible_windows:
    marker = ' <-- NOTEPAD' if 'notepad' in w['title'].lower() or 'notepad' in w['class'].lower() else ''
    print(f'  HWND={w["hwnd"]} PID={w["pid"]} Title="{w["title"][:60]}" Class="{w["class"]}" Min={w["minimized"]}{marker}')

# Check Notepad specifically
notepad_windows = [w for w in all_windows if 'notepad' in w.get('title', '').lower() or 'notepad' in w.get('class', '').lower()]
print(f'\nNotepad windows: {len(notepad_windows)}')
for w in notepad_windows:
    print(f'  HWND={w["hwnd"]} PID={w["pid"]} Title="{w["title"]}" Class="{w["class"]}" Vis={w["visible"]} Min={w["minimized"]} Rect={w["rect"]}')

# Now run the same check through UIA and PerceptionLayer
print('\n\n=== UIA CHECK ===')
from veronica.vision.uia_adapter import UIAutomationAdapter
adapter = UIAutomationAdapter()
if adapter.is_available():
    uia_windows = adapter.get_windows()
    print(f'UIA windows: {len(uia_windows)}')
    for w in uia_windows:
        if 'notepad' in w.title.lower() or 'notepad' in w.process_name.lower():
            print(f'  NOTEPAD: ID={w.window_id} Title="{w.title}" Process={w.process_name} PID={w.process_id} BBox={w.bounding_box} Visible={w.is_visible}')
else:
    print('UIA not available')

print('\n=== PERCEPTION LAYER CHECK ===')
from veronica.vision.perception import PerceptionLayer
perception = PerceptionLayer()
state = perception.get_current_state()
print(f'Perception windows: {len(state.visible_windows)}')
for w in state.visible_windows:
    if 'notepad' in w.title.lower() or 'notepad' in w.process_name.lower():
        print(f'  NOTEPAD: ID={w.window_id} Title="{w.title}" Process={w.process_name} PID={w.process_id} BBox={w.bounding_box}')

print('\n=== COMPARISON ===')
print(f'Win32 visible windows: {len(visible_windows)}')
print(f'UIA windows: {len(uia_windows) if adapter.is_available() else "N/A"}')
print(f'Perception windows: {len(state.visible_windows)}')
print(f'Notepad in Win32: {len(notepad_windows) > 0}')
print(f'Notepad in UIA: {any("notepad" in w.title.lower() or "notepad" in w.process_name.lower() for w in uia_windows) if adapter.is_available() else "N/A"}')
print(f'Notepad in Perception: {any("notepad" in w.title.lower() or "notepad" in w.process_name.lower() for w in state.visible_windows)}')