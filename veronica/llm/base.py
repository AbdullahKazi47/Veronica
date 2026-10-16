"""
The LLMProvider interface. Every model backend (local Ollama, an OpenAI-compatible
cloud endpoint, or a trivial echo stub for tests) implements this one contract, so
swapping models/providers is a config change (see llm/router.py), never a code change —
this is the concrete form of OpenJarvis's "local-first with an easy cloud escape hatch"
idea.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ChatMessage:
    role: str            # "system" | "user" | "assistant" | "tool"
    content: str
    tool_call_id: Optional[str] = None
    name: Optional[str] = None


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None
    # Machine-readable success flag. Providers set ok=False when they had to fall
    # back to an error-message response (endpoint unreachable, missing key, ...).
    # Defaults True so existing callers/tests treat every old response as successful;
    # the model router needs it to avoid reporting "task succeeded" when the selected
    # model actually failed. Added for Multi-Model Routing V1.
    ok: bool = True


@dataclass
class StreamChunk:
    """One incremental piece of streamed LLM output. Provider-neutral — never
    carries provider-specific structures, secrets, or raw HTTP payloads."""
    text: str = ""
    role: str = "assistant"  # "assistant" only for text-delays; "tool" reserved (arch-ready)
    sequence: int = 0
    finish_reason: Optional[str] = None  # None = continuing; "stop"/"length"/"tool_calls" = terminal
    done: bool = False
    metadata: dict = field(default_factory=dict)


class LLMProvider(ABC):
    name: str = "base"
    # Whether the provider can deliver PROGRESSIVE chunks (StreamChunk deltas via
    # chat_stream). Mirrors CAP_STREAMING on routing profiles. The base default of
    # False drives the non-streaming single-chunk fallback; real streaming providers
    # set True.
    supports_streaming: bool = False

    @abstractmethod
    def chat(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.4,
        voice_mode: bool = False,
    ) -> LLMResponse:
        """Send a conversation (+ optional tool schemas) and get a response back.
        ``voice_mode`` is a latency hint for short conversational turns (e.g. Ollama
        disables thinking); providers that have no use for it simply ignore it.
        Implementations must not raise for expected connectivity failures — they should
        surface that via a clear LLMResponse.content explaining the failure so the
        orchestrator can degrade gracefully (e.g. 'the local model isn't reachable')."""
        raise NotImplementedError

    def chat_stream(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.4,
        voice_mode: bool = False,
    ):
        """Yield ``StreamChunk`` objects as output arrives. Default = non-streaming
        providers degrade to a single finished chunk; implementations that truly
        stream (Ollama NDJSON, OpenAI-compatible SSE) override this. Never raises for
        expected connectivity failures — the error surfaces as a chunk with
        ``done=True`` and ``finish_reason="error"``."""
        kwargs = {"voice_mode": True} if voice_mode else {}
        yield StreamChunk(text=self.chat(messages, tools=tools, temperature=temperature, **kwargs).content,
                          sequence=0, finish_reason="stop", done=True)

    def is_available(self) -> bool:
        """Cheap reachability check, used at startup to warn the user instead of
        failing confusingly on the first real request."""
        return True
