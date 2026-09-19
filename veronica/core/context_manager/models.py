"""
Context Window Management V1 — data model.

The Context Manager answers the question: "what information should be supplied to the
model for THIS request?" These data classes are the vocabulary for that decision. Every
piece of information that could enter a request becomes a ``ContextItem``; a per-source
``ContextBudget`` bounds how much may be selected; and a ``ContextPackage`` is the
final, request-scoped result handed to the model.

Hard rules carried through the whole layer:
  - a ContextPackage is request-scoped — there is NO global current context;
  - items are DATA selected from existing sources (memory, session, tools) — this layer
    never writes memory back, never deletes a stored record, never grants permission;
  - the current user request and the safety boundary are ``required`` and never
    compressed, so a request can fail loudly rather than be silently truncated.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from veronica.llm.base import ChatMessage

# --------------------------------------------------------------------------- #
# Sources — which origin an item belongs to. Per-source budgets key off these.
# --------------------------------------------------------------------------- #
SOURCE_SYSTEM = "system"
SOURCE_SAFETY = "safety"
SOURCE_USER = "user"
SOURCE_TASK = "task"
SOURCE_CONVERSATION = "conversation"
SOURCE_TOOLS = "tools"
SOURCE_OBSERVATION = "observation"
SOURCE_MEMORY = "memory"
SOURCE_EXPERIENCE = "experience"
SOURCE_OPTIONAL = "optional"

ALL_SOURCES = frozenset({
    SOURCE_SYSTEM, SOURCE_SAFETY, SOURCE_USER, SOURCE_TASK, SOURCE_CONVERSATION,
    SOURCE_TOOLS, SOURCE_OBSERVATION, SOURCE_MEMORY, SOURCE_EXPERIENCE,
    SOURCE_OPTIONAL,
})

# --------------------------------------------------------------------------- #
# Priority ranks (lower rank = more important, selected/dropped last).
# Deterministic ordering: system/safety > current user > task > tools >
# observation > relevant memory > older memory > optional.
# --------------------------------------------------------------------------- #
PRI_HARD_SAFETY = 0       # safety boundary + permission instructions — never compressed
PRI_SYSTEM = 10           # system prompt / persona / capabilities
PRI_CURRENT_USER = 20     # the current user request — never compressed
PRI_CONVERSATION_RECENT = 26   # recent live conversation turns
PRI_TASK = 30             # active task / plan
PRI_TOOLS = 40            # tool schemas
PRI_OBSERVATION = 50      # current (live) observations
PRI_RELEVANT_MEMORY = 60  # retrieved memories / experiences relevant to this request
PRI_CONVERSATION_OLD = 66 # older conversation turns (candidates for compression)
PRI_OPTIONAL = 80         # nice-to-have extras


# --------------------------------------------------------------------------- #
# Trust levels — how much an item's content may influence decisions.
# External content (tool output, screen text) is UNTRUSTED and clearly labeled,
# never promoted to system instructions (prompt-injection defense).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class _Trust:
    label: str
    rank: int   # lower = more trusted


TRUST_TRUSTED = _Trust("trusted", 0)            # system, safety, current user
TRUST_CURRENT_USER = _Trust("current_user", 1)  # explicit current user instructions
TRUST_INTERNAL_STATE = _Trust("internal_state", 2)  # active task / live observations
TRUST_HISTORICAL = _Trust("historical", 3)      # recalled memories / past experiences
TRUST_UNTRUSTED_EXTERNAL = _Trust("untrusted_external", 4)  # tool output / screen / web

TRUST_LEVELS = {
    "trusted": TRUST_TRUSTED,
    "current_user": TRUST_CURRENT_USER,
    "internal_state": TRUST_INTERNAL_STATE,
    "historical": TRUST_HISTORICAL,
    "untrusted_external": TRUST_UNTRUSTED_EXTERNAL,
}

TrustLevel = str  # one of TRUST_LEVELS.keys()

# Status semantics that MUST survive compression/truncation verbatim. When a summary
# drops the original's status words, the compression pass refuses (see compression.py)
# rather than silently turning UNKNOWN into SUCCESS or FAILED into success.
STATUS_SEMANTIC_WORDS = frozenset({
    "unknown", "unverified", "failed", "blocked", "cancelled", "success",
    "succeeded", "partial", "pending", "stream_failed", "error",
})


@dataclass
class ContextItem:
    """One piece of information considered for a request.

    ``required=True`` items (current user request, safety boundary) are never dropped
    and never compressed. ``compressible`` items may be summarized or dropped when the
    budget demands it. Selection is deterministic on (priority_rank, relevance,
    timestamp), never random.
    """
    item_id: str
    source: str                                   # one of ALL_SOURCES
    content: str
    role: str = "system"                          # "system" | "user" | "assistant" | "tool"
    priority_rank: int = PRI_OPTIONAL
    relevance: float = 0.5                        # deterministic relevance score
    importance: float = 0.5
    token_estimate: int = 0                       # set by TokenCounter during estimate()
    timestamp: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    required: bool = False                        # never dropped / compressed
    compressible: bool = True                     # eligible for summarization
    inline: bool = True                           # False => counted in budget, not text-joined
    trust_level: TrustLevel = TRUST_HISTORICAL.label
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "source": self.source,
            "role": self.role,
            "priority_rank": self.priority_rank,
            "relevance": round(self.relevance, 3),
            "importance": round(self.importance, 3),
            "token_estimate": self.token_estimate,
            "required": self.required,
            "compressible": self.compressible,
            "inline": self.inline,
            "trust_level": self.trust_level,
            "content_len": len(self.content),
        }


@dataclass
class ContextBudget:
    """Per-source token budgets for one request. ``context_window`` is the effective
    model window (from ModelProfile when routing is on, else the config default).
    ``output_reserve``/``safety_reserve`` are subtracted from ``context_window`` to
    compute the hard cap for INPUT context — VERONICA never fills the whole window
    because the model must also produce a reply."""
    context_window: int = 8192
    system: int = 2000
    conversation: int = 3000
    memory: int = 1200
    tools: int = 1500
    observation: int = 800
    output_reserve: int = 1024
    safety_reserve: int = 256

    @property
    def input_cap(self) -> int:
        """Tokens available for input context = window − reserves. At least 256 so a
        degenerate config never produces a zero budget."""
        return max(256, self.context_window - self.output_reserve - self.safety_reserve)

    def cap_for(self, source: str) -> int:
        return {
            SOURCE_SYSTEM: self.system,
            SOURCE_SAFETY: self.system,
            SOURCE_USER: self.input_cap,          # current user is bounded only by the window
            SOURCE_TASK: self.conversation,
            SOURCE_CONVERSATION: self.conversation,
            SOURCE_TOOLS: self.tools,
            SOURCE_OBSERVATION: self.observation,
            SOURCE_MEMORY: self.memory,
            SOURCE_EXPERIENCE: self.memory,
            SOURCE_OPTIONAL: self.memory,
        }[source]

    def to_dict(self) -> dict:
        return {
            "context_window": self.context_window,
            "input_cap": self.input_cap,
            "system": self.system,
            "conversation": self.conversation,
            "memory": self.memory,
            "tools": self.tools,
            "observation": self.observation,
            "output_reserve": self.output_reserve,
            "safety_reserve": self.safety_reserve,
        }


class ContextOverflowError(RuntimeError):
    """A required item alone cannot fit in the context window. This is a CONTROL error:
    the caller must fall back to a wider model or to the non-managed assembly path —
    never silently truncate the current user request (spec Phase 21)."""


@dataclass
class ContextPackage:
    """The full, request-scoped result of one ``ContextManager.build()``.

    ``messages`` is what the orchestrator hands to the model. Everything else is
    structured diagnostics: which items were kept, dropped, or compressed, what the
    budget was, and a ``source_breakdown`` so the GUI/observability layer can explain
    "why did that context fit / not fit" without re-running the selection."""
    messages: list[ChatMessage] = field(default_factory=list)
    items: list[ContextItem] = field(default_factory=list)
    dropped_items: list[ContextItem] = field(default_factory=list)
    compressed_items: list[ContextItem] = field(default_factory=list)
    token_estimate: int = 0
    budget: Optional[ContextBudget] = None
    model_id: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    source_breakdown: dict[str, int] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "token_estimate": self.token_estimate,
            "model_id": self.model_id,
            "budget": self.budget.to_dict() if self.budget else None,
            "kept_items": len(self.items),
            "dropped_items": [i.to_dict() for i in self.dropped_items],
            "compressed_items": [i.to_dict() for i in self.compressed_items],
            "warnings": self.warnings,
            "source_breakdown": self.source_breakdown,
            "message_roles": [m.role for m in self.messages],
        }


__all__ = [
    "ContextBudget", "ContextItem", "ContextOverflowError", "ContextPackage",
    "TrustLevel", "TRUST_LEVELS",
    "TRUST_TRUSTED", "TRUST_CURRENT_USER", "TRUST_INTERNAL_STATE",
    "TRUST_HISTORICAL", "TRUST_UNTRUSTED_EXTERNAL",
    "STATUS_SEMANTIC_WORDS",
    "SOURCE_SYSTEM", "SOURCE_SAFETY", "SOURCE_USER", "SOURCE_TASK",
    "SOURCE_CONVERSATION", "SOURCE_TOOLS", "SOURCE_OBSERVATION",
    "SOURCE_MEMORY", "SOURCE_EXPERIENCE", "SOURCE_OPTIONAL", "ALL_SOURCES",
    "PRI_HARD_SAFETY", "PRI_SYSTEM", "PRI_CURRENT_USER", "PRI_CONVERSATION_RECENT",
    "PRI_TASK", "PRI_TOOLS", "PRI_OBSERVATION", "PRI_RELEVANT_MEMORY",
    "PRI_CONVERSATION_OLD", "PRI_OPTIONAL",
]