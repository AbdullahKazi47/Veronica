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

    def chat(self, messages: list[ChatMessage], tools=None, temperature: float = 0.4,
             voice_mode: bool = False) -> LLMResponse:
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        return LLMResponse(content=f"(echo) You said: {last_user}")

    def chat_stream(self, messages: list[ChatMessage], tools=None, temperature: float = 0.4,
                    voice_mode: bool = False):
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
    effort: str = "low",
) -> LLMProvider:
    """Shared construction switch. Knows the whole provider family; nothing else does."""
    name = provider_name.lower()
    if name == "ollama":
        from veronica.llm.ollama_provider import OllamaProvider
        return OllamaProvider(base_url=base_url, model=model, timeout_s=timeout_s)
    if name == "openai_compatible":
        from veronica.llm.openai_compatible_provider import OpenAICompatibleProvider
        return OpenAICompatibleProvider(base_url=base_url, api_key=api_key, model=model, timeout_s=timeout_s)
    if name == "anthropic":
        from veronica.llm.anthropic_provider import AnthropicProvider
        return AnthropicProvider(api_key=api_key, model=model, timeout_s=timeout_s, effort=effort)
    if name == "echo":
        return EchoProvider()
    raise ValueError(
        f"Unknown llm.provider '{provider_name}'. Use 'ollama', 'openai_compatible', 'anthropic', or 'echo'."
    )


class FallbackProvider(LLMProvider):
    """The main provider, with backups from other services. If the main one fails (an
    outage, an overload) the next answers instead; after a failure the backup is asked
    first for `cooldown_s`, so each turn doesn't wait on a request that will fail."""

    name = "fallback"

    def __init__(self, primary: LLMProvider, backups: list[LLMProvider], cooldown_s: float = 45.0,
                 clock=None) -> None:
        import time as _time
        self.primary, self.backups = primary, list(backups)
        self.supports_streaming = getattr(primary, "supports_streaming", False)
        self.model = getattr(primary, "model", "")
        self._cooldown, self._clock = cooldown_s, clock or _time.time
        self._primary_down_until = 0.0

    def is_available(self) -> bool:
        return any(_safe_available(p) for p in [self.primary] + self.backups)

    def chat(self, messages, tools=None, temperature: float = 0.4, voice_mode: bool = False) -> LLMResponse:
        order = [self.primary] + self.backups
        if self._clock() < self._primary_down_until:
            order = self.backups + [self.primary]
        extra = {"voice_mode": True} if voice_mode else {}
        last: Optional[LLMResponse] = None
        for p in order:
            try:
                resp = p.chat(messages, tools=tools, temperature=temperature, **extra)
            except Exception as e:  # noqa: BLE001 - providers shouldn't raise; defend anyway
                logger.warning("Provider %s raised: %s", getattr(p, "model", p), e)
                resp = LLMResponse(content="", ok=False)
            if getattr(resp, "ok", True) and (resp.content or resp.tool_calls):
                if p is self.primary:
                    self._primary_down_until = 0.0
                elif p is not order[0] or self._primary_down_until:
                    logger.info("Answered by the backup model %s.", getattr(p, "model", "?"))
                return resp
            if p is self.primary:
                self._primary_down_until = self._clock() + self._cooldown
                logger.warning("Main model failed; using the backup for the next %.0f s.", self._cooldown)
            last = resp
        return last or LLMResponse(content="", ok=False)

    def chat_stream(self, messages, tools=None, temperature: float = 0.4, voice_mode: bool = False):
        yield from self.primary.chat_stream(messages, tools=tools, temperature=temperature, voice_mode=voice_mode)


def _safe_available(p) -> bool:
    try:
        return bool(p.is_available())
    except Exception:  # noqa: BLE001
        return False


def _backup_providers(config: Config) -> list[LLMProvider]:
    raw = getattr(config.llm, "backup", None)
    items = raw if isinstance(raw, list) else ([raw] if raw else [])
    out = []
    for b in items:
        try:
            key = config.secret(b.get("api_key_env", "")) if b.get("api_key_env") else None
            if b.get("api_key_env") and not key:
                logger.warning("Backup model %s skipped: %s is not set in config/.env.",
                               b.get("model"), b.get("api_key_env"))
                continue
            out.append(_construct_provider(b.get("provider", "openai_compatible"), model=b["model"],
                                           base_url=b.get("base_url", ""), api_key=key,
                                           timeout_s=int(b.get("timeout_s", 30))))
        except Exception as e:  # noqa: BLE001 - a bad backup entry must not stop startup
            logger.warning("Backup model entry ignored (%s): %s", b, e)
    return out


def build_provider(config: Config) -> LLMProvider:
    provider_name = config.llm.provider.lower()
    provider = _construct_provider(
        provider_name,
        model=config.llm.model,
        base_url=config.llm.base_url,
        api_key=config.secret(config.llm.api_key_env),
        timeout_s=config.llm.request_timeout_s,
        effort=config.llm.anthropic_effort,
    )
    if provider_name == "ollama" and not provider.is_available():
        logger.warning(
            "Ollama isn't reachable at %s. VERONICA will still run (tools/fast-path "
            "commands work), but open-ended chat will report the model is unreachable "
            "until you `ollama serve` and `ollama pull %s`.",
            config.llm.base_url, config.llm.model,
        )
    backups = _backup_providers(config)
    if backups:
        logger.info("Backup model(s) ready: %s", ", ".join(getattr(b, "model", "?") for b in backups))
        return FallbackProvider(provider, backups)
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
