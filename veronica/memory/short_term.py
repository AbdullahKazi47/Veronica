"""
Short-term memory: the rolling in-session conversation buffer handed to the LLM as
chat history. Deliberately separate from long-term memory (see long_term.py) — Prodigy-AI
conflated "facts I should remember forever" and "what did we just say" into one
`memory.py`/`memory.json`, which makes both harder to reason about and to bound in size.
"""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class Turn:
    role: str   # "user" | "assistant" | "system"
    content: str
    at: float = 0.0


class ShortTermMemory:
    """With `persist_path`, the buffer is saved after every turn and turns younger than
    `resume_hours` are restored at start, so a restart doesn't wipe the conversation."""

    def __init__(self, max_messages: int = 20, persist_path: Optional[Path] = None,
                 resume_hours: float = 12.0) -> None:
        self._buffer: deque[Turn] = deque(maxlen=max_messages)
        self._path = persist_path
        if persist_path is not None:
            self._restore(resume_hours)

    def _restore(self, resume_hours: float) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        cutoff = time.time() - resume_hours * 3600
        for t in raw.get("turns", []):
            if t.get("at", 0) >= cutoff and t.get("role") in ("user", "assistant"):
                self._buffer.append(Turn(role=t["role"], content=t.get("content", ""), at=t["at"]))

    def _persist(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"turns": [t.__dict__ for t in self._buffer]}, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass  # persistence is best-effort; the live conversation keeps working

    def add(self, role: str, content: str) -> None:
        self._buffer.append(Turn(role=role, content=content, at=time.time()))
        self._persist()

    def as_messages(self) -> list[dict[str, str]]:
        return [{"role": t.role, "content": t.content} for t in self._buffer]

    def clear(self) -> None:
        self._buffer.clear()
        self._persist()

    def __len__(self) -> int:
        return len(self._buffer)
