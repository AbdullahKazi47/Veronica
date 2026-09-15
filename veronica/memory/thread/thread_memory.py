"""
Thread Memory V1 — the main API facade.

``ThreadMemory`` is the small, clean surface the REST of VERONICA interacts with.
The orchestrator, context engine, and future cognitive layers call these methods and
NEVER see vectors, backend types, or embedding internals.

    remember(...)      persist one memory record (auto-sanitized + auto-embedded)
    recall(...)        fetch one record by id
    get(...)           alias of recall
    update(...)        mutate a record's content/metadata (re-embeds)
    forget(...)        soft-delete a record
    search(...)        keyword-ish substring search (always available, no vectors)
    semantic_search(...)  vector retrieval via ThreadRetriever (degrades to search)
    create_episode(...)    persist a structured episode → one-or-more memory records
    consolidate(...)   run deterministic consolidation over stored records
    export(...) / import(...)  portable JSON backup
    health_check(...)  backend health, never crashes

FAILURE ISOLATION (hard requirement):
  - If the vector store fails, embedding fails, the file is corrupted, retrieval fails,
    or consolidation fails — VERONICA keeps operating with degraded memory. Every public
    method catches, logs, and returns a safe empty result.
  - Persistence is intentional: nothing raw is written; every record is sanitized and
    redacted first (see ``privacy``).
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from veronica.config import MemoryThreadConfig
from veronica.logging_setup import get_logger
from veronica.memory.thread.consolidation import MemoryConsolidator
from veronica.memory.thread.embeddings import EmbeddingProvider, make_embedding_provider
from veronica.memory.thread.models import Episode, MemoryRecord, MemoryType
from veronica.memory.thread.privacy import sanitize_memory_record
from veronica.memory.thread.retrieval import RetrievalConfig, RetrievalResult, ThreadRetriever, RetrievalQuery
from veronica.memory.thread.serialization import import_thread, validate_thread_export
from veronica.memory.thread.store import MemoryStore
from veronica.memory.thread.vector_store import VectorStore, create_vector_store

logger = get_logger("memory.thread")


class ThreadMemory:
    """The single public entry point for Thread Memory V1."""

    def __init__(
        self,
        store: Optional[MemoryStore] = None,
        vector_store: Optional[VectorStore] = None,
        embedding_provider: Optional[EmbeddingProvider] = None,
        retriever: Optional[ThreadRetriever] = None,
        consolidator: Optional[MemoryConsolidator] = None,
        config: Optional[MemoryThreadConfig] = None,
        *,
        auto_consolidate: bool = False,
    ) -> None:
        self.config = config or MemoryThreadConfig()
        self._lock = threading.Lock()
        self._store = store or MemoryStore(self.config.records_path)
        # Vector stack is optional by design: when it is unavailable Thread degrades to
        # keyword search instead of crashing.
        self._embedding = embedding_provider
        self._vector = vector_store
        self._retriever = retriever or ThreadRetriever(
            store=self._store,
            vector_store=self._vector,
            embedding_provider=self._embedding,
            config=_retrieval_config(self.config),
        )
        self._consolidator = consolidator or MemoryConsolidator(store=self._store)
        self._auto_consolidate = auto_consolidate

    # -- construction helpers ------------------------------------------------------
    @classmethod
    def build(
        cls,
        config: Optional[MemoryThreadConfig] = None,
        *,
        records_path: Optional[Path | str] = None,
        vector_path: Optional[Path | str] = None,
        vector_backend: str = "local",
        embedding_backend: str = "local",
        auto_consolidate: bool = False,
        _vector_store: Optional[VectorStore] = None,
        _embedding: Optional[EmbeddingProvider] = None,
    ) -> "ThreadMemory":
        """Construct a ThreadMemory from configuration, isolating the vector factory here
        so callers (and the orchestrator) never construct backends themselves.

        ``_vector_store`` / ``_embedding`` are test seams; production uses the factory."""
        cfg = config or MemoryThreadConfig()
        if records_path is not None:
            cfg.records_path = str(records_path)
        if vector_path is not None:
            cfg.vector_path = str(vector_path)

        embedding = _embedding or make_embedding_provider(embedding_backend)
        vector = _vector_store or create_vector_store(
            vector_backend, path=cfg.vector_path, embedding_provider=embedding,
        )
        return cls(
            config=cfg,
            embedding_provider=embedding,
            vector_store=vector,
            auto_consolidate=auto_consolidate,
        )

    # -- remember -------------------------------------------------------------------
    def remember(
        self,
        content: str,
        memory_type: str = MemoryType.CONVERSATION,
        *,
        importance: float = 0.5,
        confidence: float = 1.0,
        provenance: str = "",
        source: str = "",
        tags: Optional[Sequence[str]] = None,
        project: str = "",
        chapter: str = "",
        related_memory_ids: Optional[Sequence[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[MemoryRecord]:
        """Persist one memory. Returns the stored (sanitized) record, or None on failure."""
        record = MemoryRecord(
            content=content,
            memory_type=memory_type,
            importance=float(importance),
            confidence=float(confidence),
            provenance=provenance,
            source=source,
            tags=list(tags or []),
            project=project,
            chapter=chapter,
            related_memory_ids=list(related_memory_ids or []),
            metadata=dict(metadata or {}),
        )
        return self._store_record(record)

    def create_episode(
        self,
        episode: Episode,
        *,
        importance: float = 0.6,
        confidence: float = 0.7,
    ) -> list[MemoryRecord]:
        """Persist a structured episode. Returns the stored records (possibly several:
        the EXPERIENCE record + a PROCEDURE record for verified success paths)."""
        try:
            proposed = episode.to_memory_records(importance=importance, confidence=confidence)
            stored: list[MemoryRecord] = []
            for rec in proposed:
                result = self._store_record(rec)   # sanitizes + redacts inside
                if result is not None:
                    stored.append(result)
            return stored
        except Exception:  # noqa: BLE001
            logger.exception("create_episode failed; memory is degraded but the caller may continue.")
            return []

    def _store_record(self, record: MemoryRecord) -> Optional[MemoryRecord]:
        """Shared write path: sanitize → persist → (best-effort) embed → link vector."""
        try:
            safe = sanitize_memory_record(record)
            if not self._store.put(safe):
                return None
            self._embed_and_link(safe)
            if self._auto_consolidate:
                self.consolidate()
            logger.info("memory stored type=%s id=%s provenance=%s",
                        safe.memory_type, safe.id, safe.provenance or "-")
            return safe
        except Exception:  # noqa: BLE001
            logger.exception("ThreadMemory remember failed; memory write is degraded.")
            return None

    def _embed_and_link(self, record: MemoryRecord) -> None:
        """Best-effort: compute the vector for an embedded-index candidate and link it.
        Failure here must not undo persistence — the record stays in the JSON store and
        remains reachable via keyword search."""
        if self._vector is None or self._embedding is None:
            return
        try:
            vec = self._embedding.embed(record.content)
            self._vector.add(
                record.id,
                vec,
                metadata={"memory_id": record.id, "type": record.memory_type,
                          "importance": record.importance, "ts": record.timestamp},
            )
            record.embedding_ref = record.id
            self._store.put(record)  # persist the ref; failure is non-fatal (logged inside)
        except Exception:  # noqa: BLE001
            logger.exception("Embedding/linking failed for memory id=%s; keyword search still works.", record.id)

    # -- read -----------------------------------------------------------------------
    def recall(self, memory_id: str) -> Optional[MemoryRecord]:
        return self._store.get(memory_id)

    def get(self, memory_id: str) -> Optional[MemoryRecord]:
        return self.recall(memory_id)

    def update(
        self,
        memory_id: str,
        *,
        content: Optional[str] = None,
        importance: Optional[float] = None,
        confidence: Optional[float] = None,
        tags: Optional[Sequence[str]] = None,
        project: Optional[str] = None,
        chapter: Optional[str] = None,
        related_memory_ids: Optional[Sequence[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        provenance: Optional[str] = None,
    ) -> Optional[MemoryRecord]:
        """Mutate a record in place (safely) and re-persist. Returns the updated record
        or None if it does not exist / the update failed."""
        rec = self._store.get(memory_id)
        if rec is None:
            return None
        try:
            if content is not None:
                rec.content = str(content)
            if importance is not None:
                rec.importance = float(importance)
            if confidence is not None:
                rec.confidence = float(confidence)
            if tags is not None:
                rec.tags = list(tags)
            if project is not None:
                rec.project = str(project)
            if chapter is not None:
                rec.chapter = str(chapter)
            if related_memory_ids is not None:
                rec.related_memory_ids = list(related_memory_ids)
            if metadata is not None:
                rec.metadata.update(metadata)
            if provenance is not None:
                rec.provenance = str(provenance)
            rec.touch()
            safe = sanitize_memory_record(rec)
            if not self._store.put(safe):
                return None
            self._embed_and_link(safe)
            return safe
        except Exception:  # noqa: BLE001
            logger.exception("ThreadMemory update failed for memory id=%s", memory_id)
            return None

    def forget(self, memory_id: str, *, hard: bool = False) -> bool:
        """Soft-delete by default (record marked ``deleted``, kept for audit). Hard delete
        removes record + vector. Returns False if the id did not exist."""
        rec = self._store.get(memory_id)
        if rec is None:
            return False
        if hard:
            self._store.delete(memory_id)
            if self._vector is not None:
                self._vector.delete(memory_id)
            return True
        rec.deleted = True
        rec.touch()
        return bool(self._store.put(rec))

    # -- retrieval ------------------------------------------------------------------
    def search(self, query: str, *, memory_type: str = "", limit: int = 10,
               project: str = "", tag: str = "") -> List[MemoryRecord]:
        """Substring search — always available even when the vector backend is down."""
        return self._store.search(
            query_str=query, memory_type=memory_type, limit=limit,
            project=project, tag=tag,
        )

    def semantic_search(self, query: str, *, top_k: int = 5, memory_type: str = "") -> List[MemoryRecord]:
        """Vector-aware retrieval via ``ThreadRetriever``; degrades to keyword search when
        the vector stack is unavailable."""
        try:
            results = self._retriever.retrieve_memories(
                RetrievalQuery(query=query, memory_type=memory_type, top_k=top_k)
            )
            return results.memories
        except Exception:  # noqa: BLE001
            logger.exception("Semantic search failed; falling back to keyword search.")
            return self.search(query, memory_type=memory_type, limit=top_k)

    def retrieve(self, query: RetrievalQuery) -> RetrievalResult:
        """Full retriever access for the context engine (weights, filters, ranking).
        Isolates any failure so the context engine always has a result object."""
        try:
            return self._retriever.retrieve_memories(query)
        except Exception:  # noqa: BLE001
            logger.exception("Thread retrieval failed; returning empty result.")
            return self._retriever.empty_result(query)

    # -- consolidation ---------------------------------------------------------------
    def consolidate(self) -> dict:
        """Run deterministic consolidation. Returns a report dict; never raises."""
        try:
            return self._consolidator.consolidate()
        except Exception:  # noqa: BLE001
            logger.exception("Memory consolidation failed; memory state is unchanged.")
            return {"merged": 0, "linked": 0, "degraded": 0, "error": True}

    # -- import / export ---------------------------------------------------------------
    def export(self, path: Path | str) -> bool:
        """Export all (non-deleted) memories to portable JSON. Returns False (logged) on failure."""
        try:
            from veronica.memory.thread.serialization import export_memories
            return export_memories(self._store.all(include_deleted=False), path)
        except Exception:  # noqa: BLE001
            logger.exception("Thread export failed")
            return False

    def import_records(self, path: Path | str) -> dict:
        """Import memories from a JSON backup. Reports per-record conflicts; never
        overwrites an existing record silently (existing ids win). Returns a result dict."""
        return import_thread(path, self._store)

    def validate_export(self, path: Path | str) -> bool:
        """Validate a backup file's schema without importing it."""
        return validate_thread_export(path)

    # -- introspection ----------------------------------------------------------------
    def count(self) -> int:
        return self._store.count()

    def list_memories(self, *, memory_type: str = "", limit: int = 50,
                      include_deleted: bool = False) -> List[MemoryRecord]:
        return self._store.all(include_deleted=include_deleted, memory_type=memory_type, limit=limit)

    def health_check(self) -> dict:
        """Multi-part health report. Never raises; each backend reports independently."""
        report = {
            "store": self._store.health_check(),
            "records": self._store.count(),
        }
        if self._vector is not None:
            try:
                report["vector_store"] = self._vector.health_check()
                report["vector_rows"] = self._vector.count()
            except Exception:  # noqa: BLE001
                report["vector_store"] = False
        else:
            report["vector_store"] = None
        if self._embedding is not None:
            report["embedding_provider"] = getattr(self._embedding, "name", "?")
            report["embedding_available"] = self._embedding.is_available()
        return report


def _retrieval_config(config: MemoryThreadConfig) -> RetrievalConfig:
    return RetrievalConfig(
        top_k=config.max_context_memories,
        score_threshold=config.retrieval_threshold,
    )


__all__ = ["ThreadMemory"]