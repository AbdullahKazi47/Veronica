"""
Thread Memory V1 — portable JSON export / import / validation.

Format (v1 envelope)::

    {
        "schema": "veronica.thread-memory",
        "schema_version": 1,
        "exported_at": "<iso timestamp>",
        "count": N,
        "memories": [ {record dict}, ... ]
    }

Guarantees:
  - Export is a snapshot of non-deleted records; no secrets (already sanitized at write).
  - Import is conflict-safe: an import NEVER silently overwrites an existing record —
    existing ids win, the new copy is reported as skipped, and the caller can decide.
  - Validation checks the envelope shape and every record schema without writing anything.
  - All three functions are defensive: they return a safe False / dict and log on failure.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from veronica.logging_setup import get_logger
from veronica.memory.thread.models import MemoryRecord, now_iso

logger = get_logger("memory.thread.serialization")

SCHEMA_NAME = "veronica.thread-memory"
SCHEMA_VERSION = 1


def _envelope(records: List[MemoryRecord]) -> Dict[str, Any]:
    return {
        "schema": SCHEMA_NAME,
        "schema_version": SCHEMA_VERSION,
        "exported_at": now_iso(),
        "count": len(records),
        "memories": [r.to_dict() for r in records],
    }


def export_memories(records: List[MemoryRecord], path: Path | str) -> bool:
    """Write a snapshot of memories to ``path`` as a versioned envelope. Returns False on
    any failure (logged), never raises."""
    try:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_envelope(list(records)), f, ensure_ascii=False, indent=2)
        import os

        os.replace(tmp, target)
        logger.info("Exported %d thread memories to %s", len(records), target)
        return True
    except (OSError, TypeError, ValueError):
        logger.exception("Thread export failed to %s", path)
        return False


def validate_thread_export(path: Path | str) -> bool:
    """Validate an export's envelope + record schema WITHOUT importing. Returns True/False
    (never raises)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False
        if data.get("schema") != SCHEMA_NAME:
            return False
        if not isinstance(data.get("schema_version"), int) or data.get("schema_version") < 1:
            return False
        if not data.get("exported_at"):
            return False
        memories = data.get("memories")
        if not isinstance(memories, list):
            return False
        for entry in memories:
            if not isinstance(entry, dict):
                return False
            MemoryRecord.from_dict(entry)  # raises ValueError/KeyError on bad schema
        return True
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        logger.exception("Thread export validation failed for %s", path)
        return False


def import_thread(path: Path | str, store) -> Dict[str, int]:
    """Import memories from a validated envelope into ``store``.

    Conflict policy (hard requirement): existing ids WIN — the import never overwrites a
    record already in the store. New ids are added. Malformed records are skipped and
    counted. Returns ``{imported, conflicts, skipped_invalid, error}`` and never raises.
    """
    result = {"imported": 0, "conflicts": 0, "skipped_invalid": 0}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or data.get("schema") != SCHEMA_NAME:
            raise ValueError("not a thread-memory export")
        memories = data.get("memories")
        if not isinstance(memories, list):
            raise ValueError("export has no memories list")

        for entry in memories:
            try:
                rec = MemoryRecord.from_dict(entry)
            except (ValueError, TypeError, KeyError):
                result["skipped_invalid"] += 1
                continue
            if store.get(rec.id) is not None:
                result["conflicts"] += 1  # existing id wins — never overwrite
                continue
            if store.put(rec):
                result["imported"] += 1
            else:
                result["skipped_invalid"] += 1
        logger.info("Thread import: %d imported, %d conflicts, %d invalid",
                    result["imported"], result["conflicts"], result["skipped_invalid"])
        return result
    except Exception:  # noqa: BLE001
        logger.exception("Thread import failed for %s", path)
        return {**result, "error": True}


__all__ = ["export_memories", "import_thread", "validate_thread_export", "SCHEMA_NAME", "SCHEMA_VERSION"]