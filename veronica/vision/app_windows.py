"""
Find / bring forward the window of an installed app — for screen control's
OPEN_APPLICATION, which used to accept only Notepad.

Safety stays where it was: only apps Windows lists in the Start menu (AppIndex) can be
opened — never an executable path, file, or command string — and every action still
passes through the PermissionGate in ComputerUseLoop.
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

_UNSAFE = re.compile(r"[\\/:*?\"<>|&;%$`]|\.(?:exe|bat|cmd|ps1|vbs|lnk|msi|com|scr)\b", re.I)
_GENERIC = {"microsoft", "google", "mozilla", "the", "app", "for", "windows", "desktop", "stable"}
# Window classes of the Windows shell itself (desktop, taskbars) — not app windows.
_SHELL_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def is_safe_app_name(name: str) -> bool:
    """A friendly app name, not a path/command ("notepad", "WhatsApp" — not 'C:\\x.exe')."""
    n = (name or "").strip()
    return bool(n) and len(n) <= 60 and not _UNSAFE.search(n)


def _words(name: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9+]+", (name or "").lower()) if len(w) >= 3 and w not in _GENERIC}


def window_matches(app_name: str, title: str, process: str) -> bool:
    """'Google Chrome' ~ title '... - Google Chrome' or process chrome.exe;
    'Notepad' ~ 'Untitled - Notepad'; 'WhatsApp' ~ 'WhatsApp'."""
    words = _words(app_name)
    if not words:
        return False
    title_l, stem = (title or "").lower(), re.sub(r"\.exe$", "", (process or "").lower())
    return any(w in title_l for w in words) or any(w == stem or w in stem for w in words)


def _list_windows() -> list[tuple[int, str, str]]:
    """Visible top-level windows with a title: (hwnd, title, process exe name)."""
    import psutil
    import win32gui
    import win32process
    out: list[tuple[int, str, str]] = []

    def cb(hwnd, _):
        try:
            # The desktop background and taskbars belong to explorer.exe too; they are
            # never "the File Explorer window" (or any app's window).
            if win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd) not in _SHELL_CLASSES:
                title = win32gui.GetWindowText(hwnd)
                if title:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    try:
                        proc = psutil.Process(pid).name()
                    except Exception:
                        proc = ""
                    out.append((hwnd, title, proc))
        except Exception:
            pass
        return True
    win32gui.EnumWindows(cb, None)
    return out


def _activate(hwnd: int) -> None:
    import win32con
    import win32gui
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)


def find_app_window(app_name: str, list_windows: Optional[Callable[[], list]] = None) -> Optional[tuple[int, str]]:
    for hwnd, title, proc in (list_windows or _list_windows)():
        if window_matches(app_name, title, proc):
            return hwnd, title
    return None


def open_or_activate(app_name: str, index, list_windows: Optional[Callable[[], list]] = None,
                     activate: Optional[Callable[[int], None]] = None, wait_s: float = 8.0,
                     sleep: Callable[[float], None] = time.sleep) -> tuple[bool, str, Optional[int]]:
    """Bring the app's window forward, launching it from the Start menu if needed.
    Returns (ok, message, hwnd)."""
    # Resolved at call time (not as defaults) so the platform functions can be swapped.
    list_windows = list_windows or _list_windows
    activate = activate or _activate
    if not is_safe_app_name(app_name):
        return False, f"'{app_name}' isn't an app name (paths and commands aren't allowed).", None
    entry = index.find_app(app_name)
    if entry is None:
        return False, f"{app_name} isn't an installed Start-menu app.", None
    name = entry["name"]
    found = find_app_window(name, list_windows)
    launched = False
    if found is None:
        index.launch_app(entry)
        launched = True
        deadline = time.monotonic() + wait_s
        while found is None and time.monotonic() < deadline:
            sleep(0.4)
            found = find_app_window(name, list_windows)
    if found is None:
        return False, f"Started {name}, but its window didn't appear.", None
    hwnd, title = found
    try:
        activate(hwnd)
    except Exception as e:  # noqa: BLE001 — Windows may refuse focus changes
        return False, f"Found {name} but couldn't bring it to the front: {e}", hwnd
    return True, f"{'Opened' if launched else 'Switched to'} {name} ({title}).", hwnd
