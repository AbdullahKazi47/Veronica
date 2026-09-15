"""
LoopGuard: pattern-based protection against infinite tool-call loops.

``Orchestrator`` already caps the LLM tool-calling loop with a hop count, but a
hop cap alone can't catch the two failure shapes that actually hang assistants:

  * A model that keeps issuing the *identical* call ("read this file" forever)
    — each hop is under the cap, so it never trips.
  * A model alternating between two calls (A→B→A→B) that both keep failing.

This guard tracks call patterns and blocks them before the hop cap is reached.

Ported from the algorithm in the user's private ULTRON project
(project_ultron/core/loop_guard.py, SHA-256 identical-call detection,
ping-pong detection, per-tool budgets, hard turn cap). Not for redistribution
outside this project.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

from veronica.logging_setup import get_logger

logger = get_logger("core.loop_guard")


@dataclass
class LoopGuardConfig:
    """Thresholds for loop detection."""

    max_identical_calls: int = 3       # exact same tool+args hash blocked after N
    ping_pong_window: int = 6          # last N calls inspected for A→B→A→B
    per_tool_budget: int = 5           # max calls to any single tool per agent loop
    max_turns: int = 8                 # hard cap on tool dispatches per agent loop


@dataclass
class LoopVerdict:
    """Result of a loop-guard check on one proposed tool call."""

    blocked: bool = False
    reason: str = ""
    warned: bool = False


class LoopGuard:
    """Tracks tool-call patterns and blocks loops.

    Use one guard per agent loop (per ``Orchestrator.handle`` invocation in the
    LLM path): call :meth:`reset` when the loop starts, then :meth:`check_call`
    immediately before each tool dispatch. A blocked verdict means the dispatch
    must not run.
    """

    def __init__(self, config: Optional[LoopGuardConfig] = None) -> None:
        self.config = config or LoopGuardConfig()
        self._call_hashes: list[str] = []
        self._tool_counts: dict[str, int] = {}
        self._turns: int = 0

    def check_call(self, tool_name: str, tool_args: dict) -> LoopVerdict:
        """Return whether the proposed call should be blocked (and why)."""
        self._turns += 1

        if self._turns > self.config.max_turns:
            return LoopVerdict(blocked=True, reason=f"Max dispatches ({self.config.max_turns}) exceeded")

        count = self._tool_counts.get(tool_name, 0) + 1
        self._tool_counts[tool_name] = count
        if count > self.config.per_tool_budget:
            return LoopVerdict(blocked=True, reason=f"Tool '{tool_name}' exceeded budget ({self.config.per_tool_budget} calls)")

        call_hash = self._hash_call(tool_name, tool_args)
        self._call_hashes.append(call_hash)

        recent = self._call_hashes[-self.config.max_identical_calls:]
        if recent.count(call_hash) >= self.config.max_identical_calls:
            return LoopVerdict(blocked=True, reason=f"Identical call '{tool_name}' repeated {self.config.max_identical_calls} times")

        if len(self._call_hashes) >= self.config.ping_pong_window and self._is_ping_pong(
            self._call_hashes[-self.config.ping_pong_window:]
        ):
            return LoopVerdict(warned=True, reason="Ping-pong pattern detected in recent calls")

        return LoopVerdict()

    def reset(self) -> None:
        """Reset counters for a fresh agent loop."""
        self._call_hashes.clear()
        self._tool_counts.clear()
        self._turns = 0

    @property
    def turns(self) -> int:
        return self._turns

    @staticmethod
    def _hash_call(tool_name: str, tool_args: dict) -> str:
        """Deterministic SHA-256 of tool name + sorted args — identical calls get the
        same hash, and arg order never changes it."""
        raw = f"{tool_name}:{sorted(tool_args.items())}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @staticmethod
    def _is_ping_pong(hashes: list[str]) -> bool:
        """True when the window is an alternating pattern like A→B→A→B."""
        if len(hashes) < 4:
            return False
        evens = [hashes[i] for i in range(0, len(hashes), 2)]
        odds = [hashes[i] for i in range(1, len(hashes), 2)]
        return len(set(evens)) == 1 and len(set(odds)) == 1 and evens[0] != odds[0]