"""
LLM health monitor: tracks LLM availability and response time so the router can
skip a degraded provider on the next call instead of waiting for the user to
hit a timeout.

Ported from the algorithm in the user's private ULTRON project
(project_ultron/core/llm_health.py), rewritten to use VERONICA's
``LLMProvider`` interface and an exponential moving average for response time.
The original ran a "say OK" probe against a fixed engine; VERONICA's version
takes any provider as the probe target, so the same code works for Ollama, an
OpenAI-compatible endpoint, or the echo stub. Not for redistribution outside
this project.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from veronica.llm.base import ChatMessage, LLMProvider, LLMResponse
from veronica.logging_setup import get_logger

logger = get_logger("runtime.llm_health")


class LLMStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    INITIALIZING = "INITIALIZING"
    UNKNOWN = "UNKNOWN"


@dataclass
class LLMHealthMetrics:
    status: LLMStatus
    last_check: float
    last_success: Optional[float] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    total_requests: int = 0
    successful_requests: int = 0
    average_response_time: float = 0.0


class LLMHealthMonitor:
    """Probe an LLM provider periodically and remember how it has been doing.

    One instance per provider. ``is_available()`` flips to False after
    ``consecutive_failure_threshold`` failures in a row, and the next call
    from the router can use that flag to skip this provider in favor of a
    fallback. The probe is serialized with a lock so two callers can't race.
    """

    def __init__(
        self,
        provider: LLMProvider,
        *,
        timeout_seconds: float = 10.0,
        consecutive_failure_threshold: int = 3,
        ema_alpha: float = 0.2,
    ) -> None:
        self._provider = provider
        self._timeout = timeout_seconds
        self._fail_threshold = consecutive_failure_threshold
        self._ema_alpha = ema_alpha
        self._lock = threading.Lock()
        self.metrics = LLMHealthMetrics(
            status=LLMStatus.INITIALIZING,
            last_check=time.time(),
        )

    def check_health(self) -> LLMHealthMetrics:
        """Probe the provider with a minimal "say OK" request. Updates metrics."""
        if not self._lock.acquire(blocking=False):
            logger.debug("LLM health check already in progress, returning cached metrics")
            return self.metrics

        try:
            start = time.time()
            response: Optional[LLMResponse] = None
            error: Optional[str] = None

            messages = [
                ChatMessage(role="system", content="You are a probe. Reply concisely."),
                ChatMessage(role="user", content="Say 'OK' if you can hear me."),
            ]

            try:
                response = self._provider.chat(messages, tools=None, temperature=0.0)
            except Exception as e:  # provider contract is "don't raise" but defend
                error = str(e)

            elapsed = time.time() - start
            self.metrics.total_requests += 1
            self.metrics.last_check = time.time()

            if error is None and response and response.content.strip():
                self.metrics.status = LLMStatus.AVAILABLE
                self.metrics.last_success = self.metrics.last_check
                self.metrics.consecutive_failures = 0
                self.metrics.successful_requests += 1
                self.metrics.last_error = None
                # EMA — first observation initializes the average; later ones
                # blend with weight ``ema_alpha`` (small for stability).
                if self.metrics.average_response_time == 0.0:
                    self.metrics.average_response_time = elapsed
                else:
                    a = self._ema_alpha
                    self.metrics.average_response_time = (
                        (1 - a) * self.metrics.average_response_time + a * elapsed
                    )
            else:
                self._record_failure(
                    "Empty response" if error is None else f"Provider raised: {error}"
                )
        finally:
            self._lock.release()

        return self.metrics

    def is_available(self) -> bool:
        return self.metrics.status == LLMStatus.AVAILABLE

    def mark_unavailable(self, error: str) -> None:
        """Allow a caller (e.g. a circuit breaker) to flag a failure without
        running a full probe — useful when the router catches a real exception."""
        self._record_failure(error)

    def status_summary(self) -> dict:
        return {
            "status": self.metrics.status.value,
            "available": self.is_available(),
            "last_check": self.metrics.last_check,
            "last_success": self.metrics.last_success,
            "consecutive_failures": self.metrics.consecutive_failures,
            "success_rate": (
                self.metrics.successful_requests / max(self.metrics.total_requests, 1)
            ) * 100,
            "average_response_time": self.metrics.average_response_time,
            "last_error": self.metrics.last_error,
        }

    def _record_failure(self, error_message: str) -> None:
        self.metrics.last_error = error_message
        self.metrics.consecutive_failures += 1
        self.metrics.status = (
            LLMStatus.UNAVAILABLE
            if self.metrics.consecutive_failures >= self._fail_threshold
            else LLMStatus.ERROR
        )


__all__ = ["LLMStatus", "LLMHealthMetrics", "LLMHealthMonitor"]