"""
"Play X [on <service>] [in <browser>]" for any service, without a list of services.

* Service = whatever the user names ("Spotify", "SoundCloud", "JioSaavn", "YouTube"), or
  `assistant.default_play_service` when none is named.
* If that service is an installed app that registers its own link type (e.g. the
  Spotify app registers `spotify:`), the app is opened on a search for X.
* Otherwise the service's site is resolved by name (same resolver as "open <site>") and
  the FIRST result for X on that site is opened — on a video/music site that page
  starts playing, which is what "play" means. (DuckDuckGo's "\\" first-result redirect.)
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import quote, quote_plus, urlparse

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("tools.play")

_PLAY_PATTERN = re.compile(r"\bplay\s+(.+?)\s*[.!?]*$", re.I)
_SERVICE_SUFFIX = re.compile(r"^(.*)\s+(?:on|in|using|with|from)\s+(?:the\s+)?([a-z][\w\-]*(?:\s+[a-z][\w\-]*)?)\s*$", re.I)


def _registered_scheme(name: str) -> Optional[str]:
    """'Spotify' -> 'spotify' if Windows has a 'spotify:' URL protocol registered."""
    scheme = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if not scheme:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, scheme) as key:
            winreg.QueryValueEx(key, "URL Protocol")
        return scheme
    except (ImportError, OSError):
        return None


class YouTubeSearchTool(Tool):
    """Kept under its historical class name; the tool itself plays on any service."""

    name = "play_media"
    description = ("Play music or video. `service` is where to play it (any site or app the user names, "
                   "e.g. 'Spotify', 'SoundCloud', 'YouTube'); omit for the default. `browser` optional.")
    permission = PermissionLevel.MODERATE
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to play, e.g. 'lofi beats' or 'Arijit Singh songs'."},
            "service": {"type": "string", "description": "Where to play it; omit for the user's default."},
            "browser": {"type": "string", "description": "Optional browser, e.g. 'Brave'. Omit for the default."},
        },
        "required": ["query"],
    }

    def __init__(self, config=None, index=None, scheme_lookup=None) -> None:
        from veronica.tools.app_index import shared_index
        self.index = index or shared_index(config)
        assistant = getattr(config, "assistant", None)
        self.default_service = getattr(assistant, "default_play_service", None) or "YouTube"
        self._scheme = scheme_lookup or _registered_scheme

    def matches(self, text: str) -> bool:
        return bool(_PLAY_PATTERN.search(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        from veronica.tools.web_tools import split_browser
        m = _PLAY_PATTERN.search(text)
        if not m:
            return {}
        rest, browser = split_browser(m.group(1), self.index)        # "... in brave"
        service = ""
        s = _SERVICE_SUFFIX.match(rest)                               # "... on spotify"
        if s and not self.index.find_browser(s.group(2)):
            # Only treat it as a service if it names something real: an installed app or a
            # resolvable site — "songs in hindi" keeps "in hindi" as part of the request.
            candidate = s.group(2).strip()
            if self.index.find_app(candidate) or self._looks_like_service(candidate):
                rest, service = s.group(1).strip(), candidate
        return {"query": rest.strip(), "service": service, "browser": browser}

    def _looks_like_service(self, name: str) -> bool:
        return self.index.site_for(name) is not None  # resolves to the service's own site

    def run(self, query: str = "", service: str = "", browser: str = "", **kwargs: Any) -> ToolResult:
        query = (query or "").strip()
        if not query:
            return ToolResult.fail("What should I play?")
        service = (service or self.default_service).strip()

        # 1) Installed app with its own link type: open the app itself.
        app = self.index.find_app(service)
        scheme = self._scheme(app["name"] if app else service) if app else None
        if scheme:
            try:
                import os
                os.startfile(f"{scheme}:search:{quote(query)}")  # type: ignore[attr-defined]
                return ToolResult.ok(f"Playing {query} on {app['name']}.", service=app["name"])
            except Exception as e:  # noqa: BLE001 — fall back to the web version
                logger.warning("Opening %s via %s: failed (%s); using the web.", app["name"], scheme, e)

        # 2) The service's website: open the first result for the query there.
        own = self.index.site_for(service)
        site = urlparse(own).netloc if own else ""
        if site:
            # DuckDuckGo's "\" jumps straight to the FIRST result on that site — on a video
            # or music site that page starts playing, which is what "play" means. The
            # redirect is instant; no search page is shown.
            domain = site[4:] if site.startswith("www.") else site
            url = "https://duckduckgo.com/?q=" + quote_plus(f"\\{query} site:{domain}")
        else:  # unknown service name (often a mishearing): a normal search, default engine
            url = self.index.search_url_template.replace("{query}", quote_plus(f"{query} {service}"))
        try:
            ok, where = self.index.open_url(url, browser)
        except Exception as e:
            logger.exception("Failed to open player")
            return ToolResult.fail(f"Couldn't play {query}: {e}")
        if not ok:
            return ToolResult.fail(where)
        suffix = f" in {where}" if where != "your default browser" else ""
        return ToolResult.ok(f"Playing {query} on {service}{suffix}.", url=url, service=service, browser=where)
