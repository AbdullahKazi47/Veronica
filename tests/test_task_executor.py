"""Tests for the VERONICA brain/agent layer: the multi-step task executor,
planner, and the orchestrator's agentic (plan → execute → verify) path.

Strategy mirrors the rest of the suite: dependency-free fakes (fake gate, fake
tools, deterministic LLM), no network, no third-party test deps.
"""
from __future__ import annotations

import pytest

from veronica.automation import (
    MultiStepPlanner,
    MultiStepTask,
    TaskExecutor,
    TaskStep,
)
from veronica.automation.planner import TaskPlanStep
from veronica.config import Config
from veronica.core.orchestrator import Orchestrator
from veronica.llm.base import LLMProvider, LLMResponse
from veronica.memory.long_term import LongTermMemory
from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.tools.base import Tool, ToolResult
from veronica.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class _FakeGate:
    def check(self, tool_name, level, description=""):
        class _V:
            allowed = True
            reason = ""
        return _V()


class _ReadFileTool(Tool):
    name = "read_file"
    description = "Read a file."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"file_path": {"type": "string"}}, "required": ["file_path"]}

    def run(self, file_path="", **kwargs) -> ToolResult:
        return ToolResult.ok(f"read {file_path}", file_path=file_path)


class _WriteFileTool(Tool):
    name = "write_file"
    description = "Write a file."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}}, "required": ["file_path"]}

    def run(self, file_path="", content="", **kwargs) -> ToolResult:
        return ToolResult.ok(f"wrote {file_path}", file_path=file_path)


class _OpenWebTool(Tool):
    name = "open_website"
    description = "Open a website."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

    def run(self, url="", **kwargs) -> ToolResult:
        return ToolResult.ok(f"opened {url}", url=url)


class _FlakyTool(Tool):
    """Fails the first N times, then succeeds — exercises retry logic."""
    name = "flaky"
    description = "Fails then succeeds."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": []}

    def __init__(self, fail_times: int = 1):
        self.fail_times = fail_times
        self.calls = 0

    def run(self, text="", **kwargs) -> ToolResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            return ToolResult.fail("transient failure")
        return ToolResult.ok("recovered")


class _AlwaysFailTool(Tool):
    name = "always_fail"
    description = "Always fails."
    permission = PermissionLevel.SAFE

    def run(self, **kwargs) -> ToolResult:
        return ToolResult.fail("nope")


def _build_registry() -> ToolRegistry:
    reg = ToolRegistry(_FakeGate())
    reg.register(_ReadFileTool())
    reg.register(_WriteFileTool())
    reg.register(_OpenWebTool())
    return reg


# ---------------------------------------------------------------------------
# TaskStep / MultiStepTask
# ---------------------------------------------------------------------------

class TestTaskStep:
    def test_defaults(self):
        s = TaskStep("do a thing", "read_file", {"file_path": "/tmp/x"})
        assert s.status == "pending"
        assert s.verified is False
        assert s.attempts == 0
        d = s.to_dict()
        assert d["description"] == "do a thing"
        assert d["tool"] == "read_file"
        assert d["status"] == "pending"


class TestMultiStepTask:
    def test_progress(self):
        steps = [TaskStep("a"), TaskStep("b"), TaskStep("c"), TaskStep("d")]
        task = MultiStepTask("T1", "goal", steps)
        assert task.progress == 0
        steps[0].status = "completed"
        steps[1].status = "completed"
        assert task.progress == 50
        for s in steps:
            s.status = "completed"
        assert task.progress == 100

    def test_to_dict_shape(self):
        task = MultiStepTask("T1", "goal", [TaskStep("a", "read_file", {"file_path": "/x"})])
        d = task.to_dict()
        assert d["task_id"] == "T1"
        assert d["goal"] == "goal"
        assert d["status"] == "pending"
        assert d["total_steps"] == 1
        assert d["progress"] == 0


# ---------------------------------------------------------------------------
# TaskExecutor
# ---------------------------------------------------------------------------

