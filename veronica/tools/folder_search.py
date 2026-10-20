"""
Finding a folder (or document) by the name the user says — anywhere on the PC, fast.

Where a name lives is not always under the home folder: "placement" can be C:\\placement
or D:\\Placements. The search goes outward and stops as soon as a stage finds a good
match, so the common cases are instant and the worst case is bounded:

  1. a name she resolved before (data/aliases.json), if the folder still exists
  2. the user's folders: Desktop, Documents, Downloads, Pictures, Videos, Music, OneDrive
  3. every fixed drive's root and its first two levels
  4. a deeper breadth-first look — only if nothing good turned up, and only until the
     time or entry budget runs out (it never hangs, whatever the disk looks like)

System and bulk folders are never entered (Windows, Program Files, ProgramData, AppData,
$Recycle.Bin, System Volume Information, node_modules, .git, ...).

Matching is case-insensitive and ignores spaces/underscores/hyphens: "placement" matches
"Placement" and "placement" exactly, "Placements" as the plural, and "placement_docs"
only as a weaker "contains" match. Several equally good matches are never guessed between.
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from veronica.logging_setup import get_logger

logger = get_logger("tools.folder_search")

SKIP_DIRS = {"appdata", "node_modules", ".git", "__pycache__", "$recycle.bin", "windows", "program files",
             "program files (x86)", "programdata", ".venv", "venv", "site-packages", "system volume information",
             "windows.old", "recovery", "$windows.~bt", "$winreagent", "config.msi", "msocache", "perflogs"}
USER_FOLDERS = ("Desktop", "Documents", "Downloads", "Pictures", "Videos", "Music", "OneDrive")

EXACT, VARIANT, CONTAINS = 3, 2, 1


def norm(s: str) -> str:
    return re.sub(r"[\s_\-.]+", " ", (s or "").lower()).strip()


def _singular(s: str) -> str:
    return " ".join(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in s.split())


def tier(query: str, name: str) -> int:
    """3 = same name, 2 = plural/singular or spacing variant, 1 = contains it, 0 = no."""
    q, n = norm(query), norm(name)
    if not q or not n:
        return 0
    if q == n:
        return EXACT
    if _singular(q) == _singular(n) or q.replace(" ", "") == n.replace(" ", ""):
        return VARIANT
    words = q.split()
    if re.search(rf"(?<![a-z0-9]){re.escape(q)}", n) or all(w in n.split() for w in words):
        return CONTAINS
    return 0


@dataclass(order=False)
class Match:
    path: Path
    tier: int
    depth: int = 0

    def sort_key(self):
        return (-self.tier, self.depth, len(str(self.path)), str(self.path).lower())


@dataclass
class SearchResult:
    matches: list[Match] = field(default_factory=list)
    timed_out: bool = False       # stopped by the time or entry budget, not by running out of places
    stage: str = ""

    def best(self) -> list[Match]:
        """The matches in the top tier (what to open, or to choose between)."""
        if not self.matches:
            return []
        top = max(m.tier for m in self.matches)
        return sorted((m for m in self.matches if m.tier == top), key=Match.sort_key)


def fixed_drive_roots() -> list[Path]:
    try:
        import psutil
        roots = []
        for p in psutil.disk_partitions(all=False):
            opts = (p.opts or "").lower()
            if p.fstype and "cdrom" not in opts and "removable" not in opts and "remote" not in opts:
                roots.append(Path(p.mountpoint))
        if roots:
            return sorted(set(roots), key=lambda r: str(r).lower())
    except Exception:  # noqa: BLE001
        pass
    return [Path(f"{d}:\\") for d in "CDEFG" if os.path.isdir(f"{d}:\\")]


class FolderFinder:
    """Searches for a folder/file by spoken name. `drives=None` means 'the real fixed
    drives' only when the real home is used — a test that gives its own home gets no drives."""

    def __init__(self, home: Optional[Path] = None, drives: Optional[list[Path]] = None,
                 max_seconds: float = 1.8, max_entries: int = 80_000, deep_depth: int = 5,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.home = Path(home) if home else Path.home()
        self._real_home = home is None
        self._drives = None if drives is None else [Path(d) for d in drives]
        self.max_seconds, self.max_entries, self.deep_depth = max_seconds, max_entries, deep_depth
        self._clock = clock

    def drives(self) -> list[Path]:
        if self._drives is not None:
            return self._drives
        return fixed_drive_roots() if self._real_home else []

    def user_roots(self) -> list[Path]:
        roots = [self.home]
        roots += [self.home / n for n in USER_FOLDERS]
        return [r for r in roots if r.is_dir()]

    # -- scanning ---------------------------------------------------------------
    def _scan(self, folder: Path, state: dict) -> list[tuple[str, str, bool]]:
        """(name, path, is_dir) for each entry; listings are cached for the whole search so a
        later, deeper stage re-walks them without touching the disk again."""
        key = os.path.normcase(str(folder))
        cache = state["cache"]
        if key in cache:
            return cache[key]
        out: list[tuple[str, str, bool]] = []
        try:
            with os.scandir(folder) as it:
                for e in it:
                    state["entries"] += 1
                    try:
                        out.append((e.name, e.path, e.is_dir(follow_symlinks=False)))
                    except OSError:
                        continue
                    if state["entries"] > state["limit"]:
                        state["over"] = True
                        break
        except OSError:
            pass
        cache[key] = out
        return out

    def _out_of_budget(self, state: dict) -> bool:
        if state["over"] or self._clock() >= state["deadline"]:
            state["over"] = True
        return state["over"]

    def _bfs(self, roots: list[Path], max_depth: int, query: str, want_dir: bool, exts: Optional[set],
             state: dict, found: dict) -> None:
        level = [(r, 0) for r in roots]
        while level:
            nxt = []
            for folder, depth in level:
                if depth >= max_depth:
                    continue
                if self._out_of_budget(state):
                    return
                for name, path, is_dir in self._scan(folder, state):
                    low = name.lower()
                    if low in SKIP_DIRS or low.startswith((".", "$")):
                        continue
                    if is_dir == want_dir and (want_dir or not exts or Path(name).suffix.lower() in exts):
                        t = tier(query, name if is_dir else Path(name).stem)
                        if t:
                            real = os.path.normcase(os.path.realpath(path))
                            if real not in found or found[real].tier < t:
                                found[real] = Match(Path(path), t, depth + 1)
                    if is_dir and depth + 1 < max_depth:
                        nxt.append((Path(path), depth + 1))
                if state["over"]:
                    return
            level = nxt

    def _state(self, seconds: float, limit: int) -> dict:
        return {"entries": 0, "over": False, "cache": {}, "deadline": self._clock() + seconds, "limit": limit}

    def find(self, query: str, want_dir: bool = True, exts: Optional[set] = None, only_drive: Optional[Path] = None,
             deep: bool = False) -> SearchResult:
        """Stage by stage; the first stage that finds an exact or plural/variant match ends the search.
        `deep` gives the last stage a much bigger budget and depth (for 'search harder')."""
        factor = 5 if deep else 1
        state = self._state(self.max_seconds * factor, self.max_entries * factor)
        found: dict[str, Match] = {}
        drives = [Path(only_drive)] if only_drive else self.drives()
        stages = []
        if only_drive is None:
            stages.append(("your folders", lambda: self._bfs(self.user_roots(), 3, query, want_dir, exts, state, found)))
        stages.append(("the drives", lambda: self._bfs(drives, 3, query, want_dir, exts, state, found)))
        stages.append(("deeper", lambda: self._bfs(([self.home] if only_drive is None else []) + drives,
                                                   self.deep_depth + (3 if deep else 0), query, want_dir, exts, state, found)))
        stage_name = ""
        for stage_name, run in stages:
            run()
            if state["over"] or any(m.tier >= VARIANT for m in found.values()):
                break
        return SearchResult(sorted(found.values(), key=Match.sort_key), timed_out=state["over"], stage=stage_name)

    def find_under(self, parent: Path, query: str, want_dir: bool = True, exts: Optional[set] = None) -> SearchResult:
        """Only inside one folder ('veronica folder in projects')."""
        state = self._state(self.max_seconds, self.max_entries)
        found: dict[str, Match] = {}
        self._bfs([Path(parent)], self.deep_depth, query, want_dir, exts, state, found)
        return SearchResult(sorted(found.values(), key=Match.sort_key), timed_out=state["over"], stage="inside")

    def exists_quick(self, query: str) -> Optional[Path]:
        """A very cheap look (user folders + drive roots, first level) used to decide that a
        bare 'open <name>' is a folder — never the deeper stages."""
        state = self._state(0.4, 20_000)
        found: dict[str, Match] = {}
        self._bfs(self.user_roots(), 2, query, True, None, state, found)
        self._bfs(self.drives(), 2, query, True, None, state, found)
        best = [m for m in found.values() if m.tier >= VARIANT]
        if not best:
            return None
        best.sort(key=Match.sort_key)
        return best[0].path


def place_label(path: Path, home: Optional[Path] = None) -> str:
    """How to say where a folder is: 'in Documents', 'on the D drive', 'in Projects, on the C drive'."""
    home = Path(home) if home else Path.home()
    p = Path(path)
    parent = p.parent
    drive = (p.drive or "").rstrip(":\\/")
    if parent == Path(p.anchor):
        return f"on the {drive} drive" if drive else "at the top level"
    try:
        parent.relative_to(home)
        where = parent.name if parent != home else "your user folder"
        return f"in {where}"
    except ValueError:
        return f"in {parent.name}, on the {drive} drive" if drive else f"in {parent.name}"
