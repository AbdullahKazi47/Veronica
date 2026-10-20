"""
Everyday PC control by voice, each at the right permission level — so simple things
never fall back to a shell command (which always needs confirmation).

  no permission : minimize everything / show desktop, restore windows, minimize /
                  maximize / switch to an app's window, open folders and documents,
                  lock, sleep, cancel a pending shutdown
  asks (yes/no) : close an app's window (unsaved work), shut down / restart / sign out
                  (with a 30 s delay so it can still be cancelled)
  refused       : opening program files (.exe, .bat, ...) by path — use "open <app>"
"""
from __future__ import annotations

import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("tools.windows")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ---------------------------------------------------------------- windows

def _shell_app():
    import win32com.client
    return win32com.client.Dispatch("Shell.Application")


def _show_window(hwnd: int, how: str) -> None:
    import win32con
    import win32gui
    if how == "minimize":
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    elif how == "maximize":
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
        win32gui.SetForegroundWindow(hwnd)
    elif how == "unminimize":    # restore-all: no focus stealing, many windows at once
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE if not win32gui.IsIconic(hwnd) else win32con.SW_RESTORE)
    else:  # restore / focus
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)


def _app_windows() -> list[int]:
    from veronica.vision.app_windows import _list_windows
    return [hwnd for hwnd, _title, _proc in _list_windows()]


def _is_minimized(hwnd: int) -> bool:
    import win32gui
    return bool(win32gui.IsWindow(hwnd) and win32gui.IsIconic(hwnd))


# "all ..." with any trailing words: speech recognition turns "all my tabs" into
# "all attacks" and similar, and "minimize all <x>" only ever means everything.
_ALL = r"(?:all|everything)"
_MIN_ALL = re.compile(rf"^\s*(?:please\s+)?(?:minimi[sz]e|hide|clear)\s+{_ALL}\b|^\s*(?:please\s+)?(?:show|go\s+to)\s+(?:me\s+)?(?:the\s+)?desktop\b", re.I)
_WINDOWS = r"(?:(?:all\s+)?(?:of\s+)?(?:my\s+|the\s+)?(?:tabs?|windows?|apps?|programs?|screens?)|all|everything|them|it\s+all)"
_RESTORE_ALL = re.compile(rf"^\s*(?:please\s+)?(?:(?:restore|bring\s+back|unminimi[sz]e|undo\s+minimi[sz]e)(?:\s+{_WINDOWS}\b.*)?"
                          rf"|undo\s+(?:that|the\s+minimi[sz]e)|(?:open|show)\s+(?:me\s+)?(?:all\s+)?my\s+(?:tabs|windows)(?:\s+again)?)"
                          r"\s*(?:back|again|please)?\s*[.!]?\s*$", re.I)
_ONE = re.compile(r"^\s*(?:please\s+)?(minimi[sz]e|maximi[sz]e|restore|switch\s+to|focus(?:\s+on)?|bring\s+up)\s+"
                  r"(?:the\s+|my\s+)?(.+?)(?:\s+window)?\s*[.!]?\s*$", re.I)


