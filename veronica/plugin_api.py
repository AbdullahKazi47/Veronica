"""
The ONLY things an ability added by voice may use ("Veronica, add flight tracking").

An ability looks something up on the internet and returns a sentence to speak. It cannot
control the PC, touch files, run programs or reach the local network: abilities may not
import anything but plain-data modules and this one (checked before any code runs —
core/abilities.py), and the web calls below only go to the hosts the ability declared,
over http(s), GET only, never to a private or local address.

    from veronica.plugin_api import Ability, http_get_json, http_get_text, api_key

    class PublicHolidays(Ability):
        name = "public_holidays"
        description = "Upcoming public holidays in a country. country: two-letter code, e.g. IN."
        parameters = {"type": "object", "properties": {"country": {"type": "string"}}, "required": ["country"]}
        hosts = ["date.nager.at"]

        def run(self, country="IN"):
            days = http_get_json(f"https://date.nager.at/api/v3/NextPublicHolidays/{country}")
            return "Next: " + ", ".join(f"{d['localName']} on {d['date']}" for d in days[:3])

    EXAMPLES = [{"args": {"country": "IN"}, "expect": "Next"}]
"""
from __future__ import annotations

import ipaddress
import json
import socket
import threading
from typing import Any, Optional
from urllib.parse import urlparse

_ctx = threading.local()            # hosts the running ability may call
_MAX_BYTES = 2 * 1024 * 1024
_TIMEOUT_S = 10
# VERONICA's own keys are never handed to an ability.
_RESERVED_KEYS = {"GEMINI_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY", "FISH_AUDIO_API_KEY",
                  "VERONICA_LLM_API_KEY", "ANTHROPIC_API_KEY", "CALENDAR_ICS_URLS", "OPENAI_API_KEY"}


class AbilityError(Exception):
    """Raised for anything an ability isn't allowed to do."""


class Ability:
    name: str = ""
    description: str = ""
    parameters: dict = {"type": "object", "properties": {}, "required": []}
    hosts: list = []

    def run(self, **kwargs: Any) -> str:          # returns a sentence to speak
        raise NotImplementedError


def _allowed(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise AbilityError(f"Only web addresses are allowed: {url}")
    host = parsed.hostname.lower()
    hosts = getattr(_ctx, "hosts", None) or []
    if not any(host == h or host.endswith("." + h) for h in hosts):
        raise AbilityError(f"{host} is not one of this ability's declared sites ({', '.join(hosts) or 'none'}).")
    try:
        for info in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80)):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise AbilityError(f"{host} points at a local or private address; that's not allowed.")
    except socket.gaierror:
        raise AbilityError(f"Couldn't find {host}.")
    return url


def http_get_text(url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> str:
    import requests
    _allowed(url)
    safe_headers = {"User-Agent": "VERONICA-ability/1.0"}
    for k, v in (headers or {}).items():          # an API key header, an Accept header
        if isinstance(k, str) and isinstance(v, str) and k.lower() not in ("host", "cookie"):
            safe_headers[k] = v
    resp = requests.get(url, params=params, headers=safe_headers, timeout=_TIMEOUT_S, stream=True,
                        allow_redirects=False)
    if 300 <= resp.status_code < 400:
        target = resp.headers.get("Location", "")
        resp.close()
        if not target:
            raise AbilityError("Redirect without a location.")
        if target.startswith("/"):
            p = urlparse(url)
            target = f"{p.scheme}://{p.netloc}{target}"
        return http_get_text(target, None, headers)        # re-checked against the allowed sites
    resp.raise_for_status()
    chunks, size = [], 0
    for chunk in resp.iter_content(65536):
        size += len(chunk)
        if size > _MAX_BYTES:
            resp.close()
            raise AbilityError("The response was too large.")
        chunks.append(chunk)
    return b"".join(chunks).decode(resp.encoding or "utf-8", errors="replace")


def http_get_json(url: str, params: Optional[dict] = None, headers: Optional[dict] = None) -> Any:
    return json.loads(http_get_text(url, params, headers))


def api_key(name: str) -> str:
    """A key the user put in config/.env for this ability (e.g. AVIATIONSTACK_API_KEY).
    VERONICA's own keys are never given out. Empty string when not set."""
    name = (name or "").strip().upper()
    if name in _RESERVED_KEYS or not name.endswith(("_API_KEY", "_TOKEN", "_KEY")):
        raise AbilityError(f"{name} isn't a key an ability may read.")
    try:
        from veronica.config import load_config
        return load_config().secret(name) or ""
    except Exception:  # noqa: BLE001
        return ""


def _run_with_hosts(hosts: list, fn, *args, **kwargs):
    """Used by VERONICA (not abilities): run `fn` with the web limited to `hosts`."""
    previous = getattr(_ctx, "hosts", None)
    _ctx.hosts = [str(h).lower().strip() for h in hosts if str(h).strip()]
    try:
        return fn(*args, **kwargs)
    finally:
        _ctx.hosts = previous
