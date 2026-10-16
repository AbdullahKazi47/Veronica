from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote_plus

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("tools.web")

_SEARCH_PATTERN = re.compile(
    r"\b(search(?:\s+(?:the\s+)?(?:web|internet|online))?(?:\s+for)?|browse(?:\s+(?:the\s+)?(?:web|internet))?(?:\s+for)?|"
    r"look\s+up|google(?:\s+for)?)\s+(.+)$", re.I)
# "search bing for cats" / "look up X" with the engine named first
_SEARCH_ENGINE_FIRST = re.compile(r"\b(?:search|look\s+up)\s+(?!the\b|for\b|web\b|internet\b|online\b)"
                                  r"([a-z][\w\-]*(?:\s+search)?)\s+for\s+(.+)$", re.I)
# "... on bing" / "... using duckduckgo" / "... with brave search" ("in" is for browsers)
_ENGINE_SUFFIX = re.compile(r"^(.*)\s+(?:on|using|with|via)\s+(?:the\s+)?([a-z][\w\-]*(?:\s+search)?)$", re.I)
_NOT_ENGINE = re.compile(r"^(?:my|this|that|these|those|your|our|it|me|today|tonight|tomorrow|yesterday|now|"
                         r"weekend|weekends|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                         r"january|february|march|april|may|june|july|august|september|october|november|december)\b",
                         re.I)
# Greedy head: split at the LAST "in/on/with/using" ("lofi on youtube in brave" -> brave).
_BROWSER_SUFFIX = re.compile(r"^(.*)\s+(?:in|on|with|using)\s+(?:the\s+)?([a-z][\w]*(?:\s+[a-z]\w*)?)\s*[.!?]*$", re.I)

_BROWSER_PARAM = {"type": "string",
                  "description": "Optional browser to use, e.g. 'Brave', 'Chrome', 'Firefox', 'Edge'. "
                                 "Omit for the default browser."}


def split_browser(text: str, index) -> tuple[str, str]:
    """'lofi music in brave' -> ('lofi music', 'brave') if Brave is installed; otherwise
    the words stay part of the request ('songs in hindi' is not a browser)."""
    m = _BROWSER_SUFFIX.match(text or "")
    if m and index.find_browser(m.group(2)) is not None:
        return m.group(1).strip(), m.group(2).strip()
    return (text or "").strip(), ""


def _index(config=None, index=None):
    from veronica.tools.app_index import shared_index
    return index or shared_index(config)


def _opened(what: str, where: str) -> str:
    return f"{what} in {where}" if where != "your default browser" else what


class OpenWebsiteTool(Tool):
    name = "open_website"
    description = ("Open a website the user explicitly named ('youtube', 'netflix', 'irctc') or an address, "
                   "optionally in a specific installed browser. Not for finding things (use web_search or "
                   "a data tool) and never for a guessed or misheard name.")
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {"site": {"type": "string", "description": "Site name or domain."},
                       "browser": _BROWSER_PARAM},
        "required": ["site"],
    }

    def __init__(self, config=None, index=None, routing=None) -> None:
        from veronica.tools.system_tools import _OpenRouting
        self.routing = routing or _OpenRouting(config, _index(config, index))
        self.index = self.routing.index

    def matches(self, text: str) -> bool:
        parsed = self.routing.parse(text)
        return bool(parsed) and parsed["kind"] == "site"

    def extract_arguments(self, text: str) -> dict[str, Any]:
        parsed = self.routing.parse(text)
        return {"site": parsed["target"], "browser": parsed.get("browser", "")} if parsed else {}

    def run(self, site: str = "", browser: str = "", **kwargs: Any) -> ToolResult:
        if not site:
            return ToolResult.fail("Which website?")
        url = self.index.resolve_site(site)
        try:
            ok, where = self.index.open_url(url, browser)
        except Exception as e:
            logger.exception("Failed to open website")
            return ToolResult.fail(f"Couldn't open {site}: {e}")
        if not ok:
            return ToolResult.fail(where)
        return ToolResult.ok(f"Opening {_opened(site, where)}.", url=url, browser=where)


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
    description = ("Search the web and open the results. Uses the user's default search engine (Google) "
                   "unless they name another engine or site to search ('Bing', 'DuckDuckGo', 'YouTube').")
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "The search query."},
                       "engine": {"type": "string", "description": "Only if the user names one; omit for the default."},
                       "browser": _BROWSER_PARAM},
        "required": ["query"],
    }

    def __init__(self, config=None, index=None) -> None:
        self.index = _index(config, index)
        assistant = getattr(config, "assistant", None)
        self.default_engine = getattr(assistant, "search_engine", None) or "Google"
        # Direct results URL for the default engine (fast, no redirect); other engines
        # are reached by name through DuckDuckGo's "!bang" shortcuts.
        self.default_url = getattr(assistant, "search_url", None)

    def matches(self, text: str) -> bool:
        return bool(_SEARCH_ENGINE_FIRST.search(text) or _SEARCH_PATTERN.search(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        engine = ""
        m = _SEARCH_ENGINE_FIRST.search(text)              # "search bing for cats"
        if m:
            engine, rest = m.group(1).strip(), m.group(2)
        else:
            m = _SEARCH_PATTERN.search(text)
            if not m:
                return {}
            rest = m.group(2)
            if m.group(1).lower().startswith("google"):    # "google cats"
                engine = "Google"
        query, browser = split_browser(rest, self.index)    # "... in brave"
        e = _ENGINE_SUFFIX.match(query)                      # "... on bing"
        if e and not engine and self._is_engine(e.group(2)):
            query, engine = e.group(1).strip(), e.group(2).strip()
        return {"query": query.strip(" ?.!"), "engine": engine, "browser": browser}

    def _is_engine(self, name: str) -> bool:
        """'bing' yes; 'monday' / 'my laptop' no — a name is an engine/site only if it
        isn't a time or possessive word and resolves to that site's own address."""
        if _NOT_ENGINE.match(name.strip()):
            return False
        return self.index.site_for(name.replace(" search", "")) is not None

    def search_url(self, query: str, engine: str = "") -> tuple[str, str]:
        engine = (engine or self.default_engine).strip()
        if engine.lower() == self.default_engine.lower() and self.default_url:
            return self.default_url.replace("{query}", quote_plus(query)), self.default_engine
        bang = re.sub(r"[^a-z0-9]", "", engine.lower().replace(" search", ""))
        return f"https://duckduckgo.com/?q={quote_plus(f'!{bang} {query}')}", engine

    def run(self, query: str = "", engine: str = "", browser: str = "", **kwargs: Any) -> ToolResult:
        if not query:
            return ToolResult.fail("No search query given.")
        url, engine_name = self.search_url(query, engine)
        try:
            ok, where = self.index.open_url(url, browser)
        except Exception as e:
            logger.exception("Failed to open search")
            return ToolResult.fail(f"Couldn't search for '{query}': {e}")
        if not ok:
            return ToolResult.fail(where)
        return ToolResult.ok(f"Searching {engine_name} for {_opened(query, where)}.", url=url,
                             engine=engine_name, browser=where)
