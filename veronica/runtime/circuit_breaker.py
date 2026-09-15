"""
Circuit breaker: the self-healing pattern — stop calling a failing dependency
for a while, then let a single trial request through to see if it recovered.

The self-healing-with-circuit-breakers notion is documented in the JARVIS OS
public repo (02-preuves/AUDIT_COHERENCE.md) but arrives here as a fresh,
dependency-free implementation sized for a single-user desktop assistant: no
Prometheus, no cluster state, no Redis. One breaker per guarded dependency
(the LLM provider, TTS, a paid web tool).

States::

    CLOSED   → normal. Every failure increments a counter; at
               ``failure_threshold`` consecutive failures it flips to OPEN.
    OPEN     → refuse any call for ``reset_timeout_s`` (raises
               ``CircuitOpenError``). Afterwards it flips to HALF_OPEN.
    HALF_OPEN → lets exactly ONE trial call through. Success → CLOSED.
               Failure → OPEN again (full timeout).
"""
from __future__ import annotations

import logging
import time
from enum import Enum
from typing import Any, Callable, Optional

from veronica.logging_setup import get_logger

logger = get_logger("runtime.circuit_breaker")


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitOpenError(Exception):
    """Raised when a guarded call is refused because the breaker is OPEN."""


class CircuitBreaker:
    def __init__(
        self,
        *,
        name: str = "breaker",
        failure_threshold: int = 3,
        reset_timeout_s: float = 30.0,
    ) -> None:
        self.name = name
        self._threshold = max(1, failure_threshold)
        self._reset_timeout = max(0.001, reset_timeout_s)
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: Optional[float] = None
        self._trial_used = False
        self.total_calls = 0
        self.failed_calls = 0

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def failure_count(self) -> int:
        return self._failure_count

    def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Run ``func`` through the breaker: returning its result, re-raising its
        exception, or raising :class:`CircuitOpenError` when the breaker is open."""
        self._enter_trial_if_due()
        if self._state is CircuitState.OPEN:
            raise CircuitOpenError(
                f"{self.name}: circuit OPEN — refusing call "
                f"(retry after {self._time_until_close():.0f}s)"
            )
        if self._state is CircuitState.HALF_OPEN and self._trial_used:
            raise CircuitOpenError(f"{self.name}: trial already spent in this half-open window")

        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            self._record_failure_outcome()
            raise exc
        else:
            self._record_success_outcome()
            return result

    def record_success(self) -> None:
        """Mark a manually-observed call as successful (no exception raised)."""
        self._record_success_outcome()

    def record_failure(self) -> None:
        """Mark a manually-observed call as failed (no exception raised)."""
        self._record_failure_outcome()

    def _record_success_outcome(self) -> None:
        self.total_calls += 1
        self._failure_count = 0
        if self._state is CircuitState.HALF_OPEN:
            logger.info("%s: trial call succeeded — circuit CLOSED", self.name)
            self._state = CircuitState.CLOSED
        self._trial_used = False

    def _record_failure_outcome(self) -> None:
        self.total_calls += 1
        self.failed_calls += 1
        self._failure_count += 1
        if self._state is CircuitState.HALF_OPEN:
            logger.warning("%s: trial call failed — back to OPEN", self.name)
            self._open()
        elif self._state is CircuitState.CLOSED and self._failure_count >= self._threshold:
            logger.warning(
                "%s: opened after %d consecutive failures",
                self.name, self._failure_count,
            )
            self._open()
        else:
            logger.debug(
                "%s: failure %d/%d", self.name, self._failure_count, self._threshold
            )

    def reset(self) -> None:
        """Force the breaker back to CLOSED (e.g. on explicit user action)."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._trial_used = False
        self._opened_at = None

    def _open(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = time.monotonic()
        self._trial_used = False

    def _enter_trial_if_due(self) -> None:
        """OPEN → HALF_OPEN when the reset timeout has elapsed."""
        if (
            self._state is CircuitState.OPEN
            and self._opened_at is not None
            and time.monotonic() - self._opened_at >= self._reset_timeout
        ):
            logger.info("%s: reset timeout elapsed — allowing a trial call", self.name)
            self._state = CircuitState.HALF_OPEN
            self._trial_used = False

    def _time_until_close(self) -> float:
        if self._opened_at is None:
            return 0.0
        return max(0.0, self._reset_timeout - (time.monotonic() - self._opened_at))


__all__ = ["CircuitState", "CircuitOpenError", "CircuitBreaker"]