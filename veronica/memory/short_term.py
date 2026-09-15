"""
Short-term memory: the rolling in-session conversation buffer handed to the LLM as
chat history. Deliberately separate from long-term memory (see long_term.py) — Prodigy-AI
conflated "facts I should remember forever" and "what did we just say" into one
`memory.py`/`memory.json`, which makes both harder to reason about and to bound in size.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass


@dataclass
class Turn:
    role: str   # "user" | "assistant" | "system"
    content: str


class ShortTermMemory:
    def __init__(self, max_messages: int = 20) -> None:
        self._buffer: deque[Turn] = deque(maxlen=max_messages)

    def add(self, role: str, content: str) -> None:
        self._buffer.append(Turn(role=role, content=content))

    def as_messages(self) -> list[dict[str, str]]:
        return [{"role": t.role, "content": t.content} for t in self._buffer]

    def clear(self) -> None:
        self._buffer.clear()

    def __len__(self) -> int:
        return len(self._buffer)
