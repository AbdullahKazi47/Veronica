"""
Tab strips as Windows UI Automation sees them — Chrome, Edge, Brave, Firefox, File
Explorer (Windows 11 tabs) and VS Code editor tabs.

Reading is by UI Automation (the TabItem controls), never by guessing from the window
title, so she can name a tab, switch to it and close it. Acting is by UI Automation too
(SelectionItem.Select, the tab's own close button); keyboard shortcuts are only a
fallback after the right window has been brought forward.

Everything that touches Windows goes through an injectable backend, so the logic
(grouping, naming, retrying, falling back) is tested with fake tab strips.

Tab titles are other people's words (web pages): they are reported, never obeyed.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from veronica.logging_setup import get_logger

logger = get_logger("vision.tabs")

# process name (lower case) -> the name she uses for the app
TAB_APPS = {"chrome.exe": "Chrome", "msedge.exe": "Edge", "brave.exe": "Brave", "firefox.exe": "Firefox",
            "explorer.exe": "File Explorer", "code.exe": "VS Code", "code - insiders.exe": "VS Code"}
BROWSERS = ("Chrome", "Edge", "Brave", "Firefox")
# Closing the last tab of these closes the window itself; VS Code's editor tabs don't.
WINDOW_CLOSES_WITH_LAST_TAB = {"Chrome", "Edge", "Brave", "Firefox", "File Explorer"}

_APP_WORDS = [
    (r"google\s+chrome|chrome", "Chrome"), (r"microsoft\s+edge|edge", "Edge"), (r"brave(?:\s+browser)?", "Brave"),
    (r"firefox|fire\s+fox|mozilla(?:\s+firefox)?", "Firefox"),
    (r"(?:windows\s+|file\s+)?explorer|file\s+manager|folders?|file\s+explorer", "File Explorer"),
    (r"(?:visual\s+studio\s+)?code|vs\s*code|vscode", "VS Code"),
]


def canonical_app(name: str) -> Optional[str]:
    """'google chrome' -> 'Chrome', 'explorer' -> 'File Explorer'; None if it isn't a tabbed app.
    'browser' is deliberately None here — it can mean several apps (see Desktop tools)."""
    n = (name or "").strip().lower()
    n = re.sub(r"^(?:the|my)\s+|\s+(?:app|window|windows|browser)$", "", n).strip()
    for pattern, label in _APP_WORDS:
        if re.fullmatch(pattern, n):
            return label
    return None


def app_for_process(proc: str) -> Optional[str]:
    return TAB_APPS.get((proc or "").lower())


_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍‎‏⁠﻿"), None)
_MEMORY = re.compile(r"\s*[-–—,]\s*Memory usage\b.*$", re.I)
_NOISE = re.compile(r"\s*[-–—,]\s*(?:Audio playing|Camera recording|Microphone recording|Sharing your screen|Sleeping)\s*$", re.I)


def clean_title(title: str) -> str:
    """A tab's name without the zero-width marks and 'Memory usage - 120 MB' browsers add."""
    t = (title or "").translate(_ZERO_WIDTH)
    t = _MEMORY.sub("", t)
    t = _NOISE.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


class WindowGone(Exception):
    """The window closed (or never answered) while we were reading it."""


@dataclass
class RawTab:
    title: str
    selected: bool = False
    ref: Any = None          # backend handle (COM element, or anything for a fake)
    close_ref: Any = None    # the tab's own close button, if it has one


@dataclass
class Tab:
    index: int               # 1-based position in the window
    title: str
    selected: bool
    raw: RawTab = field(repr=False, default=None)


@dataclass
class Strip:
    hwnd: int
    app: str
    window_title: str
    tabs: list[Tab] = field(default_factory=list)
    minimized: bool = False
    readable: bool = True     # False = the window couldn't be read at all (not the same as "no tabs")

    @property
    def current(self) -> Optional[Tab]:
        return next((t for t in self.tabs if t.selected), None)


