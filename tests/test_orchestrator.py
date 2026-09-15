"""Orchestrator integration tests: compound-command splitting and loop-guard
termination in the LLM tool-calling path. These verify the new orchestration
layers end-to-end with dependency-free fakes, consistent with the rest of the
suite (see tests/test_tool_registry.py)."""
from veronica.config import Config
from veronica.core.orchestrator import Orchestrator
from veronica.llm.base import ChatMessage, LLMProvider, LLMResponse, ToolCall
from veronica.memory.long_term import LongTermMemory
from veronica.safety.permissions import PermissionGate, PermissionLevel
from veronica.tools.base import Tool, ToolResult
from veronica.tools.registry import ToolRegistry


class OpenTool(Tool):
    name = "open_app"
    description = "Open an application."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]}

    def matches(self, text: str) -> bool:
        return text.lower().startswith("open ")

    def extract_arguments(self, text: str) -> dict:
        return {"app": text[5:].strip()}

    def run(self, app: str = "", **kwargs) -> ToolResult:
        return ToolResult.ok(f"opened {app}", app=app)


class CloseTool(Tool):
    name = "close_app"
    description = "Close an application."
    permission = PermissionLevel.SAFE
    parameters = {"type": "object", "properties": {"app": {"type": "string"}}, "required": ["app"]}

    def matches(self, text: str) -> bool:
        return text.lower().startswith("close ")

    def extract_arguments(self, text: str) -> dict:
        return {"app": text[6:].strip()}

    def run(self, app: str = "", **kwargs) -> ToolResult:
        return ToolResult.ok(f"closed {app}", app=app)


class NoopLLM(LLMProvider):
    """Never calls tools — the LLM path with immediate final answer."""

    name = "noop"

    def chat(self, messages, tools=None, temperature=0.4):
        last = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return LLMResponse(content=f"(noop) you said: {last}")


class StuckLoopLLM(LLMProvider):
    """Issues the identical tool call forever — exercises the loop guard."""

    name = "stuck"

    def __init__(self):
        self.attempts = 0

    def chat(self, messages, tools=None, temperature=0.4):
        self.attempts += 1
        return LLMResponse(
            content="",
            tool_calls=[ToolCall(id=f"call-{self.attempts}", name="open_app", arguments={"app": "chrome"})],
        )


def build_orchestrator(llm: LLMProvider) -> Orchestrator:
    config = Config()
    # All tools in this test are SAFE — use CRITICAL threshold so none of them
    # require confirmation, and rely on the noop-llm / stuck-loop paths to do the
    # actual work.
    config.safety.require_confirmation_above = "CRITICAL"
    gate = PermissionGate(threshold=PermissionLevel[config.safety.require_confirmation_above])
    registry = ToolRegistry(gate)
    registry.register(OpenTool())
    registry.register(CloseTool())
    memory = LongTermMemory(config.resolve_path("data/tests-memory.json"))
    return Orchestrator(config=config, llm=llm, tools=registry, long_term_memory=memory)


def test_compound_command_runs_each_clause_sequentially():
    orch = build_orchestrator(NoopLLM())
    session = orch.new_session()
    resp = orch.handle("close spotify and open chrome", session)

    # `tool_used` is the first tool the response set; both clauses ran.
    assert resp.tool_used == "close_app"
    assert "closed spotify" in resp.text
    assert "opened chrome" in resp.text
    assert len(resp.tool_results) == 2
    assert [r["data"]["app"] for r in resp.tool_results] == ["spotify", "chrome"]


def test_conjunctive_object_is_not_split():
    orch = build_orchestrator(NoopLLM())
    session = orch.new_session()
    # "cats and dogs" is one web-search target — no tool here matches, so the fast
    # path misses and the (noop) LLM path answers without any tool dispatch.
    resp = orch.handle("search for cats and dogs", session)
    assert resp.tool_used is None
    assert "(noop)" in resp.text


def test_loop_guard_terminates_identical_calls():
    llm = StuckLoopLLM()
    orch = build_orchestrator(llm)
    session = orch.new_session()
    resp = orch.handle("open chrome", session)

    # The guard must stop the runaway before the fake LLM is spammed forever.
    assert llm.attempts <= 9
    # The final reply may be the exhausted-hops fallback (no clean answer possible
    # when every answer is a tool call).
    assert resp.text
    assert resp.tool_used == "open_app"