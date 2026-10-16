"""
God's Eye View (github.com/bilawalsidhu/gods-eye-view, MIT) — live public spatial data
(aircraft, satellites, earthquakes, public traffic cameras, weather, launches, ships) on a
3D globe. VERONICA uses it two ways:

* Voice answers: the app's own MCP server (stdio JSON-RPC) is started and its tools are
  registered as VERONICA tools ("what planes are over me?", "when can I see the ISS?").
* "Show me": every answer carries a view URL; VERONICA opens the globe on it.

Only public data the app itself provides. It does not track people (the project refuses
face recognition / person tracking, and so does this integration). Results are external
data: summarized for the user, never followed as instructions.
"""
from __future__ import annotations

import atexit
import json
import queue
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("integrations.gods_eye")

_MAX_RESULT_CHARS = 3000
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class GodsEyeService:
    """Owns the app's dev server (started hidden if not already running) and one MCP
    stdio session. Thread-safe: one request at a time."""

    def __init__(self, root: Path, base_url: str = "http://localhost:4173", auto_start: bool = True,
                 popen=subprocess.Popen) -> None:
        self.root, self.base_url, self.auto_start = Path(root), base_url.rstrip("/"), auto_start
        self._popen = popen
        self._server: Optional[subprocess.Popen] = None
        self._mcp: Optional[subprocess.Popen] = None
        self._lines: "queue.Queue[str]" = queue.Queue()
        self._lock = threading.RLock()
        self._next_id = 0
        self.tools: list[dict] = []
        self.last_view_url: Optional[str] = None
        atexit.register(self.close)

    # -- lifecycle --------------------------------------------------------------------
    @property
    def installed(self) -> bool:
        return (self.root / "server" / "mcp" / "stdio.js").is_file()

    def server_up(self) -> bool:
        try:
            urllib.request.urlopen(self.base_url, timeout=2)
            return True
        except Exception:
            return False

    def ensure_server(self, wait_s: float = 90.0) -> bool:
        if self.server_up():
            return True
        if not self.auto_start or not self.installed:
            return False
        log = open(self.root / "veronica-server.log", "ab")
        npm = "npm.cmd" if subprocess.os.name == "nt" else "npm"
        self._server = self._popen([npm, "run", "dev"], cwd=str(self.root), stdout=log, stderr=log,
                                   creationflags=_NO_WINDOW)
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            if self.server_up():
                return True
            time.sleep(1)
        return False

    def connect(self) -> list[dict]:
        """Start the server if needed, open the MCP session, return its tool list."""
        with self._lock:
            if self._mcp is not None and self._mcp.poll() is None and self.tools:
                return self.tools
            if not self.ensure_server():
                raise RuntimeError("God's Eye View server is not running and could not be started.")
            self._mcp = self._popen(["node", str(self.root / "server" / "mcp" / "stdio.js"), "--api-base",
                                     self.base_url], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, encoding="utf-8", creationflags=_NO_WINDOW)
            self._lines = queue.Queue()
            threading.Thread(target=self._pump, args=(self._mcp, self._lines), daemon=True).start()
            self._request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "veronica", "version": "1.0"}})
            self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            self.tools = self._request("tools/list").get("tools", [])
            return self.tools

    @staticmethod
    def _pump(proc, lines) -> None:
        for line in proc.stdout:
            lines.put(line)
        lines.put("")  # EOF

    def _send(self, msg: dict) -> None:
        self._mcp.stdin.write(json.dumps(msg) + "\n")
        self._mcp.stdin.flush()

    def _request(self, method: str, params: Optional[dict] = None, timeout: float = 45.0) -> dict:
        self._next_id += 1
        rid = self._next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        end = time.monotonic() + timeout
        while True:
            try:
                line = self._lines.get(timeout=max(0.1, end - time.monotonic()))
            except queue.Empty:
                raise TimeoutError(f"God's Eye View didn't answer {method} in {timeout:.0f}s")
            if not line:
                raise RuntimeError("God's Eye View MCP server stopped")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "MCP error"))
                return msg.get("result", {})

    def call(self, name: str, arguments: dict) -> dict:
        with self._lock:
            self.connect()
            try:
                result = self._request("tools/call", {"name": name, "arguments": arguments})
            except RuntimeError:
                self._mcp = None  # dead session: reconnect once
                self.connect()
                result = self._request("tools/call", {"name": name, "arguments": arguments})
        url = _view_url(result)
        if url:
            self.last_view_url = url
        return result

    def close(self) -> None:
        for proc in (self._mcp, self._server):
            if proc is not None and proc.poll() is None:
                try:
                    if subprocess.os.name == "nt":  # npm spawns children: end the whole tree
                        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                                       capture_output=True, creationflags=_NO_WINDOW)
                    else:
                        proc.terminate()
                except Exception:
                    pass


