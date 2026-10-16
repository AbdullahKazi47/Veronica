"""OpenAI-compatible provider must replay tool-call turns (Gemini 400'd without them)."""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

from veronica.llm.base import ChatMessage
from veronica.llm.openai_compatible_provider import OpenAICompatibleProvider


class Resp:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.text = body, status, str(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"{self.status_code} error")

    def json(self):
        return self.body


def reply(message):
    return Resp({"choices": [{"message": message}]})


TOOL_TURN = {
    "role": "assistant", "content": None,
    "tool_calls": [{"id": "c1", "type": "function",
                    "function": {"name": "web_search", "arguments": '{"q": "ocean"}'},
                    "extra_content": {"google": {"thought_signature": "SIG"}}}],
}


def run(provider, history, *replies):
    post = MagicMock(side_effect=list(replies))
    with patch.dict(sys.modules, {"requests": types.SimpleNamespace(post=post)}):
        result = provider.chat(history)
    return result, [c.kwargs["json"]["messages"] for c in post.call_args_list]


def test_tool_turn_is_replayed_with_ids_and_signature():
    p = OpenAICompatibleProvider(base_url="https://x/v1", api_key="k", model="m")
    history = [ChatMessage("system", "persona"), ChatMessage("user", "ocean fact")]
    first, _ = run(p, history, reply(TOOL_TURN))
    assert [(c.id, c.name, c.arguments) for c in first.tool_calls] == [("c1", "web_search", {"q": "ocean"})]

    history += [ChatMessage("assistant", ""),  # how the orchestrator records the turn
                ChatMessage("tool", "Oceans cover 71%.", tool_call_id="c1", name="web_search")]
    second, sent = run(p, history, reply({"role": "assistant", "content": "Oceans cover 71% of Earth."}))
    assert second.content == "Oceans cover 71% of Earth."
    wire = sent[0]
    assert wire[2] == TOOL_TURN  # exact replay, thought signature included
    assert wire[3] == {"role": "tool", "tool_call_id": "c1", "content": "Oceans cover 71%."}


def test_unknown_tool_turn_degrades_to_text():
    p = OpenAICompatibleProvider(base_url="https://x/v1", api_key="k", model="m")
    _, sent = run(p, [ChatMessage("user", "q"), ChatMessage("assistant", ""),
                      ChatMessage("tool", "42", tool_call_id="ghost", name="calc")],
                  reply({"role": "assistant", "content": "ok"}))
    wire = sent[0]
    assert [m["role"] for m in wire] == ["user", "user"]  # empty assistant dropped, result as text
    assert wire[1]["content"].startswith("[result of tool calc]")
    assert all("tool_call_id" not in m for m in wire)


def test_missing_tool_call_id_gets_one_so_replay_matches():
    p = OpenAICompatibleProvider(base_url="https://x/v1", api_key="k", model="m")
    turn = {"role": "assistant", "content": "",
            "tool_calls": [{"type": "function", "function": {"name": "t", "arguments": "{}"}}]}
    first, _ = run(p, [ChatMessage("user", "q")], reply(turn))
    assert first.tool_calls[0].id == "call_0"
    _, sent = run(p, [ChatMessage("user", "q"), ChatMessage("assistant", ""),
                      ChatMessage("tool", "done", tool_call_id="call_0", name="t")],
                  reply({"role": "assistant", "content": "ok"}))
    assert sent[0][1]["tool_calls"][0]["id"] == "call_0"
    assert sent[0][2]["tool_call_id"] == "call_0"


def test_plain_conversation_unchanged():
    p = OpenAICompatibleProvider(base_url="https://x/v1", api_key="k", model="m")
    history = [ChatMessage("system", "s"), ChatMessage("user", "hi"),
               ChatMessage("assistant", "hello"), ChatMessage("user", "bye")]
    _, sent = run(p, history, reply({"role": "assistant", "content": "ok"}))
    assert sent[0] == [{"role": m.role, "content": m.content} for m in history]
