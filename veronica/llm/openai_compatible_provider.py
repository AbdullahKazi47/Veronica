"""
Optional cloud provider for anyone who *wants* to point VERONICA at a paid or free-tier
hosted model (OpenRouter, OpenAI, any OpenAI-compatible endpoint). Never used unless the
user explicitly sets `llm.provider: openai_compatible` in config.yaml AND supplies an API
key — VERONICA never assumes or requires this.
"""
from __future__ import annotations

import json
from typing import Optional

from veronica.llm.base import ChatMessage, LLMProvider, LLMResponse, StreamChunk, ToolCall
from veronica.logging_setup import get_logger

logger = get_logger("llm.openai_compatible")


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"
    supports_streaming = True

    def __init__(self, base_url: str, api_key: Optional[str], model: str, timeout_s: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s

    def is_available(self) -> bool:
        return bool(self.api_key)

    def chat(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.4,
    ) -> LLMResponse:
        if not self.api_key:
            return LLMResponse(
                content=(
                    "No API key is configured for the cloud provider. Set VERONICA_LLM_API_KEY "
                    "in config/.env, or switch llm.provider back to 'ollama' in config.yaml."
                ),
                ok=False,
            )
        try:
            import requests
        except ImportError:
            return LLMResponse(content="[VERONICA] `requests` isn't installed; run `pip install requests`.", ok=False)

        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers, timeout=self.timeout_s
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Cloud LLM request failed: %s", e)
            return LLMResponse(content=f"The cloud model request failed: {e}", ok=False)

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        content = message.get("content", "") or ""
        tool_calls = []
        for i, tc in enumerate(message.get("tool_calls", []) or []):
            fn = tc.get("function", {})
            args = fn.get("arguments", "{}")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            tool_calls.append(ToolCall(id=str(tc.get("id", i)), name=fn.get("name", ""), arguments=args))

        return LLMResponse(content=content, tool_calls=tool_calls, raw=data)

    def chat_stream(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.4,
    ):
        """Stream via the OpenAI-compatible SSE endpoint (stream: True). Same
        construction as ``chat`` — no separate SDK, no second client."""
        if not self.api_key:
            yield StreamChunk(
                text=("No API key is configured for the cloud provider. Set "
                      "VERONICA_LLM_API_KEY in config/.env, or switch llm.provider "
                      "back to 'ollama' in config.yaml."),
                finish_reason="error", done=True,
            )
            return
        try:
            import requests
        except ImportError:
            yield StreamChunk(
                text="[VERONICA] `requests` isn't installed; run `pip install requests`.",
                finish_reason="error", done=True,
            )
            return

        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

        seq = 0
        # Minimal tool-call delta assembler (V1: capture tool calls on the terminal
        # chunk so the orchestrator's existing tool loop stays authoritative — the
        # full delta protocol is explicitly out of scope).
        tool_assembler: dict[int, dict] = {}
        try:
            resp = requests.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers,
                timeout=self.timeout_s, stream=True,
            )
            resp.raise_for_status()
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if not line.startswith("data:"):
                    continue
                data_str = line[len("data:"):].strip()
                if data_str == "[DONE]":
                    yield StreamChunk(text="", sequence=seq, finish_reason="stop", done=True)
                    return
                try:
                    data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                choice = (data.get("choices") or [{}])[0]
                delta = choice.get("delta", {}) or {}
                text = delta.get("content", "") if isinstance(delta, dict) else ""
                finish = choice.get("finish_reason")
                done = finish is not None or bool(data.get("done", False))
                metadata: dict = {}
                if finish:
                    metadata["finish_reason"] = finish
                if isinstance(delta, dict) and delta.get("tool_calls"):
                    for frag in delta["tool_calls"]:
                        if not isinstance(frag, dict):
                            continue
                        idx = int(frag.get("index", 0))
                        slot = tool_assembler.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                        if frag.get("id"):
                            slot["id"] = frag["id"]
                        fn = frag.get("function", {}) or {}
                        if isinstance(fn, dict):
                            if fn.get("name"):
                                slot["name"] += fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
                if finish == "tool_calls" and tool_assembler:
                    assembled = []
                    for idx in sorted(tool_assembler):
                        slot = tool_assembler[idx]
                        try:
                            args = json.loads(slot["arguments"]) if slot["arguments"] else {}
                        except json.JSONDecodeError:
                            args = {}
                        assembled.append({
                            "id": slot["id"] or f"call_{idx}",
                            "name": slot["name"],
                            "arguments": args if isinstance(args, dict) else {},
                        })
                    metadata["tool_calls"] = assembled
                yield StreamChunk(text=text or "", sequence=seq, done=done,
                                  finish_reason=finish, metadata=metadata)
                seq += 1
                if done:
                    return
        except Exception as e:  # noqa: BLE001 — connection failure is a controlled event
            logger.warning("Cloud LLM stream failed: %s", e)
            yield StreamChunk(
                text=f"The cloud model stream failed: {e}",
                finish_reason="error", done=True,
            )
