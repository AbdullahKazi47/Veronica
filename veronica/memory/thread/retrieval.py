"""
Thread Memory V1 — retrieval layer.

``ThreadRetriever`` combines vector similarity with structured metadata into a single,
deterministic, weighted ranking:

    score = similarity·w_sim  + importance·w_imp  + confidence·w_conf  + recency·w_rec  + context_bonus

Weights live in ``RetrievalConfig`` (typed, replaceable) and the formula lives in code —
there is no ML ranking in V1. Filters (memory_type / project / chapter / tags /
time_window / exclude) are applied BEFORE scoring so the assistant never fetches memories
outside the requested scope.

Failure isolation (hard requirement): if embedding or the vector store raises, retrieval
degrades to keyword search and returns a result with ``vector_available=False`` — it never
raises into the caller and never crashes the assistant.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Sequence

from veronica.logging_setup import get_logger
from veronica.memory.thread.embeddings import EmbeddingProvider
from veronica.memory.thread.models import MemoryRecord
from veronica.memory.thread.store import MemoryStore
from veronica.memory.thread.vector_store import VectorStore

logger = get_logger("memory.thread.retrieval")

# Deterministic ranking weights. Sum ≈ 1.0 so the final score is scaled by *relevance*
# (see ``_score``) and never exceeds ~1.0.
_DEFAULT_WEIGHTS = {
    "similarity": 0.50,
    "importance": 0.15,
    "confidence": 0.10,
    "recency": 0.20,
    "context_bonus": 0.05,
}


@dataclass
class RetrievalConfig:
    """Retrieval tuning. ``weights`` keys are the five factors above; the final score is

        relevance * (w_sim + w_imp·importance + w_conf·confidence + w_rec·recency + w_ctx·context)

    where ``relevance = max(vector_similarity, keyword_overlap)``. A record with ZERO
    relevance (no shared meaning, no shared token) scores exactly 0 and cannot be carried
    in by importance/confidence/recency — this is what keeps the context engine injecting
    only relevant memories."""

    top_k: int = 5
    score_threshold: float = 0.20
    max_age_days: int = 365
    weights: Dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))

    def effective_weights(self) -> Dict[str, float]:
        return {k: float(self.weights.get(k, v)) for k, v in _DEFAULT_WEIGHTS.items()}


@dataclass
class RetrievalQuery:
    query: str = ""
    memory_type: str = ""
    project: str = ""
    chapter: str = ""
    tags: Sequence[str] = ()
    top_k: int = 5
    exclude_ids: Sequence[str] = ()
    time_window_days: Optional[int] = None


@dataclass
class RetrievalResult:
    query: RetrievalQuery
    memories: List[MemoryRecord] = field(default_factory=list)
    scores: List[float] = field(default_factory=list)
    ranking_notes: List[str] = field(default_factory=list)
    vector_available: bool = False
    duration_sec: float = 0.0
    error: Optional[str] = None


def _parse_iso(ts: str) -> Optional[datetime]:
    """Parse ISO timestamps produced by ``now_iso``, tolerant of a missing TZ suffix."""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (ValueError, TypeError, AttributeError):
        return None


class ThreadRetriever:
    """Deterministic weighted retrieval.

    ``store``        — MUST be present (JSON catalog; keyword fallback and filters).
    ``vector_store`` — optional; drives the similarity factor. Missing ⇒ keyword only.
    ``provider``     — the embedding provider used for the query's own embedding.
    """

    def __init__(
        self,
        store: MemoryStore,
        vector_store: Optional[VectorStore] = None,
        embedding_provider: Optional[EmbeddingProvider] = None,
        config: Optional[RetrievalConfig] = None,
    ) -> None:
        self.store = store
        self.vector = vector_store
        self.embedding = embedding_provider
        self.config = config or RetrievalConfig()
        self._lock = threading.Lock()

    # -- public -------------------------------------------------------------------
    def retrieve_memories(self, query: RetrievalQuery) -> RetrievalResult:
        start = time.time()
        try:
            # Structural filters FIRST (type/project/chapter/tags/time/exclude).
            pool = self._pool(query)
            # Query embedding, best-effort.
            sim_ids: Dict[str, float] = {}
            vector_available = False
            if self.vector is not None and self.embedding is not None and query.query.strip():
                try:
                    vec = self.embedding.embed(query.query)
                    for uid, sim, _meta in self.vector.search(vec, top_k=max(0, query.top_k) * 4):
                        sim_ids[uid] = sim
                    vector_available = True
                except Exception:  # noqa: BLE001
                    logger.exception("Embedding/search failed; keyword-only ranking used.")
                    vector_available = False

            candidates = self._select_candidates(pool, query, sim_ids)
            if not candidates:
                return self._result(query, [], [], ["no_candidates"], vector_available, start)

            ranked = []
            for rec in candidates:
                score, factors = self._score(rec, query, sim_ids.get(rec.id, 0.0), time.time())
                if score >= self.config.score_threshold:
                    ranked.append((score, factors, rec))
            # Stable sort: score (desc), then created_at (desc) for ties.
            ranked.sort(key=lambda t: (t[0], t[2].created_at), reverse=True)
            top = ranked[: max(0, query.top_k)]

            memories = [rec for _, _, rec in top]
            scores = [s for s, _, _ in top]
            notes = self._notes(len(sim_ids), len(top), top, query)
            return self._result(query, memories, scores, notes, vector_available, start)
        except Exception:  # noqa: BLE001
            logger.exception("Retrieval failed; returning keyword fallback.")
            fallback = self.store.search(query.query, limit=max(0, query.top_k))
            return self._result(
                query, fallback, [0.0] * len(fallback),
                ["retrieval_error_fallback"], False, start, error="retrieval_failed",
            )

    def empty_result(self, query: RetrievalQuery) -> RetrievalResult:
        return self._result(query, [], [], ["empty_result"], False, time.time())

    # -- internals ----------------------------------------------------------------
    def _result(self, query, memories, scores, notes, vector_available, start, error=None) -> RetrievalResult:
        return RetrievalResult(
            query=query,
            memories=memories,
            scores=scores,
            ranking_notes=notes,
            vector_available=vector_available,
            duration_sec=time.time() - start,
            error=error,
        )

    def _pool(self, query: RetrievalQuery) -> List[MemoryRecord]:
        """Apply STRUCTURAL filters only (type/project/chapter/tags/time/exclude).
        Text selection happens later so vector search is not starved by a strict
        all-or-nothing substring. Never raises."""
        with self._lock:
            snapshot = self.store.all(include_deleted=False)
        q = (query.query or "").strip().lower()
        out = []
        for rec in snapshot:
            if query.memory_type and rec.memory_type != query.memory_type:
                continue
            if query.project and rec.project != query.project:
                continue
            if query.chapter and rec.chapter != query.chapter:
                continue
            if query.tags and not any(tag in rec.tags for tag in query.tags):
                continue
            if query.exclude_ids and rec.id in query.exclude_ids:
                continue
            if query.time_window_days is not None:
                created = _parse_iso(rec.created_at)
                if created is not None and created < datetime.now() - timedelta(days=int(query.time_window_days)):
                    continue
            out.append(rec)
        return out

    def _select_candidates(
        self,
        pool: List[MemoryRecord],
        query: RetrievalQuery,
        sim_ids: Dict[str, float],
    ) -> List[MemoryRecord]:
        """Choose which records get scored:
          - vector hits first (semantic),
          - then loose token-overlap keyword matches (works offline / for fuzzy text),
          - finally, when NOTHING matches textually, the whole filtered pool (so
            project/type-filtered recall still works for empty queries).
        """
        q = (query.query or "").strip().lower()
        if not q:
            return list(pool)
        if sim_ids:
            hits = [rec for rec in pool if rec.id in sim_ids]
            if len(hits) >= max(0, query.top_k):
                return hits
            extras = [rec for rec in pool
                      if rec.id not in sim_ids and self._keyword_match(rec, query)]
            return (hits + extras)[: max(0, query.top_k) * 4]
        hits = [rec for rec in pool if self._keyword_match(rec, query)]
        return hits[: max(0, query.top_k) * 4]

    def _keyword_match(self, rec: MemoryRecord, query: RetrievalQuery) -> bool:
        tokens = str(query.query or "").lower().split()
        if not tokens:
            return True
        hay = (" ".join([rec.content, " ".join(rec.tags), rec.project, rec.chapter])).lower()
        return any(tok in hay for tok in tokens)

    def _score(
        self,
        rec: MemoryRecord,
        query: RetrievalQuery,
        vector_sim: float,
        now_ts: float,
    ) -> tuple:
        w = self.config.effective_weights()
        vector_sim = max(0.0, min(float(vector_sim), 1.0))
        keyword_overlap = self._keyword_overlap(rec, query)
        # A record only earns points when it is actually relevant to the query.
        relevance = max(vector_sim, keyword_overlap)
        if relevance <= 0.0:
            return 0.0, {"similarity": 0.0, "keyword_overlap": 0.0,
                         "importance": 0.0, "confidence": 0.0, "recency": 0.0,
                         "context_bonus": 0.0, "relevance": 0.0}

        importance = max(0.0, min(float(rec.importance), 1.0))
        confidence = max(0.0, min(float(rec.confidence), 1.0))
        # Recency: 1.0 for today, linear decay to ~0 at max_age_days.
        recency = 0.0
        created = _parse_iso(rec.created_at)
        if created is not None:
            age_days = max(0.0, (datetime.fromtimestamp(now_ts) - created).total_seconds() / 86400.0)
            recency = max(0.0, 1.0 - age_days / max(1.0, float(self.config.max_age_days)))
        # Context bonus: current project / chapter / tags give a small in-topic boost.
        bonus = 0.0
        if query.project and rec.project == query.project:
            bonus += 0.3
        if query.chapter and rec.chapter == query.chapter:
            bonus += 0.15
        bonus += 0.05 * sum(1 for t in query.tags if t in rec.tags)

        factor_sum = (
            w.get("similarity", 0.5)
            + w.get("importance", 0.15) * importance
            + w.get("confidence", 0.1) * confidence
            + w.get("recency", 0.2) * recency
            + w.get("context_bonus", 0.05) * bonus
        )
        score = relevance * factor_sum
        factors = {
            "similarity": vector_sim,
            "keyword_overlap": keyword_overlap,
            "relevance": relevance,
            "importance": importance,
            "confidence": confidence,
            "recency": recency,
            "context_bonus": bonus,
        }
        return score, factors

    def _keyword_overlap(self, rec: MemoryRecord, query: RetrievalQuery) -> float:
        """Fraction of query tokens present (as whole tokens) in the record's searchable
        text (content + tags + project + chapter). Deterministic lexical relevance."""
        tokens = [tok for tok in str(query.query or "").lower().split() if tok]
        if not tokens:
            return 0.0
        hay = (" ".join([rec.content, " ".join(rec.tags), rec.project, rec.chapter])).lower()
        hay_words = set(hay.split())
        found = sum(1 for tok in tokens if tok in hay_words)
        return found / len(tokens)

    def _notes(self, n_vector, n_retrieved, top, query) -> List[str]:
        notes = [
            "vector_active" if n_vector else "vector_unavailable",
            f"retrieved={n_retrieved}",
            f"threshold={self.config.score_threshold}",
        ]
        if top and top[0][0] >= 0:
            notes.append(f"top_score={round(top[0][0], 3)}")
        if query.memory_type:
            notes.append(f"type_filter={query.memory_type}")
        if query.project:
            notes.append(f"project_filter={query.project}")
        return notes


__all__ = ["RetrievalConfig", "RetrievalQuery", "RetrievalResult", "ThreadRetriever"]