def _view_url(result: dict) -> Optional[str]:
    sc = result.get("structuredContent") or {}
    view = sc.get("view") if isinstance(sc, dict) else None
    if isinstance(view, dict) and view.get("url"):
        return view["url"]
    for c in result.get("content") or []:
        m = re.search(r"https?://[^\s\"']+#v=\d[^\s\"']*", c.get("text", "") if isinstance(c, dict) else "")
        if m:
            return m.group(0)
    return None


_UTC_STAMP = re.compile(r"\b(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?Z\b")


def to_local_times(text: str) -> str:
    """'2026-10-16T08:46:29.578Z' -> '2026-10-16 14:16 local time'. The app reports UTC;
    left as-is the model reads 08:46 as the user's morning (it was 2:16 PM in India)."""
    import datetime as dt

    def local(m: re.Match) -> str:
        when = dt.datetime.fromisoformat(f"{m.group(1)}T{m.group(2)}+00:00").astimezone()
        return when.strftime("%Y-%m-%d %H:%M local time")
    return _UTC_STAMP.sub(local, text)


def result_text(result: dict, limit: int = _MAX_RESULT_CHARS) -> str:
    """The summary line(s) plus the data, trimmed, for the LLM to speak from."""
    texts = [c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
    out = to_local_times("\n".join(t for t in texts if t))
    return out if len(out) <= limit else out[:limit] + " …[truncated]"


class GodsEyeTool(Tool):
    """One God's Eye View MCP tool exposed to VERONICA's brain (schema from the app)."""

    def __init__(self, service: GodsEyeService, spec: dict, location: Callable[[], Optional[dict]],
                 radius_km: float = 100.0, describe_image: Optional[Callable[[str, bytes], str]] = None) -> None:
        self.service, self.spec = service, spec
        self.name = spec["name"]
        desc = re.sub(r"\s+", " ", spec.get("description") or "").strip()
        self.description = ("[Live public data, God's Eye View] " + desc)[:400] + \
            " If the user names ANY place (a city, state or country such as 'America'), pass it as " \
            "area.place / location.place. Omit area/location only when they mean where they are."
        schema = json.loads(json.dumps(spec.get("inputSchema") or {"type": "object", "properties": {}}))
        props = schema.get("properties", {})
        if "area" in props or "location" in props:  # we fill these in from the user's location
            schema["required"] = [r for r in schema.get("required", []) if r not in ("area", "location")]
        self.parameters = schema
        self.permission = PermissionLevel.MODERATE if self.name == "show_in_gods_eye_view" else PermissionLevel.SAFE
        self._location, self._radius = location, radius_km
        self._describe_image = describe_image

    def _with_defaults(self, kwargs: dict) -> dict:
        props = self.parameters.get("properties", {})
        args = {k: v for k, v in kwargs.items() if v not in (None, "", {}, [])}
        if self.name == "show_in_gods_eye_view":
            return args  # showing: the view the user asked for, never silently "where I am"
        here = self._location() if ("area" in props and "area" not in args) or \
            ("location" in props and "location" not in args) else None
        if here:
            if "area" in props and "area" not in args and self.name not in ("get_earthquakes", "get_cyclones"):
                args["area"] = {**here, "radius_km": self._radius} if "lat" in here else here
            if "location" in props and "location" not in args:
                args["location"] = here
        return args

    def run(self, **kwargs: Any) -> ToolResult:
        args = self._with_defaults(kwargs)
        try:
            result = self.service.call(self.name, args)
        except Exception as e:  # noqa: BLE001 — the app being down is a handled outcome
            logger.warning("God's Eye View %s failed: %s", self.name, e)
            return ToolResult.fail(f"God's Eye View isn't available right now ({e}).")
        if result.get("isError"):
            return ToolResult.fail(result_text(result, 600))
        text = result_text(result)
        images = [c for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") == "image"]
        if images and self._describe_image is not None:
            import base64
            try:
                seen = self._describe_image("Describe what this live public traffic camera shows right now, "
                                            "in two spoken sentences.", base64.b64decode(images[0]["data"]))
                text = (text + "\nCamera view: " + seen).strip()
            except Exception as e:  # noqa: BLE001
                logger.warning("Camera snapshot description failed: %s", e)
        if self.name == "show_in_gods_eye_view" and self.service.last_view_url:
            _open(self.service.last_view_url)
            text = "Opened God's Eye View. " + text
        return ToolResult.ok(text or "Done.", view_url=self.service.last_view_url)


def _open(url: str, browser: str = "") -> tuple[bool, str]:
    from veronica.tools.app_index import shared_index
    return shared_index().open_url(url, browser)


_SHOW = re.compile(r"^\s*(?:please\s+)?(?:show(?:\s+me)?(?:\s+(?:that|it|this|them))?(?:\s+on\s+(?:the\s+)?(?:globe|map))?|"
                   r"(?:open|launch|start)\s+(?:the\s+)?(?:globe|god'?s\s*eye(?:\s*view)?|world\s+view|3d\s+map))"
                   r"(?:\s+in\s+(\w+))?\s*[.!]?\s*$", re.I)


class ShowGlobeTool(Tool):
    name = "open_gods_eye_view"
    description = ("Open the God's Eye View 3D globe in the browser, on the last thing looked up "
                   "(planes, satellites, earthquakes, cameras), or on the whole world.")
    permission = PermissionLevel.MODERATE
    parameters = {"type": "object", "properties": {"browser": {"type": "string"}}, "required": []}

    def __init__(self, service: GodsEyeService) -> None:
        self.service = service

    def matches(self, text: str) -> bool:
        m = _SHOW.match(text or "")
        if not m:
            return False
        bare = not re.search(r"globe|god|world|map|3d", text, re.I)
        return not bare or self.service.last_view_url is not None  # bare "show me" only after a lookup

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _SHOW.match(text or "")
        return {"browser": (m.group(1) or "") if m else ""}

    def run(self, browser: str = "", **kwargs: Any) -> ToolResult:
        if not self.service.ensure_server():
            return ToolResult.fail("God's Eye View isn't installed or couldn't start.")
        url = self.service.last_view_url or self.service.base_url
        ok, where = _open(url, browser)
        if not ok:
            return ToolResult.fail(where)
        what = "on what we just looked up" if self.service.last_view_url else "on the whole world"
        return ToolResult.ok(f"Opening God's Eye View {what}.", url=url)


def register_gods_eye(registry, config, profile) -> Optional[GodsEyeService]:
    """Add the integration to a tool registry. Tools appear once the app's MCP server
    is up (connected on a background thread, so VERONICA starts instantly)."""
    gcfg = getattr(config, "gods_eye", None)
    if not gcfg or not gcfg.enabled:
        return None
    root = config.resolve_path(gcfg.path)
    service = GodsEyeService(root, gcfg.base_url, gcfg.auto_start)
    if not service.installed:
        logger.warning("God's Eye View enabled but not installed at %s", root)
        return None
    registry.register(ShowGlobeTool(service))

    def here() -> Optional[dict]:
        coords = profile.detected_coords()
        if coords:
            return {"lat": coords[0], "lon": coords[1]}
        city = profile.home_city()
        return {"place": city} if city else None

    describe = None
    try:
        from veronica.tools.screen_tools import _openai_vision
        describe = _openai_vision(config)
    except Exception:  # noqa: BLE001
        pass
    wanted = set(gcfg.tools or [])

    def connect() -> None:
        try:
            specs = service.connect()
        except Exception as e:  # noqa: BLE001
            logger.warning("God's Eye View not connected: %s", e)
            return
        added = 0
        for spec in specs:
            if spec.get("name") in wanted:
                registry.register(GodsEyeTool(service, spec, here, gcfg.default_radius_km, describe))
                added += 1
        logger.info("God's Eye View connected: %d tool(s) added", added)

    threading.Thread(target=connect, name="gods-eye-connect", daemon=True).start()
    return service
