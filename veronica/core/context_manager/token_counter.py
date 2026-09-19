"""Replaceable token estimation — V1 uses a character-based approximation so no
heavy tokenizer dependency is required (tiktoken, provider-specific encoders, etc.).
Future versions can drop in a precise encoder by replacing ``TokenCounter`` without
touching selection, budget, or compression logic."""
from __future__ import annotations

from typing import Optional


class TokenCounter:
    """Approximate token count for English text. V1: ``len(str) // 4`` (rough
    4-chars ≈ 1-token approximation for English). Replaceable."""

    def __init__(self, factor: float = 4.0) -> None:
        self.factor = max(1.0, float(factor))

    def estimate(self, text: Optional[str]) -> int:
        return max(0, len(str(text or "")) // int(self.factor))

    def estimate_items(self, items) -> int:
        total = 0
        for it in items:
            total += self.estimate(getattr(it, "content", None) or getattr(it, "text", ""))
        return total


__all__ = ["TokenCounter"]
