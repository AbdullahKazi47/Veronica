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

    def extract_arguments(self, text: str) -> dict[str, Any]:
        if _MIN_ALL.search(text):
            return {"action": "minimize_all"}
        if _RESTORE_ALL.search(text):
            return {"action": "restore_all"}
        m = _ONE.match(text)
        if not m:
            return {}
        verb = m.group(1).lower()
        action = "minimize" if verb.startswith("minimi") else "maximize" if verb.startswith("maximi") else "focus"
        return {"action": action, "app": m.group(2).strip()}

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


class CloseWindowTool(Tool):
    name = "close_window"
    description = "Close an app's window (asks first: unsaved work could be lost)."
    permission = PermissionLevel.SENSITIVE
    parameters = {"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]}

    def __init__(self, find_window: Optional[Callable] = None, close: Optional[Callable[[int], None]] = None) -> None:
        self._find, self._close = find_window, close

    def matches(self, text: str) -> bool:
        m = _CLOSE.match(text)
        return bool(m) and not re.match(r"^(?:it|that|this|everything|all|the\s+conversation)$", m.group(1), re.I)

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _CLOSE.match(text)
        return {"app": m.group(1).strip()} if m else {}

    def run(self, app: str = "", **kwargs: Any) -> ToolResult:
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
        return ToolResult.ok(f"Closed {app}.")


# ---------------------------------------------------------------- power

def _lock() -> None:
    import ctypes
    ctypes.windll.user32.LockWorkStation()  # type: ignore[attr-defined]


def _sleep() -> None:
    subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], creationflags=_NO_WINDOW)


# The PC must be named for sleep: a bare "go to sleep" may be meant for VERONICA herself.
_PC = r"(?:my\s+|the\s+)?(?:pc|computer|laptop|system)"
_LOCK_SLEEP = re.compile(rf"^\s*(?:please\s+)?(?:(lock)\s+(?:{_PC}|(?:my\s+|the\s+)?screen)|"
                         rf"put\s+{_PC}\s+to\s+sleep|sleep\s+{_PC}|{_PC}\s+(?:to\s+)?sleep(?:\s+mode)?)"
                         r"(?:\s+now)?\s*[.!]?\s*$", re.I)


