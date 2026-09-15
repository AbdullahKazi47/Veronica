"""
Experience retrieval for adaptive planning.

Provides relevance-scored retrieval of past experiences to inform new planning decisions.
The scoring algorithm is deterministic, dependency-free, and intentionally simple (weighted
lexical similarity + tool overlap + recency + outcome quality).

Design rationale:
  Relevance weighting: goal similarity is the strongest signal (a near-identical goal is
  the best evidence for what to do next). Tool overlap is second (shared tools suggest a
  similar problem domain). Recency matters but does not dominate — a single recent success
  does not outrank a proven long-term strategy. Outcome quality adjusts rankings so
  recovered strategies receive a modest boost (they demonstrate resilience) while failed
  strategies are downweighted (the user usually prefers what worked).

  No embeddings, no vector DB, no ML: a token-overlap scorer is fast, deterministic,
  testable, and sufficient for a v1 adaptive planner.  The real power comes from the
  planner LLM interpreting the advisory context, not from the retrieval precision itself.

Privacy guarantee:
  Only redacted experiences should be stored and retrieved. This module reads from the
  ExperienceStore, which redacts secrets at write time.  No raw user content or secrets
  are logged or surfaced here.
"""
from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from veronica.learning.experience import Experience, ExperienceOutcome
from veronica.learning.store import ExperienceStore
from veronica.logging_setup import get_logger

logger = get_logger("learning.retrieval")


# --- internal helpers --------------------------------------------------------

def _tokenize(text: str) -> set[str]:
    """Lowercase alphanumeric tokens, stripping punctuation.  Stop-words are NOT removed
    because short goals like 'find README' rely on every word for relevance."""
    if not text:
        return set()
    return set(re.findall(r"[a-z0-9]+", text.lower()))


# Scoring weights (documented, not arbitrary):
#   goal   0.45 — the most direct signal of relevance
#   tool   0.25 — shared tools strongly suggest a similar problem type
#   recency 0.20 — fresh evidence is more applicable
#   outcome 0.10 — quality adjustment, not a primary ranking factor
_W_GOAL = 0.45
_W_TOOL = 0.25
_W_RECENCY = 0.20
_W_OUTCOME = 0.10

# Half-life for recency decay in seconds (7 days).  After 7 days a fresh experience
# scores ~0.5; after 14 days ~0.25.  This keeps the planner responsive to recent work
# without discarding proven long-term strategies.
_RECENCY_HALF_LIFE_DAYS = 7.0
_RECENCY_HALF_LIFE_SEC = _RECENCY_HALF_LIFE_DAYS * 86400.0


def _goal_similarity(new_goal_tokens: set[str], experience: Experience) -> float:
    """Jaccard-ish overlap between the new goal and the stored goal, with a small bonus
    for every new-goal token found in the experience goal (handles the case where the new
    goal is a subset or superset of the old one)."""
    if not new_goal_tokens:
        return 0.0
    exp_tokens = _tokenize(experience.goal)
    if not exp_tokens:
        return 0.0
    intersection = new_goal_tokens & exp_tokens
    union = new_goal_tokens | exp_tokens
    jaccard = len(intersection) / len(union) if union else 0.0
    # Coverage: what fraction of the new-goal tokens are present in the experience goal?
    coverage = len(intersection) / len(new_goal_tokens) if new_goal_tokens else 0.0
    return 0.5 * jaccard + 0.5 * coverage


def _tool_overlap(new_tools: set[str], experience: Experience) -> float:
    """Ratio of shared tools to total unique tools across both sets.  Returns 0.0 when
    neither the new goal nor the experience involve any tools."""
    exp_tools = set(t for t in (experience.tool_calls or []) if t)
    combined = new_tools | exp_tools
    if not combined:
        return 0.0
    return len(new_tools & exp_tools) / len(combined)


