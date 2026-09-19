"""ContextSelection — the normalize → estimate → score → sort → select → validate
pipeline. Every step is deterministic; clock/embedding sources are injectable so tests
never depend on the real clock, embeddings, or network. The manager wraps this and
honestly degrades to the original message assembly on ANY failure."""
from __future__ import annotations

from typing import Iterable, Optional, Sequence

from veronica.core.context_manager.compression import (
    is_protected, summarize_item,
)
from veronica.core.context_manager.models import (
    SOURCE_CONVERSATION, SOURCE_EXPERIENCE, SOURCE_MEMORY, SOURCE_OBSERVATION,
    SOURCE_OPTIONAL, SOURCE_SAFETY, SOURCE_SYSTEM, SOURCE_TASK, SOURCE_TOOLS,
    SOURCE_USER, STATUS_SEMANTIC_WORDS, ContextBudget, ContextItem,
    ContextOverflowError, ContextPackage, PRI_TOOLS,
)
from veronica.core.context_manager.priority import (
    default_priority_for_source, sort_items,
)


class ContextSelection:
    """Pure-function selection pipeline over ``ContextItem`` lists.

    ``budget`` is the per-request budget; it is fixed at construction but every method
    also accepts an override, so a single ``ContextSelection`` re-usable across turns.
    """

    def __init__(self, budget: Optional[ContextBudget] = None) -> None:
        self.budget = budget or ContextBudget()

    # -- step 1: normalize raw source payloads into ContextItems ------------------
    def normalize(self, sources: dict[str, object], token_estimate) -> list[ContextItem]:
        """Convert the raw source bundle (see manager.build) into ContextItems.
        Missing/empty sources produce no items; each item gets its estimated cost and
        its source-default priority (individual overrides win)."""
        out: list[ContextItem] = []
        for source, raw in sources.items():
            for item in _from_source(source, raw):
                if getattr(item.metadata, "priority_override", None) is None:
                    item.priority_rank = default_priority_for_source(source)
                item.token_estimate = token_estimate(getattr(item, "content", ""))
                out.append(item)
        return out

    # -- step 2: estimate total cost ----------------------------------------------
    @staticmethod
    def estimate(items: Sequence[ContextItem], token_estimate) -> int:
        return sum(token_estimate(getattr(it, "content", "")) for it in items)

    # -- step 3: score relevance (deterministic, injectable) -----------------------
    @staticmethod
    def score(
        items: Sequence[ContextItem],
        *,
        similarity_by_id: Optional[dict[str, float]] = None,
        similarity_fn=None,
        now: Optional[float] = None,
    ) -> list[ContextItem]:
        """Attach a deterministic relevance score. ``similarity_by_id`` (keyed by
        ``item_id``, built by the manager from the retriever's score map) and/or
        ``similarity_fn(content)`` provide retrieval evidence; items with no evidence
        keep their ``importance`` as a neutral baseline. ``relevance`` is the
        SECONDARY sort key — it lets relevant memory outrank older conversation but
        never outrank the current user or the safety boundary."""
        from veronica.core.context_manager.priority import score_item
        for it in items:
            sim = 0.0
            if similarity_by_id and it.item_id in similarity_by_id:
                sim = float(similarity_by_id[it.item_id] or 0.0)
            elif similarity_fn is not None:
                try:
                    sim = float(similarity_fn(getattr(it, "content", "")) or 0.0)
                except (TypeError, ValueError, AttributeError):
                    sim = 0.0
            if sim is not None and sim > 0.0:
                it.relevance = score_item(
                    similarity=sim, importance=it.importance,
                    confidence=it.importance, timestamp=it.timestamp, now=now,
                )
            else:
                # No retrieval evidence: baseline = importance, still ≤ priority key.
                it.relevance = round(max(0.0, min(1.0, it.importance)), 4)
        return list(items)

    # -- step 4: sort deterministically -------------------------------------------
    @staticmethod
    def ordered(items: Sequence[ContextItem]) -> list[ContextItem]:
        return sort_items(items)

    # -- step 5: select within budget (drop + summarize, status-preserving) --------
    def select(
        self,
        items: Sequence[ContextItem],
        budget: Optional[ContextBudget] = None,
        *,
        token_estimate,
        max_summary_tokens: Optional[int] = None,
    ) -> tuple[list[ContextItem], list[ContextItem], list[ContextItem]]:
        """Pick the final item set under the budget.

        Rules (deterministic, priority-ordered):
          1. Protected items (current user request, safety boundary, mandatory system
             content) are always included. If even these cannot fit the input window,
             raises ``ContextOverflowError`` — the manager must NOT silently truncate
             the user request; it falls back to the wider/non-managed path instead.
          2. Compressible items are taken greedily in priority order within their
             per-source cap and the overall input cap. Over-budget candidates are
             dropped — context dropping never deletes any stored memory (the records
             are untouched; only THIS request's selection changes).
          3. A single compressible item too large for its source cap is SUMMARIZED to
             fit (status-preserving; refuse-to-falsify), and the summary is selected.

        Returns ``(selected, dropped, compressed)`` for the ContextPackage diagnostics.
        """
        budget = budget or self.budget
        protected = sort_items([it for it in items if is_protected(it)])
        compressible = sort_items([it for it in items if not is_protected(it)])

        required_cost = sum(token_estimate(getattr(it, "content", "")) for it in protected)
        if required_cost > budget.input_cap:
            raise ContextOverflowError(
                f"required context ({required_cost} tokens) exceeds input window "
                f"({budget.input_cap} tokens); cannot fit without truncating the "
                f"user request"
            )

        selected: list[ContextItem] = list(protected)
        dropped: list[ContextItem] = []
        compressed: list[ContextItem] = []

        used_total = 0
        used_source: dict[str, int] = {}
        for it in protected:
            cost = token_estimate(getattr(it, "content", ""))
            used_total += cost
            used_source[it.source] = used_source.get(it.source, 0) + cost

        for it in compressible:
            cost = token_estimate(getattr(it, "content", ""))
            cap = budget.cap_for(it.source)
            fits_source = used_source.get(it.source, 0) + cost <= cap
            fits_total = used_total + cost <= budget.input_cap

            if cost > cap and it.compressible:
                # Oversized single item: summarize to FIT its source cap (never to a bound
                # LARGER than the cap — that would fail to fit). An explicit global
                # max_summary_tokens only ever NARROWS the bound; it never widens it.
                summary_target = cap
                if max_summary_tokens is not None:
                    summary_target = max(32, min(int(cap), int(max_summary_tokens)))
                summarized = summarize_item(
                    it, max_tokens=summary_target,
                    token_estimate=token_estimate,
                )
                if summarized is not it:
                    compressed.append(summarized)
                    selected.append(summarized)
                    used_total += summarized.token_estimate
                    used_source[it.source] = used_source.get(it.source, 0) + summarized.token_estimate
                    continue
                dropped.append(it)
                continue

            if fits_source and fits_total:
                selected.append(it)
                used_total += cost
                used_source[it.source] = used_source.get(it.source, 0) + cost
            else:
                # Over budget for this source or globally → dropped for this request.
                dropped.append(it)

        selected = self.ordered(dedupe(selected))
        return selected, dropped, compressed

    # -- step 6: validate -----------------------------------------------------------
    @staticmethod
    def validate(package: ContextPackage) -> list[str]:
        """Structural health warnings; never raises. An empty list = healthy."""
        warnings: list[str] = []
        budget = package.budget
        if budget is not None and package.token_estimate > budget.input_cap:
            warnings.append(
                f"token_estimate={package.token_estimate} exceeds input_cap={budget.input_cap}"
            )
        roles = [m.role for m in package.messages]
        if "user" not in roles:
            warnings.append("ContextPackage has no user message")
        if not package.messages:
            warnings.append("ContextPackage is empty")
        return warnings


