"""
Optional cloud provider for anyone who *wants* to point VERONICA at a paid or free-tier
hosted model (OpenRouter, OpenAI, any OpenAI-compatible endpoint). Never used unless the
user explicitly sets `llm.provider: openai_compatible` in config.yaml AND supplies an API
key — VERONICA never assumes or requires this.
"""
from __future__ import annotations

import json
from typing import Optional

from veronica.llm.base import ChatMessage, LLMProvider, LLMResponse, ToolCall
from veronica.logging_setup import get_logger

logger = get_logger("llm.openai_compatible")


class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"

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
                )
            )
        try:
            import requests
        except ImportError:
            return LLMResponse(content="[VERONICA] `requests` isn't installed; run `pip install requests`.")

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
            return LLMResponse(content=f"The cloud model request failed: {e}")

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