def _recency_score(experience: Experience, now: float) -> float:
    """Exponential-decay score based on age.  1.0 for brand-new, approaching 0.0 for old."""
    try:
        ts = experience.timestamp
        if not ts:
            return 0.5  # no timestamp → treat as moderately recent
        exp_time = _parse_iso(ts)
        if exp_time is None:
            return 0.5
        age = max(0.0, now - exp_time)
        return math.exp(-0.693 * age / _RECENCY_HALF_LIFE_SEC)  # ln(2) ≈ 0.693
    except Exception:  # noqa: BLE001
        return 0.5


def _parse_iso(ts: str) -> Optional[float]:
    """Best-effort ISO-8601 timestamp → epoch seconds.  Returns None on failure."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return time.mktime(time.strptime(ts, fmt))
        except (ValueError, OverflowError):
            continue
    return None


def _outcome_quality(experience: Experience) -> float:
    """Quality score in [0.0, 1.0].  Success is baseline (1.0); recovered gets a small
    bonus (0.95 — it succeeded *and* demonstrated retry resilience); failed is penalised
    (0.4) because users prefer what worked; cancelled is neutral (0.7)."""
    quality = {
        ExperienceOutcome.SUCCESS: 1.0,
        ExperienceOutcome.RECOVERED: 0.95,
        ExperienceOutcome.FAILED: 0.4,
        ExperienceOutcome.CANCELLED: 0.7,
    }
    return quality.get(experience.outcome, 0.5)


# --- public API --------------------------------------------------------------

@dataclass
class RankedExperience:
    """An experience with a computed relevance score."""
    experience: Experience
    score: float
    goal_score: float = 0.0
    tool_score: float = 0.0
    recency_score: float = 0.0
    outcome_score: float = 0.0


def rank_experiences(
    goal: str,
    experiences: Sequence[Experience],
    context: Optional[dict] = None,
) -> List[RankedExperience]:
    """Score and rank *experiences* by relevance to *goal*.

    Parameters
    ----------
    goal : str
        The new goal being planned.
    experiences : Sequence[Experience]
        Candidate experiences (typically from ExperienceStore.list_recent or .all).
    context : dict, optional
        Additional context about the current request (tool names, task type, etc.).

    Returns
    -------
    List[RankedExperience]
        Sorted descending by score (most relevant first).
    """
    new_tokens = _tokenize(goal)
    new_tools: set[str] = set()
    if context and "tools" in context:
        new_tools = set(context["tools"]) if isinstance(context["tools"], (list, set)) else set()
    now = time.time()

    ranked: list[RankedExperience] = []
    for exp in experiences:
        gs = _goal_similarity(new_tokens, exp)
        ts = _tool_overlap(new_tools, exp)
        rs = _recency_score(exp, now)
        oq = _outcome_quality(exp)
        total = _W_GOAL * gs + _W_TOOL * ts + _W_RECENCY * rs + _W_OUTCOME * oq
        ranked.append(RankedExperience(
            experience=exp, score=round(total, 4),
            goal_score=round(gs, 4), tool_score=round(ts, 4),
            recency_score=round(rs, 4), outcome_score=round(oq, 4),
        ))
    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked


def retrieve_relevant_experiences(
    goal: str,
    store: ExperienceStore,
    context: Optional[dict] = None,
    limit: int = 5,
) -> List[RankedExperience]:
    """Convenience function: load all experiences from *store*, rank them by relevance
    to *goal*, and return the top *limit*.

    Returns an empty list on any error — retrieval failure must never break planning.
    """
    try:
        all_experiences = store.all()
        if not all_experiences:
            logger.info("No experiences in store for retrieval")
            return []
        ranked = rank_experiences(goal, all_experiences, context=context)
        top = ranked[:limit]
        logger.info(
            "Retrieved %d relevant experiences (of %d total) for goal=%r",
            len(top), len(all_experiences), goal[:80],
        )
        return top
    except Exception:  # noqa: BLE001 — retrieval failure must never break planning
        logger.exception("Experience retrieval failed for goal=%r", goal[:80])
        return []
