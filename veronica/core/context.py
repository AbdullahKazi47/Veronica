"""
Per-session conversation context: bundles short-term memory with a couple of session-
scoped flags. Kept separate from the Orchestrator itself so a future multi-session
server (e.g. a GUI with several chat tabs) can hold one Context per session while
sharing a single Orchestrator/tool-registry/LLM provider.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from veronica.memory.short_term import ShortTermMemory


@dataclass
class SessionContext:
    short_term: ShortTermMemory
    turn_count: int = 0
    last_tool_used: str | None = None
