"""
Local, free, default LLM provider — talks to a local Ollama server. No API key, no
internet dependency once the model is pulled. This is the default so VERONICA works
fully offline out of the box, in line with the "no paid API keys" requirement.
"""
from __future__ import annotations

import json
from typing import Optional

from veronica.llm.base import ChatMessage, LLMProvider, LLMResponse, StreamChunk, ToolCall
from veronica.logging_setup import get_logger

logger = get_logger("llm.ollama")


class OllamaProvider(LLMProvider):
    name = "ollama"
    supports_streaming = True

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "llama3.1:8b", timeout_s: int = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def is_available(self) -> bool:
        try:
            import requests
            r = requests.get(f"{self.base_url}/api/tags", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def chat(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.4,
        voice_mode: bool = False,
    ) -> LLMResponse:
        try:
            import requests
        except ImportError:
            return LLMResponse(content="[VERONICA] `requests` isn't installed; run `pip install requests`.", ok=False)

        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": {"temperature": temperature},
        }
        if voice_mode:
            payload["think"] = False
        if tools:
            payload["tools"] = tools

        try:
            resp = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=self.timeout_s)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning("Ollama request failed: %s", e)
            return LLMResponse(
                content=(
                    "I can't reach my local language model right now (is Ollama running? "
                    f"`ollama serve` / `ollama pull {self.model}`). Error: {e}"
                ),
                ok=False,
            )

        message = data.get("message", {})
        content = message.get("content", "") or ""
        tool_calls = []
        for i, tc in enumerate(message.get("tool_calls", []) or []):
            fn = tc.get("function", {})
            args = fn.get("arguments", {})
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
        voice_mode: bool = False,
    ):
        """Stream via Ollama's NDJSON API (stream: True). Same construction as
        ``chat`` — no second client, no new dependency."""
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
            "stream": True,
            "options": {"temperature": temperature},
        }
        if voice_mode:
            payload["think"] = False
        if tools:
            payload["tools"] = tools

        seq = 0
        try:
            resp = requests.post(
                f"{self.base_url}/api/chat", json=payload,
                timeout=self.timeout_s, stream=True,
            )
            resp.raise_for_status()
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                line = raw.strip()
                if not line.startswith("{"):
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                msg = data.get("message")
                text = msg.get("content", "") if isinstance(msg, dict) else ""
                done = bool(data.get("done", False))
                reason = data.get("done_reason") if done else None
                metadata: dict = {}
                if reason:
                    metadata["done_reason"] = reason
                tool_calls = []
                if isinstance(msg, dict):
                    raw_tcs = msg.get("tool_calls")
                    if raw_tcs:
                        for i, tc in enumerate(raw_tcs):
                            fn = tc.get("function", {}) if isinstance(tc, dict) else {}
                            args = fn.get("arguments", {})
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except json.JSONDecodeError:
                                    args = {}
                            tool_calls.append({
                                "id": str(tc.get("id", i)) if isinstance(tc, dict) else str(i),
                                "name": fn.get("name", "") if isinstance(fn, dict) else "",
                                "arguments": args if isinstance(args, dict) else {},
                            })
                if tool_calls:
                    metadata["tool_calls"] = tool_calls
                    if done:                          # surfaced exactly once, on the terminal chunk
                        reason = "tool_calls"
                yield StreamChunk(text=text or "", sequence=seq, done=done,
                                  finish_reason=reason if done else None,
                                  metadata=metadata)
                seq += 1
                if done:
                    return
        except Exception as e:  # noqa: BLE001 — connection failure is a controlled event
            logger.warning("Ollama stream failed: %s", e)
            yield StreamChunk(
                text="I can't reach my local language model to stream a reply "
                     f"(is Ollama running? `ollama serve` / `ollama pull {self.model}`). Error: {e}",
                finish_reason="error", done=True,
            )