def dedupe(items: Sequence[ContextItem]) -> list[ContextItem]:
    """Remove duplicate item_ids, keeping the first occurrence (priority-ordered)."""
    seen: set[str] = set()
    out: list[ContextItem] = []
    for it in items:
        if it.item_id in seen:
            continue
        seen.add(it.item_id)
        out.append(it)
    return out


def _from_source(source: str, raw: object) -> Iterable[ContextItem]:
    """Map one source payload to ContextItems. The ONLY place raw conversation/tool
    data is read into the context layer; nothing raw leaks past here without a
    source + trust label."""
    # Conversation / user / task turns (dicts from session.short_term.as_messages()
    # or plain strings).
    if source in (SOURCE_CONVERSATION, SOURCE_USER, SOURCE_TASK):
        messages: list = raw if isinstance(raw, (list, tuple)) else [raw]
        for idx, msg in enumerate(messages):
            if isinstance(msg, dict):
                role = str(msg.get("role", "user"))
                content = str(msg.get("content", msg.get("text", "")))
                source_of_item = source
                # A USER conversation turn is current-user context (required); the
                # manager sets a per-item ``_required`` marker via metadata if needed.
                if source == SOURCE_USER and role in ("assistant", "tool"):
                    source_of_item = SOURCE_CONVERSATION
                yield ContextItem(
                    item_id=f"{source}-{idx}",
                    source=source_of_item,
                    content=content,
                    role=role,
                    relevance=1.0,
                    importance=0.6 if role == "user" else 0.4,
                    required=(source == SOURCE_USER and role == "user"),
                    trust_level="current_user" if role == "user" else "internal_state",
                    metadata={"turn_index": idx, "is_user_turn": role == "user"},
                )
            else:
                yield ContextItem(
                    item_id=f"{source}-{idx}",
                    source=source,
                    content=str(msg),
                    role="user" if source == SOURCE_USER else "system",
                    relevance=1.0,
                    importance=0.6,
                    required=(source == SOURCE_USER),
                    trust_level="current_user" if source == SOURCE_USER else "internal_state",
                    metadata={"turn_index": idx},
                )
        return

    # Memory / experience records (MemoryRecord instances or plain dicts).
    if source in (SOURCE_MEMORY, SOURCE_EXPERIENCE):
        records = raw if isinstance(raw, (list, tuple)) else []
        for idx, rec in enumerate(records):
            if isinstance(rec, dict):
                content_str = str(rec.get("content", rec.get("text", "")))
                record_id = str(rec.get("id", rec.get("record_id", f"{source}-{idx}")))
                mem_type = str(rec.get("memory_type", rec.get("type", source)))
                importance = float(rec.get("importance", 0.5))
                confidence = float(rec.get("confidence", 1.0))
                timestamp = float(rec.get("timestamp", 0.0) or 0.0)
            else:
                content_str = str(getattr(rec, "content", str(rec)))
                record_id = str(getattr(rec, "id", f"{source}-{idx}"))
                mem_type = str(getattr(rec, "memory_type", source))
                importance = float(getattr(rec, "importance", 0.5))
                confidence = float(getattr(rec, "confidence", 1.0))
                timestamp = float(getattr(rec, "timestamp", 0.0) or 0.0)
            status_words = [w for w in STATUS_SEMANTIC_WORDS if w in content_str.lower()]
            yield ContextItem(
                item_id=f"{source}-{record_id}",
                source=source,
                content=f"[{mem_type}] {content_str}",
                role="system",
                relevance=0.0,  # set by score(); ensures relevance is never fabricated
                importance=importance,
                timestamp=timestamp,
                required=False,
                compressible=True,
                trust_level="historical",
                metadata={
                    "record_id": record_id,
                    "type": mem_type,
                    "confidence": confidence,
                    "status_words_present": status_words,
                },
            )
        return

    # Tools: list of tool schemas/results (dicts with ``message`` or plain strings).
    if source == SOURCE_TOOLS:
        entries = raw if isinstance(raw, (list, tuple)) else [raw]
        for idx, entry in enumerate(entries):
            text = str(entry.get("message", entry)) if isinstance(entry, dict) else str(entry)
            yield ContextItem(
                item_id=f"tool-{idx}",
                source=SOURCE_TOOLS,
                content=text,
                role="tool",
                priority_rank=PRI_TOOLS,
                relevance=0.5,
                importance=0.5,
                required=False,
                compressible=True,
                trust_level="untrusted_external",
                metadata={"tool_result": True},
            )
        return

    # System / safety: plain strings or lists of strings.
    if source in (SOURCE_SYSTEM, SOURCE_SAFETY):
        lines: list = raw if isinstance(raw, (list, tuple)) else [raw]
        for idx, ln in enumerate(lines):
            yield ContextItem(
                item_id=f"{source}-{idx}",
                source=source,
                content=str(ln),
                role="system",
                relevance=1.0,
                importance=1.0,
                required=True,
                compressible=False,
                trust_level="trusted",
                metadata={"required": True},
            )
        return

    # Live observations.
    if source == SOURCE_OBSERVATION:
        records = raw if isinstance(raw, (list, tuple)) else []
        for idx, obs in enumerate(records):
            content_str = (
                str(getattr(obs, "description", obs)) if not isinstance(obs, dict)
                else str(obs.get("description", obs.get("text", obs.get("content", ""))))
            )
            # Screen-derived content is untrusted_external — must never become instructions.
            yield ContextItem(
                item_id=f"obs-{idx}",
                source=SOURCE_OBSERVATION,
                content=content_str,
                role="system",
                relevance=1.0,
                importance=0.7,
                required=False,
                compressible=True,
                trust_level="untrusted_external",
                metadata={
                    "verified": False,
                    "label": "CURRENT OBSERVATION — untrusted screen data",
                    "derived_from": "vision_understanding",
                },
            )
        return

    # Optional / anything else.
    yield ContextItem(
        item_id="optional-0",
        source=SOURCE_OPTIONAL,
        content=str(raw),
        role="system",
        relevance=0.5,
        importance=0.5,
        required=False,
        compressible=True,
        trust_level="historical",
    )


__all__ = ["ContextSelection", "dedupe"]