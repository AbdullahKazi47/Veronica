from __future__ import annotations

import re
import webbrowser
from typing import Any
from urllib.parse import quote_plus

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("tools.youtube")

_PLAY_PATTERN = re.compile(r"\bplay\s+(.+?)(?:\s+on\s+youtube)?$", re.I)


class YouTubeSearchTool(Tool):
    name = "play_on_youtube"
    description = "Search YouTube for a query and open the top results."
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "What to search for / play."}},
        "required": ["query"],
    }

    def matches(self, text: str) -> bool:
        return bool(_PLAY_PATTERN.search(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _PLAY_PATTERN.search(text)
        return {"query": m.group(1).strip()} if m else {}

    def run(self, query: str = "", **kwargs: Any) -> ToolResult:
        if not query:
            return ToolResult.fail("No query given.")
        url = f"https://www.youtube.com/results?search_query={quote_plus(query)}"
        try:
            webbrowser.open(url)
            return ToolResult.ok(f"Playing '{query}' on YouTube.", url=url)
        except Exception as e:
            logger.exception("Failed to open YouTube")
            return ToolResult.fail(f"Couldn't open YouTube for '{query}': {e}")
