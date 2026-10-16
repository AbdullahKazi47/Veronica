"""
Claude (Anthropic) provider via the official `anthropic` SDK's Messages API. Opt-in like
the other cloud provider: used only when `llm.provider: anthropic` is set in config.yaml
and ANTHROPIC_API_KEY is in config/.env.

Two things differ from the OpenAI-shaped providers:

* VERONICA stores the model's tool-calling turn as plain text, but Claude requires every
  tool_result to follow the exact assistant content (thinking + tool_use blocks) that
  requested it. The provider remembers that content per tool_use id and replays it
  verbatim; if it is unknown (e.g. after a restart) the result is passed as plain text
  instead of failing the request.
* Claude Opus 5.5 always thinks and takes no temperature; speed is tuned with
  `output_config.effort` (`llm.anthropic_effort`, default "low" for voice latency).
"""
from __future__ import annotations

import json
from typing import Any, Optional

from veronica.llm.base import ChatMessage, LLMProvider, LLMResponse, StreamChunk, ToolCall
from veronica.logging_setup import get_logger

logger = get_logger("llm.anthropic")

# Server-side refusal fallback: a policy decline is re-run on Anthropic's recommended
# fallback model inside the same call instead of coming back as a refusal.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_TOKENS = 16000
_MAX_REMEMBERED_TURNS = 32


