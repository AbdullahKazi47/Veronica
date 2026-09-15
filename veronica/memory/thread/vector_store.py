"""
Thread Memory V1 — vector-store abstraction and a zero-dependency local implementation.

LAYER BOUNDARY (mandated by the architecture):

    Memory API  →  VectorStore (interface)  →  LocalVectorStore  →  chosen backend

VERONICA's higher layers (ThreadMemory, ThreadRetriever, orchestrator) import ONLY
``VectorStore`` and the factory. No ChromaDB/future-backend type ever leaks upward. This
is what allows replacing ChromaDB with FAISS / Qdrant / pgvector / a cloud DB without
rewriting the brain.

V1 ships ``LocalVectorStore``: a pure-Python, persistent, in-memory vector store.
Because no ML stack is installed on this machine (no numpy, no chromadb), it stores
(embedding list, metadata) rows in JSONL and computes cosine similarity with pure Python.
The contract is identical to the future ChromaDB-backed store, so swapping is a factory
change only.

Failure isolation (hard requirement):
  - Every public method is defensive: a failure logs and returns a safe result (None /
    False / []) rather than raising into the assistant.
  - If the store file is corrupted, the reader skips bad lines and keeps the rest.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from veronica.logging_setup import get_logger
from veronica.memory.thread.embeddings import EmbeddingProvider

logger = get_logger("memory.thread.vector_store")

# A vector with all zeros + this magnitude is treated as "no embedding" (avoid divide-0).
_EPS = 1e-9


class _Row:
    """Internal storage row: vector + arbitrary metadata blob (memory id + text)."""

    __slots__ = ("vector", "metadata")

    def __init__(self, vector: Sequence[float], metadata: Dict[str, Any]) -> None:
        self.vector: List[float] = list(vector)
        self.metadata: Dict[str, Any] = dict(metadata or {})

    def to_dict(self) -> dict:
        return {"metadata": self.metadata, "vector": self.vector}

    @classmethod
    def from_dict(cls, d: dict) -> "_Row":
        return cls(vector=list(d.get("vector", []) or []), metadata=dict(d.get("metadata", {}) or {}))


class VectorStore:
    """Interface for the vector backend. VERONICA depends on this, never on a backend.

    Operations mirror the conceptual vector API: add/update/delete/get/search/count/clear/
    persist/health_check. Backend-specific types never cross this boundary — ids and
    metadata are plain str/dict.
    """

    name: str = "base"

    # -- mutation ---------------------------------------------------------------
    def add(self, uid: str, vector: Sequence[float], metadata: Optional[Dict[str, Any]] = None) -> bool:
        raise NotImplementedError

    def update(self, uid: str, vector: Sequence[float], metadata: Optional[Dict[str, Any]] = None) -> bool:
        raise NotImplementedError

    def delete(self, uid: str) -> bool:
        raise NotImplementedError

    # -- reads -------------------------------------------------------------------
    def get(self, uid: str) -> Optional[Dict[str, Any]]:
        """Return the document blob ``{metadata, vector}`` or None."""
        raise NotImplementedError

    def search(self, query_vector: Sequence[float], top_k: int = 10) -> List[Tuple[str, float, Dict[str, Any]]]:
        """Return ``[(id, similarity, metadata)]`` sorted best-first."""
        raise NotImplementedError

    def count(self) -> int:
        raise NotImplementedError

    def ids(self) -> List[str]:
        raise NotImplementedError

    # -- lifecycle ----------------------------------------------------------------
    def clear(self) -> bool:
        raise NotImplementedError

    def persist(self) -> bool:
        """Flush in-memory changes to durable storage. Returns False (logged) on failure."""
        raise NotImplementedError

    def health_check(self) -> bool:
        raise NotImplementedError


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Pure-Python cosine similarity over flat float lists."""
    if not a or not b:
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= _EPS or nb <= _EPS:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


