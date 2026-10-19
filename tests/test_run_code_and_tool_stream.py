"""Seen live 19 Oct: (1) every Gemini tool call was dropped from the stream ("completed empty",
asked Google again); (2) run_code killed a web server after 60 s, and a program whose server
lived in a daemon thread exited at once, yet she said "the server is live"."""
from __future__ import annotations

import json
import socket
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from veronica.tools import learning_tools as lt


# ---------------------------------------------------------------- tool calls in a stream

def _sse(*events) -> list[str]:
    return [("data: " + (e if isinstance(e, str) else json.dumps(e))) for e in events]


@pytest.mark.parametrize("finish", ["stop", "tool_calls"])
def test_gemini_style_tool_call_stream_is_not_dropped(monkeypatch, finish):
    import requests
    from veronica.llm.openai_compatible_provider import OpenAICompatibleProvider
    frag = {"choices": [{"delta": {"role": "assistant", "tool_calls": [{
        "id": "call_1", "extra_content": {"google": {"thought_signature": "SIG"}},
        "function": {"name": "get_weather", "arguments": '{"city":"Pune"}'}}]}, "index": 0}]}
    end = {"choices": [{"delta": {"role": "assistant"}, "finish_reason": finish, "index": 0}]}
    lines = _sse(frag, end, "[DONE]")

    class Resp:
        encoding = "utf-8"
        def raise_for_status(self):
            pass
        def iter_lines(self, decode_unicode=False):
            return iter(lines)
    monkeypatch.setattr(requests, "post", lambda *a, **k: Resp())
    p = OpenAICompatibleProvider(base_url="https://x/v1", api_key="k", model="m")
    chunks = list(p.chat_stream([], tools=[{"type": "function"}]))
    calls = [c.metadata["tool_calls"] for c in chunks if c.metadata.get("tool_calls")]
    assert calls and calls[0][0]["name"] == "get_weather" and calls[0][0]["arguments"] == {"city": "Pune"}
    assert chunks[-1].done and chunks[-1].finish_reason == "tool_calls"
    # the thought signature is replayed with the call on the next request
    replay = p._tool_turns["call_1"]["tool_calls"][0]
    assert replay["extra_content"] == {"google": {"thought_signature": "SIG"}}


# ---------------------------------------------------------------- run_code and servers

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _project(tmp_path: Path, body: str) -> Path:
    d = tmp_path / "proj"
    d.mkdir()
    (d / "main.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return d


def test_a_server_is_left_running_not_killed(tmp_path):
    port = _free_port()
    proj = _project(tmp_path, f"""
        import http.server, socketserver
        print("serving at http://localhost:{port}", flush=True)
        with socketserver.TCPServer(("127.0.0.1", {port}), http.server.SimpleHTTPRequestHandler) as s:
            s.serve_forever()
    """)
    code, output = lt.RunCodeTool._run_process([sys.executable, "main.py"], proj, 30)
    try:
        assert code == lt.RUNNING_CODE and f"listening on port {port}" in output
        assert lt._SERVERS["proj"].poll() is None                    # still alive
    finally:
        assert lt._stop_server("proj")
    assert lt._SERVERS.get("proj") is None


def test_a_finishing_program_reports_its_exit_code(tmp_path):
    proj = _project(tmp_path, "print('hello')\n")
    code, output = lt.RunCodeTool._run_process([sys.executable, "main.py"], proj, 30)
    assert code == 0 and "hello" in output


def test_a_program_that_never_finishes_or_listens_is_stopped(tmp_path):
    proj = _project(tmp_path, "import time\nwhile True: time.sleep(1)\n")
    code, output = lt.RunCodeTool._run_process([sys.executable, "main.py"], proj, 2)
    assert code == -1 and "still running after 2 seconds" in output


def test_run_code_reports_a_server_honestly(tmp_path):
    tool = lt.RunCodeTool(config=None, ask=lambda *a, **k: "",
                          runner=lambda cmd, cwd, t: (lt.RUNNING_CODE, "hi\n[running: listening on port 8080]"),
                          projects=SimpleNamespace(find=lambda name: tmp_path, entry_point=lambda p: tmp_path / "main.py",
                                                   set_last=lambda p: None),
                          knowledge=SimpleNamespace(), facts=lambda: [])
    r = tool.run()
    assert r.success and r.data["running"] and "port 8080" in r.message and "stop the server" in r.message


# ---------------------------------------------------------------- "ran fine" but the page isn't there

def test_a_dead_server_is_not_a_success():
    port = _free_port()                                              # nothing listening
    claim = lt._dead_server_claim(f"JARVIS HUD Server running at http://localhost:{port}")
    assert "nothing is listening" in claim and "serve_forever" in claim


def test_a_live_server_or_no_url_passes():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert lt._dead_server_claim(f"serving at http://127.0.0.1:{srv.server_port}/") == ""
    finally:
        srv.shutdown()
    assert lt._dead_server_claim("Done. 42 files processed.") == ""


def test_a_dead_server_goes_through_the_fix_loop(tmp_path):
    port = _free_port()
    proj = _project(tmp_path, "print('x')\n")
    runs, fixed = [], []

    def runner(cmd, cwd, t):
        runs.append(1)
        return (0, f"serving at http://localhost:{port}") if len(runs) == 1 else (0, "all good")
    tool = lt.RunCodeTool(config=None, ask=lambda *a, **k: "", runner=runner,
                          projects=SimpleNamespace(find=lambda n: proj, entry_point=lambda p: proj / "main.py",
                                                   set_last=lambda p: None),
                          knowledge=SimpleNamespace(add_lesson=lambda *a: None), facts=lambda: [])
    tool._fix = lambda p, error: fixed.append(error) or True
    tool._learn_from_fix = lambda *a: None
    r = tool.run()
    assert len(runs) == 2 and "nothing is listening" in fixed[0] and r.success and r.data["fixes"] == 1


def test_stop_server_phrases():
    stop = lt.StopCodeTool(config=None)
    for said in ("stop the server", "please stop the server", "shut down the local server", "kill the program",
                 "stop the project"):
        assert stop.matches(said), said
    for said in ("stop the music", "close Chrome", "stop", "stop the timer", "stop talking"):
        assert not stop.matches(said), said
    assert "don't have anything running" in stop.run().message


def test_the_coder_is_told_to_keep_servers_alive():
    assert "keep its own process alive" in lt._CODER_SYSTEM and "daemon thread" in lt._CODER_SYSTEM
