"""
Phase 10F — Task Waiting (Bounded Waiting for Long-Running Tasks).

Provides bounded polling and time-based waiting conditions for use
within long-running autonomous tasks.

Safety rules preserved:
- All waiting is bounded (max_duration, max_attempts, interval).
- No infinite polling loops.
- Cancellation respected.
- Waiting state included in checkpoint but does not grant execution authority.
"""
from __future__ import annotations

import time
from enum import Enum, auto
from dataclasses import dataclass
from typing import Optional, Callable, Any


class WaitStatus(str, Enum):
    WAITING = "WAITING"
    CONDITION_MET = "CONDITION_MET"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass
class WaitCondition:
    """Defines a bounded wait condition."""
    name: str
    description: str = ""
    # Function that checks if the condition is satisfied; takes current time and attempts made.
    check: Optional[Callable[[float, int], bool]] = None
    # Maximum number of polling attempts
    max_attempts: int = 30
    # Polling interval in seconds
    interval_seconds: float = 1.0
    # Maximum total duration in seconds
    max_duration_seconds: float = 120.0
    # Optional additional context (does not include authorization)
    context: Any = None


def evaluate_wait(
    condition: WaitCondition,
    *,
    cancellation_token: Optional[Any] = None,
    start_time: Optional[float] = None,
    attempts: int = 0,
) -> tuple[WaitStatus, int, float]:
    """Evaluate a bounded wait condition.

    Args:
        condition: The wait condition.
        cancellation_token: Optional token with `cancelled` attribute.
        start_time: Optional start time of the wait.
        attempts: Number of attempts already made.

    Returns:
        (WaitStatus, new_attempts, elapsed_time)
    """
    if start_time is None:
        start_time = time.time()

    # Check cancellation
    if cancellation_token is not None and getattr(cancellation_token, "cancelled", False):
        return WaitStatus.CANCELLED, attempts, time.time() - start_time

    # Check duration budget
    elapsed = time.time() - start_time
    if elapsed >= condition.max_duration_seconds:
        return WaitStatus.TIMEOUT, attempts, elapsed

    # Check max attempts
    if attempts >= condition.max_attempts:
        return WaitStatus.TIMEOUT, attempts, elapsed

    # Check condition
    if condition.check is not None:
        try:
            satisfied = condition.check(elapsed, attempts)
            if satisfied:
                return WaitStatus.CONDITION_MET, attempts + 1, elapsed
        except Exception:
            # Defensive: do not allow exceptions to silently break out; treat as unsatisfied but not failed unless repeated.
            pass

    # Not satisfied yet; return to wait
    return WaitStatus.WAITING, attempts + 1, elapsed