# ------------------------------------------------------------------ the real backend

class UIABackend:
    """comtypes UI Automation. Imported lazily; unit tests never construct this."""

    _TAB, _TAB_ITEM, _BUTTON = 50018, 50019, 50000

    def __init__(self) -> None:
        self._uia = None
        self._lib = None

    def _init(self):
        if self._uia is None:
            import comtypes.client
            comtypes.client.GetModule("UIAutomationCore.dll")
            from comtypes.gen import UIAutomationClient as lib
            self._lib = lib
            self._uia = comtypes.client.CreateObject(lib.CUIAutomation, interface=lib.IUIAutomation)
        return self._uia, self._lib

    @staticmethod
    def _alive(hwnd: int) -> bool:
        import win32gui
        return bool(win32gui.IsWindow(hwnd))

    def _root(self, hwnd: int):
        import ctypes
        uia, _lib = self._init()
        last: Optional[Exception] = None
        for attempt in range(3):
            if not self._alive(hwnd):
                raise WindowGone(str(hwnd))
            try:
                return uia.ElementFromHandle(ctypes.c_void_p(hwnd))
            except Exception as e:  # noqa: BLE001 - UIA throws COMError while a window is busy
                last = e
                time.sleep(0.15 * (attempt + 1))
        raise WindowGone(f"{hwnd}: {last}")

    def read(self, hwnd: int, kind: str) -> list[RawTab]:
        uia, lib = self._init()
        root = self._root(hwnd)
        tab_cond = uia.CreatePropertyCondition(lib.UIA_ControlTypePropertyId, self._TAB)
        item_cond = uia.CreatePropertyCondition(lib.UIA_ControlTypePropertyId, self._TAB_ITEM)
        btn_cond = uia.CreatePropertyCondition(lib.UIA_ControlTypePropertyId, self._BUTTON)
        try:
            groups = root.FindAll(lib.TreeScope_Descendants, tab_cond)
        except Exception as e:  # noqa: BLE001
            if not self._alive(hwnd):
                raise WindowGone(str(hwnd)) from e
            raise
        out: list[RawTab] = []
        for g in range(groups.Length):
            items = groups.GetElement(g).FindAll(lib.TreeScope_Children, item_cond)
            raws: list[RawTab] = []
            for i in range(items.Length):
                item = items.GetElement(i)
                close = None
                btns = item.FindAll(lib.TreeScope_Descendants, btn_cond)
                for b in range(btns.Length):
                    btn = btns.GetElement(b)
                    if "close" in (btn.CurrentName or "").lower():
                        close = btn
                        break
                selected = False
                try:
                    sp = item.GetCurrentPattern(lib.UIA_SelectionItemPatternId)
                    if sp:
                        selected = bool(sp.QueryInterface(lib.IUIAutomationSelectionItemPattern).CurrentIsSelected)
                except Exception:  # noqa: BLE001
                    pass
                raws.append(RawTab(item.CurrentName or "", selected, item, close))
            # A real tab strip has close buttons. This drops the decoy "tabs" inside page
            # content (Explorer's Recent/Favorites, VS Code's side-bar switcher). Firefox
            # tabs are kept either way — its close control isn't always exposed.
            if raws and (kind == "firefox" or any(r.close_ref is not None for r in raws)):
                out.extend(raws)
        return out

    def select(self, hwnd: int, tab: RawTab) -> None:
        _uia, lib = self._init()
        pattern = tab.ref.GetCurrentPattern(lib.UIA_SelectionItemPatternId)
        if not pattern:
            raise RuntimeError("tab can't be selected")
        pattern.QueryInterface(lib.IUIAutomationSelectionItemPattern).Select()

    def close(self, hwnd: int, tab: RawTab) -> None:
        _uia, lib = self._init()
        if tab.close_ref is None:
            raise RuntimeError("tab has no close button")
        pattern = tab.close_ref.GetCurrentPattern(lib.UIA_InvokePatternId)
        if not pattern:
            raise RuntimeError("close button can't be pressed")
        pattern.QueryInterface(lib.IUIAutomationInvokePattern).Invoke()


