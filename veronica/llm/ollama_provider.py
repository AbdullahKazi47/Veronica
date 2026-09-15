"""
Local, free, default LLM provider — talks to a local Ollama server. No API key, no
internet dependency once the model is pulled. This is the default so VERONICA works
fully offline out of the box, in line with the "no paid API keys" requirement.
"""
from __future__ import annotations

import json
from typing import Optional

from veronica.llm.base import ChatMessage, LLMProvider, LLMResponse, ToolCall
from veronica.logging_setup import get_logger

logger = get_logger("llm.ollama")


class OllamaProvider(LLMProvider):
    name = "ollama"

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
    ) -> LLMResponse:
        try:
            import requests
        except ImportError:
            return LLMResponse(content="[VERONICA] `requests` isn't installed; run `pip install requests`.")

        payload = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": {"temperature": temperature},
        }
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
                )
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
