from __future__ import annotations

import re
import webbrowser
from typing import Any
from urllib.parse import quote_plus

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("tools.web")

_OPEN_SITE_PATTERN = re.compile(r"\bopen\s+(?:the\s+)?(?:website\s+)?([a-z0-9\-]+\.(?:com|org|net|io|in|dev)|reddit|github|gmail|youtube)\b", re.I)
_SEARCH_PATTERN = re.compile(r"\b(?:search for|look up|google)\s+(.+)$", re.I)

_KNOWN_SITES = {
    "reddit": "https://reddit.com",
    "github": "https://github.com",
    "gmail": "https://mail.google.com",
    "youtube": "https://youtube.com",
}


class OpenWebsiteTool(Tool):
    name = "open_website"
    description = "Open a website in the default browser (by name like 'github' or a domain)."
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {"site": {"type": "string", "description": "Site name or domain."}},
        "required": ["site"],
    }

    def matches(self, text: str) -> bool:
        return bool(_OPEN_SITE_PATTERN.search(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _OPEN_SITE_PATTERN.search(text)
        return {"site": m.group(1).strip().lower()} if m else {}

    def run(self, site: str = "", **kwargs: Any) -> ToolResult:
        if not site:
            return ToolResult.fail("No site given.")
        url = _KNOWN_SITES.get(site, site if site.startswith("http") else f"https://{site}")
        try:
            webbrowser.open(url)
            return ToolResult.ok(f"Opening {url}.", url=url)
        except Exception as e:
            logger.exception("Failed to open website")
            return ToolResult.fail(f"Couldn't open {url}: {e}")


class FetchWebpageTool(Tool):
    name = "fetch_webpage"
    description = "Fetch a URL and return its readable text content (for reading/summarizing, not just opening it)."
    permission = PermissionLevel.SAFE
    parameters = {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "The URL to fetch."}},
        "required": ["url"],
    }

    def run(self, url: str = "", **kwargs: Any) -> ToolResult:
        if not url:
            return ToolResult.fail("No URL given.")
        if not url.startswith("http"):
            url = f"https://{url}"
        try:
            import requests
        except ImportError:
            return ToolResult.fail("requests not installed; run `pip install requests`.")
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "VERONICA-assistant/0.1"})
            resp.raise_for_status()
        except requests.RequestException as e:
            return ToolResult.fail(f"Couldn't fetch {url}: {e}")

        text = resp.text
        try:
            from html.parser import HTMLParser

            class _TextExtractor(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.parts: list[str] = []
                    self._skip = False

                def handle_starttag(self, tag, attrs):
                    if tag in ("script", "style"):
                        self._skip = True

                def handle_endtag(self, tag):
                    if tag in ("script", "style"):
                        self._skip = False

                def handle_data(self, data):
                    if not self._skip and data.strip():
                        self.parts.append(data.strip())

            parser = _TextExtractor()
            parser.feed(text)
            content = "\n".join(parser.parts)
        except Exception:
            content = text

        content = content[:8000]
        return ToolResult.ok(f"Fetched {len(content)} characters from {url}.", content=content, url=url)


class WebSearchTool(Tool):
    name = "web_search"
    description = "Search the web for a query and open the results in the default browser."
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "The search query."}},
        "required": ["query"],
    }

    def matches(self, text: str) -> bool:
        return bool(_SEARCH_PATTERN.search(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _SEARCH_PATTERN.search(text)
        return {"query": m.group(1).strip()} if m else {}

    def run(self, query: str = "", **kwargs: Any) -> ToolResult:
        if not query:
            return ToolResult.fail("No search query given.")
        url = f"https://duckduckgo.com/?q={quote_plus(query)}"
        try:
            webbrowser.open(url)
            return ToolResult.ok(f"Searching the web for '{query}'.", url=url)
        except Exception as e:
            logger.exception("Failed to open search")
            return ToolResult.fail(f"Couldn't search for '{query}': {e}")
