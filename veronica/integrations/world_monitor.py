"""
World Monitor (github.com/koala73/worldmonitor) — a live global intelligence dashboard:
conflicts, military flights, ships, markets, internet outages, disasters, country risk.

Self-hosted on this PC with Docker (external/worldmonitor, `docker compose up -d`), so its
MCP server is free: it is served at http://localhost:3000/api/mcp and accepts the operator
key VERONICA generated (WORLDMONITOR_KEY in config/.env, WORLDMONITOR_VALID_KEYS in
World Monitor's own .env). The hosted worldmonitor.app MCP needs a paid plan; this doesn't.

Its tools join VERONICA's like God's Eye's: read-only questions the model can ask
("is anything happening near Taiwan?", "any internet outages right now?"). Their answers
are live public data — never instructions. Docker containers are started on demand in the
background and data is re-seeded on a schedule; neither ever blocks VERONICA starting.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Optional

from veronica.logging_setup import get_logger
from veronica.safety.permissions import PermissionLevel
from veronica.tools.base import Tool, ToolResult

logger = get_logger("integrations.world_monitor")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class WorldMonitorClient:
    """MCP over HTTP (JSON-RPC POST; the reply is JSON or a server-sent event)."""

    def __init__(self, base_url: str, key: str, http=None, timeout: float = 30.0) -> None:
        self.url = base_url.rstrip("/") + "/api/mcp"
        self.key, self._http, self.timeout = key, http, timeout
        self._ids = 0

    def _post(self, method: str, params: Optional[dict] = None) -> dict:
        http = self._http or __import__("requests")
        self._ids += 1
        resp = http.post(self.url, timeout=self.timeout,
                         headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                                  "X-WorldMonitor-Key": self.key},
                         json={"jsonrpc": "2.0", "id": self._ids, "method": method, "params": params or {}})
        if resp.status_code in (401, 403):
            raise PermissionError("World Monitor refused the key (check WORLDMONITOR_KEY / WORLDMONITOR_VALID_KEYS).")
        resp.raise_for_status()
        body = resp.text or ""
        if "text/event-stream" in resp.headers.get("content-type", "") or body.lstrip().startswith(("event:", "data:")):
            datas = [line[5:].strip() for line in body.splitlines() if line.startswith("data:")]
            body = next((d for d in reversed(datas) if d.startswith("{")), "{}")
        msg = json.loads(body or "{}")
        if msg.get("error"):
            raise RuntimeError(msg["error"].get("message", "World Monitor error"))
        return msg.get("result") or {}

    def list_tools(self) -> list[dict]:
        return list(self._post("tools/list").get("tools") or [])

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._post("tools/call", {"name": name, "arguments": arguments})

    def is_up(self) -> bool:
        try:
            self.list_tools()
            return True
        except Exception:  # noqa: BLE001
            return False


def result_text(result: dict, limit: int = 1800) -> str:
    parts = []
    for c in result.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
    if not parts and result.get("structuredContent"):
        parts.append(json.dumps(result["structuredContent"])[:limit])
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(p for p in parts if p)).strip()
    return out if len(out) <= limit else out[:limit] + " …[truncated]"


class WorldMonitorTool(Tool):
    """One World Monitor MCP tool, for VERONICA's brain (schema from the server)."""

    permission = PermissionLevel.SAFE          # read-only public data

    def __init__(self, client: WorldMonitorClient, spec: dict) -> None:
        self.client, self.spec = client, spec
        self.name = "wm_" + re.sub(r"[^a-z0-9_]+", "_", spec["name"].lower()).strip("_")
        desc = re.sub(r"\s+", " ", spec.get("description") or spec.get("title") or "").strip()
        self.description = ("[Live world intelligence, World Monitor] " + desc)[:420]
        self.parameters = spec.get("inputSchema") or {"type": "object", "properties": {}}

    def matches(self, text: str) -> bool:
        return False                           # chosen by the model from the description

    def run(self, **kwargs: Any) -> ToolResult:
        args = {k: v for k, v in kwargs.items() if v not in (None, "", {}, [])}
        try:
            result = self.client.call_tool(self.spec["name"], args)
        except Exception as e:  # noqa: BLE001 — the stack being down is a handled outcome
            logger.warning("World Monitor %s failed: %s", self.spec["name"], e)
            return ToolResult.fail(f"World Monitor isn't available right now ({e}).")
        if result.get("isError"):
            return ToolResult.fail(result_text(result, 600) or "World Monitor couldn't answer that.")
        return ToolResult.ok(result_text(result) or "World Monitor had nothing to report.")


_OPEN = re.compile(r"^\s*(?:please\s+)?(?:open|show|launch|start)\s+(?:me\s+)?(?:the\s+)?world\s*monitor"
                   r"(?:\s+dashboard)?(?:\s+in\s+(\w+))?\s*[.!]?\s*$", re.I)


