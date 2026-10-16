"""
Discovers what is actually installed instead of hard-coding app/browser/site lists.

* Apps: every Start-menu entry Windows reports (`Get-StartApps`: desktop and Store apps,
  each launchable as `shell:AppsFolder\\<AppID>`). Cached in data/app_index.json for a
  day; a miss triggers one refresh, so newly installed apps work without a restart.
* Browsers: every browser registered under ...\\Clients\\StartMenuInternet (the list
  Windows' "default apps" settings use), with the exe that opens a URL.
* Websites: a domain or URL is used as-is; a bare name ("netflix") is resolved to
  https://www.<name>.com when that responds, else DuckDuckGo's first result for the
  name. Resolutions are cached in data/site_cache.json.
* Folders: names of folders in the user's home ("downloads", "documents", ...).

Everything is looked up at runtime; nothing names a particular app, browser or site.
"""
from __future__ import annotations

import difflib
import json
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote_plus

from veronica.logging_setup import get_logger

logger = get_logger("tools.app_index")

_CACHE_TTL_S = 24 * 3600
_MIN_REFRESH_GAP_S = 60
_DOMAIN = re.compile(r"^(?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/\S*)?$", re.I)
_NOISE = re.compile(r"\b(app|application|program|the|my)\b", re.I)


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", _NOISE.sub(" ", (name or "").lower().replace("-", " "))).strip()


def _best_match(query: str, entries: list[dict], key: str = "name") -> Optional[dict]:
    """exact > all query words at the start of the name > name contains the query
    (shortest wins, so 'whatsapp' picks 'WhatsApp' over 'WhatsApp Web') > close spelling."""
    q = _norm(query)
    if not q:
        return None
    names = [(_norm(e[key]), e) for e in entries if e.get(key)]
    for n, e in names:
        if n == q:
            return e
    starts = [(n, e) for n, e in names if n.startswith(q + " ") or n.startswith(q)]
    if starts:
        return min(starts, key=lambda ne: len(ne[0]))[1]
    words = [(n, e) for n, e in names if re.search(rf"\b{re.escape(q)}\b", n)]
    if words:
        return min(words, key=lambda ne: len(ne[0]))[1]
    close = difflib.get_close_matches(q, [n for n, _ in names], n=1, cutoff=0.82)
    return next((e for n, e in names if close and n == close[0]), None)


def _load_start_apps() -> list[dict]:
    if platform.system() != "Windows":
        return []
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress"],
        capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    data = json.loads(out.stdout or "[]")
    data = [data] if isinstance(data, dict) else data
    return [{"name": d["Name"], "app_id": d["AppID"]} for d in data if d.get("Name") and d.get("AppID")]


def _load_browsers() -> list[dict]:
    if platform.system() != "Windows":
        return []
    import winreg
    found: dict[str, dict] = {}
    roots = [(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Clients\StartMenuInternet"),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Clients\StartMenuInternet"),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Clients\StartMenuInternet")]
    for hive, path in roots:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(root)[0]):
            sub = winreg.EnumKey(root, i)
            try:
                name = winreg.QueryValue(root, sub) or sub
                cmd = winreg.QueryValue(root, sub + r"\shell\open\command")
            except OSError:
                continue
            exe = cmd.split('"')[1] if cmd.startswith('"') else cmd.split(" ")[0]
            if exe and os.path.isfile(exe):
                found.setdefault(exe.lower(), {"name": name, "exe": exe})
    return list(found.values())


