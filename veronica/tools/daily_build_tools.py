"""Voice controls for daily builds (automation/daily_builds.py)."""
from __future__ import annotations

import re
import threading
from typing import Any, Optional

from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

_BUILDS = r"(?:daily\s+)?(?:builds?|projects?|github\s+(?:builds?|projects?))"
_PAUSE = re.compile(rf"\b(?:pause|stop|halt|turn\s+off|disable)\s+(?:the\s+|your\s+|my\s+)?(?:daily\s+builds?|"
                    rf"building\s+projects|(?:daily\s+)?github\s+(?:builds?|projects?))\b", re.I)
_RESUME = re.compile(rf"\b(?:resume|restart|start|turn\s+on|enable|continue)\s+(?:the\s+|your\s+|my\s+)?"
                     rf"(?:daily\s+builds?|building\s+projects|(?:daily\s+)?github\s+(?:builds?|projects?))\b", re.I)
_SKIP = re.compile(
    r"\b(?:don'?t|do\s+not)\s+(?:do|build|make|start)\s+anything\s+(?:for\s+|else\s+)?today\b|"
    r"\bdo\s+nothing\s+(?:for\s+)?today\b|\bskip\s+(?:today'?s\s+(?:daily\s+)?(?:build|project)|today|"
    r"the\s+(?:daily\s+)?build\s+today)\b|\bno\s+(?:more\s+)?(?:daily\s+)?(?:builds?|projects?|building)\s+today\b|"
    r"^\s*(?:(?:you\s+can|just)\s+)?take\s+(?:the|a)\s+day\s+off\b|\bstop\s+(?:building\s+)?for\s+(?:today|the\s+day)\b", re.I)
_NOW = re.compile(r"\b(?:build\s+(?:something|a\s+project|one)\s+now|start\s+today'?s\s+build|"
                  r"do\s+(?:the|today'?s)\s+(?:daily\s+)?build\s+now)\b", re.I)
_PREFER = re.compile(r"\b(?:build\s+(?:me\s+)?(?:an?\s+)?(?P<a>[\w\s-]{2,40}?)\s+next|"
                     r"next\s+(?:project|build)\s+(?:should\s+be\s+|will\s+be\s+|is\s+)?(?:an?\s+)?(?P<b>[\w\s-]{2,40}?))"
                     r"\s*[.!?]*\s*$", re.I)
_STATUS = re.compile(rf"\bwhat\s+(?:did|have)\s+you\s+(?:build|built|make|made)\b|\bwhat\s+are\s+you\s+building\b|"
                     rf"\b{_BUILDS}\s+(?:status|update|progress)\b|\bhow'?s\s+(?:the\s+|today'?s\s+)?(?:daily\s+)?build\b|"
                     rf"\bstatus\s+of\s+(?:the\s+)?{_BUILDS}\b", re.I)


_SWITCH = re.compile(r"\b(?:use|using|switch|change|swap|move|go\s+back|back\s+to|instead|rather|from\s+now|"
                     r"try|with|through|via|let|make|have|karo|kar\s+do|se\s+banao|ended|expired|over|finished|"
                     r"ran\s+out|khatam)\b", re.I)
_BUILD_CONTEXT = re.compile(r"\b(?:daily|builds?|building|builder|projects?|repos?|coding|programs?|write\s+the\s+code|"
                            r"subscription|plan|limit|quota|banao)\b", re.I)


