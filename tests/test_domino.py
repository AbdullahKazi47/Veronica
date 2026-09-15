from veronica.automation.domino import (
    DominoChain,
    DominoRegistry,
    DominoStep,
    build_default_domino_registry,
)
from veronica.tools.base import Tool, ToolResult
from veronica.tools.registry import ToolRegistry


class _FakeGate:
    def check(self, tool_name: str, level, description: str):
        return _Decision(True)


class _Decision:
    def __init__(self, allowed: bool, reason: str = ""):
        self.allowed = allowed
        self.reason = reason


class _EchoTool(Tool):
    description = "Echo back the input"

    def __init__(self, name: str):
        self.name = name
        self.calls: list[dict] = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        text = kwargs.get("msg", "")
        extra = {k: v for k, v in kwargs.items() if k != "msg"}
        return ToolResult.ok(f"echo:{text}", **extra)


def _registry_with(*tools):
    reg = ToolRegistry(_FakeGate())
    for t in tools:
        reg.register(t)
    return reg


def test_placeholder_feed_forward():
    a = _EchoTool("echo_a")
    b = _EchoTool("echo_b")
    reg = _registry_with(a, b)

    chain = DominoChain(
        name="chain", description="", steps=[
            DominoStep("echo_a", name="first", kwargs={"msg": "start"}),
            DominoStep("echo_b", name="second", kwargs={"msg": "{previous}"}),
        ],
    )
    result = chain.execute(reg, text="")
    assert result.ok
    # second step received the first step's output message
    assert b.calls[0]["msg"] == "echo:start"
    assert result.results[1].message == "echo:echo:start"


def test_text_and_previous_placeholders():
    a = _EchoTool("echo_a")
    b = _EchoTool("echo_b")
    reg = _registry_with(a, b)

    chain = DominoChain(
        name="chain", description="", steps=[
            DominoStep("echo_a", name="first", kwargs={"msg": "{text}"}),
            DominoStep("echo_b", name="second", kwargs={"msg": "{previous} and {text}"}),
        ],
    )
    result = chain.execute(reg, text="hello")
    assert result.ok
    assert a.calls[0]["msg"] == "hello"
    assert b.calls[0]["msg"] == "echo:hello and hello"


def test_outputs_placeholder_references_earlier_step():
    a = _EchoTool("echo_a")
    b = _EchoTool("echo_b")
    reg = _registry_with(a, b)

    chain = DominoChain(
        name="chain", description="", steps=[
            DominoStep("echo_a", name="greeting", kwargs={"msg": "hi"}),
            DominoStep("echo_b", name="second", kwargs={"msg": "saw: {outputs.greeting}"}),
        ],
    )
    result = chain.execute(reg)
    assert result.ok
    assert b.calls[0]["msg"] == "saw: echo:hi"


def test_resolver_callable_wins_over_kwargs():
    t = _EchoTool("echo")
    reg = _registry_with(t)

    chain = DominoChain(
        name="chain", description="", steps=[
            DominoStep("echo", name="s", kwargs={"msg": "ignored"}, resolve=lambda ctx: {"msg": ctx["text"].upper()}),
        ],
    )
    result = chain.execute(reg, text="shout")
    assert result.ok
    assert t.calls[0]["msg"] == "SHOUT"


def test_stops_on_failure_by_default():
    class _FailingTool(Tool):
        name = "failing"
        description = ""
        def run(self, **kwargs):
            return ToolResult.fail("boom")

    t = _EchoTool("echo")
    reg = _registry_with(t, _FailingTool())

    chain = DominoChain(
        name="chain", description="", steps=[
            DominoStep("echo", name="one", kwargs={"msg": "x"}),
            DominoStep("failing", name="two"),
            DominoStep("echo", name="three", kwargs={"msg": "never runs"}),
        ],
    )
    result = chain.execute(reg)
    assert not result.ok
    assert len(result.results) == 2  # failing step appended, third never ran
    assert result.failed_step.tool == "failing"
    assert result.failed_step.success is False
    assert t.calls == [{"msg": "x"}]  # step three never dispatched


def test_continues_on_failure_when_configured():
    class _FailingTool(Tool):
        name = "failing"
        description = ""
        def run(self, **kwargs):
            return ToolResult.fail("boom")

    t = _EchoTool("echo")
    reg = _registry_with(t, _FailingTool())

    chain = DominoChain(
        name="chain", description="", steps=[
            DominoStep("failing", name="one"),
            DominoStep("echo", name="two", kwargs={"msg": "still runs"}),
        ],
        stops_on_failure=False,
    )
    result = chain.execute(reg)
    assert not result.ok  # a step failed
    assert result.results[1].message == "echo:still runs"  # but it kept going


def test_registry_unknown_chain():
    reg = _registry_with(_EchoTool("echo"))
    dom = DominoRegistry(reg)
    result = dom.execute("nope")
    assert not result.ok
    assert "nope" in result.summary


def test_chain_result_never_raises_on_bad_step_config():
    reg = _registry_with(_EchoTool("echo"))
    chain = DominoChain(
        name="chain", description="", steps=[
            DominoStep("echo", name="s", kwargs={"msg": "{outputs.missing}"}),
        ],
    )
    result = chain.execute(reg)
    # key error inside placeholder resolution is caught, reported as a failed step
    assert not result.ok
    assert "missing" in result.failed_step.message or "error" in result.failed_step.message.lower()


def test_builtin_chains_exist_and_dispatched_through_gate():
    # Build with stub tools under the built-in names so no network/browser fires.
    class _T(Tool):
        description = ""
        def run(self, **kwargs):
            return ToolResult.ok(f"{self.name} ran with {sorted(kwargs)}")

    tools = []
    for name in ("system_status", "get_weather", "web_search", "fetch_webpage", "take_note"):
        t = _T()
        t.name = name
        tools.append(t)
    reg = _registry_with(*tools)

    dom = build_default_domino_registry(reg)
    assert "daily_briefing" in dom.names()
    assert "quick_research" in dom.names()

    result = dom.execute("daily_briefing", text="Mumbai")
    assert result.ok
    assert len(result.results) == 3
    assert result.results[1].name == "weather"


def test_unknown_chain_message_lists_known(reg_factory=None):
    reg = _registry_with(_EchoTool("echo"))
    dom = DominoRegistry(reg)
    result = dom.execute("missing")
    assert not result.ok
    assert "Known:" in result.summary