class WindowControlTool(Tool):
    name = "window_control"
    description = ("Arrange windows without permission: 'minimize_all' (show desktop — also what 'minimize all "
                   "my tabs' means), 'restore_all' (bring back the windows that were minimized), or 'minimize' / "
                   "'maximize' / 'focus' one app's window (`app`, e.g. 'Chrome', 'WhatsApp'). Never closes anything.")
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["minimize_all", "restore_all", "minimize", "maximize", "focus"]},
            "app": {"type": "string", "description": "App whose window to act on (for single-window actions)."},
        },
        "required": ["action"],
    }

    def __init__(self, shell: Callable = _shell_app, find_window: Optional[Callable] = None,
                 show: Callable[[int, str], None] = _show_window, list_windows: Callable[[], list] = _app_windows,
                 is_minimized: Callable[[int], bool] = _is_minimized) -> None:
        self._shell, self._show = shell, show
        self._find = find_window
        self._list, self._is_min = list_windows, is_minimized
        self._minimized_by_me: list[int] = []   # what "restore" brings back

    def _find_window(self, app: str):
        if self._find is not None:
            return self._find(app)
        from veronica.vision.app_windows import find_app_window
        return find_app_window(app)

    def matches(self, text: str) -> bool:
        return bool(_MIN_ALL.search(text) or _RESTORE_ALL.search(text) or
                    (_ONE.match(text) and not _MIN_ALL.search(text)))

    @staticmethod
    def _means_everything(app: str) -> bool:
        return bool(re.match(r"^(?:all|every|everything)\b|^(?:my\s+|the\s+)?(?:tabs|windows|apps|programs|screens)$",
                             (app or "").strip(), re.I))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        if _MIN_ALL.search(text):
            return {"action": "minimize_all"}
        if _RESTORE_ALL.search(text):
            return {"action": "restore_all"}
        m = _ONE.match(text)
        if not m:
            return {}
        verb = m.group(1).lower()
        app = m.group(2).strip()
        if self._means_everything(app):              # "restore all tabs", "minimize my windows"
            return {"action": "minimize_all" if verb.startswith("minimi") else "restore_all"}
        action = "minimize" if verb.startswith("minimi") else "maximize" if verb.startswith("maximi") else "focus"
        return {"action": action, "app": app}

    def _minimize_all(self) -> ToolResult:
        try:
            self._minimized_by_me = [h for h in self._list() if not self._is_min(h)]
        except Exception:  # noqa: BLE001 - restore then falls back to every minimized window
            self._minimized_by_me = []
        self._shell().MinimizeAll()
        return ToolResult.ok("Done. Everything's minimized.")

    def _restore_all(self) -> ToolResult:
        """Bring back what minimize-all hid; if that's unknown (e.g. after a restart),
        every minimized window. Windows' own UndoMinimizeALL is unreliable, so it's
        only the last resort."""
        try:
            targets = [h for h in self._minimized_by_me if self._is_min(h)]
            if not targets:
                targets = [h for h in self._list() if self._is_min(h)]
        except Exception:  # noqa: BLE001
            targets = []
        if not targets:
            self._shell().UndoMinimizeALL()
            return ToolResult.ok("Windows restored.")
        for hwnd in reversed(targets):   # reversed: the window that was on top ends up on top
            try:
                self._show(hwnd, "unminimize")
            except Exception:  # noqa: BLE001 - one stubborn window shouldn't stop the rest
                logger.debug("Couldn't restore window %s", hwnd)
        self._minimized_by_me = []
        n = len(targets)
        return ToolResult.ok(f"Restored {n} window{'s' if n != 1 else ''}.")

    def run(self, action: str = "", app: str = "", **kwargs: Any) -> ToolResult:
        action = (action or "").lower()
        try:
            if action == "minimize_all":
                return self._minimize_all()
            if action == "restore_all":
                return self._restore_all()
            if action in ("minimize", "maximize", "focus", "restore"):
                if not app:
                    return ToolResult.fail("Which app's window?")
                found = self._find_window(app)
                if found is None:
                    return ToolResult.fail(f"I don't see a {app} window open.")
                self._show(found[0], "restore" if action == "focus" else action)
                verb = {"minimize": "Minimized", "maximize": "Maximized"}.get(action, "Switched to")
                return ToolResult.ok(f"{verb} {app}.")
        except Exception as e:  # noqa: BLE001
            logger.warning("Window action %s failed: %s", action, e)
            return ToolResult.fail(f"Couldn't do that with the windows: {e}")
        return ToolResult.fail("I can minimize or restore everything, or minimize, maximize or switch to one app.")


_CLOSE = re.compile(r"^\s*(?:please\s+)?(?:close|quit|exit)\s+(?:the\s+|my\s+)?(.+?)(?:\s+(?:window|app))?\s*[.!]?\s*$", re.I)


_TABS = re.compile(r"\btabs?\b", re.I)    # "close all tabs" is close_tab's, never a window called "all tabs"


