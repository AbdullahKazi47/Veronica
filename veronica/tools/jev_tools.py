"""
Jev by voice (the decision layer — core/jev.py):
  "Jev, should I take the metro or an auto to work?"   "help me choose between a Pixel and an iPhone"
  "compare Python and JavaScript for a beginner"        "should I study tonight or tomorrow morning?"
  "Jev, why?"  /  "why did you pick that?"              -> the scores behind the last recommendation
  "Jev, how would you handle 'minimise all'?"           -> which command / model a request goes to
Speech recognition often hears "Jev" as "Jeff", "Chev" or "Jeeves"; those work too.
"""
from __future__ import annotations

import re
import time
from typing import Any, Optional

from veronica.core.jev import Jev, explain_decision, explain_route, say_decision
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

_JEV = r"(?:jev|jeff|jeeves|chev|jav|jab|jaev)"
_ROUTE = re.compile(rf"^\s*(?:{_JEV}\s*,?\s*)?how\s+would\s+you\s+handle\s+[\"']?(?P<q>.+?)[\"']?\s*\??\s*$|"
                    r"^\s*which\s+(?:tool|model|command)\s+(?:would\s+(?:you\s+use|handle)|handles)\s+(?:for\s+)?[\"']?(?P<q2>.+?)[\"']?\s*\??\s*$",
                    re.I)
_WHY = re.compile(rf"^\s*(?:{_JEV}\s*,?\s*)?why\s*\??\s*$|^\s*why\s+(?:did|would)\s+you\s+(?:pick|choose|say|recommend|go\s+with)\s+"
                  r"(?:that|this|it)\b|^\s*explain\s+(?:that|your|the)\s+(?:decision|recommendation|choice|scores?)\b", re.I)
_DECIDE = re.compile(
    rf"^\s*{_JEV}\s*[,:]?\s+(?P<q>.+)$|"
    r"\bhelp\s+me\s+(?:decide|choose|pick)\b|"
    r"^\s*(?:should\s+i|do\s+i|would\s+it\s+be\s+better\s+to)\b.+\bor\b.+|"
    r"\bwhich\s+(?:is|one\s+is|would\s+be)\s+better\b|\bwhich\s+(?:one\s+)?should\s+i\s+(?:pick|choose|get|buy|take|go\s+with)\b|"
    r"^\s*compare\s+.+\s+(?:and|vs\.?|versus|with|to)\s+.+", re.I)
_WHY_WINDOW_S = 30 * 60


class JevDecideTool(Tool):
    name = "jev_decide"
    description = ("Jev, the decision layer: for a choice between options ('should I X or Y', 'help me choose', "
                   "'compare A and B'), score each option on the criteria that matter and recommend one, with the "
                   "main risk. `question` is the decision in the user's words.")
    permission = PermissionLevel.SAFE          # advice only: Jev never acts
    parameters = {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}

    def __init__(self, jev: Jev) -> None:
        self.jev = jev

    def matches(self, text: str) -> bool:
        if _ROUTE.match(text) or _WHY.match(text):
            return False
        return bool(_DECIDE.search(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = re.match(rf"^\s*{_JEV}\s*[,:]?\s+(?P<q>.+)$", text, re.I)
        self._last_addressed = bool(m)          # "Jev, ..." -> he answers as Jev
        return {"question": (m.group("q") if m else text).strip(), "as_jev": bool(m)}

    def run(self, question: str = "", **kwargs: Any) -> ToolResult:
        if not question.strip():
            return ToolResult.fail("What are you deciding between?")
        try:
            d = self.jev.decide(question)
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Jev couldn't work that one out right now: {e}")
        as_jev = bool(kwargs.get("as_jev", getattr(self, "_last_addressed", False)))
        return ToolResult.ok(say_decision(d, as_jev=as_jev), ranking=d.ranking, totals=d.totals,
                             close_call=d.close_call)


class JevWhyTool(Tool):
    name = "jev_why"
    description = "Explain the scores and criteria behind Jev's last recommendation."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, jev: Jev) -> None:
        self.jev = jev

    def matches(self, text: str) -> bool:
        if not _WHY.match(text):
            return False
        d = self.jev.last()
        return d is not None and time.time() - d.at < _WHY_WINDOW_S

    def run(self, **kwargs: Any) -> ToolResult:
        d = self.jev.last()
        if d is None:
            return ToolResult.fail("Jev hasn't made a recommendation yet.")
        return ToolResult.ok(explain_decision(d))


class JevRouteTool(Tool):
    name = "jev_route"
    description = "Explain which quick command or model VERONICA would use for a request, and why (doesn't run it)."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}

    def __init__(self, registry, router=None, analyzer=None) -> None:
        self.registry, self.router, self.analyzer = registry, router, analyzer

    def matches(self, text: str) -> bool:
        return bool(_ROUTE.match(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _ROUTE.match(text)
        return {"request": (m.group("q") or m.group("q2")).strip()} if m else {}

    def run(self, request: str = "", **kwargs: Any) -> ToolResult:
        if not request:
            return ToolResult.fail("Which request?")
        return ToolResult.ok(explain_route(request, self.registry, self.router, self.analyzer))
