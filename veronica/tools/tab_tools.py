"""
Tabs — list them, switch to one, close one or all — in Chrome, Edge, Brave, Firefox,
File Explorer and VS Code.

  no permission : list_tabs, switch_tab
  asks (yes/no) : close_tab (a tab can hold unsaved work) — the same level as close_window

The tab strip is read with UI Automation (vision/uia_tabs.py) so a tab can be named and
targeted; the tools look at what is open before acting ("this", "the docs tab", "all
tabs") and look again afterwards, so what she says is what actually happened:

  "Closed 6 Chrome tabs."                         — all six are gone (checked)
  "Closed 5 of 6 Chrome tabs; one is still open." — it was probably asking to save
  "I pressed close, but I can't confirm ..."      — the strip couldn't be read again

Closing the last tab of a browser or File Explorer window closes that window; she says
so. Tab titles are web pages' words: reported, never obeyed.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Optional

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult
from veronica.vision.uia_tabs import (
    BROWSERS, WINDOW_CLOSES_WITH_LAST_TAB, Strip, Tab, TabReader, canonical_app, titles_removed,
)

logger = get_logger("tools.tabs")

_APP = (r"(?:google\s+chrome|chrome|microsoft\s+edge|edge|brave(?:\s+browser)?|firefox|mozilla\s+firefox|"
        r"(?:windows\s+|file\s+)?explorer|file\s+manager|vs\s*code|vscode|visual\s+studio\s+code|code|browser)")
_IN_APP = rf"(?:\s+(?:in|on|of|from|for)\s+(?:the\s+|my\s+)?(?P<app>{_APP})(?:\s+(?:window|browser|app))?)?"
_END = r"\s*(?:please)?\s*[.!?]*\s*$"
_ORD = r"first|second|third|fourth|fifth|sixth|seventh|eighth|ninth"
_ORDS = {w: i + 1 for i, w in enumerate(_ORD.split("|"))}

_LIST = re.compile(r"^\s*(?:please\s+)?(?:(?:list|read\s+out|read|tell\s+me|name)\s+(?:me\s+)?(?:all\s+)?(?:of\s+)?(?:the\s+|my\s+)?(?:open\s+)?tabs"
                   r"|what\s+tabs\s+(?:are\s+(?:open|up)|do\s+i\s+have(?:\s+open)?)|which\s+tabs\s+(?:are\s+)?open"
                   r"|how\s+many\s+tabs(?:\s+(?:are\s+open|do\s+i\s+have(?:\s+open)?|are\s+there))?)"
                   rf"{_IN_APP}{_END}", re.I)
_SWITCH = re.compile(r"^\s*(?:please\s+)?(?:(?:switch|go|jump|move|change|flip)\s+(?:over\s+)?to\s+(?:the\s+|my\s+)?|(?=(?:next|previous)\s+tab))"
                     rf"(?:(?P<rel>next|previous|last)\s+tab|tab\s+(?:number\s+)?(?P<num>\d+)|(?P<ord>{_ORD})\s+tab|(?P<name>.+?)\s+tab)"
                     rf"{_IN_APP}{_END}", re.I)
_CLOSE = re.compile(r"^\s*(?:please\s+)?close\s+(?:"
                    r"(?:the\s+|my\s+)?(?P<cur>this|current|active)\s+tab"
                    r"|tab\s+(?:number\s+)?(?P<num>\d+)"
                    rf"|(?:the\s+)?(?P<ord>{_ORD})\s+tab"
                    r"|(?P<all>all|every|all\s+of)\s+(?:the\s+|my\s+)?(?:open\s+)?(?:(?P<allname>[\w .'-]+?)\s+)?tabs?"
                    r"|(?:the\s+)?tab\s+(?:named|called|titled)\s+(?P<name2>.+?)"
                    r"|(?:the\s+)?(?P<name>.+?)\s+tab)"
                    rf"{_IN_APP}{_END}", re.I)
# A phrase that is really "tabs" — close_window must leave these alone.
TAB_PHRASE = re.compile(r"\btabs?\b", re.I)


def _short(title: str, n: int = 42) -> str:
    t = title or "untitled"
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def match_tabs(tabs: list[Tab], name: str) -> list[Tab]:
    """The best-matching tabs for a spoken name: exact title, then title containing the
    phrase, then every word present. Only the top tier is returned."""
    q = _norm(name)
    if not q:
        return []
    words = q.split()
    tiers: dict[int, list[Tab]] = {3: [], 2: [], 1: []}
    for t in tabs:
        title = _norm(t.title)
        if title == q:
            tiers[3].append(t)
        elif q in title:
            tiers[2].append(t)
        elif all(w in title.split() or w in title for w in words):
            tiers[1].append(t)
    for level in (3, 2, 1):
        if tiers[level]:
            return tiers[level]
    return []


def budget_left(strip: Strip, targets: list[Tab]) -> list[str]:
    """Titles of the targeted tabs still present in `strip`."""
    still = Counter(t.title for t in strip.tabs)
    return [t.title for t in targets if still[t.title] > 0]


def _args(m: re.Match) -> dict[str, Any]:
    app = (m.groupdict().get("app") or "").strip()
    return {"app": app} if app else {}


class _TabTool(Tool):
    def __init__(self, reader: Optional[TabReader] = None) -> None:
        self._reader = reader

    @property
    def reader(self) -> TabReader:
        if self._reader is None:
            self._reader = TabReader()
        return self._reader

    # -- which strips? ---------------------------------------------------------------
    def _app_strips(self, app_word: str) -> tuple[Optional[list[Strip]], Optional[ToolResult]]:
        w = (app_word or "").strip().lower()
        if re.sub(r"^(?:the|my)\s+", "", w) in ("browser", "web browser"):
            strips = [s for s in self.reader.strips() if s.app in BROWSERS]
            label = "browser"
        else:
            label = canonical_app(app_word)
            if label is None:
                return None, ToolResult.fail(f"I can read tabs in Chrome, Edge, Brave, Firefox, File Explorer and VS Code — not {app_word}.")
            strips = self.reader.strips(label)
        if not strips:
            return None, ToolResult.fail(f"I don't see any {label} tabs open.")
        return strips, None

    def _which_app(self, strips: list[Strip]) -> Optional[ToolResult]:
        apps = list(dict.fromkeys(s.app for s in strips))
        if len(apps) > 1:
            return ToolResult.fail("Tabs are open in " + " and ".join(apps) + ". Which one do you mean?", ask="app", apps=apps)
        return None

    def _choose_strips(self, app_word: str, prefer_front: bool) -> tuple[Optional[list[Strip]], Optional[ToolResult]]:
        """The strips an instruction is about: the named app's, else (for 'this/current')
        the window in front, else the one app that has tabs. Several apps → ask."""
        if app_word:
            return self._app_strips(app_word)
        if prefer_front:
            front = self.reader.front_strip()
            if front is not None:
                return [front], None
        strips = self.reader.strips()
        if not strips:
            return None, ToolResult.fail("I don't see any tabs open that I can read.")
        asked = self._which_app(strips)
        if asked is not None:
            return None, asked
        return strips, None


class ListTabsTool(_TabTool):
    name = "list_tabs"
    description = ("List the open tabs (Chrome, Edge, Brave, Firefox, File Explorer, VS Code) — all of them, or one app's "
                   "(`app`). Also answers 'how many tabs are open'. No permission needed.")
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"app": {"type": "string", "description": "e.g. Chrome, Edge, Explorer, VS Code."}},
                  "required": []}

    def matches(self, text: str) -> bool:
        return bool(_LIST.match(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _LIST.match(text)
        return _args(m) if m else {}

    def run(self, app: str = "", **kwargs: Any) -> ToolResult:
        if app:
            strips, err = self._app_strips(app)
        else:
            strips, err = self.reader.strips(), None
            if not strips:
                err = ToolResult.fail("I don't see any tabs open that I can read.")
        if err is not None:
            return err
        parts, total = [], 0
        for s in strips:
            total += len(s.tabs)
            shown = "; ".join(("current: " if t.selected else "") + _short(t.title) for t in s.tabs[:8])
            more = f"; and {len(s.tabs) - 8} more" if len(s.tabs) > 8 else ""
            parts.append(f"{s.app} has {_plural(len(s.tabs), 'tab')}: {shown}{more}")
        return ToolResult.ok(". ".join(parts) + ".", tabs=total,
                             windows=[{"app": s.app, "titles": [t.title for t in s.tabs]} for s in strips])


class SwitchTabTool(_TabTool):
    name = "switch_tab"
    description = ("Switch to a tab by name ('the Gmail tab'), number, or next/previous, in Chrome, Edge, Brave, Firefox, "
                   "File Explorer or VS Code. No permission needed; never closes anything.")
    permission = PermissionLevel.SAFE
    parameters = {"type": "object",
                  "properties": {"name": {"type": "string", "description": "Words from the tab's title."},
                                 "number": {"type": "integer", "description": "1 = the first tab."},
                                 "relative": {"type": "string", "enum": ["next", "previous", "last"]},
                                 "app": {"type": "string"}},
                  "required": []}

    def matches(self, text: str) -> bool:
        m = _SWITCH.match(text)
        # "switch to the chrome tab" is a window, not a tab title; "switch to tab" alone isn't a command
        return bool(m)

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _SWITCH.match(text)
        if not m:
            return {}
        a = _args(m)
        if m.group("rel"):
            a["relative"] = m.group("rel").lower()
        elif m.group("num"):
            a["number"] = int(m.group("num"))
        elif m.group("ord"):
            a["number"] = _ORDS[m.group("ord").lower()]
        elif m.group("name"):
            a["name"] = m.group("name").strip()
        return a

    def run(self, name: str = "", number: int = 0, relative: str = "", app: str = "", **kwargs: Any) -> ToolResult:
        name = (name or "").strip()
        if name and canonical_app(name):          # "switch to the Chrome tab" = Chrome's current tab
            app, name = app or name, ""
            if not (number or relative):
                return ToolResult.fail("Which tab? Say its name or number.")
        if name and not app:                      # a tab by name is looked for in every app
            strips, err = self.reader.strips(), None
            if not strips:
                return ToolResult.fail("I don't see any tabs open that I can read.")
        else:
            strips, err = self._choose_strips(app, prefer_front=bool(number or relative))
            if err is not None:
                return err
        target: Optional[Tab] = None
        strip: Optional[Strip] = None
        if relative:
            strip = strips[0]
            cur = strip.current
            if not cur or len(strip.tabs) < 2:
                return ToolResult.fail(f"{strip.app} has only one tab." if len(strip.tabs) == 1 else "I can't tell which tab is current.")
            i = cur.index - 1
            j = len(strip.tabs) - 1 if relative == "last" else (i + 1) % len(strip.tabs) if relative == "next" else (i - 1) % len(strip.tabs)
            target = strip.tabs[j]
        elif number:
            strip = strips[0]
            if not 1 <= int(number) <= len(strip.tabs):
                return ToolResult.fail(f"{strip.app} has {_plural(len(strip.tabs), 'tab')}; there's no tab {number}.")
            target = strip.tabs[int(number) - 1]
        elif name:
            hits = [(s, t) for s in strips for t in match_tabs(s.tabs, name)]
            if not hits:
                return ToolResult.fail(f"I don't see a tab called {name}.")
            if len(hits) > 1:
                return ToolResult.fail(f"{len(hits)} tabs match {name}: " + "; ".join(f"{s.app}, {_short(t.title, 30)}" for s, t in hits[:3])
                                       + ". Say more of the name, or its number.", ask="tab")
            strip, target = hits[0]
        else:
            return ToolResult.fail("Which tab? Say its name, its number, or next or previous.")
        if not self.reader.focus(strip.hwnd):
            logger.info("Switching tab without bringing %s forward", strip.app)
        try:
            self.reader.select(strip, target)
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"I couldn't switch to that tab in {strip.app}: {e}")
        now = self._reselect(strip.hwnd)
        if now is not None and now.current is not None and now.current.title == target.title:
            return ToolResult.ok(f"Switched to {_short(target.title)} in {strip.app}.", title=target.title, app=strip.app)
        return ToolResult.fail(f"I asked {strip.app} to switch to {_short(target.title)}, but I can't confirm it did.",
                               unconfirmed=True)

    def _reselect(self, hwnd: int) -> Optional[Strip]:
        for _ in range(4):
            strip = self.reader.strip_for(hwnd)
            if strip is not None and strip.current is not None:
                return strip
            self.reader._sleep(0.15)
        return None


class CloseTabTool(_TabTool):
    name = "close_tab"
    description = ("Close tabs: the current/'this' tab (scope 'current'), tabs by name (scope 'named', `name` = words from "
                   "the title; `match_all` for every match), a numbered tab (`number`), or every tab of an app (scope 'all', "
                   "`app`). Asks first — a tab can hold unsaved work. Reports what was really closed. Closing the last tab "
                   "of a browser or Explorer window closes that window.")
    permission = PermissionLevel.SENSITIVE
    parameters = {"type": "object",
                  "properties": {"scope": {"type": "string", "enum": ["current", "named", "all", "number"]},
                                 "name": {"type": "string"}, "number": {"type": "integer"},
                                 "app": {"type": "string"}, "match_all": {"type": "boolean"}},
                  "required": ["scope"]}

    def matches(self, text: str) -> bool:
        return bool(_CLOSE.match(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _CLOSE.match(text)
        if not m:
            return {}
        a = _args(m)
        if m.group("cur"):
            a["scope"] = "current"
        elif m.group("num") or m.group("ord"):
            a["scope"], a["number"] = "number", int(m.group("num")) if m.group("num") else _ORDS[m.group("ord").lower()]
        elif m.group("all"):
            allname = (m.group("allname") or "").strip()
            if allname and allname.lower() not in ("of", "the", "my", "open"):
                a.update({"scope": "named", "name": allname, "match_all": True})
            else:
                a["scope"] = "all"
        elif m.group("name2"):
            a.update({"scope": "named", "name": m.group("name2").strip()})
        else:
            name = m.group("name").strip()
            if canonical_app(name) or re.sub(r"^(?:the|my)\s+", "", name.lower()) in ("browser", "web browser"):
                a.update({"scope": "current", "app": a.get("app") or name})
            else:
                a.update({"scope": "named", "name": name})
        return a

    def run(self, scope: str = "current", name: str = "", number: int = 0, app: str = "", match_all: bool = False,
            **kwargs: Any) -> ToolResult:
        scope = (scope or "current").lower()
        if scope == "all":
            strips, err = self._choose_strips(app, prefer_front=False)
            if err is not None:
                return err
            jobs = [(s, list(s.tabs)) for s in strips]
        elif scope in ("current", "number"):
            strips, err = self._choose_strips(app, prefer_front=True)
            if err is not None:
                return err
            strip = strips[0]
            if scope == "number":
                if not 1 <= int(number or 0) <= len(strip.tabs):
                    return ToolResult.fail(f"{strip.app} has {_plural(len(strip.tabs), 'tab')}; there's no tab {number}.")
                tab = strip.tabs[int(number) - 1]
            else:
                tab = strip.current
                if tab is None:
                    return ToolResult.fail(f"I can't tell which {strip.app} tab is the current one — name it, or say its number.")
            jobs = [(strip, [tab])]
        elif scope == "named":
            if not (name or "").strip():
                return ToolResult.fail("Which tab should I close?")
            strips, err = self._choose_strips(app, prefer_front=False) if app else (self.reader.strips(), None)
            if err is not None:
                return err
            hits = [(s, t) for s in strips for t in match_tabs(s.tabs, name)]
            if not hits:
                return ToolResult.fail(f"I don't see a tab called {name}, so I closed nothing.")
            if len(hits) > 1 and not match_all:
                return ToolResult.fail(f"{len(hits)} tabs match {name}: " + "; ".join(f"{s.app}, {_short(t.title, 30)}" for s, t in hits[:3])
                                       + ". I closed nothing — say more of the name, or its number, or 'close all' of them.", ask="tab")
            jobs = []
            for s in strips:
                mine = [t for hs, t in hits if hs is s]
                if mine:
                    jobs.append((s, mine))
        else:
            return ToolResult.fail("I can close this tab, a tab by name or number, or all of an app's tabs.")
        return self._close_jobs(jobs)

    def _close_jobs(self, jobs: list[tuple[Strip, list[Tab]]]) -> ToolResult:
        reports = [self._close_in_window(strip, targets) for strip, targets in jobs]
        wanted = sum(r["wanted"] for r in reports)
        closed = sum(r["closed"] for r in reports)
        unknown = any(r["unknown"] for r in reports)
        by_app: dict[str, list[dict]] = {}
        for r in reports:
            by_app.setdefault(r["app"], []).append(r)
        sentences = []
        for app, rs in by_app.items():
            c, w = sum(r["closed"] for r in rs), sum(r["wanted"] for r in rs)
            gone = sum(1 for r in rs if r["window_gone"])
            left = [t for r in rs for t in r["left"]]
            if any(r["unknown"] for r in rs) and c == 0:
                sentences.append(f"I pressed close on {_plural(w, app + ' tab')}, but I can't confirm it worked")
                continue
            if c == 0:
                n = len(left)
                sentences.append(f"{'The' if n == 1 else 'Those'} {_plural(n, app + ' tab')} {'is' if n == 1 else 'are'} still open"
                                 " — probably asking to save")
                continue
            s = f"Closed { _plural(c, app + ' tab') }" if c == w else f"Closed {c} of {_plural(w, app + ' tab')}"
            if gone and app in WINDOW_CLOSES_WITH_LAST_TAB:
                s += f"; that closed the {app} window too" if gone == 1 else f"; that closed {gone} {app} windows too"
            if left:
                n = len(left)
                s += f"; {n} {'is' if n == 1 else 'are'} still open — probably asking to save"
            elif any(r["unknown"] for r in rs):
                s += "; I can't confirm the rest"
            sentences.append(s)
        message = ". ".join(sentences) + "."
        if closed == wanted and not unknown:
            return ToolResult.ok(message, closed=closed, wanted=wanted)
        return ToolResult.fail(message, closed=closed, wanted=wanted, unconfirmed=unknown)

    def _close_in_window(self, strip: Strip, targets: list[Tab]) -> dict[str, Any]:
        """Close `targets` (the tabs she looked at) one at a time, re-reading the strip
        after each, then report from a final read — not from what was pressed."""
        r = self.reader
        before = [t.title for t in strip.tabs]
        budget = Counter(t.title for t in targets)
        wanted = len(targets)
        unknown = False
        current = strip
        for _ in range(wanted + 2):
            pick = next((t for t in current.tabs if budget[t.title] > 0), None)
            if pick is None:
                break
            budget[pick.title] -= 1
            count = len(current.tabs)
            try:
                r.close(current, pick)
            except Exception as e:  # noqa: BLE001
                logger.warning("Couldn't close tab %r in %s: %s", pick.title[:40], strip.app, e)
                continue
            nxt = r.settle(strip.hwnd, count)
            if nxt is None:
                current = Strip(strip.hwnd, strip.app, strip.window_title, [])
                break
            if not nxt.tabs:
                current = nxt                                 # no editor tabs left (VS Code), or the window is going
                break
            current = nxt
        gone = not r.window_exists(strip.hwnd)
        after_titles: list[str] = []
        if not gone:
            final = r.read_window(strip.hwnd)
            if final is not None and any(budget_left(final, targets)):
                r._sleep(1.0)                                  # Electron/Chromium update their tree a beat late
                final = r.read_window(strip.hwnd)
            # A browser or Explorer window with zero readable tabs is a window we can't see into;
            # VS Code with every editor closed really has none.
            if final is None or not final.readable or (not final.tabs and strip.app in WINDOW_CLOSES_WITH_LAST_TAB):
                unknown = True
            else:
                after_titles = [t.title for t in final.tabs]
        removed = titles_removed(before, after_titles) if (gone or not unknown) else []
        wanted_titles = Counter(t.title for t in targets)
        closed_targets = 0
        left: list[str] = []
        pool = Counter(removed)
        for title, n in wanted_titles.items():
            took = min(n, pool[title])
            pool[title] -= took
            closed_targets += took
            left += [title] * (n - took)
        if unknown and not gone:
            closed_targets = min(closed_targets, wanted)
        return {"app": strip.app, "wanted": wanted, "closed": closed_targets, "left": left if not unknown else [],
                "window_gone": gone, "unknown": unknown and not gone}
