"""
Teach VERONICA how she mishears you:
  "when I say attacks, I mean tabs"   -> every transcript gets "attacks" -> "tabs"
  "you heard crystal but I said restore"
  "what corrections do you have?" / "forget the correction for attacks"
"""
from __future__ import annotations

import re
from typing import Any, Optional

from veronica.safety.permissions import PermissionLevel
from veronica.speech.corrections import Corrections
from veronica.tools.base import Tool, ToolResult

_Q = r"[\"'“”‘’]?"
_TEACH = re.compile(
    rf"^\s*(?:please\s+)?(?:when|if|whenever)\s+i\s+say\s+{_Q}(?P<heard>.+?){_Q}\s*,?\s+i\s+mean\s+{_Q}(?P<meant>.+?){_Q}\s*[.!]?\s*$|"
    rf"^\s*(?:you\s+)?(?:heard|wrote|got)\s+{_Q}(?P<heard2>.+?){_Q}\s*,?\s+(?:but|when)\s+i\s+said\s+{_Q}(?P<meant2>.+?){_Q}\s*[.!]?\s*$|"
    rf"^\s*(?:you\s+misheard|that\s+was\s+wrong)\s*,?\s+i\s+said\s+{_Q}(?P<meant3>.+?){_Q}\s+not\s+{_Q}(?P<heard3>.+?){_Q}\s*[.!]?\s*$",
    re.I)
_LIST = re.compile(r"\b(?:what|which|list|show)\b.*\bcorrections?\b|\bwhat\s+(?:words\s+)?do\s+you\s+mishear\b", re.I)
_FORGET = re.compile(rf"^\s*(?:please\s+)?(?:forget|remove|delete|stop)\s+(?:the\s+)?correct(?:ion|ing)\s+(?:for\s+)?{_Q}(.+?){_Q}\s*[.!]?\s*$", re.I)


def _corrections(config) -> Corrections:
    rel = getattr(getattr(config, "stt", None), "corrections_path", "data/stt_corrections.json")
    return Corrections(config.resolve_path(rel) if config is not None else rel)


class SpeechCorrectionTool(Tool):
    name = "speech_correction"
    description = ("Teach or manage how VERONICA corrects mishearings of the user's voice. "
                   "action 'add' with heard + meant ('when I say attacks I mean tabs'), 'remove' with heard, "
                   "or 'list'.")
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "remove", "list"]},
            "heard": {"type": "string", "description": "What speech recognition writes."},
            "meant": {"type": "string", "description": "What the user actually says."},
        },
        "required": ["action"],
    }

    def __init__(self, config=None, corrections: Optional[Corrections] = None) -> None:
        self.config = config
        self._c = corrections

    @property
    def store(self) -> Corrections:
        if self._c is None:
            self._c = _corrections(self.config)
        return self._c

    def matches(self, text: str) -> bool:
        return bool(_TEACH.match(text) or _FORGET.match(text) or _LIST.search(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _TEACH.match(text)
        if m:
            heard = m.group("heard") or m.group("heard2") or m.group("heard3")
            meant = m.group("meant") or m.group("meant2") or m.group("meant3")
            return {"action": "add", "heard": heard, "meant": meant}
        m = _FORGET.match(text)
        if m:
            return {"action": "remove", "heard": m.group(1)}
        return {"action": "list"}

    def run(self, action: str = "list", heard: str = "", meant: str = "", **kwargs: Any) -> ToolResult:
        if action == "add":
            if not self.store.add(heard, meant):
                return ToolResult.fail("I couldn't save that correction. Tell me the word I hear and the word you mean.")
            return ToolResult.ok(f"Got it. When I hear \"{heard.strip()}\", I'll take it as \"{meant.strip()}\".")
        if action == "remove":
            if not self.store.remove(heard):
                return ToolResult.fail(f"I don't have a correction for \"{heard}\".")
            return ToolResult.ok(f"Removed the correction for \"{heard}\".")
        items = self.store.all()
        if not items:
            return ToolResult.ok("I don't have any speech corrections yet. Say 'when I say X, I mean Y' to teach me.")
        said = "; ".join(f"{h} means {m}" for h, m in list(items.items())[:8])
        return ToolResult.ok(f"My corrections: {said}.", count=len(items))
