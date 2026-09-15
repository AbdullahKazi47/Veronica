"""
Picks the configured LLM provider. This is the ONE function anything in VERONICA calls
to get a model — nothing else should construct a provider directly, which is what makes
"easy model/provider switching" a one-line config change instead of a code change.
"""
from __future__ import annotations

from typing import Optional

from veronica.config import Config
from veronica.llm.base import ChatMessage, LLMProvider, LLMResponse
from veronica.logging_setup import get_logger

logger = get_logger("llm.router")


class EchoProvider(LLMProvider):
    """A dependency-free stub used for tests and for `--no-llm` runs: no network,
    no model, deterministic output. Never used unless explicitly selected."""

    name = "echo"

    def chat(self, messages: list[ChatMessage], tools=None, temperature: float = 0.4) -> LLMResponse:
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return LLMResponse(content=f"(echo) You said: {last_user}")


def build_provider(config: Config) -> LLMProvider:
    provider_name = config.llm.provider.lower()

    if provider_name == "ollama":
        from veronica.llm.ollama_provider import OllamaProvider
        provider = OllamaProvider(base_url=config.llm.base_url, model=config.llm.model, timeout_s=config.llm.request_timeout_s)
        if not provider.is_available():
            logger.warning(
                "Ollama isn't reachable at %s. VERONICA will still run (tools/fast-path "
                "commands work), but open-ended chat will report the model is unreachable "
                "until you `ollama serve` and `ollama pull %s`.",
                config.llm.base_url, config.llm.model,
            )
        return provider

    if provider_name == "openai_compatible":
        from veronica.llm.openai_compatible_provider import OpenAICompatibleProvider
        api_key = config.secret(config.llm.api_key_env)
        return OpenAICompatibleProvider(
            base_url=config.llm.base_url, api_key=api_key, model=config.llm.model, timeout_s=config.llm.request_timeout_s
        )

    if provider_name == "echo":
        return EchoProvider()

    raise ValueError(f"Unknown llm.provider '{provider_name}'. Use 'ollama', 'openai_compatible', or 'echo'.")
