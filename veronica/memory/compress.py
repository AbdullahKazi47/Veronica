"""
Conversation compression for small-context local LLMs.

VERONICA's ``ShortTermMemory`` keeps a fixed rolling window (a ``deque``), which
is right for short sessions but throws away the *beginning* of a long session
even when the start contained context worth keeping. This module adds a
middle-out compression pass, ported to VERONICA's message format from the
algorithm used in the user's private ULTRON project
(project_ultron/core/loop_guard.py, ``compress_conversation``). Not for
redistribution outside this project.

Strategy (kept deliberately simple):
  1. System messages are always kept.
  2. The first non-system exchange is kept (it usually sets the topic).
  3. The last four messages are kept in full (most recent context).
  4. The middle is collapsed into a single short context block.
"""
from __future__ import annotations

from typing import Any


def compress_messages(
    messages: list[dict[str, str]],
    max_messages: int = 50,
    *,
    recent: int = 4,
) -> list[dict[str, str]]:
    """Compress ``messages`` when it exceeds ``max_messages``.

    Returns ``messages`` unchanged when it is within budget. Otherwise returns
    a list with the same shape (``{"role": ..., "content": ...}``) that keeps
    system messages, the first exchange, and the last ``recent`` messages, and
    summarizes everything in between.

    The summary preserves nothing factual — it only records *that* interaction
    happened, so a long session's tail never loses the thread of "we were
    mid-task". Exact content that still matters must live in long-term memory.
    """
    if len(messages) <= max_messages:
        return messages

    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    # Nothing worth compressing if there is almost no non-system history.
    if len(non_system) <= recent + 2:
        return messages

    first_exchange = non_system[:2]
    recent_msgs = non_system[-recent:]
    middle = non_system[2:-recent]

    user_in_middle = sum(1 for m in middle if m.get("role") == "user")
    tool_markers = sum(1 for m in middle if _looks_like_tool_turn(m))

    summary = {
        "role": "user",
        "content": (
            f"[Earlier conversation summary: {user_in_middle} user messages and "
            f"{tool_markers} tool interactions occurred before the last "
            f"{recent} exchanges. Topic and intent carried forward from the "
            f"initial exchange.]"
        ),
    }
    ack = {
        "role": "assistant",
        "content": "Understood — I have the earlier context and we are continuing.",
    }

    compressed = system_msgs + first_exchange + [summary, ack] + recent_msgs
    return compressed


def _looks_like_tool_turn(message: dict[str, Any]) -> bool:
    """Heuristic: does this message carry a tool result / tool call marker."""
    content = message.get("content", "")
    if isinstance(content, str):
        return "[tool" in content.lower()
    # Future-proof: a tool message may be a structured list, not a string.
    return "tool" in str(content).lower()