"""'Run a voice test' — the guided microphone / recognition / talk-over check
(voice/voice_test.py). Needs voice mode: the CLI connects the real mic and speaker."""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

_tester: Optional[Callable[[], str]] = None


def set_voice_tester(fn: Optional[Callable[[], str]]) -> None:
    global _tester
    _tester = fn


_VOICE_TEST = re.compile(
    r"\b(?:run|do|start|try)\s+(?:a\s+|the\s+)?(?:voice|mic(?:rophone)?|audio|hearing|speech)\s+(?:test|check)\b|"
    r"\b(?:test|check)\s+(?:your|my|the)\s+(?:voice|mic(?:rophone)?|hearing|ears|audio)\b|"
    r"^\s*(?:voice|mic(?:rophone)?)\s+(?:test|check)\s*[.!?]?\s*$|\bcan\s+you\s+hear\s+me\s+(?:well|properly|ok)\b", re.I)


class VoiceTestTool(Tool):
    name = "voice_test"
    description = ("Run a one-minute guided voice check: room noise, how well VERONICA hears the user, and "
                   "whether saying 'Veronica' interrupts her. Voice mode only.")
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, tester: Optional[Callable[[], str]] = None) -> None:
        self._own = tester

    def matches(self, text: str) -> bool:
        return bool(_VOICE_TEST.search(text))

    def run(self, **kwargs: Any) -> ToolResult:
        tester = self._own or _tester
        if tester is None:
            return ToolResult.fail("The voice test needs voice mode. Start me with --wake or --voice.")
        try:
            return ToolResult.ok(tester())
        except Exception as e:  # noqa: BLE001
            return ToolResult.fail(f"The voice test ran into a problem: {e}")
