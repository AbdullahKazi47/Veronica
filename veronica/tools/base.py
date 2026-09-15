"""
The Tool contract every capability implements.

This is the concrete answer to "extensible plugin/tool architecture": a Tool is a small,
self-describing, independently testable unit. The orchestrator, the LLM's function-calling
prompt, and the permission system all derive what they need (name, description, JSON
schema, risk level) from this one class — adding a capability never requires touching the
orchestrator.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from veronica.safety.permissions import PermissionLevel


@dataclass
class ToolResult:
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def ok(message: str, **data: Any) -> "ToolResult":
        return ToolResult(success=True, message=message, data=data)

    @staticmethod
    def fail(message: str, **data: Any) -> "ToolResult":
        return ToolResult(success=False, message=message, data=data)


class Tool(ABC):
    """Subclass this for every new capability. See tools/utility_tools.py for a minimal example."""

    name: str = "unnamed_tool"
    description: str = "No description provided."
    permission: PermissionLevel = PermissionLevel.SAFE
    # JSON-schema-ish parameter spec, used both for the LLM tool-calling prompt and for
    # lightweight validation before run() is called.
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    def matches(self, text: str) -> bool:
        """
        Cheap, deterministic pre-check used by the fast-path router (see
        core/orchestrator.py) so obvious commands never have to round-trip through the
        LLM. Override for tools that should be fast-path-routable; default: never
        fast-matched, i.e. only reachable via explicit LLM tool-calling.
        """
        return False

    def extract_arguments(self, text: str) -> dict[str, Any]:
        """Pull arguments out of free text for the fast path. Default: no arguments."""
        return {}

    @abstractmethod
    def run(self, **kwargs: Any) -> ToolResult:
        """Execute the tool. Must not raise for expected/handled failures — return
        ToolResult.fail(...) instead so the orchestrator can report cleanly."""
        raise NotImplementedError

    def schema(self) -> dict[str, Any]:
        """OpenAI/Ollama-style function-calling schema, derived automatically."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