class TestTaskExecutor:
    def test_create_and_run_success(self):
        ex = TaskExecutor(_build_registry())
        task = ex.create_task("research thing", [
            {"description": "read the notes", "tool": "read_file", "args": {"file_path": "/tmp/notes.md"}},
            {"description": "open the page", "tool": "open_website", "args": {"url": "https://example.com"}},
        ])
        assert task.status == "pending"
        assert len(task.steps) == 2

        progress_events = []
        outcome = ex.execute_task(task, progress_callback=lambda tid, c, t, d: progress_events.append((c, t)))

        assert outcome["success"] is True
        assert task.status == "completed"
        assert task.progress == 100
        assert all(s.status == "completed" for s in task.steps)
        assert len(progress_events) == 3  # before each step + final
        assert progress_events[-1] == (2, 2)
        # Recorded to history
        assert ex.get_history() and ex.get_history()[-1]["status"] == "completed"

    def test_verification_note_for_existing_file(self, tmp_path):
        f = tmp_path / "real.txt"
        f.write_text("hello")
        ex = TaskExecutor(_build_registry())
        task = ex.create_task("touch file", [
            {"description": "read it", "tool": "read_file", "args": {"file_path": str(f)}},
        ])
        ex.execute_task(task)
        step = task.steps[0]
        assert step.verified is True
        assert "exists" in step.verification_note

    def test_verification_note_for_missing_file_is_advisory(self, tmp_path):
        # Missing file → tool still reports success (fake), but verification flags it.
        ex = TaskExecutor(_build_registry())
        task = ex.create_task("read ghost", [
            {"description": "read", "tool": "read_file", "args": {"file_path": str(tmp_path / "ghost.txt")}},
        ])
        ex.execute_task(task)
        step = task.steps[0]
        assert step.verified is False
        assert "MISSING" in step.verification_note
        # Advisory: the step still completes because the tool reported success.
        assert step.status == "completed"

    def test_invalid_url_flagged(self):
        ex = TaskExecutor(_build_registry())
        task = ex.create_task("open bad url", [
            {"description": "open", "tool": "open_website", "args": {"url": "not-a-url"}},
        ])
        ex.execute_task(task)
        step = task.steps[0]
        assert step.verified is False
        assert "invalid" in step.verification_note

    def test_retry_recovers(self):
        reg = _build_registry()
        flaky = _FlakyTool(fail_times=1)
        reg.register(flaky)
        ex = TaskExecutor(reg)
        task = ex.create_task("recover", [
            {"description": "flaky step", "tool": "flaky", "args": {}},
        ])
        outcome = ex.execute_task(task)
        assert outcome["success"] is True
        assert flaky.calls == 2  # failed once, retried once
        assert task.status == "completed"

    def test_exhausts_retries_then_fails(self):
        reg = _build_registry()
        reg.register(_AlwaysFailTool())
        ex = TaskExecutor(reg)
        task = ex.create_task("doomed", [
            {"description": "always fails", "tool": "always_fail", "args": {}},
        ])
        outcome = ex.execute_task(task)
        assert outcome["success"] is False
        assert task.status == "failed"
        assert task.steps[0].status == "failed"
        assert task.steps[0].attempts == ex.MAX_RETRIES + 1  # initial + retries

    def test_step_failure_stops_following_steps(self):
        reg = _build_registry()
        reg.register(_AlwaysFailTool())
        ex = TaskExecutor(reg)
        task = ex.create_task("stop early", [
            {"description": "fails", "tool": "always_fail", "args": {}},
            {"description": "never reached", "tool": "read_file", "args": {"file_path": "/x"}},
        ])
        outcome = ex.execute_task(task)
        assert outcome["success"] is False
        assert task.steps[1].status == "pending"  # never ran

    def test_cancel_between_steps(self):
        ex = TaskExecutor(_build_registry())
        task = ex.create_task("cancel me", [
            {"description": "one", "tool": "read_file", "args": {"file_path": "/a"}},
            {"description": "two", "tool": "read_file", "args": {"file_path": "/b"}},
            {"description": "three", "tool": "read_file", "args": {"file_path": "/c"}},
        ])
        assert ex.cancel_task(task.task_id) is True
        outcome = ex.execute_task(task)
        assert task.status == "cancelled"
        assert outcome["success"] is False
        # No step ran because cancellation happened before execution started.
        assert all(s.status in ("pending", "cancelled") for s in task.steps)

    def test_cancel_unknown_task_returns_false(self):
        ex = TaskExecutor(_build_registry())
        assert ex.cancel_task("NOPE") is False

    def test_get_task_and_list(self):
        ex = TaskExecutor(_build_registry())
        task = ex.create_task("g", [{"description": "d", "tool": "read_file", "args": {"file_path": "/x"}}])
        detail = ex.get_task(task.task_id)
        assert detail is not None and detail["goal"] == "g"
        assert ex.get_task("missing") is None
        assert len(ex.list_tasks()) == 1

    def test_event_callback_fired(self):
        seen = []
        ex = TaskExecutor(_build_registry(), tool_event_callback=seen.append)
        task = ex.create_task("g", [{"description": "d", "tool": "read_file", "args": {"file_path": "/x"}}])
        ex.execute_task(task)
        assert any("read_file" in line for line in seen)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