# ------------------------------------------------------------------ the reader

def _default_windows():
    from veronica.vision.app_windows import _list_windows
    return _list_windows()


def _win_is_minimized(hwnd: int) -> bool:
    import win32gui
    return bool(win32gui.IsWindow(hwnd) and win32gui.IsIconic(hwnd))


def _win_show(hwnd: int, how: str) -> None:
    import win32con
    import win32gui
    if how == "restore":
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    elif how == "minimize":
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    elif how == "focus":
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)


def _win_foreground() -> int:
    import win32gui
    return int(win32gui.GetForegroundWindow() or 0)


def _win_exists(hwnd: int) -> bool:
    import win32gui
    return bool(win32gui.IsWindow(hwnd))


def _press(keys: tuple[str, ...]) -> None:
    import pyautogui
    pyautogui.hotkey(*keys)


class TabReader:
    """Lists tab strips and switches/closes tabs. All Windows access is injectable."""

    def __init__(self, backend: Any = None, list_windows: Callable[[], list] = _default_windows,
                 is_minimized: Callable[[int], bool] = _win_is_minimized,
                 show: Callable[[int, str], None] = _win_show, foreground: Callable[[], int] = _win_foreground,
                 exists: Callable[[int], bool] = _win_exists, press: Callable[[tuple], None] = _press,
                 sleep: Callable[[float], None] = time.sleep, settle_s: float = 0.9) -> None:
        self.backend = backend or UIABackend()
        self._windows, self._is_min, self._show = list_windows, is_minimized, show
        self._foreground, self._exists, self._press, self._sleep = foreground, exists, press, sleep
        self.settle_s = settle_s

    # -- reading ------------------------------------------------------------------
    def read_strip(self, hwnd: int, app: str, title: str = "") -> Strip:
        """The tab strip of one window; [] tabs if it has none or can't be read. A minimized
        window that shows no tabs is brought up (not focused) for the read and put back."""
        kind = app.lower().split()[0] if app else ""
        minimized = False
        try:
            minimized = bool(self._is_min(hwnd))
        except Exception:  # noqa: BLE001
            pass
        raws = self._read(hwnd, kind)
        if not raws and minimized:
            try:
                self._show(hwnd, "restore")
                self._sleep(0.4)
                raws = self._read(hwnd, kind)
            finally:
                try:
                    self._show(hwnd, "minimize")
                except Exception:  # noqa: BLE001
                    pass
        readable = raws is not None
        tabs = [Tab(i + 1, clean_title(r.title), r.selected, r) for i, r in enumerate(raws or [])]
        return Strip(hwnd, app, title, tabs, minimized, readable)

    def _read(self, hwnd: int, kind: str) -> Optional[list[RawTab]]:
        """The tabs, [] if the window has none, None if it could not be read."""
        try:
            return list(self.backend.read(hwnd, kind))
        except WindowGone:
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning("Couldn't read the tabs of window %s: %s", hwnd, e)
            return None

    def strips(self, app: Optional[str] = None) -> list[Strip]:
        """Every window of a tabbed app (or of all of them) that really has tabs."""
        out: list[Strip] = []
        try:
            windows = self._windows()
        except Exception as e:  # noqa: BLE001
            logger.warning("Couldn't list windows: %s", e)
            return out
        for hwnd, title, proc in windows:
            label = app_for_process(proc)
            if not label or (app and label != app):
                continue
            strip = self.read_strip(hwnd, label, title)
            if strip.tabs:
                out.append(strip)
        return out

    def strip_for(self, hwnd: int) -> Optional[Strip]:
        for h, title, proc in self._windows():
            if h == hwnd and app_for_process(proc):
                strip = self.read_strip(h, app_for_process(proc) or "", title)
                return strip if strip.tabs else None
        return None

    def read_window(self, hwnd: int) -> Optional[Strip]:
        """The window's strip even when it has no tabs (VS Code with every editor closed);
        None if the window is gone or isn't a tabbed app."""
        for h, title, proc in self._windows():
            if h == hwnd and app_for_process(proc):
                return self.read_strip(h, app_for_process(proc) or "", title)
        return None

    def current_app(self) -> Optional[str]:
        """The tabbed app whose window is in front right now, if any."""
        try:
            front = self._foreground()
            for hwnd, _title, proc in self._windows():
                if hwnd == front:
                    return app_for_process(proc)
        except Exception:  # noqa: BLE001
            pass
        return None

    def front_strip(self) -> Optional[Strip]:
        try:
            front = self._foreground()
        except Exception:  # noqa: BLE001
            return None
        return self.strip_for(front) if front else None

    def window_exists(self, hwnd: int) -> bool:
        try:
            return bool(self._exists(hwnd))
        except Exception:  # noqa: BLE001
            return False

    # -- acting -------------------------------------------------------------------
    def focus(self, hwnd: int) -> bool:
        try:
            self._show(hwnd, "focus")
            return True
        except Exception as e:  # noqa: BLE001 - Windows may refuse a focus change
            logger.info("Couldn't bring window %s forward: %s", hwnd, e)
            return False

    def select(self, strip: Strip, tab: Tab) -> str:
        """Make `tab` the current one. Returns 'uia', 'keyboard' or raises RuntimeError."""
        try:
            self.backend.select(strip.hwnd, tab.raw)
            return "uia"
        except Exception as e:  # noqa: BLE001
            logger.info("UI Automation couldn't select tab %r (%s); trying the keyboard", tab.title[:40], e)
        if not self.focus(strip.hwnd):
            raise RuntimeError("couldn't bring the window forward")
        n = tab.index
        keys = ("ctrl", "9") if n > 8 and n == len(strip.tabs) else ("ctrl", str(n)) if n <= 8 else None
        if keys is None:
            raise RuntimeError("that tab can't be reached by shortcut")
        self._press(keys)
        return "keyboard"

    def close(self, strip: Strip, tab: Tab) -> str:
        """Press the tab's close button; fall back to Ctrl+W on the current tab only."""
        try:
            self.backend.close(strip.hwnd, tab.raw)
            return "uia"
        except Exception as e:  # noqa: BLE001
            logger.info("UI Automation couldn't close tab %r (%s); trying the keyboard", tab.title[:40], e)
        if not tab.selected:
            if self.select(strip, tab) == "keyboard":
                pass
        if not self.focus(strip.hwnd):
            raise RuntimeError("couldn't bring the window forward")
        self._press(("ctrl", "w"))
        return "keyboard"

    def settle(self, hwnd: int, before: int, want_less: bool = True) -> Optional[Strip]:
        """Re-read a window's strip until its tab count changed (or the wait is up).
        None = the window is gone."""
        deadline = time.monotonic() + self.settle_s
        strip: Optional[Strip] = None
        while True:
            if not self.window_exists(hwnd):
                return None
            strip = self.strip_for(hwnd)
            if strip is None:        # window there, but no tabs read: it's closing, or unreadable
                if not self.window_exists(hwnd):
                    return None
                strip = Strip(hwnd, "", "", [])
            # An empty strip is "the window is closing" (it vanishes a moment later) or VS Code
            # with no editors: keep looking until the wait is up rather than call it settled.
            if (strip.tabs and len(strip.tabs) != before) or time.monotonic() >= deadline:
                return strip
            self._sleep(0.15)


def titles_removed(before: list[str], after: list[str]) -> list[str]:
    """Which titles are in `before` but gone from `after` (duplicates counted)."""
    left = list(after)
    gone: list[str] = []
    for t in before:
        if t in left:
            left.remove(t)
        else:
            gone.append(t)
    return gone
