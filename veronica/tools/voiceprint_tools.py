"""'Only listen to me' by voice: "learn my voice", "stop / start checking my voice",
"forget my voice", "is voice check on?" (voice/voiceprint.py does the work)."""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

_LEARN = re.compile(r"\b(?:learn|remember|recogni[sz]e|train\s+on|record)\s+my\s+voice\b|"
                    r"\bonly\s+(?:listen|respond|answer)\s+to\s+me\b|\bjust\s+(?:listen|respond)\s+to\s+me\b", re.I)
_OFF = re.compile(r"\b(?:stop|don'?t|turn\s+off|disable)\b.{0,20}\b(?:check(?:ing)?|recogni[sz](?:e|ing))\s+my\s+voice\b|"
                  r"\blisten\s+to\s+everyone\b", re.I)
_ON = re.compile(r"\b(?:start|turn\s+on|enable|resume)\b.{0,20}\b(?:check(?:ing)?|recogni[sz](?:e|ing))\s+my\s+voice\b", re.I)
_FORGET = re.compile(r"\b(?:forget|delete|erase|remove)\s+my\s+voice(?:print)?\b", re.I)
_STATUS = re.compile(r"\bis\s+(?:the\s+)?voice\s+check\s+on\b|\bdo\s+you\s+know\s+my\s+voice\b", re.I)

_enroller: Optional[Callable[[], str]] = None


def set_voice_enroller(fn: Optional[Callable[[], str]]) -> None:
    """The CLI hands in how to record the sentences (it owns the mic and the speaker)."""
    global _enroller
    _enroller = fn


class VoiceprintTool(Tool):
    name = "voiceprint"
    description = ("Learn the user's voice so VERONICA only responds to them (ignores TV, other people), "
                   "or turn that check off/on, or forget the voiceprint. Voice mode only.")
    permission = PermissionLevel.SAFE
    parameters = {"type": "object",
                  "properties": {"action": {"type": "string", "enum": ["learn", "off", "on", "forget", "status"]}},
                  "required": []}

    def __init__(self, config=None, voiceprint=None, enroller: Optional[Callable[[], str]] = None) -> None:
        self.config = config
        self._vp = voiceprint
        self._own = enroller

    def vp(self):
        if self._vp is None:
            from veronica.voice.voiceprint import get_voiceprint
            self._vp = get_voiceprint(self.config)
        return self._vp

    def matches(self, text: str) -> bool:
        return any(p.search(text) for p in (_LEARN, _OFF, _ON, _FORGET, _STATUS))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        for action, p in (("forget", _FORGET), ("off", _OFF), ("on", _ON), ("status", _STATUS), ("learn", _LEARN)):
            if p.search(text):
                return {"action": action}
        return {"action": "status"}

    def run(self, action: str = "learn", **kwargs: Any) -> ToolResult:
        vp = self.vp()
        if action == "forget":
            return ToolResult.ok("Done. I've deleted your voiceprint; I'll listen to anyone again."
                                 if vp.forget() else "I hadn't learned your voice yet.")
        if action in ("off", "on"):
            if not vp.enrolled:
                return ToolResult.fail("I haven't learned your voice yet. Say 'learn my voice' first.")
            vp.set_enabled(action == "on")
            return ToolResult.ok("Okay, I'll only respond to your voice." if action == "on"
                                 else "Okay, I'll listen to anyone for now. Say 'start checking my voice' to switch back.")
        if action == "status":
            if not vp.enrolled:
                return ToolResult.ok("I don't know your voice yet. Say 'learn my voice' and I'll learn it in a minute.")
            return ToolResult.ok("Yes, I only respond to your voice." if vp.active
                                 else "I know your voice, but the check is off right now.")
        enroller = self._own or _enroller
        if enroller is None:
            return ToolResult.fail("Learning your voice needs voice mode. Start me with --wake.")
        if not vp.available():
            return ToolResult.fail("The voice model isn't installed. Run scripts/get_voice_models.py.")
        try:
            return ToolResult.ok(enroller())
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"Learning your voice ran into a problem: {e}")
