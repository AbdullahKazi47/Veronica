"""
Thread Memory V1 — record store (persistent JSONL).

``MemoryStore`` persists ``MemoryRecord`` objects as JSONL (one record per line) in the
same spirit as ``ExperienceStore``: append-friendly, restart-surviving, and tolerant of a
corrupted tail line. It is the JSON catalog that keeps working even when the (optional)
vector backend is down.

Guarantees:
  - Every write goes through ``privacy.sanitize_memory_record`` (defense in depth — the
    facade already sanitizes, but a caller that bypasses it still cannot leak secrets).
  - Records are never silently dropped: a bad line is skipped and logged, the rest load.
  - Thread-safe via a single lock; public methods never raise into the caller.
  - ``all()`` is a snapshot filtered by type/chapter/project; soft-deleted records are
    excluded unless explicitly requested.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from veronica.logging_setup import get_logger
from veronica.memory.thread.models import MemoryRecord
from veronica.memory.thread.privacy import sanitize_memory_record

logger = get_logger("memory.thread.store")


class MemoryStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._records: Dict[str, MemoryRecord] = {}
        self._dirty = False
        self._load()
        # A cheap id->index for filter queries is unnecessary at V1 scale (records are
        # held in one dict); scans are fine for thousands of rows.

    # -- persistence ---------------------------------------------------------------
    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = MemoryRecord.from_dict(json.loads(line))
                        self._records[rec.id] = rec
                    except (ValueError, TypeError):
                        logger.warning("Skipping a corrupted memory line in %s", self.path)
        except OSError:
            logger.exception("Failed to read memory store at %s; starting empty.", self.path)

    def _persist(self) -> bool:
        """Rewrite the whole catalog. Small files, so full rewrite is fine for V1."""
        if not self._dirty:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                for rec in self._records.values():
                    f.write(json.dumps(sanitize_memory_record(rec).to_dict(), ensure_ascii=False) + "\n")
            os.replace(tmp, self.path)
            self._dirty = False
            return True
        except OSError:
            logger.exception("Failed to persist memory store at %s", self.path)
            return False

    def _mark_dirty(self) -> None:
        self._dirty = True

    # -- mutation -----------------------------------------------------------------
    def put(self, record: MemoryRecord) -> bool:
        """Insert or overwrite one record. Sanitizes on the way in."""
        try:
            safe = sanitize_memory_record(record)
            with self._lock:
                self._records[safe.id] = safe
                self._mark_dirty()
                self._persist()
            return True
        except Exception:  # noqa: BLE001
            logger.exception("MemoryStore.put failed for id=%s", record.id)
            return False

    def delete(self, memory_id: str) -> bool:
        try:
            with self._lock:
                removed = self._records.pop(memory_id, None) is not None
                if removed:
                    self._mark_dirty()
                    self._persist()
            return removed
        except Exception:  # noqa: BLE001
            logger.exception("MemoryStore.delete failed for id=%s", memory_id)
            return False

    # -- reads ---------------------------------------------------------------------
    def get(self, memory_id: str) -> Optional[MemoryRecord]:
        try:
            with self._lock:
                return self._records.get(memory_id)
        except Exception:  # noqa: BLE001
            logger.exception("MemoryStore.get failed for id=%s", memory_id)
            return None

    def search(
        self,
        query_str: str = "",
        *,
        memory_type: str = "",
        limit: int = 10,
        project: str = "",
        tag: str = "",
        include_deleted: bool = False,
    ) -> List[MemoryRecord]:
        """Deterministic substring search over content + tags + metadata strings.
        Order: most-recently-created first (stable), then limit. Never raises."""
        q = query_str.strip().lower()
        try:
            out: List[MemoryRecord] = []
            with self._lock:
                records = [r for r in self._records.values()
                           if include_deleted or not r.deleted]
            for rec in records:
                if memory_type and rec.memory_type != memory_type:
                    continue
                if project and rec.project != project:
                    continue
                if tag and tag not in rec.tags:
                    continue
                if q:
                    hay = (" ".join([rec.content, " ".join(rec.tags), rec.project,
                                     rec.chapter, " ".join(rec.metadata.keys())])).lower()
                    if q not in hay:
                        continue
                out.append(rec)
            out.sort(key=lambda r: r.created_at, reverse=True)
            return out[: max(0, limit)]
        except Exception:  # noqa: BLE001
            logger.exception("MemoryStore.search failed")
            return []

    def all(self, *, include_deleted: bool = False, memory_type: str = "",
            limit: int = 0) -> List[MemoryRecord]:
        """Snapshot of all records (oldest-first) with optional filters. limit<=0 = all."""
        try:
            with self._lock:
                records = [r for r in self._records.values()
                           if (include_deleted or not r.deleted)
                           and (not memory_type or r.memory_type == memory_type)]
            records.sort(key=lambda r: r.created_at)
            if limit and limit > 0:
                return records[:limit]
            return records
        except Exception:  # noqa: BLE001
            logger.exception("MemoryStore.all failed")
            return []

    def count(self) -> int:
        try:
            with self._lock:
                return len(self._records)
        except Exception:  # noqa: BLE001
            return 0

    def count_by_type(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        try:
            for r in self.all():
                counts[r.memory_type] = counts.get(r.memory_type, 0) + 1
        except Exception:  # noqa: BLE001
            logger.exception("MemoryStore.count_by_type failed")
        return counts

    # -- lifecycle ----------------------------------------------------------------
    def clear(self) -> bool:
        try:
            with self._lock:
                self._records.clear()
                self._mark_dirty()
                self._persist()
            return True
        except Exception:  # noqa: BLE001
            logger.exception("MemoryStore.clear failed")
            return False

    def reset_to(self, records: List[MemoryRecord]) -> bool:
        """Replace the whole catalog from an explicit list (tests / import)."""
        safe = [sanitize_memory_record(r) for r in records]
        try:
            with self._lock:
                self._records = {r.id: r for r in safe}
                self._mark_dirty()
                self._persist()
            return True
        except Exception:  # noqa: BLE001
            logger.exception("MemoryStore.reset_to failed")
            return False

    def health_check(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return os.access(self.path.parent, os.W_OK)
        except OSError:
            logger.exception("Memory store health check failed")
            return False


__all__ = ["MemoryStore"]