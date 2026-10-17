"""
Routines: one phrase runs several commands — "good night", "work mode", "I'm leaving".

A routine is a name plus a list of steps, and each step is an ordinary spoken command
("lock my PC", "volume to 20", "what's the weather tomorrow"). Running a routine sends
each step through the normal command path, so every step meets the same permission
gate as if you had said it yourself: a routine can never skip a question (sleep and
shutdown still ask). Stored in data/routines.json; created and edited by voice.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from veronica.core import jsonsafe

_lock = threading.Lock()
MAX_STEPS = 12
MAX_NAME = 40


def routine_key(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", (name or "").lower())).strip()


# "lock the pc, set volume to 20 and then read the weather" -> three steps.
_SPLIT = re.compile(r"\s*(?:;|\.\s+|,?\s+and\s+then\s+|,?\s+then\s+|,?\s+after\s+that\s+|,\s+(?:and\s+)?)\s*", re.I)


def parse_steps(text: str) -> list[str]:
    parts = [p.strip(" .,") for p in _SPLIT.split(text or "")]
    steps = []
    for p in parts:
        p = re.sub(r"^(?:first|second|third|next|finally|also|and)\s+", "", p, flags=re.I).strip(" .,")
        if p:
            steps.append(p)
    return steps[:MAX_STEPS]


@dataclass
class Routine:
    name: str
    steps: list[str] = field(default_factory=list)
    created: float = field(default_factory=time.time)
    runs: int = 0


class RoutineStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _load(self) -> dict[str, Routine]:
        data, _ = jsonsafe.read_json(self.path, default={})
        out = {}
        for key, raw in (data or {}).items():
            try:
                out[key] = Routine(**raw)
            except TypeError:
                continue
        return out

    def _save(self, items: dict[str, Routine]) -> None:
        jsonsafe.write_json(self.path, {k: asdict(v) for k, v in items.items()})

    def all(self) -> list[Routine]:
        return sorted(self._load().values(), key=lambda r: r.created)

    def get(self, name: str) -> Optional[Routine]:
        return self._load().get(routine_key(name))

    def save(self, name: str, steps: list[str]) -> Routine:
        name = re.sub(r"\s+", " ", (name or "").strip(" .,!?\"'"))[:MAX_NAME]
        if not routine_key(name):
            raise ValueError("A routine needs a name.")
        steps = [s.strip() for s in steps if s and s.strip()][:MAX_STEPS]
        if not steps:
            raise ValueError("A routine needs at least one step.")
        with _lock:
            items = self._load()
            old = items.get(routine_key(name))
            items[routine_key(name)] = Routine(name=name, steps=steps,
                                               created=old.created if old else time.time(),
                                               runs=old.runs if old else 0)
            self._save(items)
            return items[routine_key(name)]

    def delete(self, name: str) -> Optional[str]:
        with _lock:
            items = self._load()
            gone = items.pop(routine_key(name), None)
            if gone is None:
                return None
            self._save(items)
            return gone.name

    def add_step(self, name: str, step: str) -> Optional[Routine]:
        r = self.get(name)
        if r is None:
            return None
        return self.save(r.name, r.steps + [step])

    def remove_step(self, name: str, number: int) -> Optional[tuple[Routine, str]]:
        r = self.get(name)
        if r is None or not 1 <= number <= len(r.steps):
            return None
        removed = r.steps[number - 1]
        steps = r.steps[: number - 1] + r.steps[number:]
        if not steps:
            self.delete(r.name)
            return Routine(name=r.name, steps=[]), removed
        return self.save(r.name, steps), removed

    def mark_run(self, name: str) -> None:
        with _lock:
            items = self._load()
            r = items.get(routine_key(name))
            if r is not None:
                r.runs += 1
                self._save(items)
