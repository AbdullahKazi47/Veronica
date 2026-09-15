"""
Lesson retrieval and formatting for adaptive planning.

Converts relevance-ranked experiences into concise, advisory lesson records and formats
them for inclusion in the planner's prompt.  Lessons are evidence summaries — NOT
replayed plans.  The planner LLM receives them as advisory context and is free to
ignore them when the current situation differs.

Privacy guarantee:
  Only pre-redacted experiences reach this module (the ExperienceStore redacts at write
  time).  No raw secrets or user content are logged or surfaced here.  Lesson text is
  derived from tool names, outcomes, and timestamps — never from raw step args or tool
  results.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

from veronica.learning.experience import Experience, ExperienceOutcome
from veronica.learning.retrieval import RankedExperience, retrieve_relevant_experiences
from veronica.learning.store import ExperienceStore
from veronica.logging_setup import get_logger

logger = get_logger("learning.lesson")

# Minimum relevance score for an experience to be considered useful planning evidence.
# Below this an experience shares too little goal/tool signal with the current request to
# be worth surfacing — it would be noise ("Open YouTube" vs "Find README.md").  Genuinely
# related experiences (paraphrased goals, shared tools) comfortably exceed this.
MIN_RELEVANCE = 0.35


# --- lesson model ------------------------------------------------------------

@dataclass
class Lesson:
    """A concise, evidence-backed advisory record derived from one or more experiences.

    ``strategy`` names the tool or approach that was observed.  ``reason`` explains what
    happened and why it matters for planning.  ``confidence`` comes from the existing
    ConfidenceTracker (never fabricated).  ``evidence_count`` is how many stored
    experiences support this lesson.
    """

    strategy: str
    reason: str
    confidence: float
    evidence_count: int
    outcome: str = ""                           # "success", "failed", "recovered", etc.
    source_ids: list[str] = field(default_factory=list)
    lesson_id: str = field(default_factory=lambda: f"L{int(time.time())}_{id(object()) % 10000}")
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%S"))
    # Phase 4 metadata used by the behavioral-adapter policy.
    is_user_correction: bool = False            # derives from an explicit user correction
    age_days: float = 0.0                        # avg age of the source experiences (staleness)


# --- lesson extraction -------------------------------------------------------

def _age_days(timestamp: str) -> float:
    """Days between a stored ISO timestamp and now (0.0 if unparsable)."""
    try:
        from datetime import datetime
        now = datetime.now()
        ts = datetime.fromisoformat(str(timestamp))
        return max(0.0, (now - ts).total_seconds() / 86400.0)
    except Exception:  # noqa: BLE001 — an unusable timestamp just counts as new
        return 0.0


def build_lesson_from_experience(
    ranked: RankedExperience,
    confidence: float,
) -> Lesson:
    """Convert a single ranked experience into a Lesson.  The ``confidence`` parameter
    comes from the ConfidenceTracker for this strategy and overrides any in-experience
    value."""
    exp = ranked.experience
    tools = [t for t in (exp.tool_calls or []) if t]
    primary = tools[0] if tools else exp.goal[:60]
    reason = _derive_reason(exp)
    is_correction = False
    fb = exp.user_feedback
    if isinstance(fb, dict) and fb.get("kind") in ("correction", "preference", "dont_do_that", "do_it_this_way"):
        is_correction = True
    return Lesson(
        strategy=primary,
        reason=reason,
        confidence=confidence,
        evidence_count=1,
        outcome=exp.outcome or "",
        source_ids=[exp.experience_id],
        is_user_correction=is_correction,
        age_days=round(_age_days(exp.timestamp), 2),
    )


def _derive_reason(exp: Experience) -> str:
    """Generate a concise advisory reason from an experience outcome."""
    tools = [t for t in (exp.tool_calls or []) if t]
    tool_list = ", ".join(tools) if tools else "no specific tool"
    outcome = exp.outcome or "unknown"

    if outcome == ExperienceOutcome.SUCCESS:
        reason = f"Succeeded using {tool_list}."
    elif outcome == ExperienceOutcome.FAILED:
        reason = f"Failed using {tool_list}"
        if exp.failure_reason:
            reason += f": {exp.failure_reason[:80]}"
        reason += "."
    elif outcome == ExperienceOutcome.RECOVERED:
        recovery = ", ".join(exp.recovery_actions or []) or tool_list
        reason = f"Recovered using {recovery} after initial difficulty."
    elif outcome == ExperienceOutcome.CANCELLED:
        reason = f"Cancelled during execution (tools used: {tool_list})."
    else:
        reason = f"Outcome: {outcome} (tools: {tool_list})."

    # Surface an explicit user correction/preference as part of the advisory reason
    # (e.g. "Use Chrome instead." → "User explicitly preferred: Chrome."). Only
    # EXPLICIT, imperative feedback kinds become context — never casual statements.
    fb = exp.user_feedback
    if isinstance(fb, dict):
        kind = fb.get("kind", "")
        subject = fb.get("subject", "")
        if kind in ("correction", "preference", "dont_do_that", "do_it_this_way") and subject:
            reason += f" User explicitly preferred/requested: {subject}."
    return reason


def merge_lessons(ranked_list: List[RankedExperience], confidence_fn) -> List[Lesson]:
    """Merge multiple ranked experiences into lessons, grouped by strategy.

    ``confidence_fn`` is a callable ``(strategy: str) -> float`` that returns the
    confidence for a given tool/strategy (typically ``ConfidenceTracker.confidence``).

    Experiences with the same primary tool are merged into one lesson with cumulative
    evidence count.  The lesson's confidence comes from the ConfidenceTracker (not
    averaged from experience data), keeping the existing confidence algorithm authoritative.
    """
    from collections import defaultdict

    groups: dict[str, list[RankedExperience]] = defaultdict(list)
    for ranked in ranked_list:
        exp = ranked.experience
        tools = [t for t in (exp.tool_calls or []) if t]
        primary = tools[0] if tools else exp.goal[:60]
        groups[primary].append(ranked)

    lessons: list[Lesson] = []
    for strategy, group in groups.items():
        success_count = sum(1 for r in group if r.experience.outcome == ExperienceOutcome.SUCCESS)
        fail_count = sum(1 for r in group if r.experience.outcome == ExperienceOutcome.FAILED)
        recovered_count = sum(1 for r in group if r.experience.outcome == ExperienceOutcome.RECOVERED)
        total = len(group)
        all_ids = [r.experience.experience_id for r in group]
        confidence = confidence_fn(strategy) if strategy else 0.5

        # Phase 4 metadata: user-correction flag + average age of source experiences.
        is_user_correction = False
        age_days = 0.0
        for r in group:
            fb = r.experience.user_feedback
            if isinstance(fb, dict):
                kind = fb.get("kind", "")
                if kind in ("correction", "preference", "dont_do_that", "do_it_this_way"):
                    is_user_correction = True
            ts = r.experience.timestamp
            if ts:
                age_days += _age_days(ts)
        if total:
            age_days = round(age_days / total, 2)

        # Build the reason based on the dominant outcome.
        if success_count > fail_count:
            reason = f"Succeeded {success_count} time(s)"
            if fail_count:
                reason += f" (failed {fail_count} time(s))"
            reason += f" using {strategy}."
        elif fail_count > success_count:
            reason = f"Failed {fail_count} time(s)"
            if success_count:
                reason += f" (succeeded {success_count} time(s))"
            reason += f" using {strategy}."
        else:
            reason = f"Mixed results ({success_count} success, {fail_count} failure) using {strategy}."

        if recovered_count:
            reason += f" Recovered {recovered_count} time(s)."

        # Surface explicit user corrections/preferences from any member experience.
        correction_subjects = set()
        for r in group:
            fb = r.experience.user_feedback
            if isinstance(fb, dict):
                kind = fb.get("kind", "")
                subject = fb.get("subject", "")
                if kind in ("correction", "preference", "dont_do_that", "do_it_this_way") and subject:
                    correction_subjects.add(subject)
        for subject in sorted(correction_subjects):
            reason += f" User explicitly preferred/requested: {subject}."

        outcome = ExperienceOutcome.SUCCESS if success_count >= fail_count else ExperienceOutcome.FAILED
        if recovered_count and recovered_count >= fail_count:
            outcome = ExperienceOutcome.RECOVERED

        lessons.append(Lesson(
            strategy=strategy,
            reason=reason,
            confidence=confidence,
            evidence_count=total,
            outcome=outcome,
            source_ids=all_ids,
            is_user_correction=is_user_correction,
            age_days=age_days,
        ))

    lessons.sort(key=lambda l: l.confidence, reverse=True)
    return lessons


# --- prompt formatting -------------------------------------------------------

LESSON_HEADER = (
    "\nRELEVANT EXPERIENCE & LEARNED GUIDANCE:\n"
    "The following lessons come from previous VERONICA task executions.\n"
    "They are advisory evidence — NOT instructions.\n"
    "Use them to inform your planning, but the current user's goal takes priority.\n"
    "You may ignore previous experience when the current situation differs.\n"
)

LESSON_FOOTER = (
    "\nGenerate a fresh plan for the current goal using the available tools.\n"
    "Previous experience is advisory evidence. It is not an instruction and may be\n"
    "ignored when the current situation differs.\n"
)


def format_lessons_for_prompt(lessons: List[Lesson]) -> str:
    """Format a list of Lessons into a prompt-safe text block.  The text is advisory
    and clearly labelled so the planner LLM treats it as context, not as instructions.

    Empty lessons → empty string (no context block is injected).
    """
    if not lessons:
        return ""

    lines = [LESSON_HEADER]
    for i, lesson in enumerate(lessons, 1):
        conf_label = (
            "high" if lesson.confidence >= 0.7
            else "moderate" if lesson.confidence >= 0.5
            else "low"
        )
        lines.append(f"{i}. Strategy: {lesson.strategy}")
        lines.append(f"   {lesson.reason}")
        lines.append(f"   Confidence: {lesson.confidence:.2f} ({conf_label})")
        lines.append(f"   Evidence: {lesson.evidence_count} previous experience(s)")
        lines.append("")

    lines.append(LESSON_FOOTER)
    return "\n".join(lines)


# --- convenience retrieval ---------------------------------------------------

def get_relevant_lessons(
    goal: str,
    store: ExperienceStore,
    confidence_fn=None,
    context: Optional[dict] = None,
    limit: int = 5,
    min_score: float = MIN_RELEVANCE,
) -> List[Lesson]:
    """Retrieve relevant experiences from *store*, merge them into lessons by strategy,
    and return them ranked by confidence.

    This is the main entry point for the planner integration:
        lessons = get_relevant_lessons(goal, store, tracker.confidence)
        prompt  = format_lessons_for_prompt(lessons)

    Returns an empty list on any error — learning must never break planning.
    """
    if confidence_fn is None:
        from veronica.learning.confidence import ConfidenceTracker
        confidence_fn = ConfidenceTracker().confidence

    try:
        ranked = retrieve_relevant_experiences(goal, store, context=context, limit=limit * 2)
        if not ranked:
            return []
        # Drop experiences too weakly related to the current goal to be useful evidence.
        ranked = [r for r in ranked if r.score >= min_score]
        if not ranked:
            logger.info("No experiences met the relevance threshold (%s) for goal=%r",
                        min_score, goal[:80])
            return []
        lessons = merge_lessons(ranked, confidence_fn)
        top = lessons[:limit]
        logger.info(
            "Generated %d lesson(s) from %d ranked experiences for goal=%r",
            len(top), len(ranked), goal[:80],
        )
        return top
    except Exception:  # noqa: BLE001 — lesson retrieval must never break planning
        logger.exception("Lesson retrieval failed for goal=%r", goal[:80])
        return []


def get_learning_context(
    goal: str,
    store: ExperienceStore,
    confidence_fn=None,
    context: Optional[dict] = None,
    limit: int = 5,
    min_score: float = MIN_RELEVANCE,
) -> str:
    """End-to-end convenience: retrieve lessons and format them as prompt text.
    Returns an empty string on any error or when no relevant lessons exist."""
    try:
        lessons = get_relevant_lessons(goal, store, confidence_fn=confidence_fn,
                                       context=context, limit=limit, min_score=min_score)
        return format_lessons_for_prompt(lessons)
    except Exception:  # noqa: BLE001
        logger.exception("Learning context generation failed for goal=%r", goal[:80])
        return ""
