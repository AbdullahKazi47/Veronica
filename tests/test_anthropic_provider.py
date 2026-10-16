"""Claude provider (llm.provider: anthropic) — fake SDK client, no network, no key."""
from __future__ import annotations

from types import SimpleNamespace

from veronica.config import Config
from veronica.llm.anthropic_provider import FALLBACK_BETA, AnthropicProvider, to_anthropic_tools
from veronica.llm.base import ChatMessage
from veronica.llm.router import build_provider

TOOLS = [{"type": "function", "function": {
    "name": "get_time", "description": "Current time",
    "parameters": {"type": "object", "properties": {"tz": {"type": "string"}}}}}]


class Block:
    def __init__(self, **fields):
        self.fields = fields

    def model_dump(self, exclude_none=False):
        return dict(self.fields)


def message(*blocks, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(content=list(blocks), stop_reason=stop_reason, stop_details=stop_details)


class FakeStream:
    def __init__(self, final, deltas):
        self.final, self.text_stream = final, deltas

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.final


class FakeClient:
    def __init__(self, *replies, stream=None, error=None):
        self.replies, self.calls, self._stream, self.error = list(replies), [], stream, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create, stream=self._open))

    def _create(self, **params):
        self.calls.append(params)
        if self.error:
            raise self.error
        return self.replies.pop(0)

    def _open(self, **params):
        self.calls.append(params)
        return self._stream


def provider(client):
    return AnthropicProvider(api_key="test-key", model="claude-opus-5-5", effort="low", client=client)


def test_plain_reply_request_shape():
    c = FakeClient(message(Block(type="text", text="Hello there.")))
    r = provider(c).chat([ChatMessage("system", "You are VERONICA."), ChatMessage("user", "hi")],
                         temperature=0.9)
    assert r.ok and r.content == "Hello there." and r.tool_calls == []
    p = c.calls[0]
    assert p["model"] == "claude-opus-5-5" and p["system"] == "You are VERONICA."
    assert p["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]
    assert p["output_config"] == {"effort": "low"}
    assert p["fallbacks"] == "default" and p["betas"] == [FALLBACK_BETA]
    assert "temperature" not in p and "tools" not in p  # Opus 5.5 rejects temperature


def test_tool_schemas_are_converted():
    assert to_anthropic_tools(TOOLS) == [{"name": "get_time", "description": "Current time",
                                          "input_schema": TOOLS[0]["function"]["parameters"]}]


def test_tool_round_trip_replays_exact_assistant_content():
    thinking = Block(type="thinking", thinking="", signature="sig")
    use = Block(type="tool_use", id="tu_1", name="get_time", input={"tz": "IST"})
    c = FakeClient(message(thinking, Block(type="text", text="Checking."), use, stop_reason="tool_use"),
                   message(Block(type="text", text="It is 3 pm.")))
    p = provider(c)
    history = [ChatMessage("system", "persona"), ChatMessage("user", "what time is it")]
    first = p.chat(history, tools=TOOLS)
    assert [(t.id, t.name, t.arguments) for t in first.tool_calls] == [("tu_1", "get_time", {"tz": "IST"})]

    # The orchestrator appends the turn as plain text plus a role="tool" result.
    history += [ChatMessage("assistant", first.content),
                ChatMessage("tool", "15:00", tool_call_id="tu_1", name="get_time")]
    second = p.chat(history, tools=TOOLS)
    assert second.content == "It is 3 pm."
    msgs = c.calls[1]["messages"]
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == [thinking.fields, {"type": "text", "text": "Checking."}, use.fields]
    assert msgs[2] == {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "tu_1", "content": "15:00"}]}


def test_unknown_tool_turn_degrades_to_text_instead_of_failing():
    c = FakeClient(message(Block(type="text", text="ok")))
    provider(c).chat([ChatMessage("user", "q"), ChatMessage("assistant", "calling"),
                      ChatMessage("tool", "42", tool_call_id="never_seen", name="calc")])
    msgs = c.calls[0]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[2]["content"][0]["text"].startswith("[result of tool calc]")
    assert not any(b.get("type") == "tool_result" for m in msgs for b in m["content"])


def test_conversation_must_start_with_user_and_roles_merge():
    c = FakeClient(message(Block(type="text", text="ok")))
    provider(c).chat([ChatMessage("assistant", "Welcome back."), ChatMessage("user", "a"),
                      ChatMessage("user", "b")])
    msgs = c.calls[0]["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert [b["text"] for b in msgs[2]["content"]] == ["a", "b"]


def test_refusal_is_not_reported_as_success():
    details = SimpleNamespace(explanation="policy", category="cyber")
    c = FakeClient(message(stop_reason="refusal", stop_details=details))
    r = provider(c).chat([ChatMessage("user", "x")])
    assert r.ok is False and "can't help" in r.content


def test_missing_key_and_api_errors_degrade_without_raising():
    r = AnthropicProvider(api_key=None).chat([ChatMessage("user", "hi")])
    assert r.ok is False and "ANTHROPIC_API_KEY" in r.content
    r = provider(FakeClient(error=RuntimeError("boom"))).chat([ChatMessage("user", "hi")])
    assert r.ok is False and "boom" in r.content


def test_stream_yields_deltas_then_terminal_tool_calls():
    final = message(Block(type="text", text="Let me check."),
                    Block(type="tool_use", id="tu_9", name="get_time", input={}), stop_reason="tool_use")
    chunks = list(provider(FakeClient(stream=FakeStream(final, ["Let me ", "check."])))
                  .chat_stream([ChatMessage("user", "time?")], tools=TOOLS))
    assert "".join(c.text for c in chunks) == "Let me check."
    assert chunks[-1].done and chunks[-1].finish_reason == "tool_calls"
    assert chunks[-1].metadata["tool_calls"] == [{"id": "tu_9", "name": "get_time", "arguments": {}}]


def test_router_builds_anthropic_provider_from_config(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    cfg = Config()
    cfg.llm.provider, cfg.llm.model = "anthropic", "claude-opus-5-5"
    cfg.llm.api_key_env, cfg.llm.anthropic_effort = "ANTHROPIC_API_KEY", "medium"
    p = build_provider(cfg)
    assert isinstance(p, AnthropicProvider) and p.api_key == "k" and p.effort == "medium"
