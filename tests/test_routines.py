"""Routines: one phrase, several commands — and every step still meets the gate."""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

import pytest

from veronica.config import Config
from veronica.core.routines import RoutineStore, parse_steps
from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.tools.base import Tool, ToolResult
from veronica.tools.routine_tools import CreateRoutineTool, EditRoutineTool, RoutineInfoTool, RunRoutineTool


@pytest.fixture
def store(tmp_path):
    return RoutineStore(tmp_path / "routines.json")


def test_parse_steps():
    assert parse_steps("set an alarm for 7 am, then lock my pc and then mute the volume") == [
        "set an alarm for 7 am", "lock my pc", "mute the volume"]
    assert parse_steps("first open spotify; next play lofi. finally lock the pc") == [
        "open spotify", "play lofi", "lock the pc"]


def test_create_by_voice_and_list(store):
    create = CreateRoutineTool(store=store)
    text = "create a routine called good night: set an alarm for 7 am, then lock my PC"
    assert create.matches(text)
    res = create.run(**create.extract_arguments(text))
    assert res.success and res.data["steps"] == ["set an alarm for 7 am", "lock my PC"]
    info = RoutineInfoTool(store=store)
    assert info.matches("what routines do I have")
    assert "good night (2 steps)" in info.run().message
    assert info.matches("what does good night do?")
    assert "lock my PC" in info.run(**info.extract_arguments("what does good night do?")).message


@pytest.mark.parametrize("said", ["good night", "Good night.", "run good night", "start my good night routine",
                                  "ok now good night", "good night routine"])
def test_routine_name_triggers_it(store, said):
    store.save("good night", ["lock my pc"])
    assert RunRoutineTool(store=store).matches(said)


def test_unrelated_phrases_dont_trigger(store):
    store.save("work mode", ["open vs code"])
    run = RunRoutineTool(store=store)
    for said in ("what's the weather", "work", "open vs code", "run diagnostics"):
        assert not run.matches(said)


def test_runs_every_step_in_order_and_reports(store):
    store.save("leaving", ["mute the volume", "lock my pc"])
    said = []
    run = RunRoutineTool(store=store, runner=lambda step: said.append(step) or f"Did: {step}")
    res = run.run(name="leaving")
    assert said == ["mute the volume", "lock my pc"]
    assert res.success and "Did: mute the volume" in res.message and "Did: lock my pc" in res.message
    assert store.get("leaving").runs == 1


def test_a_failing_step_doesnt_stop_the_rest(store):
    store.save("x", ["boom", "fine"])
    done = []

    def runner(step):
        if step == "boom":
            raise RuntimeError("bad")
        done.append(step)
        return "ok"
    res = RunRoutineTool(store=store, runner=runner).run(name="x")
    assert done == ["fine"] and res.data["failed"] == 1


def test_no_routine_inside_a_routine(store):
    store.save("loop", ["loop"])
    run = RunRoutineTool(store=store)
    run.runner = lambda step: run.run(name=step).message
    assert "can't start another routine" in run.run(name="loop").message


def test_edit_routine(store):
    store.save("good night", ["lock my pc"])
    edit = EditRoutineTool(store=store)
    t = "add mute the volume to my good night routine"
    assert edit.matches(t) and edit.run(**edit.extract_arguments(t)).success
    assert store.get("good night").steps == ["lock my pc", "mute the volume"]
    t = "remove step one from good night"
    assert edit.run(**edit.extract_arguments(t)).success and store.get("good night").steps == ["mute the volume"]
    t = "delete the good night routine"
    assert edit.matches(t) and edit.run(**edit.extract_arguments(t)).success and store.get("good night") is None


# ---------------------------------------------------------------- safety: steps still meet the gate

class _Sleep(Tool):
    name = "sleep_pc"
    description = "sleep"
    permission = PermissionLevel.SENSITIVE
    parameters = {"type": "object", "properties": {}, "required": []}
    slept = []
    def matches(self, text): return bool(re.search(r"laptop to sleep", text))
    def run(self, **kw):
        _Sleep.slept.append(1)
        return ToolResult.ok("Going to sleep.")


class _Mute(Tool):
    name = "mute"
    description = "mute"
    permission = PermissionLevel.MODERATE
    parameters = {"type": "object", "properties": {}, "required": []}
    def matches(self, text): return "mute" in text
    def run(self, **kw): return ToolResult.ok("Muted.")


def test_routine_steps_still_ask_permission(store):
    from veronica.core.orchestrator import Orchestrator
    from veronica.llm.router import EchoProvider
    from veronica.memory.long_term import LongTermMemory
    from veronica.tools.registry import ToolRegistry
    asked = []
    gate = PermissionGate(threshold=PermissionLevel.SENSITIVE, confirm_callback=lambda d, l: asked.append(d) or False)
    reg = ToolRegistry(gate)
    run = RunRoutineTool(store=store)
    for t in (run, _Sleep(), _Mute()):
        reg.register(t)
    cfg = Config()
    cfg.routing.enabled = False
    orch = Orchestrator(config=cfg, llm=EchoProvider(), tools=reg,
                        long_term_memory=LongTermMemory(Path(tempfile.mkdtemp()) / "m.json"))
    session = orch.new_session()
    run.runner = lambda step: orch.handle(step, session).text
    store.save("good night", ["mute the volume", "put the laptop to sleep"])
    reply = orch.handle("good night", orch.new_session()).text
    assert asked and "sleep_pc" in asked[0]          # the routine did not skip the question
    assert _Sleep.slept == []                        # "no" was respected
    assert "Muted" in reply


def test_conversation_ending_phrase_runs_a_routine_named_like_it(store):
    from types import SimpleNamespace
    from veronica.ui.cli import CLIApp
    store.save("good night", ["lock my pc"])
    app = CLIApp.__new__(CLIApp)
    reg = {"run_routine": RunRoutineTool(store=store)}
    app.orchestrator = SimpleNamespace(tools=SimpleNamespace(get=reg.get))
    assert not app._ends_conversation("good night")   # it's a routine: run it
    assert app._ends_conversation("thanks")            # ordinary ending still ends


def test_greeting_is_remembered_across_restarts(tmp_path):
    """Restarting VERONICA several times in an hour used to greet (and spend voice credits) each time."""
    from types import SimpleNamespace
    from veronica.core.proactive import ProactiveMonitor
    cfg = SimpleNamespace(greet_on_open=True, greet_cooldown_minutes=60)
    said, now = [], [1000.0]
    kw = dict(collect=lambda **k: None, briefing=lambda: "Good morning.", clock=lambda: now[0],
              state_path=tmp_path / "proactive_state.json")
    ProactiveMonitor(cfg, lambda t, u: said.append(t), **kw).on_open("startup")
    now[0] += 600                                     # restarted 10 minutes later
    ProactiveMonitor(cfg, lambda t, u: said.append(t), **kw).on_open("startup")
    assert said == ["Good morning."]
    now[0] += 3600                                    # an hour later: greet again
    ProactiveMonitor(cfg, lambda t, u: said.append(t), **kw).on_open("startup")
    assert len(said) == 2
