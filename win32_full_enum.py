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
    except:
        pass
    return True

win32gui.EnumWindows(enum_callback, None)
print(f'Total windows (all): {len(all_windows)}')
for w in all_windows:
    marker = ' <-- NOTEPAD' if 'notepad' in w['title'].lower() or 'notepad' in w['class'].lower() else ''
    print(f'  HWND={w["hwnd"]} PID={w["pid"]} Title="{w["title"][:60]}" Class="{w["class"]}" Vis={w["visible"]} Min={w["minimized"]} Max={w["maximized"]}{marker}')