class OpenWorldMonitorTool(Tool):
    name = "open_world_monitor"
    description = "Open the World Monitor live dashboard (conflicts, flights, ships, markets, outages) in the browser."
    permission = PermissionLevel.MODERATE
    parameters = {"type": "object", "properties": {"browser": {"type": "string"}}, "required": []}

    def __init__(self, service: "WorldMonitorService", index=None) -> None:
        self.service, self._index = service, index

    def matches(self, text: str) -> bool:
        return bool(_OPEN.match(text))

    def extract_arguments(self, text: str) -> dict[str, Any]:
        m = _OPEN.match(text)
        return {"browser": (m.group(1) or "") if m else ""}

    def run(self, browser: str = "", **kwargs: Any) -> ToolResult:
        if not self.service.client.is_up():
            self.service.start_async()
            return ToolResult.fail("World Monitor is still starting (it runs in Docker). Give it a minute and ask again."
                                   + ("" if self.service.docker_ready() else
                                      " Docker isn't running: open Docker Desktop first."))
        if self._index is None:
            from veronica.tools.app_index import shared_index
            self._index = shared_index()
        ok, where = self._index.open_url(self.service.base_url, browser)
        return ToolResult.ok("Opening World Monitor.") if ok else ToolResult.fail(where)


class WorldMonitorService:
    """Starts the Docker stack on demand and keeps its data fresh (seeders)."""

    def __init__(self, root: Path, base_url: str, key: str, seed_every_minutes: float = 60) -> None:
        self.root, self.base_url = Path(root), base_url.rstrip("/")
        self.client = WorldMonitorClient(base_url, key)
        self.seed_every_s = max(10.0, seed_every_minutes) * 60
        self._starting = threading.Lock()
        self.status = "not started"

    @property
    def installed(self) -> bool:
        return (self.root / "docker-compose.yml").exists() and (self.root / ".env").exists()

    @staticmethod
    def docker_ready() -> bool:
        if shutil.which("docker") is None:
            return False
        try:
            r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True,
                               timeout=20, creationflags=_NO_WINDOW)
            return r.returncode == 0 and bool(r.stdout.strip())
        except Exception:  # noqa: BLE001
            return False

    def start(self) -> bool:
        """docker compose up -d (builds images the first time — slow, once)."""
        if self.client.is_up():
            self.status = "running"
            return True
        if not self.docker_ready():
            self.status = "Docker isn't running"
            return False
        with self._starting:
            self.status = "starting"
            r = subprocess.run(["docker", "compose", "up", "-d"], cwd=str(self.root), capture_output=True, text=True,
                               timeout=3600, creationflags=_NO_WINDOW)
            if r.returncode != 0:
                self.status = "failed to start"
                logger.warning("World Monitor didn't start: %s", (r.stderr or r.stdout)[-500:])
                return False
            for _ in range(60):
                if self.client.is_up():
                    self.status = "running"
                    return True
                time.sleep(5)
        self.status = "started, not answering yet"
        return False

    def start_async(self) -> None:
        if not self._starting.locked():
            threading.Thread(target=self.start, name="world-monitor-start", daemon=True).start()

    def seed_once(self) -> bool:
        """Fill World Monitor's database from its public sources (its own seeder scripts)."""
        script = self.root / "scripts" / "run-seeders.sh"
        sh = shutil.which("bash") or shutil.which("sh")
        if not script.exists() or sh is None:
            return False
        try:
            r = subprocess.run([sh, str(script)], cwd=str(self.root), capture_output=True, text=True, timeout=3600,
                               creationflags=_NO_WINDOW)
            logger.info("World Monitor seeders finished (exit %s).", r.returncode)
            return r.returncode == 0
        except Exception as e:  # noqa: BLE001
            logger.warning("World Monitor seeding failed: %s", e)
            return False

    def keep_seeding(self, stop: threading.Event) -> None:
        while not stop.is_set():
            if self.client.is_up():
                self.seed_once()
            stop.wait(self.seed_every_s)


def register_world_monitor(registry, config) -> Optional[WorldMonitorService]:
    """Add the 'open World Monitor' command now; World Monitor's own tools once its stack
    answers (started in the background if needed)."""
    wcfg = getattr(config, "world_monitor", None)
    if wcfg is None or not getattr(wcfg, "enabled", False):
        return None
    key = config.secret(getattr(wcfg, "key_env", "WORLDMONITOR_KEY")) or ""
    service = WorldMonitorService(config.resolve_path(wcfg.path), wcfg.base_url, key,
                                  getattr(wcfg, "seed_every_minutes", 60))
    if not service.installed or not key:
        logger.warning("World Monitor enabled but not set up at %s (or no key).", service.root)
        return None
    registry.register(OpenWorldMonitorTool(service))
    allowed = set(getattr(wcfg, "tools", None) or [])
    stop = threading.Event()

    def connect() -> None:
        if not service.client.is_up() and getattr(wcfg, "auto_start", True):
            service.start()
        try:
            specs = service.client.list_tools()
        except Exception as e:  # noqa: BLE001
            logger.warning("World Monitor not connected (%s): %s", service.status, e)
            return
        added = 0
        for spec in specs[: int(getattr(wcfg, "max_tools", 40))]:
            if allowed and spec.get("name") not in allowed:
                continue
            tool = WorldMonitorTool(service.client, spec)
            if registry.get(tool.name) is None:
                registry.register(tool)
                added += 1
        logger.info("World Monitor connected: %d tool(s) added", added)
        if getattr(wcfg, "seed_every_minutes", 60):
            threading.Thread(target=service.keep_seeding, args=(stop,), name="world-monitor-seed", daemon=True).start()

    threading.Thread(target=connect, name="world-monitor-connect", daemon=True).start()
    return service
