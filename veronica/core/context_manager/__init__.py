"""Context Window Management V1 package.

Exports the public data model and the ContextManager API.
"""

from veronica.core.context_manager.compression import (
    compress_to_budget,
    drop_lowest_value,
    is_protected,
    preserves_status,
    summarize_item,
)
from veronica.core.context_manager.manager import (
    ContextManager,
    ContextPackage,
    ContextBudget,
    ContextItem,
    ContextOverflowError,
)
from veronica.core.context_manager.priority import (
    default_priority_for_source,
    score_item,
    sort_items,
    _SOURCE_PRIORITY,
    _DEFAULT_SCORE_WEIGHTS,
)
from veronica.core.context_manager.selection import ContextSelection, dedupe
from veronica.core.context_manager.token_counter import TokenCounter
from veronica.core.context_manager.models import (
    PRI_TASK,
    PRI_OBSERVATION,
    PRI_RELEVANT_MEMORY,
    PRI_CURRENT_USER,
    PRI_SYSTEM,
    PRI_HARD_SAFETY,
    PRI_CONVERSATION_RECENT,
    PRI_TOOLS,
    PRI_CONVERSATION_OLD,
    PRI_OPTIONAL,
    SOURCE_CONVERSATION,
    SOURCE_EXPERIENCE,
    SOURCE_MEMORY,
    SOURCE_OBSERVATION,
    SOURCE_OPTIONAL,
    SOURCE_SAFETY,
    SOURCE_SYSTEM,
    SOURCE_TASK,
    SOURCE_TOOLS,
    SOURCE_USER,
    TRUST_LEVELS,
    STATUS_SEMANTIC_WORDS,
)

__all__ = [
    # Manager
    "ContextManager",
    "ContextPackage",
    "ContextBudget",
    "ContextItem",
    "ContextOverflowError",
    # Selection
    "ContextSelection",
    "dedupe",
    # Token estimation
    "TokenCounter",
    # Priority & scoring
    "default_priority_for_source",
    "score_item",
    "sort_items",
    "_SOURCE_PRIORITY",
    "_DEFAULT_SCORE_WEIGHTS",
    # Compression
    "compress_to_budget",
    "drop_lowest_value",
    "is_protected",
    "preserves_status",
    "summarize_item",
    # Constants
    "PRI_TASK",
    "SOURCE_CONVERSATION",
    "SOURCE_EXPERIENCE",
    "SOURCE_MEMORY",
    "SOURCE_OBSERVATION",
    "SOURCE_OPTIONAL",
    "SOURCE_SAFETY",
    "SOURCE_SYSTEM",
    "SOURCE_TASK",
    "SOURCE_TOOLS",
    "SOURCE_USER",
    "TRUST_LEVELS",
    "STATUS_SEMANTIC_WORDS",
]