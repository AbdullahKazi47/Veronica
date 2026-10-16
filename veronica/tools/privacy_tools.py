"""
"What do you send to the cloud?" / "Have you blocked anything sensitive?" — explain the
privacy boundary and report what the guard has scrubbed (counts only, never the values).
"""
from __future__ import annotations

import re
from typing import Any

from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

_WHAT_SENT = re.compile(
    r"\bwhat\s+(?:do\s+you|data|info(?:rmation)?)\s+(?:do\s+you\s+)?(?:send|share|give)\b.*\b(?:cloud|gemini|google|llm|model|internet|online)\b|"
    r"\bwhat\s+(?:gets?|is)\s+sent\b.*\b(?:cloud|gemini|google|llm|model)\b|"
    r"\bdo\s+you\s+(?:send|share|leak|steal)\b.*\b(?:my\s+)?(?:data|info|secrets?|passwords?)\b|"
    r"\bis\s+my\s+(?:data|info(?:rmation)?)\s+(?:safe|private|protected)\b", re.I)
_WHAT_BLOCKED = re.compile(
    r"\b(?:have\s+you|did\s+you|what\s+have\s+you)\s+(?:blocked|redacted|scrubbed|hidden|protected|caught|removed)\b|"
    r"\b(?:redaction|privacy)\s+(?:report|log|count|stats?|status)\b|"
    r"\bwhat\s+(?:secrets?|sensitive)\b.*\b(?:blocked|caught|redacted|protected)\b", re.I)

_CATEGORY_WORDS = {
    "secret": "credentials or keys", "custom": "items matching your own patterns",
    "card": "card numbers", "financial": "financial codes (CVV, PIN or account numbers)",
    "ssn": "US social security numbers", "aadhaar": "Aadhaar numbers",
    "pan": "PAN numbers", "ifsc": "IFSC codes",
}


class PrivacyReportTool(Tool):
    name = "privacy_report"
    description = ("Explain what VERONICA sends to the cloud model and what she keeps on the machine, or "
                   "report how many sensitive items the privacy guard has blocked this session. Use for "
                   "'what do you send to the cloud', 'is my data safe', 'have you redacted anything'.")
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {}, "required": []}

    def __init__(self, config=None) -> None:
        self.config = config

    def matches(self, text: str) -> bool:
        return bool(_WHAT_SENT.search(text) or _WHAT_BLOCKED.search(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        return {"mode": "blocked" if _WHAT_BLOCKED.search(text) and not _WHAT_SENT.search(text) else "explain"}

    def run(self, mode: str = "explain", **kwargs: Any) -> ToolResult:
        from veronica.safety import privacy
        guard = privacy.get_guard()
        counts = guard.report()
        total = sum(counts.values())
        if mode == "blocked":
            return ToolResult.ok(self._blocked_line(counts, total), total=total, counts=counts)
        # explain + a one-line tally
        if not getattr(guard.cfg, "enabled", True):
            return ToolResult.ok("Heads up: the privacy guard is currently switched off in your config, so "
                                 "nothing is being scrubbed. Turn it back on under privacy in config.yaml.")
        tail = (" So far this session, " + self._blocked_line(counts, total, short=True)) if total else \
               " So far this session I haven't had to block anything."
        return ToolResult.ok(
            "Here's the honest picture. I think with Google's Gemini in the cloud, so your actual requests, and "
            "the facts you've asked me to remember, do go to it — that's how I answer you. But before anything "
            "leaves this machine I strip out secrets and sensitive IDs: passwords, API keys and tokens, card "
            "numbers, CVV and PIN codes, bank and IFSC numbers, and government IDs like Aadhaar, PAN and social "
            "security numbers. Those are replaced with a placeholder, even if you say one out loud or it's on "
            "your screen. I never send your API key itself, and I don't keep the things I block." + tail,
            total=total, counts=counts)

    @staticmethod
    def _blocked_line(counts: dict, total: int, short: bool = False) -> str:
        if not total:
            return "I haven't blocked anything sensitive this session."
        bits = [f"{n} {_CATEGORY_WORDS.get(cat, cat)}" for cat, n in sorted(counts.items(), key=lambda kv: -kv[1])]
        joined = bits[0] if len(bits) == 1 else ", ".join(bits[:-1]) + " and " + bits[-1]
        lead = "" if short else "This session I've kept "
        return f"{lead}{joined} from reaching the cloud model."
