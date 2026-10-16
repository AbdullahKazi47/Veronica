"""
Long-term memory: durable facts that persist across restarts (e.g. "my name is Abdul",
"I prefer dark mode"). Backed by a JSON file today — deliberately hidden behind this
class's interface (get/set/remember/forget/search) rather than exposed as raw JSON reads
scattered through the codebase (Prodigy-AI's `memory_parser.py` pattern), so the storage
can later be swapped for SQLite or a vector store without touching any caller.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from veronica.logging_setup import get_logger

logger = get_logger("memory.long_term")


@dataclass
class Fact:
    key: str
    value: Any
    created: str = field(default_factory=lambda: datetime.now().isoformat())


class LongTermMemory:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._facts: dict[str, Fact] = {}
        self._mtime: Optional[float] = None
        self._load()

    def _load(self) -> None:
        self._facts = {}
        if not self.path.exists():
            self._mtime = None
            return
        try:
            self._mtime = self.path.stat().st_mtime
            from veronica.core import jsonsafe
            raw, note = jsonsafe.read_json(self.path, default={})
            if note:
                logger.warning("Long-term memory: %s", note)
            for key, entry in (raw or {}).items():
                self._facts[key] = Fact(key=key, value=entry.get("value"), created=entry.get("created", ""))
        except Exception:
            logger.exception("Failed to load long-term memory from %s; starting empty.", self.path)

    def _refresh(self) -> None:
        # Several LongTermMemory objects can share the file (the memory tools write, the
        # orchestrator reads into every prompt); pick up changes made by the others.
        try:
            mtime = self.path.stat().st_mtime if self.path.exists() else None
        except OSError:
            return
        if mtime != self._mtime:
            self._load()

    def _save(self) -> None:
        from veronica.core import jsonsafe
        payload = {k: {"value": f.value, "created": f.created} for k, f in self._facts.items()}
        jsonsafe.write_json(self.path, payload)   # atomic + keeps a .bak for self-heal
        self._mtime = self.path.stat().st_mtime

    def remember(self, key: str, value: Any) -> None:
        with self._lock:
            self._refresh()
            self._facts[key] = Fact(key=key, value=value)
            self._save()
        logger.info("Remembered fact '%s'", key)

    def recall(self, key: str) -> Optional[Any]:
        with self._lock:
            self._refresh()
            fact = self._facts.get(key)
        return fact.value if fact else None

    def forget(self, key: str) -> bool:
        with self._lock:
            self._refresh()
            existed = key in self._facts
            self._facts.pop(key, None)
            if existed:
                self._save()
        return existed

    def search(self, query: str) -> list[Fact]:
        q = query.lower()
        return [f for f in self.all_facts() if q in f.key.lower() or q in str(f.value).lower()]

    def all_facts(self) -> list[Fact]:
        with self._lock:
            self._refresh()
            return list(self._facts.values())