class LockOrSleepTool(Tool):
    name = "lock_or_sleep"
    description = "Lock the PC or put it to sleep (no permission needed; nothing is lost). action: 'lock' or 'sleep'."
    permission = PermissionLevel.MODERATE
    parameters = {"type": "object", "properties": {"action": {"type": "string", "enum": ["lock", "sleep"]}},
                  "required": ["action"]}

    def __init__(self, lock: Callable = _lock, sleep: Callable = _sleep) -> None:
        self._lock, self._sleep = lock, sleep

    def matches(self, text: str) -> bool:
        return bool(_LOCK_SLEEP.match(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _LOCK_SLEEP.match(text)
        return {"action": "lock" if m and m.group(1) else "sleep"}

    def run(self, action: str = "lock", **kwargs: Any) -> ToolResult:
        if platform.system() != "Windows" and self._lock is _lock:
            return ToolResult.fail("Only supported on Windows.")
        try:
            (self._sleep if action == "sleep" else self._lock)()
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Couldn't {action} the PC: {e}")
        return ToolResult.ok("Locked." if action != "sleep" else "Going to sleep.")


_SHUTDOWN = re.compile(r"^\s*(?:please\s+)?(?:(shut\s*down|turn\s+off|power\s+off)|(restart|reboot)|"
                       r"(sign\s+out|log\s+out|log\s+off))(?:\s+(?:my\s+|the\s+)?(?:pc|computer|laptop|system))?"
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
                        r"^\s*(?:please\s+)?open\s+(?:the\s+|my\s+)?((?:[\w\- ]+[\\/])*[\w\- ]+\.\w{2,5})\s*[.!]?\s*$", re.I)
_DOC_KIND = {"file": None, "document": None, "pdf": {".pdf"}, "photo": {".jpg", ".jpeg", ".png", ".heic", ".webp"},
             "picture": {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp"},
             "image": {".jpg", ".jpeg", ".png", ".heic", ".webp", ".gif", ".bmp"},
             "video": {".mp4", ".mkv", ".mov", ".avi", ".webm"},
             "spreadsheet": {".xlsx", ".xls", ".csv", ".ods"}, "presentation": {".pptx", ".ppt", ".odp"}}


def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", " ", (s or "").lower()).strip()


class OpenPathTool(Tool):
    name = "open_path"
    description = ("Open a folder or a document/photo/video by name or path, e.g. 'projects folder in "
                   "documents', 'resume pdf', 'C:\\Users\\me\\Notes'. No permission needed. Never runs programs.")
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Folder/file name or full path."},
            "kind": {"type": "string", "enum": ["folder", "file"], "description": "What to look for."},
            "inside": {"type": "string", "description": "Optional parent folder name, e.g. 'documents'."},
        },
        "required": ["name"],
    }

    def __init__(self, home: Optional[Path] = None, opener: Optional[Callable[[str], None]] = None,
                 max_depth: int = 5, max_entries: int = 60_000) -> None:
        self.home = Path(home) if home else Path.home()
        self._open = opener or (lambda p: os.startfile(p))  # type: ignore[attr-defined]
        self.max_depth, self.max_entries = max_depth, max_entries

    def matches(self, text: str) -> bool:
        return bool(_OPEN_PATH.match(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _OPEN_PATH.match(text)
        if not m:
            return {}
        if m.group(4):
            return {"name": m.group(4).strip(), "kind": "file"}
        word = m.group(2).lower()
        args = {"name": m.group(1).strip(), "kind": "folder" if word in ("folder", "directory") else word}
        if m.group(3):
            args["inside"] = m.group(3).strip()
        return args

    def _search(self, root: Path, name: str, want_dir: bool, exts: Optional[set]) -> Optional[Path]:
        """Breadth-first: the shallowest match wins (so 'projects' finds Documents\\Projects
        before some deeply nested folder of the same name)."""
        target = _norm(Path(name).stem if not want_dir else name)
        level, seen, best = [root], 0, None
        for _depth in range(self.max_depth):
            nxt = []
            for folder in level:
                try:
                    entries = list(os.scandir(folder))
                except OSError:
                    continue
                for e in entries:
                    seen += 1
                    if seen > self.max_entries:
                        return best
                    low = e.name.lower()
                    if low.startswith(".") or low in _SKIP_DIRS:
                        continue
                    is_dir = e.is_dir(follow_symlinks=False)
                    stem = _norm(e.name if is_dir else Path(e.name).stem)
                    if is_dir == want_dir and (not exts or Path(e.name).suffix.lower() in exts):
                        if stem == target:
                            return Path(e.path)
                        if best is None and target and target in stem:
                            best = Path(e.path)
                    if is_dir:
                        nxt.append(e.path)
            if best is not None:
                return best
            level = nxt
        return best

    def run(self, name: str = "", kind: str = "folder", inside: str = "", **kwargs: Any) -> ToolResult:
        name = (name or "").strip().strip('"')
        if not name:
            return ToolResult.fail("Which folder or file?")
        want_dir = (kind or "folder") == "folder"
        exts = _DOC_KIND.get(kind) if not want_dir else None
        path = Path(os.path.expandvars(os.path.expanduser(name)))
        if not path.is_absolute() or not path.exists():
            root = self.home
            if inside:
                parent = self._search(self.home, inside, True, None)
                if parent is None:
                    return ToolResult.fail(f"I couldn't find a {inside} folder.")
                root = parent
            found = self._search(root, name, want_dir, exts)
            if found is None and not want_dir and kind in ("file", "document") and "." in name:
                found = self._search(root, name, False, {Path(name).suffix.lower()})
            if found is None:
                where = f" in {inside}" if inside else ""
                return ToolResult.fail(f"I couldn't find a {name} {'folder' if want_dir else kind or 'file'}{where}.")
            path = found
        if path.is_file() and path.suffix.lower() in _EXECUTABLE:
            return ToolResult.fail(f"{path.name} is a program or script. I won't run those by file — say "
                                   "'open <app name>' for apps.")
        try:
            self._open(str(path))
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Couldn't open {path.name}: {e}")
        return ToolResult.ok(f"Opening {path.name}.", path=str(path))