class TestPlanner:
    def test_simple_request_is_not_complex(self):
        p = MultiStepPlanner()
        assert p.is_complex("open chrome") is False

    def test_compound_request_is_complex(self):
        p = MultiStepPlanner()
        assert p.is_complex("open chrome and then open notes") is True

    def test_create_plan_returns_single_step_without_llm(self):
        p = MultiStepPlanner()
        plan = p.create_plan("open chrome")
        assert len(plan) == 1
        assert isinstance(plan[0], TaskPlanStep)

    def test_fallback_open_pattern(self):
        # A garbage LLM forces the LLM path to fail, so the deterministic
        # fallback plan ("open X and open Y") is used instead.
        p = MultiStepPlanner()
        plan = p.create_plan("open chrome and open notes", tools_schema=[], llm=_PlanningLLM("not json"))
        tools = [s.tool_name for s in plan]
        assert tools.count("open_app") >= 1


class _PlanningLLM(LLMProvider):
    """Returns a hardcoded multi-step JSON plan."""
    name = "planner"

    def __init__(self, json_text: str):
        self.json_text = json_text

    def chat(self, messages, tools=None, temperature=0.4):
        return LLMResponse(content=self.json_text)


class TestPlannerWithLLM:
    def test_llm_plan_parses_steps(self):
        llm = _PlanningLLM(
            '[{"description": "Read the file", "tool": "read_file", "args": {"file_path": "/tmp/a"}},'
            ' {"description": "Open the site", "tool": "open_website", "args": {"url": "https://x.com"}}]'
        )
        p = MultiStepPlanner()
        # We're testing the LLM JSON parser, not the complexity heuristic — force it.
        p.is_complex = lambda q: True
        plan = p.create_plan("research task", tools_schema=[], llm=llm)
        assert len(plan) == 2
        assert plan[0].tool_name == "read_file"
        assert plan[0].args["file_path"] == "/tmp/a"
        assert plan[1].tool_name == "open_website"

    def test_llm_plan_falls_back_on_garbage(self):
        llm = _PlanningLLM("not json at all")
        p = MultiStepPlanner()
        p.is_complex = lambda q: True
        plan = p.create_plan("research task", tools_schema=[], llm=llm)
        assert len(plan) >= 1  # fallback always returns at least one step


# ---------------------------------------------------------------------------
# Orchestrator agentic path
# ---------------------------------------------------------------------------

class _SingleStepLLM(LLMProvider):
    """Returns a plan with a single step → orchestrator should fall through to LLM."""
    name = "single"

    def chat(self, messages, tools=None, temperature=0.4):
        return LLMResponse(content="final answer")


def _build_orchestrator(llm: LLMProvider, planner=None, executor=None) -> Orchestrator:
    config = Config()
    config.safety.require_confirmation_above = "CRITICAL"
    gate = PermissionGate(threshold=PermissionLevel[config.safety.require_confirmation_above])
    registry = ToolRegistry(gate)
    registry.register(_ReadFileTool())
    registry.register(_OpenWebTool())
    memory = LongTermMemory(config.resolve_path("data/tests-memory.json"))
    if executor is None:
        executor = TaskExecutor(registry)
    if planner is None:
        planner = MultiStepPlanner()
    return Orchestrator(
        config=config, llm=llm, tools=registry, long_term_memory=memory,
        planner=planner, task_executor=executor,
    )


class TestOrchestratorAgentic:
    def test_single_step_plan_falls_through_to_llm(self):
        orch = _build_orchestrator(_SingleStepLLM())
        session = orch.new_session()
        resp = orch.handle("open chrome", session)
        # "open chrome" isn't an agentic request, and even if planned it'd be one
        # step → normal LLM path.
        assert resp.task is None
        assert "final answer" in resp.text

    def test_multi_step_goal_executes_as_task(self):
        planner = MultiStepPlanner()
        # Force a multi-step plan regardless of the goal text.
        planner.is_complex = lambda q: True
        llm = _PlanningLLM(
            '[{"description": "Read a", "tool": "read_file", "args": {"file_path": "/a"}},'
            ' {"description": "Open site", "tool": "open_website", "args": {"url": "https://x.com"}}]'
        )
        orch = _build_orchestrator(llm, planner=planner)
        session = orch.new_session()
        resp = orch.handle("plan and run a research task", session)
        assert resp.task is not None
        assert resp.task["status"] == "completed"
        assert resp.task["total_steps"] == 2
        assert "Executed plan" in resp.text
