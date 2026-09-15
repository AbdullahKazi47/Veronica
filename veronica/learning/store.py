"""
ExperienceStore — durable, append-only persistence for Experiences.

Backing store is a JSONL file (one JSON object per line). Chosen over SQLite because
VERONICA has no SQLite infrastructure today (long-term memory is JSON), and an append-only
log is the natural fit for an experience record: writes never rewrite history, reads load
the file on demand, and a partial/corrupt tail line is skipped without losing the rest.

Fault-tolerance rules (the same spirit as long-term memory):
  * Survives process restarts — data is on disk, not in memory.
  * Never crashes VERONICA if storage is unavailable — every public method is defensive
    and returns empty/safe results on error.
  * Never blocks the main execution path unnecessarily — a record() is a single append.
  * NEVER persists secrets — every record is passed through ``redact_experience`` on the
    way in.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from veronica.learning.experience import Experience, ExperienceOutcome, redact_experience
from veronica.logging_setup import get_logger

logger = get_logger("learning.store")


class ExperienceStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._cache: dict[str, Experience] | None = None  # lazy, invalidated on write

    # -- persistence ---------------------------------------------------------------
    def _ensure_file(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def record(self, experience: Experience) -> bool:
        """Persist one experience. Sanitizes secrets first. Returns False on any
        failure (logging it) rather than raising, so learning can never break the
        caller. A crash mid-write can leave a partial trailing line, which the reader
        skips gracefully."""
        try:
            safe = redact_experience(experience)
            line = json.dumps(safe.to_dict(), ensure_ascii=False)
            with self._lock:
                self._ensure_file()
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
                # Cache is now stale.
                if self._cache is not None:
                    self._cache = None
            logger.info("experience recorded id=%s outcome=%s", safe.experience_id, safe.outcome)
            return True
        except Exception:  # noqa: BLE001 — storage failure must never break the caller
            logger.exception("Failed to record experience to %s", self.path)
            return False

    def _load_all(self) -> list[Experience]:
        """Read every valid line. Skips corrupted lines; returns [] if the file is
        missing or unreadable. Never raises."""
        if not self.path.exists():
            return []
        records: list[Experience] = []
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(Experience.from_dict(json.loads(line)))
                    except (ValueError, TypeError):
                        # A partial line from an interrupted write — skip it, keep the rest.
                        logger.warning("Skipping a corrupted experience line in %s", self.path)
        except OSError:
            logger.exception("Failed to read experience store at %s", self.path)
        return records

    def _ensure_cache(self) -> dict[str, Experience]:
        if self._cache is None:
            self._cache = {e.experience_id: e for e in self._load_all()}
        return self._cache

    # -- queries -------------------------------------------------------------------
    def get(self, experience_id: str) -> Optional[Experience]:
        try:
            return self._ensure_cache().get(experience_id)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to read experience %s", experience_id)
            return None

    def list_recent(self, limit: int = 20) -> list[Experience]:
        try:
            records = list(self._ensure_cache().values())
            records.sort(key=lambda e: e.timestamp, reverse=True)
            return records[:limit]
        except Exception:  # noqa: BLE001
            logger.exception("Failed to list recent experiences")
            return []

    def search(self, query: str, limit: int = 50) -> list[Experience]:
        """Substring search over goal, tool_calls, failed_step, and failure_reason."""
        q = query.lower()
        try:
            out = []
            for e in self._ensure_cache().values():
                hay = " ".join([
                    e.goal,
                    " ".join(e.tool_calls),
                    e.failed_step,
                    e.failure_reason,
                    " ".join(e.recovery_actions),
                ]).lower()
                if q in hay:
                    out.append(e)
            return out[:limit]
        except Exception:  # noqa: BLE001
            logger.exception("Failed to search experiences")
            return []

    def filter(self, *, outcome: str = "", tool: str = "", limit: int = 50) -> list[Experience]:
        """Retrieve experiences matching an outcome and/or a tool that ran."""
        try:
            out = []
            for e in self._ensure_cache().values():
                if outcome and e.outcome != outcome:
                    continue
                if tool and tool not in e.tool_calls:
                    continue
                out.append(e)
            return out[:limit]
        except Exception:  # noqa: BLE001
            logger.exception("Failed to filter experiences")
            return []

    def count(self) -> int:
        try:
            return len(self._ensure_cache())
        except Exception:  # noqa: BLE001
            return 0

    def count_by_outcome(self) -> dict[str, int]:
        counts = {ExperienceOutcome.SUCCESS: 0, ExperienceOutcome.FAILED: 0,
                  ExperienceOutcome.RECOVERED: 0, ExperienceOutcome.CANCELLED: 0}
        try:
            for e in self._ensure_cache().values():
                if e.outcome in counts:
                    counts[e.outcome] += 1
            return counts
        except Exception:  # noqa: BLE001
            return counts

    def all(self) -> list[Experience]:
        try:
            return list(self._ensure_cache().values())
        except Exception:  # noqa: BLE001
            return []

    def reset(self) -> bool:
        """Delete all stored experiences (for tests / user reset). Returns False if the
        file could not be removed."""
        try:
            with self._lock:
                self._cache = {}
                if self.path.exists():
                    self.path.unlink()
            logger.info("Experience store reset at %s", self.path)
            return True
        except OSError:
            logger.exception("Failed to reset experience store at %s", self.path)
            return False

    def replace_from_records(self, records: list[Experience]) -> bool:
        """Rewrite the whole store from an explicit list (used by tests to seed data
        deterministically). Returns False on failure."""
        try:
            with self._lock:
                self._ensure_file()
                with open(self.path, "w", encoding="utf-8") as f:
                    for e in records:
                        f.write(json.dumps(redact_experience(e).to_dict(), ensure_ascii=False) + "\n")
                self._cache = None
            return True
        except OSError:
            logger.exception("Failed to rewrite experience store at %s", self.path)
            return False
