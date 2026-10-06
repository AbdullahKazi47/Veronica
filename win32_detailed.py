#!/usr/bin/env python
# -*- coding: utf-8 -*-
import win32gui
import win32process

all_windows = []
def enum_callback(hwnd, _):
    try:
        title = win32gui.GetWindowText(hwnd)
        class_name = win32gui.GetClassName(hwnd)
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        rect = win32gui.GetWindowRect(hwnd)
        is_minimized = win32gui.IsIconic(hwnd)
        is_maximized = win32gui.IsZoomed(hwnd)
        visible = win32gui.IsWindowVisible(hwnd)
        all_windows.append({
            'hwnd': hwnd,
            'title': title,
            'class': class_name,
            'pid': pid,
            'rect': rect,
            'minimized': is_minimized,
            'maximized': is_maximized,
            'visible': visible
        })
    except Exception as e:
        all_windows.append({
            'hwnd': hwnd,
            'title': 'ERROR',
            'class': 'ERROR',
            'pid': 0,
            'rect': (0,0,0,0),
            'minimized': False,
            'maximized': False,
            'visible': False,
            'error': str(e)
        })
    return True

win32gui.EnumWindows(enum_callback, None)
print(f'Total windows (all): {len(all_windows)}')

# Filter visible
visible_windows = [w for w in all_windows if w.get('visible', False)]
print(f'Visible windows: {len(visible_windows)}')

# Look for notepad
notepad_windows = [w for w in all_windows if 'notepad' in w.get('title', '').lower() or 'notepad' in w.get('class', '').lower()]
print(f'Notepad windows (all): {len(notepad_windows)}')
for w in notepad_windows:
    print(f'  HWND={w["hwnd"]} PID={w["pid"]} Title="{w["title"]}" Class="{w["class"]}" Vis={w.get("visible")} Min={w.get("minimized")} Max={w.get("maximized")} Rect={w.get("rect")}')

# Print all visible windows
print('\nAll visible windows:')
for w in visible_windows:
    marker = ' <-- NOTEPAD' if 'notepad' in w['title'].lower() or 'notepad' in w['class'].lower() else ''
    print(f'  HWND={w["hwnd"]} PID={w["pid"]} Title="{w["title"][:60]}" Class="{w["class"]}" Min={w["minimized"]} Max={w["maximized"]}{marker}')

# Also check: is the Notepad window owned by a different process that we can't see?
# Let's check process names
print('\nProcess names for all windows:')
proc_names = {}
for w in all_windows:
    pid = w.get('pid', 0)
    if pid not in proc_names:
        try:
            import psutil
            proc = psutil.Process(pid)
            proc_names[pid] = proc.name()
        except:
            proc_names[pid] = 'unknown'

for w in all_windows:
    if w.get('visible', False) or 'notepad' in w.get('title', '').lower():
        print(f'  HWND={w["hwnd"]} PID={w["pid"]} ({proc_names.get(w["pid"], "?")}) Title="{w["title"][:60]}" Class="{w["class"]}" Vis={w.get("visible")}')