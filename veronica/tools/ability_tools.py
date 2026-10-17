"""
Abilities by voice:
  "add an ability for flight tracking"  /  "build a skill that tells me cricket scores"
      -> she writes it, safety-checks it, tests it, and tells you what it does and which sites it uses
  "activate it"  /  "switch on the flight tracking ability"     -> asks yes/no, then it's live
  "what abilities have you added?"   /   "remove the flight tracking ability"  (asks)
"""
from __future__ import annotations

import re
import time
from typing import Any

from veronica.core.abilities import AbilityManager
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

_BUILD = re.compile(
    r"^\s*(?:please\s+)?(?:can\s+you\s+)?(?:add|build|create|make|give\s+yourself|teach\s+yourself|get)\s+"
    r"(?:a\s+|an\s+|the\s+|new\s+|yourself\s+)*(?:ability|skill|feature|capability|power)\s+"
    r"(?:for|to|that|which|so\s+(?:that\s+)?you\s+can|where\s+you)?\s*(?P<what>.+?)\s*[.!]?\s*$|"
    r"^\s*(?:please\s+)?(?:add|build|create)\s+(?:a\s+|an\s+|the\s+)?(?P<what2>.+?)\s+(?:ability|skill|feature)\s*[.!]?\s*$", re.I)
_ACTIVATE = re.compile(
    r"^\s*(?:yes\s*,?\s*|ok(?:ay)?\s*,?\s*)?(?:please\s+)?(?:activate|enable|switch\s+on|turn\s+on|install|start\s+using)\s+"
    r"(?:(?P<it>it|that|this|that\s+one)|(?:the\s+|my\s+)?(?P<name>.+?)\s+(?:ability|skill))\s*[.!]?\s*$", re.I)
_REMOVE = re.compile(r"^\s*(?:please\s+)?(?:remove|delete|uninstall|disable|turn\s+off|switch\s+off|get\s+rid\s+of)\s+"
                     r"(?:the\s+|my\s+)?(?P<name>.+?)\s+(?:ability|skill)\s*[.!]?\s*$", re.I)
_LIST = re.compile(r"\b(?:what|which|list|show)\b.*\b(?:abilities|skills)\b|\bnew\s+abilities\b", re.I)
_PENDING_S = 15 * 60


class BuildAbilityTool(Tool):
    name = "build_ability"
    description = ("Give VERONICA a NEW ability when the user asks her to add one (e.g. 'add flight tracking', "
                   "'learn to check cricket scores'): she writes, safety-checks and tests a small add-on that "
                   "looks information up on the internet. It is NOT switched on until the user approves it. "
                   "`request` is what the ability should do. Abilities can't control the PC.")
    permission = PermissionLevel.MODERATE     # building runs only safety-checked, web-read-only test code
    parameters = {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}

    def __init__(self, manager: AbilityManager) -> None:
        self.manager = manager

    def matches(self, text: str) -> bool:
        return bool(_BUILD.match(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _BUILD.match(text)
        return {"request": (m.group("what") or m.group("what2")).strip()} if m else {}

    def run(self, request: str = "", **kwargs: Any) -> ToolResult:
        if not request.strip():
            return ToolResult.fail("What should the new ability do?")
        res = self.manager.build(request)
        if not res["ok"]:
            return ToolResult.fail(res["message"])
        name = res["name"].replace("_", " ")
        sites = ", ".join(res["hosts"]) or "no websites"
        passed = sum(1 for r in res["results"] if r["ok"])
        sample = res["results"][0]["output"] if res["results"] else ""
        return ToolResult.ok(
            f"I built a {name} ability. {res['summary']} It uses {sites}. It passed {passed} of "
            f"{len(res['results'])} tests" + (f"; for example: {sample[:160]}" if sample else "") +
            ". It's not switched on yet; say 'activate it' when you want it.", name=res["name"], hosts=res["hosts"])


class ActivateAbilityTool(Tool):
    name = "activate_ability"
    description = "Switch on an ability VERONICA built (asks the user first). `name` is the ability's name."
    permission = PermissionLevel.SENSITIVE    # new code becomes part of her: always a yes/no
    parameters = {"type": "object", "properties": {"name": {"type": "string"}}, "required": []}

    def __init__(self, manager: AbilityManager) -> None:
        self.manager = manager

    def _fresh_pending(self) -> str:
        m = self.manager
        return m.pending if m.pending and time.time() - m.pending_at < _PENDING_S else ""

    def matches(self, text: str) -> bool:
        m = _ACTIVATE.match(text)
        if not m:
            return False
        return bool(self._fresh_pending()) if m.group("it") else self.manager.find(m.group("name") or "") is not None

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _ACTIVATE.match(text)
        if m and m.group("name"):
            return {"name": self.manager.find(m.group("name")) or m.group("name")}
        return {"name": self._fresh_pending()}

    def run(self, name: str = "", **kwargs: Any) -> ToolResult:
        name = self.manager.find(name) if name else self._fresh_pending()
        if not name:
            return ToolResult.fail("Which ability? I don't have one waiting.")
        ok, msg = self.manager.activate(name)
        return ToolResult.ok(msg + " Just ask me naturally and I'll use it.") if ok else ToolResult.fail(msg)


class ListAbilitiesTool(Tool):
    name = "list_abilities"
    description = "List the abilities the user has added to VERONICA."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, manager: AbilityManager) -> None:
        self.manager = manager

    def matches(self, text: str) -> bool:
        return bool(_LIST.search(text))

    def run(self, **kwargs: Any) -> ToolResult:
        items = self.manager.list()
        waiting = self.manager.pending
        if not items:
            tail = f" One is built and waiting: {waiting.replace('_', ' ')}; say 'activate it'." if waiting else ""
            return ToolResult.ok("You haven't added any abilities yet. Try: 'add an ability for flight tracking'." + tail)
        names = [f"{i['name'].replace('_', ' ')} (uses {', '.join(i['hosts'])})" for i in items]
        return ToolResult.ok("Added abilities: " + "; ".join(names) + ".", names=[i["name"] for i in items])


class RemoveAbilityTool(Tool):
    name = "remove_ability"
    description = "Remove an ability the user added (asks first; the code is kept aside, not destroyed)."
    permission = PermissionLevel.SENSITIVE
    parameters = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

    def __init__(self, manager: AbilityManager) -> None:
        self.manager = manager

    def matches(self, text: str) -> bool:
        m = _REMOVE.match(text)
        return bool(m) and self.manager.find(m.group("name")) is not None

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _REMOVE.match(text)
        return {"name": self.manager.find(m.group("name")) or m.group("name")} if m else {}

    def run(self, name: str = "", **kwargs: Any) -> ToolResult:
        found = self.manager.find(name)
        if not found or not self.manager.remove(found):
            return ToolResult.fail(f"I don't have an added ability called {name}.")
        return ToolResult.ok(f"Removed the {found.replace('_', ' ')} ability.")
