"""
Picks the configured LLM provider. This is the ONE place anything in VERONICA calls
to construct a provider — nothing else should build one directly, which is what makes
"easy model/provider switching" a one-line config change instead of a code change.

Multi-Model Routing V1: the single-model path is unchanged (``build_provider``), and
the routing package reuses the same construction via ``build_provider_for_model`` so
registered routing models are real providers, not a second implementation.
"""
from __future__ import annotations

from typing import Optional

from veronica.config import Config
from veronica.llm.base import ChatMessage, LLMProvider, LLMResponse, StreamChunk
from veronica.logging_setup import get_logger

logger = get_logger("llm.router")


class EchoProvider(LLMProvider):
    """A dependency-free stub used for tests and for `--no-llm` runs: no network,
    no model, deterministic output. Never used unless explicitly selected.

    Streaming variant (Streaming LLM V1): ``chat_stream`` deterministically emits the
    reply as one delta per space-separated word, then a final done chunk. Concatenating
    all deltas reproduces ``(echo) You said: <last user>`` exactly — tests rely on this
    for ordering + final-text assertions."""

    name = "echo"
    supports_streaming = True

    def chat(self, messages: list[ChatMessage], tools=None, temperature: float = 0.4) -> LLMResponse:
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return LLMResponse(content=f"(echo) You said: {last_user}")

    def chat_stream(self, messages: list[ChatMessage], tools=None, temperature: float = 0.4):
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        reply = f"(echo) You said: {last_user}"
        if not reply:
            yield StreamChunk(text="", finish_reason="stop", done=True)
            return
        parts = reply.split(" ")
        for i, tok in enumerate(parts):
            piece = tok + (" " if i < len(parts) - 1 else "")
            yield StreamChunk(text=piece, sequence=i)
        yield StreamChunk(text="", sequence=len(parts), finish_reason="stop", done=True)


def _construct_provider(
    provider_name: str,
    model: str,
    base_url: str,
    api_key: Optional[str],
    timeout_s: int = 60,
) -> LLMProvider:
    """Shared construction switch. Knows the whole provider family; nothing else does."""
    name = provider_name.lower()
    if name == "ollama":
        from veronica.llm.ollama_provider import OllamaProvider
        return OllamaProvider(base_url=base_url, model=model, timeout_s=timeout_s)
    if name == "openai_compatible":
        from veronica.llm.openai_compatible_provider import OpenAICompatibleProvider
        return OpenAICompatibleProvider(base_url=base_url, api_key=api_key, model=model, timeout_s=timeout_s)
    if name == "echo":
        return EchoProvider()
    raise ValueError(
        f"Unknown llm.provider '{provider_name}'. Use 'ollama', 'openai_compatible', or 'echo'."
    )


def build_provider(config: Config) -> LLMProvider:
    provider_name = config.llm.provider.lower()
    provider = _construct_provider(
        provider_name,
        model=config.llm.model,
        base_url=config.llm.base_url,
        api_key=config.secret(config.llm.api_key_env),
        timeout_s=config.llm.request_timeout_s,
    )
    if provider_name == "ollama" and not provider.is_available():
        logger.warning(
            "Ollama isn't reachable at %s. VERONICA will still run (tools/fast-path "
            "commands work), but open-ended chat will report the model is unreachable "
            "until you `ollama serve` and `ollama pull %s`.",
            config.llm.base_url, config.llm.model,
        )
    return provider


def build_provider_for_model(
    provider_name: str,
    model: str,
    base_url: str = "http://localhost:11434",
    api_key: Optional[str] = None,
    timeout_s: int = 60,
) -> LLMProvider:
    """Construct a provider for a single registered routing model. Same construction
    rules as ``build_provider`` — one source of provider truth."""
    return _construct_provider(
        provider_name,
        model=model,
        base_url=base_url,
        api_key=api_key,
        timeout_s=timeout_s,
    )