def _window_exists(hwnd: int) -> bool:
    import win32gui
    return bool(win32gui.IsWindow(hwnd) and win32gui.IsWindowVisible(hwnd))


class CloseWindowTool(Tool):
    name = "close_window"
    description = ("Close an app's window (asks first: unsaved work could be lost). Checks the window is really gone "
                   "and says so if it isn't (it may be asking to save). For tabs use close_tab, not this.")
    permission = PermissionLevel.SENSITIVE
    parameters = {"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]}

    def __init__(self, find_window: Optional[Callable] = None, close: Optional[Callable[[int], None]] = None,
                 exists: Optional[Callable[[int], bool]] = None, sleep: Callable[[float], None] = time.sleep,
                 wait_s: float = 2.0) -> None:
        self._find, self._close = find_window, close
        self._exists, self._sleep, self.wait_s = exists, sleep, wait_s

    def matches(self, text: str) -> bool:
        m = _CLOSE.match(text)
        return (bool(m) and not _TABS.search(text)
                and not re.match(r"^(?:it|that|this|everything|all|the\s+conversation)$", m.group(1), re.I))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _CLOSE.match(text)
        return {"app": m.group(1).strip()} if m else {}

    def _gone(self, hwnd: int) -> Optional[bool]:
        """True = the window is gone, False = still there, None = couldn't look."""
        check = self._exists or _window_exists
        deadline = time.monotonic() + self.wait_s
        try:
            while True:
                if not check(hwnd):
                    return True
                if time.monotonic() >= deadline:
                    return False
                self._sleep(0.2)
        except Exception as e:  # noqa: BLE001
            logger.debug("Couldn't check the window: %s", e)
            return None

    def run(self, app: str = "", **kwargs: Any) -> ToolResult:
        if _TABS.search(app or ""):
            return ToolResult.fail("That's about tabs, not a window — I'll use the tab tools for it.")
        from veronica.vision.app_windows import find_app_window
        found = (self._find or find_app_window)(app)
        if found is None:
            return ToolResult.fail(f"I don't see a {app} window open.")
        try:
            if self._close is not None:
                self._close(found[0])
            else:
                import win32con
                import win32gui
                win32gui.PostMessage(found[0], win32con.WM_CLOSE, 0, 0)  # polite close: the app can ask to save
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Couldn't close {app}: {e}")
        gone = self._gone(found[0])
        if gone is True:
            return ToolResult.ok(f"Closed {app}.")
        if gone is False:
            return ToolResult.fail(f"I asked {app} to close, but its window is still open — it may be asking to save. "
                                   "Nothing was forced.", unconfirmed=True)
        return ToolResult.fail(f"I sent the close to {app}, but I can't confirm it worked.", unconfirmed=True)


# ---------------------------------------------------------------- power

def _lock() -> None:
    import ctypes
    ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]


def _sleep() -> None:
    subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], creationflags=_NO_WINDOW)


# The PC must be named: a bare "go to sleep" is said to VERONICA (it ends the
# conversation), never to the computer.
_PC = r"(?:my\s+|the\s+)?(?:pc|computer|laptop|system)"
_LOCK = re.compile(rf"^\s*(?:please\s+)?lock\s+(?:{_PC}|(?:my\s+|the\s+)?screen)(?:\s+now)?\s*[.!]?\s*$", re.I)
_SLEEP = re.compile(rf"^\s*(?:please\s+)?(?:put\s+{_PC}\s+to\s+sleep|sleep\s+{_PC}|{_PC}\s+(?:to\s+)?sleep(?:\s+mode)?)"
                    r"(?:\s+now)?\s*[.!]?\s*$", re.I)


