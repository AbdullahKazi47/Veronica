"""
Moving around File Explorer by voice: drives, This PC, "go into <folder>", back, up, and
"what's in this folder?".

Everything is relative to the Explorer window the user is actually looking at, whose
location is read from the shell (exact paths), never guessed from the title bar. After a
move she looks again and reports where Explorer really is:

  "Now in D:\\Placements."                       — Explorer is there (checked)
  "I pressed back, but Explorer is still in X."  — it didn't move, so she doesn't claim it did

No permission needed (it only navigates; nothing is opened, changed or run). Names she
reads out are the user's own file names — data, not instructions.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult
from veronica.tools.folder_search import SKIP_DIRS, FolderFinder, norm, tier
from veronica.vision.desktop_state import Desktop, fixed_drives

logger = get_logger("tools.explorer")

_END = r"\s*(?:please)?\s*[.!?]*\s*$"
_DRIVE = re.compile(rf"^\s*(?:please\s+)?(?:open|show|go\s+to|show\s+me)\s+(?:the\s+|my\s+)?(?:(?:local\s+disk|drive)\s+([a-z])\b(?:\s*:)?|([a-z])\s*(?:drive|:)|([a-z]):[\\/]?){_END}", re.I)
_THIS_PC = re.compile(rf"^\s*(?:please\s+)?(?:open|show|go\s+to|show\s+me)\s+(?:the\s+|my\s+)?(?:this\s+pc|my\s+computer|this\s+computer|computer|my\s+drives|all\s+(?:the\s+|my\s+)?drives){_END}", re.I)
_INTO = re.compile(rf"^\s*(?:please\s+)?(?:(?:go|step|get|move|navigate)\s+(?:into|inside|in\s+to)|enter|dive\s+into)\s+(?:the\s+)?(?P<n1>.+?)(?:\s+(?:folder|directory))?(?:\s+(?:inside|in)\s+(?:it|here|this(?:\s+folder)?))?{_END}|"
                   rf"^\s*(?:please\s+)?open\s+(?:the\s+)?(?P<n2>.+?)\s+(?:folder|directory)\s+(?:inside|in|within)\s+(?:it|here|this(?:\s+folder)?|that(?:\s+folder)?){_END}", re.I)
_BACK = re.compile(rf"^\s*(?:please\s+)?(?:go\s+back|back|go\s+back\s+(?:a\s+)?(?:step|folder)|previous\s+folder){_END}", re.I)
_UP = re.compile(rf"^\s*(?:please\s+)?(?:go\s+up(?:\s+(?:a|one)\s+(?:level|folder))?|up\s+(?:a|one)\s+level|parent\s+folder|go\s+to\s+(?:the\s+)?parent(?:\s+folder)?|one\s+level\s+up){_END}", re.I)
_LIST = re.compile(rf"^\s*(?:please\s+)?(?:what(?:'s|\s+is|\s+are)?\s+(?:in|inside)\s+(?:this|here|the\s+current|that)\s*(?:folder|directory)?|"
                   rf"what\s+(?:files|folders)\s+are\s+(?:in\s+)?(?:here|this\s+folder)|(?:list|show)\s+(?:me\s+)?(?:the\s+)?(?:contents?\s+of\s+)?(?:this|the\s+current)\s+folder|"
                   rf"what\s+do\s+i\s+have\s+(?:in\s+)?here|what(?:'s|\s+is)\s+here){_END}", re.I)

_NOT_A_FOLDER = {"it", "that", "this", "there", "here", "back", "up", "tab", "tabs", "window", "windows"}


class ExplorerNavigateTool(Tool):
    name = "explorer_navigate"
    description = ("Move File Explorer: open a drive ('open D drive'), This PC, go into a folder of the one in front "
                   "('go into projects'), go back, go up a level, or say what's in this folder. action: drive | this_pc | into | "
                   "back | up | list; `name` for into; `letter` for drive. Reads Explorer's real location first and checks "
                   "where it ended up. No permission needed.")
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["drive", "this_pc", "into", "back", "up", "list"]},
                       "name": {"type": "string", "description": "Folder to go into."},
                       "letter": {"type": "string", "description": "Drive letter, e.g. D."}},
        "required": ["action"],
    }

    def __init__(self, desktop: Optional[Desktop] = None, opener: Optional[Callable[[str], None]] = None,
                 drives: Optional[Callable[[], list[str]]] = None) -> None:
        self._desktop = desktop
        self._open = opener or (lambda p: os.startfile(p))  # type: ignore[attr-defined]
        self._drives = drives or fixed_drives

    @property
    def desktop(self) -> Desktop:
        if self._desktop is None:
            self._desktop = Desktop()
        return self._desktop

    # -- routing ---------------------------------------------------------------------
    def matches(self, text: str) -> bool:
        """Drives and This PC always; the relative ones ("go into", "back", "up", "what's in
        here") only when File Explorer is there to be relative to — and "back"/"up" only when
        it is the window in front, so "go back" said over a web page isn't taken for Explorer."""
        action = self.extract_arguments(text).get("action")
        if not action:
            return False
        if action in ("drive", "this_pc"):
            return True
        try:
            if action in ("back", "up"):
                front = self.desktop.foreground_window()
                return bool(front and front.app == "File Explorer")
            return bool(self.desktop.explorer_windows())
        except Exception:  # noqa: BLE001
            return False

    def extract_arguments(self, text: str) -> dict[str, Any]:
        if _THIS_PC.match(text):
            return {"action": "this_pc"}
        m = _DRIVE.match(text)
        if m:
            return {"action": "drive", "letter": (m.group(1) or m.group(2) or m.group(3)).upper()}
        if _LIST.match(text):
            return {"action": "list"}
        if _BACK.match(text):
            return {"action": "back"}
        if _UP.match(text):
            return {"action": "up"}
        m = _INTO.match(text)
        if m:
            name = (m.group("n1") or m.group("n2") or "").strip()
            if name and name.lower() not in _NOT_A_FOLDER:
                return {"action": "into", "name": name}
        return {}

    # -- helpers ---------------------------------------------------------------------
    def _where(self):
        """(explorer window she is working with, its description) — or an error result."""
        win = self.desktop.front_explorer()
        if win is None:
            return None, ToolResult.fail("There's no File Explorer window open. Say 'open Documents' or 'open C drive' first.")
        return win, None

    @staticmethod
    def _describe(win) -> str:
        return win.path or win.name or "This PC"

    def _children(self, win) -> Optional[list[tuple[str, str, bool]]]:
        """(name, path, is_dir) for what's in the window's folder; drives for This PC."""
        if win.is_this_pc:
            return [(f"{d[:1]} drive", d, True) for d in self._drives()]
        if not win.path:
            return None
        try:
            with os.scandir(win.path) as it:
                return [(e.name, e.path, e.is_dir(follow_symlinks=False)) for e in it]
        except OSError:
            return None

    # -- run -------------------------------------------------------------------------
    def run(self, action: str = "", name: str = "", letter: str = "", **kwargs: Any) -> ToolResult:
        action = (action or "").lower()
        try:
            if action == "drive":
                return self._drive(letter)
            if action == "this_pc":
                return self._this_pc()
            if action in ("into", "back", "up", "list"):
                win, err = self._where()
                if err is not None:
                    return err
                return {"into": lambda: self._into(win, name), "back": lambda: self._back(win),
                        "up": lambda: self._up(win), "list": lambda: self._list(win)}[action]()
        except Exception as e:  # noqa: BLE001
            logger.warning("Explorer action %s failed: %s", action, e)
            return ToolResult.fail(f"I couldn't do that in File Explorer: {e}")
        return ToolResult.fail("I can open a drive or This PC, go into a folder, go back or up, or list what's here.")

    def _drive(self, letter: str) -> ToolResult:
        letter = (letter or "").strip()[:1].upper()
        if not letter:
            return ToolResult.fail("Which drive?")
        root = f"{letter}:\\"
        have = [d[:1] for d in self._drives()]
        if letter not in have and not Path(root).exists():
            return ToolResult.fail(f"I don't see a {letter} drive. I can see " + ", ".join(have) + ".")
        self._open(root)
        if self.desktop.explorer_shows(root):
            return ToolResult.ok(f"Opened the {letter} drive.", path=root, verified=True)
        return ToolResult.fail(f"I asked Windows to open the {letter} drive, but I can't see Explorer showing it.", unconfirmed=True)

    def _this_pc(self) -> ToolResult:
        self._open("shell:MyComputerFolder")
        for _ in range(1 + int(8 * self.desktop.wait_scale)):
            if any(e.is_this_pc for e in self.desktop.explorer_windows()):
                return ToolResult.ok("Opened This PC.", verified=True)
            self.desktop._sleep(0.25)
        return ToolResult.fail("I asked Windows to open This PC, but I can't see Explorer showing it.", unconfirmed=True)

    def _into(self, win, name: str) -> ToolResult:
        name = (name or "").strip()
        if not name:
            return ToolResult.fail("Which folder?")
        kids = self._children(win)
        if kids is None:
            return ToolResult.fail(f"I can't read what's in {self._describe(win)}.")
        dirs = [(n, p) for n, p, d in kids if d and n.lower() not in SKIP_DIRS and not n.startswith("$")]
        hits = self._best(dirs, name)
        if not hits and win.path:                       # not directly inside: look a few levels down
            sub = FolderFinder(home=Path(win.path), drives=[], max_seconds=1.2).find_under(Path(win.path), name, True)
            hits = [(m.path.name, str(m.path)) for m in sub.best()]
        if not hits:
            return ToolResult.fail(f"I don't see a {name} folder in {self._describe(win)}.")
        if len(hits) > 1:
            return ToolResult.fail(f"{len(hits)} folders fit {name} in {self._describe(win)}: " + "; ".join(n for n, _ in hits[:3])
                                   + ". Say which one.", ask="folder")
        target_name, target = hits[0]
        now = self.desktop.navigate(win, target)
        if now is not None and now.path and now.path.rstrip("\\").lower() == target.rstrip("\\").lower():
            return ToolResult.ok(f"Now in {target}.", path=target, verified=True)
        where = self._describe(now) if now is not None else "an unknown place"
        return ToolResult.fail(f"I sent Explorer to {target_name}, but it's showing {where} — I can't confirm it went.", unconfirmed=True)

    @staticmethod
    def _best(dirs: list[tuple[str, str]], name: str) -> list[tuple[str, str]]:
        scored = [(tier(name, n), n, p) for n, p in dirs]
        top = max((s for s, _n, _p in scored), default=0)
        return [(n, p) for s, n, p in scored if s and s == top]

    def _back(self, win) -> ToolResult:
        before = self._describe(win)
        self.desktop.explorer.back(win)
        for _ in range(1 + int(8 * self.desktop.wait_scale)):
            now = next((e for e in self.desktop.explorer_windows() if e.hwnd == win.hwnd), None)
            if now is not None and self._describe(now) != before:
                return ToolResult.ok(f"Went back to {self._describe(now)}.", path=now.path, verified=True)
            self.desktop._sleep(0.2)
        return ToolResult.fail(f"I pressed back, but File Explorer is still in {before} — there may be nothing earlier.", unconfirmed=True)

    def _up(self, win) -> ToolResult:
        if win.is_this_pc or not win.path:
            return ToolResult.fail("This PC is as far up as it goes.")
        here = Path(win.path)
        parent = here.parent
        target = str(parent) if parent != here else "shell:MyComputerFolder"
        now = self.desktop.navigate(win, target)
        if parent == here:
            ok = now is not None and now.is_this_pc
            return (ToolResult.ok("Up to This PC.", verified=True) if ok else
                    ToolResult.fail("I sent Explorer up to This PC, but I can't confirm it went.", unconfirmed=True))
        if now is not None and now.path and now.path.rstrip("\\").lower() == str(parent).rstrip("\\").lower():
            return ToolResult.ok(f"Up to {parent}.", path=str(parent), verified=True)
        return ToolResult.fail(f"I sent Explorer up, but I can't confirm it's in {parent}.", unconfirmed=True)

    def _list(self, win) -> ToolResult:
        kids = self._children(win)
        where = self._describe(win)
        if kids is None:
            return ToolResult.fail(f"I can't read what's in {where}.")
        folders = sorted((n for n, _p, d in kids if d and not n.startswith(".")), key=str.lower)
        files = sorted((n for n, _p, d in kids if not d and not n.startswith(".")), key=str.lower)
        if not folders and not files:
            return ToolResult.ok(f"{where} is empty.", path=win.path)
        bits = []
        if folders:
            bits.append(f"{len(folders)} folder{'s' if len(folders) != 1 else ''}: " + ", ".join(folders[:8])
                        + (f" and {len(folders) - 8} more" if len(folders) > 8 else ""))
        if files:
            bits.append(f"{len(files)} file{'s' if len(files) != 1 else ''}: " + ", ".join(files[:6])
                        + (f" and {len(files) - 6} more" if len(files) > 6 else ""))
        return ToolResult.ok(f"{where} has " + "; ".join(bits) + ".", path=win.path, folders=len(folders), files=len(files))
