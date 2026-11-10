"""
Who writes the code for a daily build. Nothing here is wired to one tool: the builders are a
list (config `daily_builds.builders`, or the defaults below), found on this PC at run time, and
the user picks one by voice in their own words ("Claude Code's subscription ended, use
Antigravity instead"). A new tool is a new entry in the config, not new code.

Kinds:
  claude   Claude Code, headless, locked to the project folder (the safest one)
  command  any other command-line coding agent: `command` + `args`, with {folder} and {task}
  window   a coding app with an agent chat but no headless mode (Antigravity): VERONICA opens
           the folder in it, hands the agent the task, and watches the folder until the work
           is there. The app's own settings decide what its agent may do.
  self     VERONICA's own model ladder writes the files (no subscription needed at all)

Whatever builds it, VERONICA's own checks still decide what reaches GitHub.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

DEFAULT_BUILDERS: list[dict] = [
    {"name": "Claude Code", "kind": "claude", "command": "claude",
     "aliases": ["claude code", "claude", "cloud code", "clod code", "claud code"]},
    {"name": "Antigravity", "kind": "window", "command": "antigravity",
     "aliases": ["antigravity", "anti gravity", "anti-gravity", "antigravity ide"],
     "signed_out_log": r"%APPDATA%\Antigravity\logs\language_server.log", "signed_out_text": "not logged into"},
    # Named as she says it ("the code is written by my own model"). Her name is not an alias:
    # "Veronica, use Antigravity" must not count as naming this one.
    {"name": "my own model", "kind": "self",
     "aliases": ["yourself", "your own brain", "your own model", "your own", "on your own", "by yourself",
                 "your brain", "own model", "gemini"]},
]

TASK_LINE = "Read BUILD_BRIEF.md in this folder and do exactly what it says. Work only in this folder."
BRIEF_FILE = "BUILD_BRIEF.md"


@dataclass
class Builder:
    name: str
    kind: str = "command"
    command: str = ""
    aliases: list = field(default_factory=list)
    args: list = field(default_factory=list)        # kind "command": e.g. ["run", "--dir", "{folder}", "{task}"]
    signed_out_log: str = ""                         # a log file that says when the app is signed out
    signed_out_text: str = ""

    def exe(self) -> str:
        return shutil.which(self.command) or "" if self.command else ""

    def installed(self, has_model: bool = True) -> bool:
        return has_model if self.kind == "self" else bool(self.exe())

    def signed_out(self, tail_bytes: int = 4000) -> bool:
        """True when the app's own log says, in its last lines, that nobody is signed in."""
        if not self.signed_out_log or not self.signed_out_text:
            return False
        p = Path(os.path.expandvars(self.signed_out_log))
        try:
            with p.open("rb") as f:
                f.seek(max(0, p.stat().st_size - tail_bytes))
                return self.signed_out_text.lower() in f.read().decode("utf-8", "replace").lower()
        except OSError:
            return False


def load_builders(cfg) -> list[Builder]:
    rows = list(getattr(cfg, "builders", None) or DEFAULT_BUILDERS)
    out = []
    for row in rows:
        if isinstance(row, dict) and row.get("name"):
            known = {k: row[k] for k in Builder.__dataclass_fields__ if k in row}
            out.append(Builder(**known))
    return out


def by_name(builders: list[Builder], name: str) -> Optional[Builder]:
    n = (name or "").strip().lower()
    for b in builders:
        if n and (n == b.name.lower() or n in (a.lower() for a in b.aliases)):
            return b
    return None


# Words around a tool's name that mean "not this one". They only break a tie when the user
# names two tools in one sentence; with one tool named, that tool is simply the answer.
_AWAY_BEFORE = re.compile(r"(?:instead\s+of|rather\s+than|in\s+place\s+of|not|no\s+more|stop\s+using|don'?t\s+use|"
                          r"without|drop|leave|forget|ki\s+jagah|ke\s+bajaye|nahi)\s+(?:the\s+|using\s+|with\s+|my\s+)*$", re.I)
_AWAY_AFTER = re.compile(r"^(?:'s)?\s*(?:subscription|plan|limit|quota|credits?|trial)?\s*(?:has\s+|have\s+|is\s+|are\s+|"
                         r"got\s+|just\s+)*(?:ended|expired|finished|over|ran\s+out|run\s+out|gone|used\s+up|stopped|"
                         r"can'?t|cannot|won'?t|isn'?t\s+working|not\s+working|doesn'?t\s+work|khatam|nahi\s+chal)", re.I)


def mentions(text: str, builders: list[Builder]) -> list[tuple[Builder, int, int]]:
    """Each builder named in the text, with where (start, end) its longest matching name sits."""
    t = (text or "").lower()
    found = []
    for b in builders:
        best = None
        for alias in sorted({b.name.lower(), *[a.lower() for a in b.aliases]}, key=len, reverse=True):
            m = re.search(r"(?<![a-z])" + re.escape(alias).replace(r"\ ", r"[\s-]*") + r"(?![a-z])", t)
            if m and (best is None or (m.end() - m.start()) > (best[1] - best[0])):
                best = (m.start(), m.end())
        if best:
            found.append((b, best[0], best[1]))
    return found


def choose(text: str, builders: list[Builder],
           ask_model: Optional[Callable[[str], str]] = None) -> tuple[Optional[Builder], list[Builder]]:
    """Which builder the user wants from now on. Returns (builder, the ones that were named).

    One named: that one. Two or more ("use Antigravity instead of Claude Code", "Claude Code's
    subscription ended, switch to Antigravity"): the one not being moved away from; still unsure,
    the model is asked which one is meant; still unsure, None and the caller asks the user."""
    found = mentions(text, builders)
    named = [b for b, _, _ in found]
    if len(found) <= 1:
        return (named[0] if named else None), named
    t = (text or "").lower()
    wanted = [b for b, s, e in found if not (_AWAY_BEFORE.search(t[:s]) or _AWAY_AFTER.search(t[e:]))]
    if len(wanted) == 1:
        return wanted[0], named
    if ask_model is not None:
        try:
            names = ", ".join(b.name for b in named)
            answer = ask_model(f"The user said: \"{text}\"\nWhich ONE of these should be used from now on: {names}?\n"
                               "Reply with just that name, or 'unclear'.") or ""
            picked = [b for b in named if b.name.lower() in answer.lower()]
            if len(picked) == 1:
                return picked[0], named
        except Exception:  # noqa: BLE001
            pass
    return None, named


_FILE_BLOCK = re.compile(r"^=== FILE: (?P<path>[^\n=]+?) ===\n(?P<body>.*?)\n=== END FILE ===", re.S | re.M)

SELF_FORMAT = """
Write the whole project now. Reply with every file in exactly this form and nothing else:

=== FILE: relative/path.ext ===
<the file's full contents>
=== END FILE ===
"""


def write_files_from_reply(reply: str, folder: Path, max_files: int = 40, max_bytes: int = 400_000) -> list[str]:
    """Write the files a model replied with into `folder` only. Returns the paths written.
    Absolute paths, '..', .git and anything oversized are refused."""
    written: list[str] = []
    root = folder.resolve()
    for m in _FILE_BLOCK.finditer(reply or ""):
        rel = m.group("path").strip().replace("\\", "/")
        body = m.group("body")
        parts = rel.split("/")
        if (not rel or rel.startswith("/") or ":" in rel or ".." in parts or ".git" in parts
                or len(body.encode("utf-8")) > max_bytes or len(written) >= max_files):
            continue
        target = (root / rel).resolve()
        if root not in target.parents:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body + "\n", encoding="utf-8")
        written.append(rel)
    return written
