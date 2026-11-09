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


class DailyBuildsTool(Tool):
    name = "daily_builds"
    description = ("Daily builds: one small project a day, picked by VERONICA and built by Claude Code, each in a "
                   "new GitHub repo named after it, in the background. Actions: status ('what did you build?'), "
                   "skip_today ('don't do anything today': stop now, carry on tomorrow), pause, resume, now ('build something now'), prefer (the kind of project to build next).")
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["status", "skip_today", "pause", "resume", "now", "prefer"]},
        "kind": {"type": "string", "description": "for prefer: e.g. 'game', 'web app', 'python tool'"}},
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
        m = _PREFER.search(t)
        if m:
            return "prefer", (m.group("a") or m.group("b") or "").strip()
        if _STATUS.search(t):
            return "status", ""
        return "", ""

    def matches(self, text: str) -> bool:
        return bool(self._action(text)[0])

    def extract_arguments(self, text: str) -> dict[str, Any]:
        action, kind = self._action(text)
        return {"action": action or "status", "kind": kind}

    def run(self, action: str = "status", kind: str = "", **kwargs: Any) -> ToolResult:
        b = self.builder
        if b is None or not getattr(b.cfg, "enabled", False):
            return ToolResult.ok("Daily builds are switched off in the config (daily_builds.enabled).")
        if action == "skip_today":
            stopped = b.skip_today()
            return ToolResult.ok(("Okay, I've stopped the build I was working on. " if stopped else "Okay. ")
                                 + "Nothing more today; I'll pick it up again tomorrow.")
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
