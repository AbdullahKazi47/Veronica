"""
Smoke tests for the Stitch bridge (EventBroadcaster, StitchCommandHandler, HTTP).

Strategy:
  - Unit-test the command handler and broadcaster with fakes (no network).
  - Spin up StitchHttpServer on an ephemeral port for one live round-trip check.
  - No third-party test deps; everything is stdlib + VERONICA core.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from unittest.mock import MagicMock

import pytest

from veronica.automation import TaskExecutor
from veronica.automation.planner import TaskPlanStep
from veronica.config import Config
from veronica.core.orchestrator import Orchestrator, OrchestratorResponse
from veronica.tools.base import Tool, ToolResult
from veronica.tools.registry import ToolRegistry
from veronica.ui.stitch.bridge import (
    EventBroadcaster,
    StitchCommandHandler,
    StitchHttpServer,
)


def _json_from_sse(line: str) -> dict:
    """Turn one 'data: {...}' SSE frame into its payload dict."""
    return json.loads(line.replace("data: ", "").strip())


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

class _FakeGate:
    """Always-allow permission gate for test registries."""
    def check(self, tool_name, level, description=""):
        class _V:
            allowed = True
            reason = ""
        return _V()


class _EchoTool(Tool):
    description = "Echo tool for testing"
    def run(self, **kwargs):
        return ToolResult.ok(f"echo:{kwargs.get('text', '')}")


def _build_config() -> Config:
    """A default Config tuned for tests: echo LLM, voice disabled, quiet paths."""
    cfg = Config()
    cfg.llm.provider = "echo"
    cfg.stt.backend = "none"
    cfg.tts.backend = "none"
    cfg.memory.long_term_path = "data/test-memory.json"
    return cfg


def _build_orchestrator(config: Config | None = None) -> Orchestrator:
    config = config or _build_config()
    reg = ToolRegistry(_FakeGate())
    reg.register(_EchoTool())
    llm = MagicMock()
    rv = MagicMock()
    rv.content = "LLM reply"
    rv.tool_calls = None  # explicit: no tool calls on this turn
    llm.chat.return_value = rv
    mem = MagicMock()
    mem.all.return_value = []
    return Orchestrator(config=config, llm=llm, tools=reg, long_term_memory=mem)


# ---------------------------------------------------------------------------
# EventBroadcaster unit tests
# ---------------------------------------------------------------------------

class TestEventBroadcaster:
    def test_subscribe_returns_queue(self):
        bc = EventBroadcaster()
        q = bc.subscribe()
        assert bc.subscriber_count == 1

    def test_publish_delivers_to_subscribers(self):
        bc = EventBroadcaster()
        q = bc.subscribe()
        bc.publish("test_signal", ["hello"])
        raw = q.get(timeout=1)
        payload = json.loads(raw.replace("data: ", "").strip())
        assert payload["signal"] == "test_signal"
        assert payload["args"] == ["hello"]

    def test_unsubscribe_removes_subscriber(self):
        bc = EventBroadcaster()
        q = bc.subscribe()
        assert bc.subscriber_count == 1
        bc.unsubscribe(q)
        assert bc.subscriber_count == 0

    def test_full_queue_is_dropped_not_blocking(self):
        bc = EventBroadcaster()
        q = bc.subscribe()  # maxsize=200
        for _ in range(200):
            q.put_nowait("filler")
        # Should not raise or block; the slow consumer is dropped so the UI stalls nothing.
        bc.publish("overflow_test", [])
        assert bc.subscriber_count == 0  # full queue removed from the fan-out set

    def test_multiple_subscribers_all_receive(self):
        bc = EventBroadcaster()
        q1 = bc.subscribe()
        q2 = bc.subscribe()
        bc.publish("multi", [42])
        assert "multi" in q1.get(timeout=1)
        assert "multi" in q2.get(timeout=1)


# ---------------------------------------------------------------------------
# StitchCommandHandler unit tests (no HTTP)
# ---------------------------------------------------------------------------

class TestStitchCommandHandler:
    def _handler(self) -> tuple[StitchCommandHandler, EventBroadcaster]:
        config = _build_config()
        orch = _build_orchestrator(config)
        bc = EventBroadcaster()
        return StitchCommandHandler(orch, config, bc), bc

    def test_send_command_dispatches_to_orchestrator(self):
        handler, bc = self._handler()
        q = bc.subscribe()  # subscribe before the worker thread fires
        result = handler.handle("sendCommand", ["echo hello"])
        assert result["ok"] is True
        # Wait briefly for worker thread, then drain events.
        time.sleep(0.4)
        events = []
        while not q.empty():
            events.append(q.get_nowait())
        signals = [_json_from_sse(e)["signal"] for e in events]
        assert "set_core_state" in signals
        assert "show_ultron_message" in signals
        # The last core state should return to idle once the reply is in.
        states = [p["args"][0] for p in (_json_from_sse(e) for e in events) if p["signal"] == "set_core_state"]
        assert states[-1] == "idle"

    def test_request_system_status_returns_stats(self):
        handler, _ = self._handler()
        result = handler.handle("requestSystemStatus")
        assert result["ok"] is True
        assert "cpu" in result
        assert "ram" in result
        assert "net" in result

    def test_unknown_method_returns_error(self):
        handler, _ = self._handler()
        result = handler.handle("nonexistent_method")
        assert result["ok"] is False
        assert "Unknown method" in result["error"]

    def test_empty_command_returns_ok(self):
        handler, _ = self._handler()
        result = handler.handle("sendCommand", [""])
        assert result["ok"] is True

    def test_start_listening_when_stt_disabled(self):
        handler, bc = self._handler()
        result = handler.handle("startListening")
        assert result["ok"] is False
        assert "stt" in result.get("reason", "").lower() or "disabled" in result.get("reason", "").lower()

    def test_stop_listening(self):
        handler, _ = self._handler()
        result = handler.handle("stopListening")
        assert result["ok"] is True

    def test_tool_event_callback_is_installed(self):
        handler, _ = self._handler()
        # The handler should have set tool_event_callback on the orchestrator
        assert handler.orchestrator.tool_event_callback is not None
        assert callable(handler.orchestrator.tool_event_callback)

    def test_tool_event_callback_publishes_activity(self):
        handler, bc = self._handler()
        q = bc.subscribe()  # subscribe before firing the callback
        handler.orchestrator.tool_event_callback("test tool fire")
        raw = q.get(timeout=1)
        payload = _json_from_sse(raw)
        assert payload["signal"] == "show_tool_activity"
        assert payload["args"][0] == "test tool fire"

    def test_request_memory(self):
        handler, _ = self._handler()
        result = handler.handle("requestMemory")
        assert result["ok"] is True
        assert "count" in result


# ---------------------------------------------------------------------------
# Multi-step task commands (bridge → executor → SSE)
# ---------------------------------------------------------------------------

class _NamedEchoTool(Tool):
    name = "echo_tool"
    description = "Echoes text."
    def run(self, **kwargs):
        return ToolResult.ok("echo ok")


class _FakePlanner:
    """Returns a fixed two-step plan so task tests are deterministic (no LLM)."""
    def create_plan(self, goal, tools_schema=None, llm=None):
        return [
            TaskPlanStep(1, "Echo once", "echo_tool", {"text": "a"}),
            TaskPlanStep(2, "Echo twice", "echo_tool", {"text": "b"}),
        ]


class TestTaskCommands:
    def _handler(self) -> tuple[StitchCommandHandler, EventBroadcaster]:
        config = _build_config()
        reg = ToolRegistry(_FakeGate())
        reg.register(_NamedEchoTool())
        llm = MagicMock()
        rv = MagicMock()
        rv.content = "LLM reply"
        rv.tool_calls = None
        llm.chat.return_value = rv
        mem = MagicMock()
        mem.all.return_value = []
        orch = Orchestrator(config=config, llm=llm, tools=reg, long_term_memory=mem)
        bc = EventBroadcaster()
        handler = StitchCommandHandler(orch, config, bc, planner=_FakePlanner())
        return handler, bc

    def test_start_task_emits_started_and_completed(self):
        handler, bc = self._handler()
        q = bc.subscribe()
        result = handler.handle("startTask", ["do a research task"])
        assert result["ok"] is True
        # The task runs on a worker thread; give it time to plan + execute.
        time.sleep(0.6)
        events = []
        while not q.empty():
            events.append(_json_from_sse(q.get_nowait()))
        signals = [e["signal"] for e in events]
        assert "task_started" in signals
        assert "task_completed" in signals
        started = next(e for e in events if e["signal"] == "task_started")
        assert len(started["args"]) == 3  # task_id, goal, total_steps
        assert started["args"][2] == 2

    def test_get_task_status(self):
        handler, bc = self._handler()
        result = handler.handle("startTask", ["do a research task"])
        assert result["ok"] is True
        time.sleep(0.6)
        tasks = handler.handle("listTasks")
        assert tasks["ok"] is True
        assert len(tasks["tasks"]) == 1
        task_id = tasks["tasks"][0]["task_id"]
        detail = handler.handle("getTaskStatus", [task_id])
        assert detail["ok"] is True
        assert detail["task"]["status"] == "completed"

    def test_cancel_unknown_task(self):
        handler, _ = self._handler()
        result = handler.handle("cancelTask", ["NOPE"])
        assert result["ok"] is False

    def test_empty_task_goal_rejected(self):
        handler, _ = self._handler()
        result = handler.handle("startTask", [""])
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# Live HTTP round-trip (ephemeral port)
# ---------------------------------------------------------------------------

class TestStitchHttpServer:
    @pytest.fixture()
    def server_url(self):
        config = _build_config()
        orch = _build_orchestrator(config)
        srv = StitchHttpServer(orch, config, host="127.0.0.1", port=0)
        # Grab the actual port assigned by the OS
        actual_port = srv._server.server_address[1]
        srv.serve_in_background()
        yield f"http://127.0.0.1:{actual_port}"
        srv.shutdown()

    def test_get_index_serves_html(self, server_url):
        resp = urllib.request.urlopen(f"{server_url}/")
        html = resp.read().decode()
        assert "VERONICA" in html
        assert "core-canvas-container" in html

    def test_post_api_returns_json(self, server_url):
        payload = json.dumps({"method": "requestSystemStatus", "args": []}).encode()
        req = urllib.request.Request(
            f"{server_url}/api",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req)
        body = json.loads(resp.read().decode())
        assert body["ok"] is True
        assert "cpu" in body

    def test_sse_endpoint_sends_backend_ready(self, server_url):
        """Connect to /events and verify the first message is backend_ready."""
        # Use a short timeout to avoid hanging if the event never arrives.
        import socket
        host, port = "127.0.0.1", int(server_url.split(":")[-1])
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect((host, port))
            sock.sendall(b"GET /events HTTP/1.1\r\nHost: localhost\r\n\r\n")
            # Headers and the handshake frame arrive as separate writes; keep
            # reading until either the frame shows up or the deadline passes.
            data = ""
            while "backend_ready" not in data:
                chunk = sock.recv(1024).decode(errors="replace")
                if not chunk:
                    break
                data += chunk
            assert "backend_ready" in data
            assert "200" in data.split("\r\n")[0]  # HTTP 200 OK
        finally:
            sock.close()

    def test_post_unknown_method_returns_error(self, server_url):
        payload = json.dumps({"method": "nope", "args": []}).encode()
        req = urllib.request.Request(
            f"{server_url}/api",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req)
        body = json.loads(resp.read().decode())
        assert body["ok"] is False
        assert "Unknown method" in body["error"]

    def test_get_nonexistent_path_returns_404(self, server_url):
        try:
            urllib.request.urlopen(f"{server_url}/nope")
            assert False, "Expected HTTPError for 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
