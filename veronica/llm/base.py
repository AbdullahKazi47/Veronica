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


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def chat(
        self,
        messages: list[ChatMessage],
        tools: Optional[list[dict]] = None,
        temperature: float = 0.4,
    ) -> LLMResponse:
        """Send a conversation (+ optional tool schemas) and get a response back.
        Implementations must not raise for expected connectivity failures — they should
        surface that via a clear LLMResponse.content explaining the failure so the
        orchestrator can degrade gracefully (e.g. 'the local model isn't reachable')."""
        raise NotImplementedError

    def is_available(self) -> bool:
        """Cheap reachability check, used at startup to warn the user instead of
        failing confusingly on the first real request."""
        return True
