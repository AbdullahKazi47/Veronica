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
        self._lock = threading.Lock()
        self._facts: dict[str, Fact] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for key, entry in raw.items():
                self._facts[key] = Fact(key=key, value=entry.get("value"), created=entry.get("created", ""))
        except Exception:
            logger.exception("Failed to load long-term memory from %s; starting empty.", self.path)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {k: {"value": f.value, "created": f.created} for k, f in self._facts.items()}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def remember(self, key: str, value: Any) -> None:
        with self._lock:
            self._facts[key] = Fact(key=key, value=value)
            self._save()
        logger.info("Remembered fact '%s'", key)

    def recall(self, key: str) -> Optional[Any]:
        fact = self._facts.get(key)
        return fact.value if fact else None

    def forget(self, key: str) -> bool:
        with self._lock:
            existed = key in self._facts
            self._facts.pop(key, None)
            if existed:
                self._save()
        return existed

    def search(self, query: str) -> list[Fact]:
        q = query.lower()
        return [f for f in self._facts.values() if q in f.key.lower() or q in str(f.value).lower()]

    def all_facts(self) -> list[Fact]:
        return list(self._facts.values())