class DailyBuildsTool(Tool):
    name = "daily_builds"
    description = ("Daily builds: one small project a day, picked by VERONICA and built by a coding tool (Claude Code, "
                   "Antigravity, her own model...), each in a new GitHub repo named after it, in the background. "
                   "Actions: use_builder (change WHICH coding tool writes the code, e.g. 'Claude Code's subscription "
                   "ended, use Antigravity instead': pass the user's whole sentence as text), builders (list them), "
                   "status ('what did you build?'), "
                   "skip_today ('don't do anything today': stop now, carry on tomorrow), pause, resume, now ('build something now'), prefer (the kind of project to build next).")
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["status", "use_builder", "builders", "skip_today", "pause", "resume",
                                              "now", "prefer"]},
        "kind": {"type": "string", "description": "for prefer: e.g. 'game', 'web app', 'python tool'"},
        "text": {"type": "string", "description": "for use_builder: what the user said, in their words"}},
        "required": ["action"]}

    def __init__(self, builder=None) -> None:
        self.builder = builder

    def _action(self, text: str) -> tuple[str, str]:
        t = text or ""
        if _SKIP.search(t):
            return "skip_today", ""
        if _PAUSE.search(t):
            return "pause", ""
        if _RESUME.search(t):
            return "resume", ""
        if _NOW.search(t):
            return "now", ""
        if self._names_a_switch(t):
            return "use_builder", ""
        m = _PREFER.search(t)
        if m:
            return "prefer", (m.group("a") or m.group("b") or "").strip()
        if _STATUS.search(t):
            return "status", ""
        return "", ""

    def _names_a_switch(self, text: str) -> bool:
        """A sentence that names one of the coding tools she can build with (whatever they are on
        this PC) together with a word about switching or building. 'Open Antigravity' is not one."""
        builders = getattr(self.builder, "builders", None)
        if not builders:
            return False
        from veronica.automation.builders import mentions
        found = mentions(text, builders)
        if not found:
            return False
        rest = text.lower()
        for _, s, e in sorted(found, key=lambda f: -f[1]):
            rest = rest[:s] + " " + rest[e:]               # the tools' own names don't count as context
        switching = bool(_SWITCH.search(rest))
        return switching and (len(found) >= 2 or bool(_BUILD_CONTEXT.search(rest)))

    def matches(self, text: str) -> bool:
        return bool(self._action(text)[0])

    def extract_arguments(self, text: str) -> dict[str, Any]:
        action, kind = self._action(text)
        return {"action": action or "status", "kind": kind, "text": text if action == "use_builder" else ""}

    def _use_builder(self, text: str) -> ToolResult:
        b = self.builder
        chosen, named = b.use_builder(text)
        if chosen is None:
            if len(named) >= 2:
                return ToolResult.fail("Which one should I use: " + " or ".join(n.name for n in named) + "?")
            return ToolResult.fail("I didn't catch which coding tool you mean. " + b.builders_report())
        reason = b.why_unusable(chosen)
        if reason:
            return ToolResult.ok(f"Okay, I'll use {chosen.name} for the daily builds, but right now it {reason}. "
                                 "Until that's sorted I'll fall back to another builder.", builder=chosen.name)
        extra = (" It works in its own window, so you'll see it open when I build."
                 if chosen.kind == "window" else "")
        return ToolResult.ok(f"Okay, from now on {chosen.name} writes the code for the daily builds.{extra}",
                             builder=chosen.name)

    def run(self, action: str = "status", kind: str = "", text: str = "", **kwargs: Any) -> ToolResult:
        b = self.builder
        if b is None or not getattr(b.cfg, "enabled", False):
            return ToolResult.ok("Daily builds are switched off in the config (daily_builds.enabled).")
        if action == "skip_today":
            stopped = b.skip_today()
            return ToolResult.ok(("Okay, I've stopped the build I was working on. " if stopped else "Okay. ")
                                 + "Nothing more today; I'll pick it up again tomorrow.")
        if action == "use_builder":
            return self._use_builder(text or kwargs.get("builder") or kind)
        if action == "builders":
            return ToolResult.ok(b.builders_report())
        if action == "pause":
            b.pause()
            return ToolResult.ok("Okay, daily builds are paused. Say 'resume daily builds' to start again.")
        if action == "resume":
            b.resume()
            return ToolResult.ok("Daily builds are back on.")
        if action == "prefer":
            if not kind:
                return ToolResult.fail("What kind of project should I build next?")
            b.prefer(kind)
            return ToolResult.ok(f"Got it, the next project will be a {kind}.")
        if action == "now":
            threading.Thread(target=b.step, name="daily-builds-now", daemon=True).start()
            return ToolResult.ok("Starting a build now in the background. I'll tell you when it's in the repo.")
        return ToolResult.ok(b.status())


def make_builder(config, llm) -> Optional[Any]:
    """The builder for this config, or None when the section is missing."""
    cfg = getattr(config, "daily_builds", None)
    if cfg is None:
        return None
    from veronica.automation.daily_builds import DailyBuilder
    from veronica.tools.registry import _summarizer
    return DailyBuilder(cfg, config.resolve_path("data/daily_builds.json"), idea_maker=_summarizer(llm))
