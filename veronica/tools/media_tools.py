"""
Media keys and quick facts.

* MediaControlTool presses the system media keys (play/pause, next, previous, stop), so it
  controls whatever is playing — Spotify, YouTube in any browser, VLC — without knowing
  or listing any player.
* WikipediaTool answers "who is ..." with the first sentences of the Wikipedia summary
  (keyless REST API). Text is external/untrusted: spoken back, never executed.
"""
from __future__ import annotations

import platform
import re
from typing import Any
from urllib.parse import quote

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("tools.media")

# Windows virtual-key codes for the media keys (documented constants, not a player list).
_VK = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}
_KEYEVENTF_KEYUP = 0x0002

_THING = r"(?:\s+(?:the\s+)?(?:music|song|track|video|playback|audio|media|it|this))?"
_MEDIA = [
    ("play_pause", re.compile(rf"^\s*(?:please\s+)?(?:pause|resume|unpause|continue){_THING}\s*[.!]?\s*$", re.I)),
    ("play_pause", re.compile(rf"^\s*(?:please\s+)?play{_THING}?\s*(?:again)?\s*[.!]?\s*$", re.I)),
    ("stop", re.compile(rf"^\s*(?:please\s+)?stop\s+(?:the\s+)?(?:music|song|video|playback|audio|media)\s*[.!]?\s*$", re.I)),
    ("next", re.compile(r"^\s*(?:please\s+)?(?:next|skip)(?:\s+(?:this\s+|the\s+)?(?:song|track|video|one))?\s*[.!]?\s*$|"
                        r"\b(?:play|go\s+to)\s+(?:the\s+)?next\s+(?:song|track|video)\b", re.I)),
    ("previous", re.compile(r"^\s*(?:please\s+)?(?:previous|last|go\s+back)(?:\s+(?:song|track|video))\s*[.!]?\s*$|"
                            r"\b(?:play|go\s+to)\s+(?:the\s+)?(?:previous|last)\s+(?:song|track|video)\b", re.I)),
]
_SPOKEN = {"play_pause": "Done.", "next": "Next track.", "previous": "Previous track.", "stop": "Stopped."}


def _press(vk: int) -> None:
    import ctypes
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)


class MediaControlTool(Tool):
    name = "media_control"
    description = ("Control whatever media is playing (any app or browser) with the system media keys. "
                   "action: 'play_pause' (toggles), 'next', 'previous', 'stop'.")
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": list(_VK)}},
        "required": ["action"],
    }

    def __init__(self, presser=None) -> None:
        self._press = presser or _press

    def matches(self, text: str) -> bool:
        return any(p.search(text) for _, p in _MEDIA)

    def extract_arguments(self, text: str) -> dict[str, Any]:
        return {"action": next((a for a, p in _MEDIA if p.search(text)), "play_pause")}

    def run(self, action: str = "play_pause", **kwargs: Any) -> ToolResult:
        action = (action or "play_pause").lower()
        if action not in _VK:
            return ToolResult.fail("I can play or pause, skip to the next or previous track, or stop.")
        if platform.system() != "Windows" and self._press is _press:
            return ToolResult.fail("Media keys are only supported on Windows.")
        try:
            self._press(_VK[action])
        except Exception as e:
            logger.exception("Media key failed")
            return ToolResult.fail(f"Couldn't press the media key: {e}")
        return ToolResult.ok(_SPOKEN[action], action=action)


# ---------------------------------------------------------------- Wikipedia

_WHO = re.compile(r"^\s*(?:(?:hey\s+)?(?:can\s+you\s+)?(?:tell\s+me\s+)?)?(?:who\s+(?:is|was|were)|"
                  r"(?:look\s+up|search)\s+(.+?)\s+on\s+wikipedia|wikipedia)\s*(.*?)\s*[?.!]*\s*$", re.I)
# Pronouns are personal (memory/LLM); "the/a ..." asks about a role ("who is the prime
# minister") whose article describes the office, not today's holder — leave to the LLM.
_NOT_A_TOPIC = re.compile(r"^(?:you|your|yourself|i|me|my|we|us|this|that|it|there|he|she|they|him|her|them|"
                          r"the|a|an|our|his|their)\b", re.I)


class WikipediaTool(Tool):
    name = "wikipedia_summary"
    description = "Short factual summary of a person, place or thing from Wikipedia."
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"topic": {"type": "string", "description": "What to look up, e.g. 'APJ Abdul Kalam'."}},
        "required": ["topic"],
    }

    def __init__(self, config=None, http=None) -> None:
        self._http = http
        lang = getattr(getattr(config, "assistant", None), "news_language", "en") or "en"
        self.base = f"https://{lang}.wikipedia.org"

    def _topic(self, text: str) -> str:
        m = _WHO.match(text or "")
        if not m:
            return ""
        return (m.group(1) or m.group(2) or "").strip()

    def matches(self, text: str) -> bool:
        topic = self._topic(text)
        return bool(topic) and not _NOT_A_TOPIC.match(topic) and len(topic.split()) <= 6

    def extract_arguments(self, text: str) -> dict[str, Any]:
        return {"topic": self._topic(text)}

    def run(self, topic: str = "", **kwargs: Any) -> ToolResult:
        topic = (topic or "").strip()
        if not topic:
            return ToolResult.fail("Who or what should I look up?")
        http = self._http
        if http is None:
            import requests as http
        headers = {"User-Agent": "VERONICA/1.0 (personal voice assistant)"}
        try:
            # Search first, so spoken spellings ("abdul kalam") find the right article title.
            found = http.get(f"{self.base}/w/api.php", timeout=6, headers=headers, params={
                "action": "opensearch", "search": topic, "limit": 1, "namespace": 0, "format": "json"}).json()
            title = found[1][0] if len(found) > 1 and found[1] else topic
            page = http.get(f"{self.base}/api/rest_v1/page/summary/{quote(title.replace(' ', '_'))}",
                            timeout=6, headers=headers).json()
        except Exception as e:  # noqa: BLE001
            logger.warning("Wikipedia lookup failed: %s", e)
            return ToolResult.fail("I couldn't reach Wikipedia right now.")
        if page.get("type") == "disambiguation":
            return ToolResult.ok(f"{title} can mean several things. Can you be more specific?", title=title)
        extract = (page.get("extract") or "").strip()
        if not extract:
            return ToolResult.fail(f"I couldn't find anything on Wikipedia about {topic}.")
        sentences = re.split(r"(?<=[.!?])\s+", extract)
        return ToolResult.ok(" ".join(sentences[:2]), title=page.get("title", title),
                             url=page.get("content_urls", {}).get("desktop", {}).get("page", ""))