def to_anthropic_tools(tools: Optional[list[dict]]) -> list[dict]:
    """OpenAI/Ollama function schemas -> Anthropic tool definitions."""
    out = []
    for t in tools or []:
        fn = t.get("function", t) if isinstance(t, dict) else {}
        if not fn.get("name"):
            continue
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
        })
    return out


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    supports_streaming = True

    def __init__(self, api_key: Optional[str], model: str = "claude-opus-5-5",
                 timeout_s: int = 60, effort: str = "low", client: Any = None) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.effort = effort
        self._client = client
        # tool_use id -> the full assistant content (thinking + text + tool_use blocks)
        self._tool_turns: dict[str, list[dict]] = {}

    # -- plumbing ---------------------------------------------------------------
    def is_available(self) -> bool:
        return bool(self.api_key) or self._client is not None

    def _get_client(self):
        if self._client is None:
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.api_key, timeout=float(self.timeout_s))
        return self._client

    def _missing_key(self) -> Optional[str]:
        if self.is_available():
            return None
        return ("No Anthropic API key is configured. Add ANTHROPIC_API_KEY=... to config/.env "
                "(create one at https://platform.claude.com/settings/keys), or switch llm.provider "
                "back in config/config.yaml.")

    def _remember(self, content: list[dict]) -> None:
        ids = [b["id"] for b in content if b.get("type") == "tool_use"]
        for i in ids:
            self._tool_turns[i] = content
        while len(self._tool_turns) > _MAX_REMEMBERED_TURNS:
            self._tool_turns.pop(next(iter(self._tool_turns)))

    def _build(self, messages: list[ChatMessage]) -> tuple[str, list[dict]]:
        """VERONICA messages -> (system, Anthropic messages)."""
        system_parts: list[str] = []
        out: list[dict] = []

        def add(role: str, blocks: list[dict]) -> None:
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": list(blocks)})

        i = 0
        while i < len(messages):
            m = messages[i]
            if m.role == "system":
                if m.content:
                    system_parts.append(m.content)
                i += 1
                continue
            if m.role == "assistant":
                # Collect the tool results that answer this turn.
                j, results = i + 1, []
                while j < len(messages) and messages[j].role == "tool":
                    results.append(messages[j])
                    j += 1
                replay = next((self._tool_turns[r.tool_call_id] for r in results
                               if r.tool_call_id in self._tool_turns), None)
                if replay is not None:
                    known = {b["id"] for b in replay if b.get("type") == "tool_use"}
                    add("assistant", replay)
                    blocks = [{"type": "tool_result", "tool_use_id": r.tool_call_id,
                               "content": r.content or "(no output)"} for r in results
                              if r.tool_call_id in known]
                    # Every tool_use needs a result; mark any the orchestrator skipped.
                    answered = {b["tool_use_id"] for b in blocks}
                    blocks += [{"type": "tool_result", "tool_use_id": k, "is_error": True,
                                "content": "(not executed)"} for k in known - answered]
                    add("user", blocks)
                    extra = [r for r in results if r.tool_call_id not in known]
                else:
                    if m.content:
                        add("assistant", [{"type": "text", "text": m.content}])
                    extra = results
                for r in extra:  # unknown tool turn: degrade to plain text, never fail
                    add("user", [{"type": "text", "text": f"[result of tool {r.name or 'unknown'}]\n{r.content}"}])
                i = j
                continue
            if m.role == "tool":
                add("user", [{"type": "text", "text": f"[result of tool {m.name or 'unknown'}]\n{m.content}"}])
            else:
                add("user", [{"type": "text", "text": m.content or "(empty)"}])
            i += 1

        if out and out[0]["role"] != "user":  # the API requires a user turn first
            out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(conversation start)"}]})
        # Strip secrets / sensitive IDs before anything leaves the machine.
        from veronica.safety import privacy
        guard = privacy.get_guard()
        for msg in out:
            for block in msg.get("content") or []:
                if isinstance(block, dict):
                    if isinstance(block.get("text"), str):
                        block["text"] = guard.scrub_text(block["text"])
                    if isinstance(block.get("content"), str):
                        block["content"] = guard.scrub_text(block["content"])
        return guard.scrub_text("\n\n".join(system_parts)), out

    def _params(self, messages: list[ChatMessage], tools: Optional[list[dict]]) -> dict:
        system, msgs = self._build(messages)
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "messages": msgs,
            "output_config": {"effort": self.effort},
            "betas": [FALLBACK_BETA],
            "fallbacks": "default",
        }
        if system:
            params["system"] = system
        converted = to_anthropic_tools(tools)
        if converted:
            params["tools"] = converted
        return params

    def _to_response(self, message) -> LLMResponse:
        if getattr(message, "stop_reason", None) == "refusal":
            details = getattr(message, "stop_details", None)
            why = getattr(details, "explanation", None) if details else None
            return LLMResponse(content="I can't help with that request." + (f" ({why})" if why else ""),
                               raw=message, ok=False)
        content = [b.model_dump(exclude_none=True) for b in message.content]
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
        calls = []
        for b in content:
            if b.get("type") == "tool_use":
                args = b.get("input") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                calls.append(ToolCall(id=b["id"], name=b.get("name", ""), arguments=args))
        if calls:
            self._remember(content)
        return LLMResponse(content=text, tool_calls=calls, raw=message)

    # -- LLMProvider ------------------------------------------------------------
    def chat(self, messages: list[ChatMessage], tools: Optional[list[dict]] = None,
             temperature: float = 0.4, voice_mode: bool = False) -> LLMResponse:
        # temperature: not supported by Claude Opus 5.5 (effort is the control); ignored.
        missing = self._missing_key()
        if missing:
            return LLMResponse(content=missing, ok=False)
        try:
            message = self._get_client().beta.messages.create(**self._params(messages, tools))
        except Exception as e:  # noqa: BLE001 — degrade like the other providers, never raise
            logger.warning("Claude request failed: %s", e)
            return LLMResponse(content=f"The Claude request failed: {e}", ok=False)
        return self._to_response(message)

    def chat_stream(self, messages: list[ChatMessage], tools: Optional[list[dict]] = None,
                    temperature: float = 0.4, voice_mode: bool = False):
        missing = self._missing_key()
        if missing:
            yield StreamChunk(text=missing, finish_reason="error", done=True)
            return
        seq = 0
        try:
            with self._get_client().beta.messages.stream(**self._params(messages, tools)) as stream:
                for text in stream.text_stream:
                    if text:
                        yield StreamChunk(text=text, sequence=seq)
                        seq += 1
                final = stream.get_final_message()
        except Exception as e:  # noqa: BLE001
            logger.warning("Claude stream failed: %s", e)
            yield StreamChunk(text=f"The Claude stream failed: {e}", sequence=seq,
                              finish_reason="error", done=True)
            return
        resp = self._to_response(final)
        if not resp.ok:
            yield StreamChunk(text=resp.content, sequence=seq, finish_reason="error", done=True)
            return
        metadata: dict = {}
        finish = "stop"
        if resp.tool_calls:
            finish = "tool_calls"
            metadata["tool_calls"] = [{"id": c.id, "name": c.name, "arguments": c.arguments}
                                      for c in resp.tool_calls]
        yield StreamChunk(text="", sequence=seq, finish_reason=finish, done=True, metadata=metadata)