class LocalVectorStore(VectorStore):
    """Persistent in-memory vector store over JSONL. Zero dependencies.

    Rows are held in a dict {uid: _Row}. ``persist()`` / construction rewrites the whole
    file (V1-friendly; tiny datasets). A partial/corrupt tail line is skipped, never fatal.
    """

    name = "local"

    def __init__(self, path: Path | str, embedding_provider: Optional[EmbeddingProvider] = None) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._embedding = embedding_provider
        self._rows: Dict[str, _Row] = {}
        self._load()

    # -- persistence --------------------------------------------------------------
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
                        blob = json.loads(line)
                        uid = blob.get("id")
                        if not uid:
                            continue
                        row = _Row.from_dict(blob)
                        self._rows[str(uid)] = row
                    except (ValueError, TypeError):
                        logger.warning("Skipping a corrupted vector-store line in %s", self.path)
        except OSError:
            logger.exception("Failed to read vector store at %s; starting empty.", self.path)

    def persist(self) -> bool:
        try:
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(self.path.suffix + ".tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    for uid, row in self._rows.items():
                        f.write(json.dumps({"id": uid, **row.to_dict()}, ensure_ascii=False) + "\n")
                os.replace(tmp, self.path)
            logger.debug("Vector store persisted: %d rows", len(self._rows))
            return True
        except OSError:
            logger.exception("Failed to persist vector store at %s", self.path)
            return False

    # -- mutation ---------------------------------------------------------------
    # Every mutation is durable immediately: the row changes in memory and the JSONL is
    # rewritten (same contract as MemoryStore.put). Failure to persist is logged and
    # reported as False — but never raises into the caller.
    def add(self, uid: str, vector: Sequence[float], metadata: Optional[Dict[str, Any]] = None) -> bool:
        try:
            with self._lock:
                self._rows[uid] = _Row(vector=list(vector), metadata=dict(metadata or {}))
            return self.persist()
        except Exception:  # noqa: BLE001
            logger.exception("VectorStore.add failed for uid=%s", uid)
            return False

    def update(self, uid: str, vector: Sequence[float], metadata: Optional[Dict[str, Any]] = None) -> bool:
        try:
            with self._lock:
                if uid not in self._rows:
                    return False
                self._rows[uid] = _Row(vector=list(vector), metadata=dict(metadata or {}))
            return self.persist()
        except Exception:  # noqa: BLE001
            logger.exception("VectorStore.update failed for uid=%s", uid)
            return False

    def delete(self, uid: str) -> bool:
        try:
            with self._lock:
                removed = self._rows.pop(uid, None) is not None
            if removed:
                return self.persist()
            return False
        except Exception:  # noqa: BLE001
            logger.exception("VectorStore.delete failed for uid=%s", uid)
            return False

    # -- reads -------------------------------------------------------------------
    def get(self, uid: str) -> Optional[Dict[str, Any]]:
        try:
            row = self._rows.get(uid)
            return row.to_dict() if row else None
        except Exception:  # noqa: BLE001
            logger.exception("VectorStore.get failed for uid=%s", uid)
            return None

    def search(self, query_vector: Sequence[float], top_k: int = 10) -> List[Tuple[str, float, Dict[str, Any]]]:
        try:
            with self._lock:
                rows = list(self._rows.items())
            scored = []
            for uid, row in rows:
                sim = _cosine_similarity(query_vector, row.vector)
                if sim > 0.0:
                    scored.append((uid, sim, row.metadata))
            scored.sort(key=lambda t: t[1], reverse=True)
            return scored[: max(0, top_k)]
        except Exception:  # noqa: BLE001
            logger.exception("VectorStore.search failed")
            return []

    def count(self) -> int:
        try:
            with self._lock:
                return len(self._rows)
        except Exception:  # noqa: BLE001
            return 0

    def ids(self) -> List[str]:
        try:
            with self._lock:
                return list(self._rows.keys())
        except Exception:  # noqa: BLE001
            return []

    def clear(self) -> bool:
        try:
            with self._lock:
                self._rows.clear()
            return self.persist()
        except Exception:  # noqa: BLE001
            logger.exception("VectorStore.clear failed")
            return False

    def health_check(self) -> bool:
        """Healthy if the file's parent directory is writable (or absent → creatable)."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return os.access(self.path.parent, os.W_OK)
        except OSError:
            logger.exception("Vector store health check failed")
            return False


def create_vector_store(
    store_type: str = "local",
    *,
    path: Optional[Path | str] = None,
    embedding_provider: Optional[EmbeddingProvider] = None,
) -> VectorStore:
    """Factory. Unknown store types fall back to 'local' and log a warning — config can
    never break the assistant."""
    if store_type == "local":
        return LocalVectorStore(path=path or Path("data/thread_vectors.jsonl"),
                                embedding_provider=embedding_provider)
    logger.warning("Unknown vector store %r; falling back to 'local'.", store_type)
    return LocalVectorStore(path=path or Path("data/thread_vectors.jsonl"),
                            embedding_provider=embedding_provider)


__all__ = ["VectorStore", "LocalVectorStore", "create_vector_store"]