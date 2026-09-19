"""Deterministic priority ordering and relevance scoring for context items.

No ML, no second embedding system: priority and relevance are pure functions of data the
manager already holds (item source/importance/confidence/similarity/age). This mirrors
the ``ThreadRetriever`` weighting philosophy — deterministic, replaceable weights.
"""
from __future__ import annotations

import time
from typing import Iterable

from veronica.core.context_manager.models import (
    PRI_CONVERSATION_RECENT, PRI_CURRENT_USER, PRI_HARD_SAFETY, PRI_OBSERVATION,
    PRI_OPTIONAL, PRI_RELEVANT_MEMORY, PRI_SYSTEM, PRI_TASK, PRI_TOOLS, ContextItem,
    SOURCE_CONVERSATION, SOURCE_EXPERIENCE, SOURCE_MEMORY, SOURCE_OBSERVATION,
    SOURCE_OPTIONAL, SOURCE_SAFETY, SOURCE_SYSTEM, SOURCE_TASK, SOURCE_TOOLS,
    SOURCE_USER,
)

# Source -> default priority rank (overridable per-item at construction).
_SOURCE_PRIORITY = {
    SOURCE_SAFETY: PRI_HARD_SAFETY,
    SOURCE_SYSTEM: PRI_SYSTEM,
    SOURCE_USER: PRI_CURRENT_USER,
    SOURCE_TASK: PRI_TASK,
    SOURCE_TOOLS: PRI_TOOLS,
    SOURCE_OBSERVATION: PRI_OBSERVATION,
    SOURCE_MEMORY: PRI_RELEVANT_MEMORY,
    SOURCE_EXPERIENCE: PRI_RELEVANT_MEMORY,
    SOURCE_CONVERSATION: PRI_CONVERSATION_RECENT,
    SOURCE_OPTIONAL: PRI_OPTIONAL,
}


def default_priority_for_source(source: str) -> int:
    return _SOURCE_PRIORITY.get(source, PRI_OPTIONAL)


# --------------------------------------------------------------------------- #
# Relevance scoring — deterministic weighted formula (V1).
# --------------------------------------------------------------------------- #
# Weights sum ≈ 1.0. A zero-similarity memory scores 0 and cannot ride in on
# importance/recency alone — only RELEVANT context is injected, keeping the
# "relevant memory > older memory" ordering honest.
_DEFAULT_SCORE_WEIGHTS = {
    "similarity": 0.50,
    "importance": 0.20,
    "confidence": 0.15,
    "recency": 0.15,
}


def score_item(
    similarity: float = 0.0,
    *,
    importance: float = 0.5,
    confidence: float = 1.0,
    timestamp: float = 0.0,
    now: float | None = None,
    max_age_days: float = 365.0,
    weights: dict | None = None,
) -> float:
    """Deterministic relevance in [0, 1]. ``similarity`` is the dominant factor;
    a record the retriever found irrelevant (sim ~ 0) lands near 0 regardless of
    importance/recency/confidence — the same rule that keeps Thread retrieval honest.
    ``now``/``timestamp`` are injectable for deterministic tests."""
    w = _DEFAULT_SCORE_WEIGHTS if weights is None else weights
    now = now if now is not None else time.time()

    w_sim = float(w.get("similarity", 0.50))
    w_imp = float(w.get("importance", 0.20))
    w_conf = float(w.get("confidence", 0.15))
    w_rec = float(w.get("recency", 0.15))

    sim = max(0.0, min(1.0, float(similarity)))
    imp = max(0.0, min(1.0, float(importance)))
    conf = max(0.0, min(1.0, float(confidence)))

    if timestamp > 0:
        age_days = max(0.0, (now - timestamp) / 86400.0)
        recency = max(0.0, 1.0 - (age_days / max(1.0, float(max_age_days))))
    else:
        recency = 0.0

    score = sim * w_sim + imp * w_imp + conf * w_conf + recency * w_rec
    # Gate: relevance must exist. A zero-similarity item is not relevant context.
    if sim <= 0.0:
        score = 0.0
    return round(max(0.0, min(1.0, score)), 4)


def sort_items(items: Iterable[ContextItem]) -> list[ContextItem]:
    """Deterministic sort: priority rank asc, then relevance desc, then timestamp desc,
    then item_id asc (total order, never random)."""
    return sorted(
        items,
        key=lambda it: (
            it.priority_rank,
            -it.relevance,
            -it.timestamp if it.timestamp else 0.0,
            it.item_id,
        ),
    )


__all__ = [
    "default_priority_for_source", "score_item", "sort_items",
    "_SOURCE_PRIORITY", "_DEFAULT_SCORE_WEIGHTS",
]