class LockPCTool(Tool):
    name = "lock_pc"
    description = "Lock the PC (the lock screen; nothing is closed or lost). No permission needed."
    permission = PermissionLevel.MODERATE
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, lock: Callable = _lock) -> None:
        self._lock = lock

    def matches(self, text: str) -> bool:
        return bool(_LOCK.match(text))

    def run(self, **kwargs: Any) -> ToolResult:
        if platform.system() != "Windows" and self._lock is _lock:
            return ToolResult.fail("Only supported on Windows.")
        try:
            self._lock()
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Couldn't lock the PC: {e}")
        return ToolResult.ok("Locked.")


class SleepPCTool(Tool):
    name = "sleep_pc"
    description = ("Put the COMPUTER to sleep. ALWAYS asks the user first. Only when the user clearly asks for "
                   "the PC/laptop/computer to sleep — never for 'go to sleep', 'sleep' or 'goodnight' said to "
                   "VERONICA, which only mean stop listening.")
    permission = PermissionLevel.SENSITIVE   # it takes the PC away mid-task: yes/no first
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, sleep: Callable = _sleep) -> None:
        self._sleep = sleep

    def matches(self, text: str) -> bool:
        return bool(_SLEEP.match(text))

    def run(self, **kwargs: Any) -> ToolResult:
        if platform.system() != "Windows" and self._sleep is _sleep:
            return ToolResult.fail("Only supported on Windows.")
        try:
            self._sleep()
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Couldn't put the PC to sleep: {e}")
        return ToolResult.ok("Going to sleep.")


_SHUTDOWN = re.compile(r"^\s*(?:please\s+)?(?:(shut\s*down|turn\s+off|power\s+off)|(restart|reboot)|"
                       r"(sign\s+(?:me\s+)?out|log\s+(?:me\s+)?(?:out|off)))"
                       r"(?:\s+(?:of\s+)?(?:my\s+|the\s+)?(?:pc|computer|laptop|system|windows))?"
                       r"(?:\s+in\s+(\d+)\s+(minutes?|mins?|seconds?|secs?))?\s*[.!]?\s*$", re.I)
_SHUTDOWN_DELAY_S = 30


