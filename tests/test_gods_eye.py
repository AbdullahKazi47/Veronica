"""God's Eye View integration: real JSON-RPC client against a fake MCP server, location
defaults, 'show me' links, and routing order. No app, browser or network needed."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from veronica.integrations import gods_eye as ge
from veronica.safety.permissions import PermissionLevel

FAKE_SERVER = textwrap.dedent(r'''
    import json, sys
    TOOLS = [
        {"name": "aircraft_in_area", "description": "Aircraft in an area, nearest first.",
         "inputSchema": {"type": "object", "properties": {"area": {"type": "object"}, "limit": {"type": "integer"}}}},
        {"name": "next_satellite_pass", "description": "Next visible pass.",
         "inputSchema": {"type": "object", "properties": {"location": {"type": "object"}}, "required": ["location"]}},
        {"name": "find_military_installations", "description": "x", "inputSchema": {"type": "object", "properties": {}}},
        {"name": "show_in_gods_eye_view", "description": "Show a view.",
         "inputSchema": {"type": "object", "properties": {"area": {"type": "object"}}}},
    ]
    for line in sys.stdin:
        msg = json.loads(line)
        if "id" not in msg:
            continue
        m, rid = msg["method"], msg["id"]
        if m == "initialize":
            res = {"serverInfo": {"name": "fake"}, "protocolVersion": "2025-06-18"}
        elif m == "tools/list":
            res = {"tools": TOOLS}
        else:
            args = msg["params"]["arguments"]
            url = "http://localhost:4173/#v=2&args=" + json.dumps(args, sort_keys=True).replace(" ", "")
            res = {"content": [{"type": "text", "text": "8 aircraft near you."},
                               {"type": "text", "text": json.dumps({"args": args})}],
                   "structuredContent": {"view": {"url": url}}}
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": res}), flush=True)
''')


@pytest.fixture
def service(tmp_path):
    root = tmp_path / "gev"
    (root / "server" / "mcp").mkdir(parents=True)
    (root / "server" / "mcp" / "stdio.js").write_text("// placeholder")
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE_SERVER)

    def popen(cmd, **kw):  # run the fake MCP server instead of node
        return subprocess.Popen([sys.executable, str(script)], **{k: v for k, v in kw.items() if k != "creationflags"})
    svc = ge.GodsEyeService(root, popen=popen)
    svc.server_up = lambda: True
    yield svc
    svc.close()


def spec(service, name):
    return next(s for s in service.connect() if s["name"] == name)


def test_client_lists_and_calls_tools(service):
    names = [t["name"] for t in service.connect()]
    assert names[:2] == ["aircraft_in_area", "next_satellite_pass"]
    result = service.call("aircraft_in_area", {"limit": 3})
    assert ge.result_text(result).startswith("8 aircraft near you.")
    assert service.last_view_url.startswith("http://localhost:4173/#v=2")


def test_area_and_location_default_to_where_the_user_is(service):
    here = lambda: {"lat": 12.98, "lon": 77.36}
    air = ge.GodsEyeTool(service, spec(service, "aircraft_in_area"), here, radius_km=80)
    assert air.permission == PermissionLevel.SAFE
    assert air.run().data["view_url"].endswith('"area":{"lat":12.98,"lon":77.36,"radius_km":80}}')
    sat = ge.GodsEyeTool(service, spec(service, "next_satellite_pass"), here)
    assert "location" not in sat.parameters.get("required", [])   # the LLM may omit it
    assert '"location":{"lat":12.98,"lon":77.36}' in sat.run().data["view_url"]
    named = air.run(area={"place": "Mumbai"})
    assert '"place":"Mumbai"' in named.data["view_url"]          # a named place wins


def test_show_in_globe_opens_the_browser_and_is_moderate(service):
    show = ge.GodsEyeTool(service, spec(service, "show_in_gods_eye_view"), lambda: None)
    assert show.permission == PermissionLevel.MODERATE
    with patch.object(ge, "_open", return_value=(True, "your default browser")) as opened:
        result = show.run(area={"place": "Bengaluru"})
    assert result.message.startswith("Opened God's Eye View.")
    assert opened.call_args.args[0] == service.last_view_url


def test_show_me_opens_the_last_answer_and_open_the_globe_works_anytime(service):
    tool = ge.ShowGlobeTool(service)
    assert not tool.matches("show me")                 # nothing looked up yet
    assert tool.matches("open the globe") and tool.matches("open god's eye view")
    service.call("aircraft_in_area", {})
    assert tool.matches("show me") and tool.matches("show me on the globe")
    with patch.object(ge, "_open", return_value=(True, "Brave")) as opened:
        assert tool.run(**tool.extract_arguments("show me that in brave")).message == \
            "Opening God's Eye View on what we just looked up."
    assert opened.call_args.args == (service.last_view_url, "brave")


def test_app_down_is_a_clean_failure(tmp_path):
    root = tmp_path / "gev"
    (root / "server" / "mcp").mkdir(parents=True)
    (root / "server" / "mcp" / "stdio.js").write_text("")
    svc = ge.GodsEyeService(root, auto_start=False)
    svc.server_up = lambda: False
    tool = ge.GodsEyeTool(svc, {"name": "get_earthquakes", "inputSchema": {"type": "object", "properties": {}}},
                          lambda: None)
    result = tool.run()
    assert result.success is False and "isn't available" in result.message


def test_only_configured_tools_are_registered_and_globe_wins_over_open_site(service, tmp_path, monkeypatch):
    from veronica.safety.permissions import PermissionGate
    from veronica.tools.registry import ToolRegistry
    registry = ToolRegistry(PermissionGate())
    cfg = SimpleNamespace(gods_eye=SimpleNamespace(enabled=True, path=str(service.root), base_url=service.base_url,
                                                   auto_start=False, default_radius_km=50,
                                                   tools=["aircraft_in_area", "next_satellite_pass"]),
                          resolve_path=lambda p: Path(p), llm=None)
    monkeypatch.setattr(ge, "GodsEyeService", lambda *a, **k: service)
    profile = SimpleNamespace(detected_coords=lambda: (12.9, 77.3), home_city=lambda: "Bengaluru")
    ge.register_gods_eye(registry, cfg, profile)
    import time
    for _ in range(100):
        if registry.get("next_satellite_pass"):
            break
        time.sleep(0.05)
    names = {t.name for t in registry.all()}
    assert {"open_gods_eye_view", "aircraft_in_area", "next_satellite_pass"} <= names
    assert "find_military_installations" not in names       # not in the configured list
    assert registry.find_fast_match("open the globe").name == "open_gods_eye_view"


def test_disabled_or_missing_install_registers_nothing(tmp_path):
    from veronica.safety.permissions import PermissionGate
    from veronica.tools.registry import ToolRegistry
    registry = ToolRegistry(PermissionGate())
    off = SimpleNamespace(gods_eye=SimpleNamespace(enabled=False))
    assert ge.register_gods_eye(registry, off, None) is None
    missing = SimpleNamespace(gods_eye=SimpleNamespace(enabled=True, path=str(tmp_path / "nope"), base_url="x",
                                                       auto_start=False, default_radius_km=1, tools=[]),
                              resolve_path=lambda p: Path(p))
    assert ge.register_gods_eye(registry, missing, None) is None
    assert list(registry.all()) == []


def test_utc_times_become_local_before_the_model_sees_them(monkeypatch):
    import datetime as dt
    stamp = "ISS next rises at 2026-10-16T08:46:29.578Z in the NW."
    expected = dt.datetime(2026, 10, 16, 8, 46, tzinfo=dt.timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    assert ge.to_local_times(stamp) == f"ISS next rises at {expected} local time in the NW."
    assert ge.to_local_times("no times here") == "no times here"


def test_show_on_globe_never_fills_in_the_users_location(service):
    show = ge.GodsEyeTool(service, spec(service, "show_in_gods_eye_view"), lambda: {"lat": 12.98, "lon": 77.36})
    with patch.object(ge, "_open", return_value=(True, "x")):
        show.run(layers=["cctv"])
    assert "12.98" not in service.last_view_url
