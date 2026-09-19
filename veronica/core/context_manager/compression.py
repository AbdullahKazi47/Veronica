"""Context compression — priority-ordered, status-preserving.

Compression only ever touches ``compressible`` items:
  - first pass: DROP low-priority compressible items (older conversation, low-value
    memory) before anything else;
  - second pass: SUMMARIZE remaining compressible items (large tool results, long
    observations) down to a configured budget.

Never compressed: the current user request, the safety boundary, mandatory
permission/system items. Compression PRESERVES status semantics — a summary that would
elide UNKNOWN / FAILED / UNVERIFIED / BLOCKED is refused (see ``preserves_status``)
so context compression can never turn unknown into known, or failed into success.

Context dropping NEVER deletes a stored memory: these functions only produce a
different ContextPackage; the underlying records are untouched by design."""
from __future__ import annotations

import re
from typing import Iterable, Optional, Sequence

from veronica.core.context_manager.models import (
    STATUS_SEMANTIC_WORDS, ContextItem,
)


def is_protected(item: ContextItem) -> bool:
    """Items that selection/compression may never drop or summarize: the current user
    request, the safety boundary, mandatory permission/system content."""
    if item.required:
        return True
    if not item.compressible:
        return True
    return False


def preserves_status(original: str, summary: str) -> bool:
    """True when ``summary`` retains every status-semantic word the original carried.
    A compression that would elide UNKNOWN/FAILED/UNVERIFIED/BLOCKED is refused
    (honest compression — never a silent status flip)."""
    if not original:
        return True
    missing = [
        w for w in STATUS_SEMANTIC_WORDS
        if re.search(rf"\b{w}\b", original.lower()) and not re.search(rf"\b{w}\b", summary.lower())
    ]
    return not missing


def summarize_item(item: ContextItem, max_tokens: int, *, token_estimate) -> ContextItem:
    """Return a summarized copy of ``item`` bounded to ``max_tokens`` estimated tokens.
    Refuses (returns the original item unchanged) when the summary would violate
    ``preserves_status`` — e.g. a FAILED tool result can be truncated, but the summary
    must keep saying it failed."""
    if is_protected(item):
        return item
    text = item.content
    current = token_estimate(text)
    if current <= max_tokens:
        return item
    char_budget = max(16, int(max_tokens) * 4)
    head, tail = text[: char_budget // 2], text[-char_budget // 4:]
    candidate = f"{head}…[truncated: {current}→{max_tokens} tokens]{tail}"
    if not preserves_status(text, candidate):
        # Keep the status words by nudging the tail inward until they survive.
        tail = text[-char_budget // 2:]
        candidate = f"{head}…[truncated]{tail}"
    if not preserves_status(text, candidate):
        return item  # refuse rather than falsify status
    from dataclasses import replace
    return replace(
        item,
        content=candidate,
        token_estimate=max(0, token_estimate(candidate)),
        metadata={**item.metadata, "compressed": True, "original_len": len(text)},
    )


def drop_lowest_value(
    items: Sequence[ContextItem],
    *,
    over_by: int,
    token_estimate,
) -> tuple[list[ContextItem], list[ContextItem]]:
    """Drop compressible items from the LEAST valued upward until ``over_by`` tokens are
    recovered. Protected items are never considered. Returns (kept, dropped)."""
    kept: list[ContextItem] = []
    dropped: list[ContextItem] = []
    recovered = 0
    # Least valued first: highest priority_rank, then lowest relevance.
    candidates = sorted(
        (it for it in items if not is_protected(it)),
        key=lambda it: (it.priority_rank, it.relevance, it.item_id),
        reverse=True,
    )
    protected = [it for it in items if is_protected(it)]
    for it in candidates:
        if recovered >= over_by:
            kept.append(it)
            continue
        recovered += token_estimate(getattr(it, "content", ""))
        dropped.append(it)
    return _restore_order(protected + kept), dropped


def compress_to_budget(
    items: Sequence[ContextItem],
    budget_tokens: int,
    *,
    token_estimate,
    max_tokens_per_summary: Optional[int] = None,
) -> tuple[list[ContextItem], list[ContextItem], list[ContextItem]]:
    """Run the two-pass priority-ordered compression.

    Returns ``(kept, dropped, compressed)`` — a single selected set and the two
    diagnostic lists. ``budget_tokens`` is the per-source budget for the items being
    compressed. The current user request, safety, and mandatory items are untouchable."""
    # --- pass 1: recover space FIRST by dropping compressible items, lowest value up --
    total = sum(token_estimate(getattr(it, "content", "")) for it in items)
    kept: list[ContextItem] = []
    dropped: list[ContextItem] = []
    compressed: list[ContextItem] = []

    if total > budget_tokens:
        over = total - budget_tokens
        kept, dropped = drop_lowest_value(
            items, over_by=over, token_estimate=token_estimate,
        )
    else:
        kept = list(items)

    # --- pass 2: summarize the largest remaining compressible items to reclaim space --
    cap = max_tokens_per_summary or int(budget_tokens * 0.10)
    out: list[ContextItem] = []
    for it in kept:
        if token_estimate(getattr(it, "content", "")) > cap and not is_protected(it):
            summarized = summarize_item(it, max_tokens=cap, token_estimate=token_estimate)
            if summarized is not it:
                compressed.append(summarized)
                out.append(summarized)
                continue
        out.append(it)
    return out, dropped, compressed


def _restore_order(items: Iterable[ContextItem]) -> list[ContextItem]:
    """Restore the canonical priority order for a selected set."""
    from veronica.core.context_manager.priority import sort_items
    return sort_items(items)


__all__ = [
    "compress_to_budget", "drop_lowest_value", "is_protected", "preserves_status",
    "summarize_item",
]