class ShutdownTool(Tool):
    name = "shutdown_pc"
    description = ("Shut down, restart or sign out of the PC. ALWAYS asks the user first; runs with a delay "
                   "(default 30 s) so it can be cancelled with cancel_shutdown. action: shutdown | restart | sign_out.")
    permission = PermissionLevel.SENSITIVE
    parameters = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["shutdown", "restart", "sign_out"]},
                       "delay_seconds": {"type": "integer"}},
        "required": ["action"],
    }

    def __init__(self, runner: Callable[[list[str]], Any] = None) -> None:
        self._run = runner or (lambda cmd: subprocess.run(cmd, creationflags=_NO_WINDOW, check=True))

    def matches(self, text: str) -> bool:
        return bool(_SHUTDOWN.match(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _SHUTDOWN.match(text)
        if not m:
            return {}
        action = "shutdown" if m.group(1) else "restart" if m.group(2) else "sign_out"
        args: dict[str, Any] = {"action": action}
        if m.group(4):
            args["delay_seconds"] = int(m.group(4)) * (60 if m.group(5).lower().startswith("m") else 1)
        return args

    def run(self, action: str = "shutdown", delay_seconds: Optional[int] = None, **kwargs: Any) -> ToolResult:
        delay = max(0, int(delay_seconds if delay_seconds is not None else _SHUTDOWN_DELAY_S))
        try:
            if action == "sign_out":
                self._run(["shutdown", "/l"])
                return ToolResult.ok("Signing you out.")
            flag = "/r" if action == "restart" else "/s"
            self._run(["shutdown", flag, "/t", str(delay), "/c", "VERONICA: say 'cancel shutdown' to stop this."])
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Couldn't {action.replace('_', ' ')}: {e}")
        what = "Restarting" if action == "restart" else "Shutting down"
        when = f"in {delay} seconds" if delay < 120 else f"in {delay // 60} minutes"
        return ToolResult.ok(f"{what} {when}. Say 'cancel shutdown' if you change your mind.", delay=delay)


_CANCEL_SHUTDOWN = re.compile(r"\b(?:cancel|stop|abort)\s+(?:the\s+)?(?:shut\s*down|restart|reboot)\b|"
                              r"\bdon'?t\s+(?:shut\s*down|restart)\b", re.I)


class CancelShutdownTool(Tool):
    name = "cancel_shutdown"
    description = "Cancel a pending shutdown or restart."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, runner: Callable[[list[str]], Any] = None) -> None:
        self._run = runner or (lambda cmd: subprocess.run(cmd, creationflags=_NO_WINDOW, capture_output=True))

    def matches(self, text: str) -> bool:
        return bool(_CANCEL_SHUTDOWN.search(text))

    def run(self, **kwargs: Any) -> ToolResult:
        self._run(["shutdown", "/a"])
        return ToolResult.ok("Cancelled. Not going anywhere.")


# ---------------------------------------------------------------- folders and documents

# Programs/scripts are launched by name through open_application, never by path here.
_EXECUTABLE = {".exe", ".bat", ".cmd", ".com", ".ps1", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msi",
               ".msp", ".scr", ".pif", ".lnk", ".reg", ".cpl", ".hta", ".jar", ".py", ".pyw", ".sh"}
_SKIP_DIRS = {"appdata", "node_modules", ".git", "__pycache__", "$recycle.bin", "windows", "program files",
              "program files (x86)", "programdata", ".venv", "venv", "site-packages"}
_OPEN_PATH = re.compile(r"^\s*(?:please\s+)?(?:open|show(?:\s+me)?)\s+(?:up\s+)?(?:the\s+|my\s+)?(.+?)\s+"
                        r"(folder|directory|file|document|pdf|photo|picture|image|video|spreadsheet|presentation)"
                        r"(?:\s+(?:in|from|inside)\s+(?:my\s+|the\s+)?(.+?))?\s*[.!]?\s*$|"
                        r"^\s*(?:please\s+)?open\s+(?:the\s+|my\s+)?((?:[\w\- ]+[\\/])*[\w\- ]+\.\w{2,5})\s*[.!]?\s*$|"
                        r"^\s*(?:please\s+)?open\s+(?:the\s+|my\s+|a\s+)?(folder|directory|file|document)\s+"
                        r"(?:named|name|called|titled)\s+(.+?)\s*[.!]?\s*$", re.I)
_DOC_KIND = {"file": None, "document": None, "pdf": {".pdf"}, "photo": {".jpg", ".jpeg", ".png", ".heic", ".webp"},
             "picture": {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp"},
             "image": {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp"},
             "video": {".mp4", ".mkv", ".mov", ".avi", ".webm"},
             "spreadsheet": {".xlsx", ".xls", ".csv", ".ods"}, "presentation": {".pptx", ".ppt", ".odp"}}


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", " ", (s or "").lower()).strip()


_ABS_PATH = re.compile(r"^\s*(?:please\s+)?open\s+(?:the\s+)?([a-z]:[\\/][^\"<>|?*]*?)\s*[.!]?\s*$", re.I)
_PICK = re.compile(r"^\s*(?:please\s+)?(?:open\s+)?(?:the\s+)?(?:(first|second|third|1st|2nd|3rd)(?:\s+one)?|"
                   r"(?:number|option)\s+(\d)|(\d))(?:\s+one)?\s*[.!]?\s*$", re.I)
_DEEPER = re.compile(r"^\s*(?:please\s+)?(?:search|look|try|dig)\s+(?:a\s+(?:bit|little)\s+)?(?:deeper|harder|further|everywhere)"
                     r"(?:\s+for\s+it)?\s*[.!]?\s*$", re.I)
_ON_DRIVE = re.compile(r"^\s*(?:please\s+)?(?:it'?s|try|look|search|check)\s+(?:for\s+it\s+)?(?:on|in)\s+(?:the\s+)?([a-z])\s*"
                       r"(?:drive|:)?\s*[.!]?\s*$", re.I)
_DRIVE_WORD = re.compile(r"^(?:the\s+)?([a-z])(?::|\s+drive)?$", re.I)
_ORDINAL = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3}
_STATE_FRESH_S = 120


class OpenPathTool(Tool):
    name = "open_path"
    description = ("Open a folder or a document/photo/video by name or path, anywhere on the PC (your folders, then every "
                   "drive): 'placement folder', 'projects folder in documents', 'resume pdf', 'C:\\Users\\me\\Notes'. "
                   "A name that fits several folders is asked about, never guessed. Remembers what it found. "
                   "`deep` searches harder; `drive` limits to one drive. No permission needed. Never runs programs.")
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Folder/file name or full path."},
            "kind": {"type": "string", "enum": ["folder", "file"], "description": "What to look for."},
            "inside": {"type": "string", "description": "Optional parent folder name, e.g. 'documents', or a drive like 'D'."},
            "pick": {"type": "integer", "description": "Choose the Nth folder from the ones just offered."},
            "deep": {"type": "boolean", "description": "Search harder and deeper (slower)."},
            "drive": {"type": "string", "description": "Only look on this drive letter."},
        },
        "required": [],
    }

    def __init__(self, home: Optional[Path] = None, opener: Optional[Callable[[str], None]] = None,
                 max_depth: int = 5, max_entries: int = 80_000, drives: Optional[list] = None, config=None,
                 aliases=None, routing=None, verify: Optional[Callable[[Path, bool], Optional[bool]]] = None,
                 desktop=None, max_seconds: float = 1.8, clock: Callable[[], float] = time.monotonic,
                 drive_map: Optional[dict] = None) -> None:
        from veronica.tools.folder_search import FolderFinder
        self.home = Path(home) if home else Path.home()
        self._injected_opener = opener is not None
        self._open = opener or (lambda p: os.startfile(p))  # type: ignore[attr-defined]
        self.max_depth, self.max_entries = max_depth, max_entries
        self.finder = FolderFinder(home=home, drives=drives, max_seconds=max_seconds, max_entries=max_entries,
                                   deep_depth=max_depth, clock=clock)
        if aliases is None and config is not None:
            from veronica.core.understanding import Aliases
            aliases = Aliases(config.resolve_path("data/aliases.json"))
        self.aliases = aliases
        self.routing = routing
        self._verify = verify
        self._desktop = desktop
        self._clock = clock
        self._drive_map = drive_map or {}      # letter -> root (tests); real: 'D' -> D:\\
        self._pending: Optional[dict] = None     # the folders just offered ("which one?")
        self._last: Optional[dict] = None        # the last search, for "search deeper" / "try the D drive"
        self._before: Optional[set] = None

    # -- routing ---------------------------------------------------------------------
    def _fresh(self, state: Optional[dict]) -> bool:
        return bool(state) and self._clock() - state["at"] <= _STATE_FRESH_S

    def matches(self, text: str) -> bool:
        if _OPEN_PATH.match(text) or _ABS_PATH.match(text):
            return True
        if self._fresh(self._pending) and _PICK.match(text):
            return True
        if self._fresh(self._last) and (_DEEPER.match(text) or _ON_DRIVE.match(text)):
            return True
        if self.routing is not None:
            parsed = self.routing.parse(text)
            return bool(parsed) and parsed["kind"] == "place"
        return False

    def extract_arguments(self, text: str) -> dict[str, Any]:
        if self._fresh(self._pending):
            m = _PICK.match(text)
            if m:
                word = (m.group(1) or m.group(2) or m.group(3)).lower()
                return {"pick": _ORDINAL.get(word) or int(word)}
        if self._fresh(self._last):
            if _DEEPER.match(text):
                return {"deep": True}
            m = _ON_DRIVE.match(text)
            if m:
                return {"drive": m.group(1).upper(), "deep": True}
        m = _ABS_PATH.match(text)
        if m:
            return {"name": m.group(1).strip(), "kind": "file" if Path(m.group(1)).suffix else "folder"}
        m = _OPEN_PATH.match(text)
        if not m:
            if self.routing is not None:
                parsed = self.routing.parse(text)
                if parsed and parsed["kind"] == "place":
                    return {"name": parsed["target"], "kind": "folder"}
            return {}
        if m.group(4):
            return {"name": m.group(4).strip(), "kind": "file"}
        if m.group(5):
            word = m.group(5).lower()
            return {"name": m.group(6).strip(), "kind": "folder" if word in ("folder", "directory") else "file"}
        word = m.group(2).lower()
        args = {"name": m.group(1).strip(), "kind": "folder" if word in ("folder", "directory") else word}
        if m.group(3):
            args["inside"] = m.group(3).strip()
        return args

    # -- searching -------------------------------------------------------------------
    def _remembered(self, name: str) -> Optional[Path]:
        """A folder she resolved before — but only if it is still there."""
        if self.aliases is None:
            return None
        try:
            path = self.aliases.place(name)
        except Exception:  # noqa: BLE001
            return None
        if path and Path(path).is_dir():
            return Path(path)
        if path:
            try:
                self.aliases.forget_place(name)
            except Exception:  # noqa: BLE001
                pass
        return None

    def _remember(self, name: str, path: Path) -> None:
        if self.aliases is not None:
            try:
                self.aliases.remember_place(name, str(path))
            except Exception:  # noqa: BLE001
                logger.debug("Couldn't remember %s", name)

    def _ask(self, name: str, want_dir: bool, matches: list) -> ToolResult:
        from veronica.tools.folder_search import place_label
        shown = matches[:3]
        ords = ["first", "second", "third"]
        what = "folders" if want_dir else "files"
        parts = [f"{ords[i]}, {m.path.name} {place_label(m.path, self.home)}" for i, m in enumerate(shown)]
        more = f" (and {len(matches) - 3} more)" if len(matches) > 3 else ""
        self._pending = {"at": self._clock(), "paths": [m.path for m in shown], "name": name, "want_dir": want_dir}
        return ToolResult.fail(f"I found {len(matches)} {what} called {name}: " + "; ".join(parts) + f"{more}. Which one?",
                               ask="path", choices=[str(m.path) for m in shown])

    def run(self, name: str = "", kind: str = "folder", inside: str = "", pick: int = 0, deep: bool = False,
            drive: str = "", **kwargs: Any) -> ToolResult:
        if pick:
            if not self._fresh(self._pending):
                return ToolResult.fail("I'm not waiting on a choice — which folder do you want?")
            paths = self._pending["paths"]
            if not 1 <= int(pick) <= len(paths):
                return ToolResult.fail(f"I only offered {len(paths)}. Say first, second or third.")
            chosen = paths[int(pick) - 1]
            if not chosen.exists():
                return ToolResult.fail(f"{chosen.name} isn't there any more.")
            asked = self._pending["name"]
            self._remember(asked, chosen)
            self._pending = None
            return self._open_path(chosen, asked)
        if not (name or "").strip() and self._fresh(self._last) and (deep or drive):
            name, kind = self._last["name"], self._last["kind"]
            inside = inside or self._last.get("inside", "")
        name = (name or "").strip().strip('"')
        if not name:
            return ToolResult.fail("Which folder or file?")
        want_dir = (kind or "folder") == "folder"
        exts = _DOC_KIND.get(kind) if not want_dir else None
        path = Path(os.path.expandvars(os.path.expanduser(name)))
        if path.is_absolute() and path.exists():
            return self._open_path(path, name)
        if path.is_absolute() and re.match(r"^[a-z]:[\\/]", name, re.I):
            return ToolResult.fail(f"There's nothing at {name}.")

        only_drive = None
        letter = None
        if drive:
            letter = drive.strip()[:1].upper()
        elif inside:
            m = _DRIVE_WORD.match(inside.strip())
            if m:
                letter = m.group(1).upper()
        if letter:
            only_drive = Path(self._drive_map.get(letter) or f"{letter}:\\")
            if not only_drive.exists():
                return ToolResult.fail(f"I don't see a {letter} drive.")
            inside = "" if not drive else inside

        if want_dir and not inside and not deep and not only_drive:
            hit = self._remembered(name)
            if hit is not None:
                return self._open_path(hit, name, remembered=True)

        parent: Optional[Path] = None
        if inside:
            found_parent = self.finder.find(inside, True, None, deep=deep)
            best = found_parent.best()
            if not best:
                return ToolResult.fail(f"I couldn't find a {inside} folder.")
            parent = best[0].path

        query, search_exts = name, exts
        if not want_dir and "." in Path(name).name:
            query, search_exts = Path(name).stem, (exts or {Path(name).suffix.lower()})
        if parent is not None:
            from veronica.tools.folder_search import FolderFinder
            sub = FolderFinder(home=parent, drives=[], max_seconds=self.finder.max_seconds * (5 if deep else 1),
                               max_entries=self.max_entries, deep_depth=self.max_depth, clock=self._clock)
            result = sub.find_under(parent, query, want_dir, search_exts)
        else:
            result = self.finder.find(query, want_dir, search_exts, only_drive=only_drive, deep=deep)
        self._last = {"at": self._clock(), "name": name, "kind": kind or "folder", "inside": inside}
        matches = result.best()
        if not matches and not want_dir and kind in ("file", "document") and "." in name and parent is None:
            result = self.finder.find(Path(name).stem, False, {Path(name).suffix.lower()}, only_drive=only_drive, deep=deep)
            matches = result.best()
        if not matches:
            where = f" in {inside}" if inside else (f" on the {letter} drive" if letter else "")
            what = "folder" if want_dir else kind or "file"
            tail = (" I ran out of time looking — say 'search deeper' and I'll look harder, or tell me which drive it's on."
                    if result.timed_out else
                    " Say 'search deeper' to look harder, or tell me which drive it's on.")
            return ToolResult.fail(f"I couldn't find a {name} {what}{where}.{tail}")
        if len(matches) > 1:
            return self._ask(name, want_dir, matches)
        if want_dir:
            self._remember(name, matches[0].path)
        return self._open_path(matches[0].path, name)

    # -- opening, then checking ---------------------------------------------------------
    def _check(self, path: Path) -> Optional[bool]:
        """True = seen, False = looked and didn't see, None = no way to look."""
        if self._verify is not None:
            return self._verify(path, path.is_dir())
        if self._injected_opener:
            return None
        try:
            desktop = self._desktop
            if desktop is None:
                from veronica.vision.desktop_state import Desktop
                desktop = self._desktop = Desktop()
            return desktop.explorer_shows(str(path)) if path.is_dir() else self._file_window_opened(desktop, path)
        except Exception as e:  # noqa: BLE001
            logger.debug("Couldn't verify the open: %s", e)
            return None

    def _file_window_opened(self, desktop, path: Path) -> bool:
        return desktop.window_appeared([path.stem, path.name], wait_s=3.0, before=self._before) is not None

    def _open_path(self, path: Path, asked: str, remembered: bool = False) -> ToolResult:
        if path.is_file() and path.suffix.lower() in _EXECUTABLE:
            return ToolResult.fail(f"{path.name} is a program or script. I won't run those by file — say "
                                   "'open <app name>' for apps.")
        self._before = None
        if not path.is_dir() and not self._injected_opener and self._verify is None:
            try:
                from veronica.vision.desktop_state import Desktop
                self._desktop = self._desktop or Desktop()
                self._before = {w.hwnd for w in self._desktop.windows()}
            except Exception:  # noqa: BLE001
                self._before = None
        try:
            self._open(str(path))
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Couldn't open {path.name}: {e}")
        seen = self._check(path)
        if seen is False or (seen is None and self._verify is not None):
            return ToolResult.fail(f"I asked Windows to open {path.name}, but I can't confirm it's showing.",
                                   path=str(path), unconfirmed=True)
        note = " (the one I remembered)" if remembered else ""
        return ToolResult.ok(f"Opening {path.name}{note}.", path=str(path), verified=seen is True)
