"""Self-heal: resilient JSON (backup/restore), the health watchdog, and the voice tool.
Non-destructive by design: it restarts and restores, never edits code or settings."""
from __future__ import annotations

import json
import threading
import time

import pytest

from veronica.core import jsonsafe
from veronica.core.self_heal import HealthMonitor
from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.tools.registry import build_default_registry
from veronica.tools.self_heal_tools import SelfHealTool


# ---------------------------------------------------------------- resilient JSON

def test_write_keeps_backup_and_read_restores_from_it(tmp_path):
    p = tmp_path / "memory.json"
    jsonsafe.write_json(p, {"a": 1})
    jsonsafe.write_json(p, {"a": 2})                    # first good copy -> .bak
    assert json.loads((tmp_path / "memory.json.bak").read_text()) == {"a": 1}

    p.write_text("{ this is broken", encoding="utf-8")  # corruption (power cut mid-edit)
    data, note = jsonsafe.read_json(p, default={})
    assert data == {"a": 1} and note and "restored" in note.lower()
    assert (tmp_path / "memory.json.corrupt").exists()  # broken file kept, not deleted
    assert json.loads(p.read_text()) == {"a": 1}        # main file healed in place


def test_read_without_backup_starts_fresh_and_quarantines(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("not json", encoding="utf-8")
    data, note = jsonsafe.read_json(p, default={"fresh": True})
    assert data == {"fresh": True} and "fresh" in note.lower()
    assert (tmp_path / "x.json.corrupt").exists()


def test_check_file_repairs(tmp_path):
    p = tmp_path / "k.json"
    jsonsafe.write_json(p, {"ok": 1})
    jsonsafe.write_json(p, {"ok": 2})
    p.write_text("broken", encoding="utf-8")
    healthy, note = jsonsafe.check_file(p, repair=True)
    assert healthy and note
    assert jsonsafe.check_file(tmp_path / "never-written.json")[0]  # missing = fine


def test_long_term_memory_survives_corruption(tmp_path):
    from veronica.memory.long_term import LongTermMemory
    path = tmp_path / "memory.json"
    m = LongTermMemory(path)
    m.remember("city", "Bengaluru")
    m.remember("pet", "cat")                             # second save makes a .bak
    path.write_text("corrupt!!", encoding="utf-8")
    m2 = LongTermMemory(path)                            # reload after corruption
    assert m2.recall("city") is not None


# ---------------------------------------------------------------- watchdog

def test_dead_worker_is_restarted():
    started = {"n": 0}

    def restart():
        started["n"] += 1
        threading.Thread(target=lambda: time.sleep(5), name="demo-worker-x", daemon=True).start()

    mon = HealthMonitor()
    mon.watch_threads("demo-worker-", 1, restart, "demo worker")
    results = mon.check(repair=True)
    r = next(x for x in results if x.name == "demo worker")
    assert started["n"] == 1 and r.ok and r.repaired

    results2 = mon.check(repair=True)                    # already alive now: no new restart
    assert started["n"] == 1 and next(x for x in results2 if x.name == "demo worker").ok


def test_watchdog_reports_when_restart_fails():
    mon = HealthMonitor()
    mon.watch_threads("never-", 1, lambda: None, "ghost")   # restart does nothing
    r = next(x for x in mon.check(repair=True) if x.name == "ghost")
    assert not r.ok and not r.repaired


def test_check_only_mode_does_not_restart():
    calls = []
    mon = HealthMonitor()
    mon.watch_threads("absent-", 1, lambda: calls.append(1), "x")
    r = next(x for x in mon.check(repair=False) if x.name == "x")
    assert not r.ok and calls == []


def test_data_file_check_repairs_during_round(tmp_path):
    p = tmp_path / "state.json"
    jsonsafe.write_json(p, {"v": 1})
    jsonsafe.write_json(p, {"v": 2})
    p.write_text("xx", encoding="utf-8")
    mon = HealthMonitor(data_files=[p])
    r = next(x for x in mon.check(repair=True) if x.name == "state.json")
    assert r.ok and r.repaired


def test_disk_and_model_reporting():
    class DownLLM:
        def is_available(self): return False
    mon = HealthMonitor(llm=DownLLM(), disk_free_min_gb=0)
    results = {r.name: r for r in mon.check()}
    assert not results["model"].ok and "reachable" in results["model"].detail
    assert results["disk"].ok                            # 0 GB minimum -> never flags


def test_announce_only_fixed_or_broken(tmp_path):
    said = []
    p = tmp_path / "m.json"
    jsonsafe.write_json(p, {"v": 1}); jsonsafe.write_json(p, {"v": 2})
    p.write_text("broken", encoding="utf-8")
    mon = HealthMonitor(speak=lambda t, u: said.append((t, u)), data_files=[p])
    mon.check(repair=True)
    assert any("restored" in t.lower() or "repair" in t.lower() for t, _ in said)


# ---------------------------------------------------------------- tool

def test_tool_routing_and_permission():
    reg = build_default_registry(PermissionGate())
    for text in ("heal yourself", "run a self check", "are you okay", "run a health check", "fix yourself"):
        assert reg.find_fast_match(text).name == "self_heal", text
    assert SelfHealTool.permission < PermissionLevel.SENSITIVE   # no prompt for a safe check


def test_tool_uses_live_monitor_and_reports(tmp_path):
    from veronica.tools.self_heal_tools import set_live_monitor
    p = tmp_path / "mem.json"
    jsonsafe.write_json(p, {"v": 1}); jsonsafe.write_json(p, {"v": 2})
    p.write_text("broken", encoding="utf-8")
    mon = HealthMonitor(data_files=[p])
    set_live_monitor(lambda: mon)
    try:
        res = SelfHealTool().run()
        assert res.success and ("fixed" in res.message.lower() or "restored" in res.message.lower())
    finally:
        set_live_monitor(None)


def test_tool_all_healthy_message(tmp_path):
    from veronica.tools.self_heal_tools import set_live_monitor
    mon = HealthMonitor(disk_free_min_gb=0)
    set_live_monitor(lambda: mon)
    try:
        res = SelfHealTool().run()
        assert res.success and res.data["healthy"] and "healthy" in res.message.lower()
    finally:
        set_live_monitor(None)
