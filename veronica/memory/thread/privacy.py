"""
Thread Memory V1 — secret redaction for memory records.

Memory is part of VERONICA's security boundary. Before ANY memory record reaches the
persistent store we sanitize it: dict keys that look like secrets and values that look
like secrets become ``[REDACTED]``.

The pattern here deliberately reuses the vocabulary from
``veronica.learning.experience`` (the project's canonical secret vocabulary) rather than
inventing a second one, and applies it to the memory-specific surfaces: record ``content``,
``metadata``, ``tags``, and ``source``.

Rules:
  - ``content`` that looks like a standalone secret value → replaced with stub text.
  - ``metadata`` is recursively redacted (same value tree logic as Experience).
  - ``provenance`` / ``source`` / ``project`` / ``chapter`` are identifier-ish strings;
    they are redacted only if they *are* a secret value.
  - ``related_memory_ids`` / ``embedding_ref`` are opaque ids — never redacted.
"""
from __future__ import annotations

from typing import Any

from veronica.learning.experience import (
    REDACTED,
    redact_value,
    _looks_like_secret_value,
)

from veronica.memory.thread.models import MemoryRecord


def _redacted_content_signature() -> str:
    return f"[REDACTED — content removed for privacy]"


def sanitize_text(value: str) -> str:
    """Return ``value`` unchanged, or the redaction stub if it looks like a secret."""
    if not value:
        return value
    return _redacted_content_signature() if _looks_like_secret_value(value) else value


def sanitize_memory_record(record: MemoryRecord) -> MemoryRecord:
    """Return a sanitized *copy* of ``record`` safe to persist. Never mutates input.

    The copy keeps every field; only secret-looking content is masked. id, timestamps,
    ids, and embedding_ref survive untouched so the record stays addressable and its
    provenance history is intact."""
    sanitized = MemoryRecord(
        content=sanitize_text(record.content),
        memory_type=record.memory_type,
        id=record.id,
        timestamp=record.timestamp,
        created_at=record.created_at,
        updated_at=record.updated_at,
        importance=record.importance,
        confidence=record.confidence,
        provenance=sanitize_text(record.provenance),
        source=sanitize_text(record.source),
        tags=[sanitize_text(t) for t in record.tags],
        project=sanitize_text(record.project),
        chapter=sanitize_text(record.chapter),
        related_memory_ids=list(record.related_memory_ids),
        metadata=redact_value(record.metadata),              # recursive: covers nested dicts/lists
        embedding_ref=record.embedding_ref,
        deleted=record.deleted,
    )
    # Trim redacted tags so the vocabulary stays clean: a tag that was a secret (e.g.
    # an API key pasted as a tag) becomes an empty marker, not a stored secret.
    sanitized.tags = [t for t in sanitized.tags if t != REDACTED]
    return sanitized


def sanitize_episode_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Recursively redact a raw episode's metadata before it becomes memory metadata."""
    return redact_value(metadata)


__all__ = ["sanitize_memory_record", "sanitize_episode_metadata", "sanitize_text"]