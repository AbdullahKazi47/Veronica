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
        try:
            visible = win32gui.IsWindowVisible(hwnd)
        except Exception as e:
            visible = f"ERROR: {e}"
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
            'visible': f"ERROR: {e}"
        })
    return True

win32gui.EnumWindows(enum_callback, None)
print(f'Total windows (all): {len(all_windows)}')

# Check visible status
for w in all_windows[:20]:
    print(f'  HWND={w["hwnd"]} PID={w["pid"]} Title="{w["title"][:50]}" Class="{w["class"]}" Vis={w["visible"]} Min={w["minimized"]} Max={w["maximized"]}')

# Count actual True visible
true_visible = [w for w in all_windows if w.get('visible') is True]
print(f'\nTrue visible count: {len(true_visible)}')
for w in true_visible:
    print(f'  HWND={w["hwnd"]} PID={w["pid"]} Title="{w["title"][:50]}" Class="{w["class"]}"')

# Check for Notepad specifically
print('\nSearching for Notepad...')
for w in all_windows:
    title_lower = w['title'].lower()
    class_lower = w['class'].lower()
    if 'notepad' in title_lower or 'notepad' in class_lower:
        print(f'  FOUND: HWND={w["hwnd"]} PID={w["pid"]} Title="{w["title"]}" Class="{w["class"]}" Vis={w["visible"]}')

# Also check process list for notepad
import psutil
print('\nProcess list check for notepad:')
for proc in psutil.process_iter(['pid', 'name', 'exe', 'username']):
    try:
        if 'notepad' in proc.info['name'].lower():
            print(f'  PID={proc.info["pid"]} Name={proc.info["name"]} Exe={proc.info["exe"]} User={proc.info["username"]}')
    except:
        pass