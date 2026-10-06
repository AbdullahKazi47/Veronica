#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
VERONICA UIA/Notepad Visibility Deep Diagnostic
Read-only inspection — no modifications, no clicks, no typing, no execution.
"""

import sys
import os
sys.path.insert(0, os.getcwd())

def section(title):
    print(f"\n{'='*70}")
    print(f" {title}")
    print(f"{'='*70}")

def subsection(title):
    print(f"\n--- {title} ---")

def main():
    section("1. ENUMERATE TOP-LEVEL WINDOWS (Win32)")

    # Win32 enumeration
    try:
        import win32gui
        import win32process

        print("Win32 API: AVAILABLE")

        all_windows = []
        def enum_callback(hwnd, _):
            if win32gui.IsWindowVisible(hwnd):
                try:
                    title = win32gui.GetWindowText(hwnd)
                    class_name = win32gui.GetClassName(hwnd)
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    rect = win32gui.GetWindowRect(hwnd)
                    is_minimized = win32gui.IsIconic(hwnd)
                    is_maximized = win32gui.IsZoomed(hwnd)
                    all_windows.append({
                        'hwnd': hwnd,
                        'title': title,
                        'class': class_name,
                        'pid': pid,
                        'rect': rect,
                        'minimized': is_minimized,
                        'maximized': is_maximized
                    })
                except Exception as e:
                    pass
            return True

        win32gui.EnumWindows(enum_callback, None)
        print(f"Total visible windows (Win32): {len(all_windows)}")

        # Find Notepad-related
        notepad_windows = [w for w in all_windows
                          if 'notepad' in w['title'].lower()
                          or 'notepad' in w['class'].lower()
                          or w['title'].strip().lower() == 'untitled - notepad'
                          or w['title'].strip().lower() == 'notepad']

        print(f"Notepad-related windows (Win32): {len(notepad_windows)}")
        for w in notepad_windows:
            print(f"  HWND={w['hwnd']} PID={w['pid']} Title=\"{w['title']}\" Class=\"{w['class']}\" Min={w['minimized']} Max={w['maximized']} Rect={w['rect']}")

        # Also print first 20 windows for context
        print(f"\nFirst 20 windows for context:")
        for w in all_windows[:20]:
            marker = " <-- NOTEPAD?" if 'notepad' in w['title'].lower() or 'notepad' in w['class'].lower() else ""
            print(f"  HWND={w['hwnd']} PID={w['pid']} Title=\"{w['title'][:60]}\" Class=\"{w['class']}\" Min={w['minimized']} Max={w['maximized']}{marker}")

        win32_notepad_found = len(notepad_windows) > 0
        win32_notepad_pids = [w['pid'] for w in notepad_windows]

    except Exception as e:
        print(f"Win32 enumeration FAILED: {e}")
        import traceback
        traceback.print_exc()
        win32_notepad_found = False
        win32_notepad_pids = []
        all_windows = []

    section("2. PROCESS CHECK")

    if win32_notepad_pids:
        try:
            import psutil
            for pid in win32_notepad_pids:
                try:
                    proc = psutil.Process(pid)
                    print(f"Notepad PID {pid}: name={proc.name()}, exe={proc.exe()}, cmdline={proc.cmdline()}")
                    print(f"  Session: {proc.sessions() if hasattr(proc, 'sessions') else 'N/A'}")
                    print(f"  Username: {proc.username()}")
                except psutil.NoSuchProcess:
                    print(f"Notepad PID {pid}: PROCESS NO LONGER EXISTS")
                except Exception as e:
                    print(f"Notepad PID {pid}: ERROR - {e}")
        except ImportError:
            print("psutil not available, using tasklist fallback")
            try:
                import subprocess
                for pid in win32_notepad_pids:
                    result = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV"],
                        capture_output=True, text=True, timeout=5
                    )
                    print(f"Tasklist for PID {pid}: {result.stdout}")
            except Exception as e:
                print(f"Tasklist failed: {e}")
    else:
        print("No Notepad PID found via Win32")

    # Check VERONICA process
    try:
        import os
        veronica_pid = os.getpid()
        import psutil
        veronica_proc = psutil.Process(veronica_pid)
        print(f"\nVERONICA process: PID={veronica_pid}, name={veronica_proc.name()}, exe={veronica_proc.exe()}")
        print(f"  Username: {veronica_proc.username()}")
    except Exception as e:
        print(f"VERONICA process info failed: {e}")

    section("3. DESKTOP/SESSION CHECK")

    try:
        import win32api
        import win32ts
        import win32security

        # Get current session ID
        session_id = win32ts.WTSGetActiveConsoleSessionId()
        print(f"Active console session ID: {session_id}")

        # Get window station
        try:
            station = win32api.GetProcessWindowStation()
            print(f"Window station: {station}")
        except:
            print("Window station: unable to get")

        # Get desktop name
        try:
            desktop = win32api.GetThreadDesktop(win32api.GetCurrentThreadId())
            print(f"Desktop handle: {desktop}")
        except:
            print("Desktop: unable to get")

        # Get process session
        if win32_notepad_pids:
            for pid in win32_notepad_pids:
                try:
                    proc_session = win32ts.WTSGetProcessSessionId(pid)
                    print(f"Notepad PID {pid} session: {proc_session}")
                    if proc_session != session_id:
                        print(f"  ** SESSION MISMATCH: Notepad session {proc_session} != VERONICA session {session_id}")
                except Exception as e:
                    print(f"Notepad PID {pid} session check failed: {e}")

        veronica_session = win32ts.WTSGetProcessSessionId(veronica_pid)
        print(f"VERONICA PID {veronica_pid} session: {veronica_session}")

    except Exception as e:
        print(f"Desktop/session check failed: {e}")

    section("4. UIA ROOT/TREE SCOPE CHECK (using existing UIAutomationAdapter)")

    try:
        from veronica.vision.uia_adapter import UIAutomationAdapter
        adapter = UIAutomationAdapter()

        print(f"UIA Adapter available: {adapter.is_available()}")

        if adapter.is_available():
            # Get all windows via UIA
            uia_windows = adapter.get_windows()
            print(f"UIA windows found: {len(uia_windows)}")

            uia_notepad = [w for w in uia_windows
                          if 'notepad' in w.title.lower()
                          or 'notepad' in w.process_name.lower()]
            print(f"Notepad windows via UIA: {len(uia_notepad)}")
            for w in uia_notepad:
                print(f"  ID={w.window_id} Title=\"{w.title}\" Process={w.process_name} PID={w.process_id} BBox={w.bounding_box} Visible={w.is_visible}")

            # Get desktop elements
            desktop_elements = adapter.get_desktop_elements()
            print(f"\nDesktop elements (UIA TreeScope_Children): {len(desktop_elements)}")
            for elem in desktop_elements:
                if 'notepad' in elem.name.lower() or 'notepad' in (elem.class_name or '').lower():
                    print(f"  NOTEPAD DESKTOP ELEMENT: {elem.name} ({elem.control_type}) BBox={elem.bounding_box}")

            # Check active window
            active = adapter.get_active_window()
            print(f"\nActive window (UIA): {active.title if active else 'None'} (PID={active.process_id if active else 'N/A'})")

            # Check if UIA root can see notepad via tree traversal
            uia_notepad_found = len(uia_notepad) > 0
            uia_notepad_pids = [w.process_id for w in uia_notepad]
        else:
            print("UIA Adapter NOT available")
            uia_notepad_found = False
            uia_notepad_pids = []
            uia_windows = []

    except Exception as e:
        print(f"UIA Adapter check FAILED: {e}")
        import traceback
        traceback.print_exc()
        uia_notepad_found = False
        uia_notepad_pids = []
        uia_windows = []

    section("5. NOTEPAD VARIANT CHECK")

    # Check what Notepad executable is running
    if win32_notepad_pids:
        for pid in win32_notepad_pids:
            try:
                import psutil
                proc = psutil.Process(pid)
                exe = proc.exe().lower()
                name = proc.name().lower()
                print(f"PID {pid}: exe={exe}, name={name}")
                if 'notepad' in name or 'notepad' in exe:
                    if 'windowsapps' in exe or 'system32' in exe:
                        print(f"  -> Classic Notepad (system32) or Store Notepad (WindowsApps)")
                    else:
                        print(f"  -> Other Notepad variant")
            except Exception as e:
                print(f"  PID {pid} variant check failed: {e}")
    else:
        print("No Notepad PIDs to check")

    section("6. PERCEPTION COMPARISON")

    try:
        from veronica.vision.perception import PerceptionLayer
        perception = PerceptionLayer()

        print(f"PerceptionLayer available: {perception.is_available()}")
        print(f"Perception sources: {perception.get_perception_sources()}")

        state = perception.get_current_state()
        print(f"\nPerceptionLayer.get_current_state() windows: {len(state.visible_windows)}")

        perception_notepad = [w for w in state.visible_windows
                             if 'notepad' in w.title.lower()
                             or 'notepad' in w.process_name.lower()]
        print(f"Notepad in PerceptionLayer: {len(perception_notepad)}")
        for w in perception_notepad:
            print(f"  ID={w.window_id} Title=\"{w.title}\" Process={w.process_name} PID={w.process_id} BBox={w.bounding_box}")

        # Compare sets
        print("\n--- COMPARISON ---")
        print(f"Win32 windows: {len(all_windows)}")
        print(f"UIA windows: {len(uia_windows)}")
        print(f"Perception windows: {len(state.visible_windows)}")

        win32_titles = set(w['title'] for w in all_windows if w['title'])
        uia_titles = set(w.title for w in uia_windows if w.title)
        perception_titles = set(w.title for w in state.visible_windows if w.title)

        print(f"\nWin32-only titles (not in UIA): {win32_titles - uia_titles}")
        print(f"UIA-only titles (not in Win32): {uia_titles - win32_titles}")
        print(f"Perception-only titles (not in UIA): {perception_titles - uia_titles}")
        print(f"UIA-only titles (not in Perception): {uia_titles - perception_titles}")

        # Check for Notepad in each
        notepad_in_win32 = any('notepad' in t.lower() for t in win32_titles)
        notepad_in_uia = any('notepad' in t.lower() for t in uia_titles)
        notepad_in_perception = any('notepad' in t.lower() for t in perception_titles)

        print(f"\nNotepad in Win32: {notepad_in_win32}")
        print(f"Notepad in UIA: {notepad_in_uia}")
        print(f"Notepad in Perception: {notepad_in_perception}")

    except Exception as e:
        print(f"PerceptionLayer comparison FAILED: {e}")
        import traceback
        traceback.print_exc()

    section("7. SCREEN CAPTURE CROSS-CHECK")

    try:
        from veronica.vision.perception import ScreenCaptureProvider
        screen_provider = ScreenCaptureProvider()

        if screen_provider.is_available():
            print("ScreenCaptureProvider: AVAILABLE")

            # Take screenshot
            screenshot_bytes = screen_provider.take_screenshot()
            if screenshot_bytes:
                print(f"Screenshot captured: {len(screenshot_bytes)} bytes")

                # Use PIL to check if notepad is visually present
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(screenshot_bytes))
                print(f"Screen size: {img.size}")

                # Check if Notepad window title bar is visible in screenshot
                # We'll do a simple OCR check on the screenshot
                try:
                    import pytesseract
                    text = pytesseract.image_to_string(img)
                    notepad_in_ocr = 'notepad' in text.lower() or 'untitled' in text.lower()
                    print(f"Notepad-like text in OCR: {notepad_in_ocr}")
                    if notepad_in_ocr:
                        # Show relevant lines
                        for line in text.split('\n'):
                            if 'notepad' in line.lower() or 'untitled' in line.lower():
                                print(f"  OCR line: {line.strip()}")
                except ImportError:
                    print("pytesseract not available for OCR check")
                except Exception as e:
                    print(f"OCR check failed: {e}")
            else:
                print("Screenshot capture failed")
        else:
            print("ScreenCaptureProvider: NOT AVAILABLE")

    except Exception as e:
        print(f"Screen capture cross-check FAILED: {e}")
        import traceback
        traceback.print_exc()

    section("8. FINAL DIAGNOSIS")

    print("Evidence summary:")
    print(f"  Win32 sees Notepad: {win32_notepad_found}")
    print(f"  UIA sees Notepad: {uia_notepad_found}")
    print(f"  PerceptionLayer sees Notepad: {notepad_in_perception if 'notepad_in_perception' in locals() else 'N/A'}")
    print(f"  Screen capture shows Notepad: {'OCR check done' if 'notepad_in_ocr' in locals() else 'N/A'}")

    if win32_notepad_found and not uia_notepad_found:
        diagnosis = "B — Win32 sees Notepad, UIA does not"
    elif not win32_notepad_found and not uia_notepad_found:
        diagnosis = "A — Notepad is not actually on this desktop/session"
    elif uia_notepad_found and not notepad_in_perception:
        diagnosis = "C — UIA sees Notepad, PerceptionLayer misses it"
    elif uia_notepad_found and notepad_in_perception:
        diagnosis = "G — Other — Notepad visible at all layers but something else is wrong"
    else:
        diagnosis = "G — Other"

    print(f"\nPRIMARY DIAGNOSIS: {diagnosis}")

    section("SAFETY CONFIRMATION")
    print("- No clicks performed")
    print("- No typing performed")
    print("- No application launched")
    print("- No ComputerUseLoop executed")
    print("- No ActionProposal created")
    print("- No authorization created")
    print("- PermissionGate untouched")
    print("- AutonomyPolicy untouched")
    print("- No files modified")
    print("- No safety boundary weakened")

if __name__ == "__main__":
    main()