class AppIndex:
    def __init__(self, config=None, app_loader: Callable[[], list[dict]] = _load_start_apps,
                 browser_loader: Callable[[], list[dict]] = _load_browsers,
                 site_checker: Optional[Callable[[str], bool]] = None,
                 home: Optional[Path] = None) -> None:
        data_dir = config.resolve_path("data") if config else Path("data")
        self._app_cache = data_dir / "app_index.json"
        self._site_cache = data_dir / "site_cache.json"
        self._app_loader, self._browser_loader = app_loader, browser_loader
        self._site_checker = site_checker or self._site_responds
        self._apps: Optional[list[dict]] = None
        self._browsers: Optional[list[dict]] = None
        self._last_refresh = 0.0
        self.home = home or Path.home()
        assistant = getattr(config, "assistant", None)
        self.default_browser = getattr(assistant, "default_browser", None) or None
        self.search_url_template = getattr(assistant, "search_url", None) or "https://www.google.com/search?q={query}"

    # -- apps ----------------------------------------------------------------------
    def apps(self) -> list[dict]:
        if self._apps is None:
            try:
                cached = json.loads(self._app_cache.read_text(encoding="utf-8"))
                if time.time() - cached.get("saved", 0) < _CACHE_TTL_S:
                    self._apps = cached["apps"]
            except Exception:
                pass
        if self._apps is None:
            self.refresh()
        return self._apps or []

    def refresh(self) -> None:
        self._last_refresh = time.time()
        try:
            self._apps = self._app_loader()
            self._app_cache.parent.mkdir(parents=True, exist_ok=True)
            self._app_cache.write_text(json.dumps({"saved": time.time(), "apps": self._apps}), encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — discovery failure must not break the assistant
            logger.warning("App discovery failed: %s", e)
            self._apps = self._apps or []

    def find_app(self, name: str) -> Optional[dict]:
        hit = _best_match(name, self.apps())
        if hit is None and time.time() - self._last_refresh > _MIN_REFRESH_GAP_S:
            self.refresh()  # maybe installed since the cache was written
            hit = _best_match(name, self._apps or [])
        return hit

    def launch_app(self, entry: dict) -> None:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{entry['app_id']}"])

    # -- browsers --------------------------------------------------------------------
    def browsers(self) -> list[dict]:
        if self._browsers is None:
            try:
                self._browsers = self._browser_loader()
            except Exception as e:  # noqa: BLE001
                logger.warning("Browser discovery failed: %s", e)
                self._browsers = []
        return self._browsers

    def find_browser(self, name: str) -> Optional[dict]:
        if not name:
            return None
        hit = _best_match(name, self.browsers())
        if hit is None:  # "edge" -> "Microsoft Edge", "chrome" -> "Google Chrome"
            q = _norm(name)
            hit = next((b for b in self.browsers() if q and q in _norm(b["name"]).split()), None)
        return hit

    def open_url(self, url: str, browser: str = "") -> tuple[bool, str]:
        """Open url in the named browser, the configured default, or the system default."""
        wanted = (browser or self.default_browser or "").strip()
        if wanted:
            b = self.find_browser(wanted)
            if b is None:
                names = ", ".join(sorted({x["name"] for x in self.browsers()})) or "none found"
                return False, f"I couldn't find a browser called {wanted}. Installed: {names}."
            subprocess.Popen([b["exe"], url])
            return True, b["name"]
        if platform.system() == "Windows":
            os.startfile(url)  # type: ignore[attr-defined]  # the user's default browser
        else:
            import webbrowser
            webbrowser.open(url)
        return True, "your default browser"

    # -- folders ---------------------------------------------------------------------
    def find_folder(self, name: str) -> Optional[Path]:
        q = _norm(name)
        if not q or not self.home.is_dir():
            return None
        try:
            folders = [p for p in self.home.iterdir() if p.is_dir() and not p.name.startswith(".")]
        except OSError:
            return None
        for p in folders:
            if p.name.lower() == q or p.name.lower() == q.rstrip("s") or p.name.lower().rstrip("s") == q:
                return p
        return None

    # -- websites --------------------------------------------------------------------
    @staticmethod
    def looks_like_domain(target: str) -> bool:
        return bool(_DOMAIN.match((target or "").strip()))

    def site_for(self, target: str) -> Optional[str]:
        """The site's own address if `target` IS a website (a URL/domain, or a name whose
        www.<name>.com answers); None otherwise. "camera and america" (a misheard
        sentence) is not a website and must not be guessed into one."""
        t = (target or "").strip()
        if t.lower().startswith(("http://", "https://")):
            return t
        if self.looks_like_domain(t):
            return "https://" + t
        key = _norm(t)
        try:
            cache = json.loads(self._site_cache.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
        if key in cache:
            hit = cache[key]
            return None if (not hit or "duckduckgo.com" in hit) else hit  # old fallback entries
        slug = re.sub(r"[^a-z0-9]", "", key)
        url = f"https://www.{slug}.com" if slug and self._site_checker(f"https://www.{slug}.com") else None
        cache[key] = url
        try:
            self._site_cache.parent.mkdir(parents=True, exist_ok=True)
            self._site_cache.write_text(json.dumps(cache, indent=1), encoding="utf-8")
        except OSError:
            pass
        return url

    def resolve_site(self, target: str) -> str:
        """The site's address, or — when the name isn't a site — a search for it on the
        user's default search engine (Google unless configured otherwise)."""
        return self.site_for(target) or self.search_url_template.replace("{query}", quote_plus((target or "").strip()))

    @staticmethod
    def _site_responds(url: str) -> bool:
        try:
            import requests
            r = requests.head(url, timeout=2.5, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
            return r.status_code < 400 or r.status_code in (403, 405)  # some sites refuse HEAD
        except Exception:
            return False


_shared: Optional[AppIndex] = None


def shared_index(config=None) -> AppIndex:
    """One index per process so discovery runs once, not once per tool."""
    global _shared
    if _shared is None:
        _shared = AppIndex(config)
    return _shared
