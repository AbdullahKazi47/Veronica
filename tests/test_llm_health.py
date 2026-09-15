import time

import pytest

from veronica.llm.base import ChatMessage, LLMProvider, LLMResponse
from veronica.runtime.llm_health import LLMHealthMonitor, LLMStatus


class HealthyLLM(LLMProvider):
    name = "healthy"

    def chat(self, messages, tools=None, temperature=0.4):
        self._last_round = len(messages)
        return LLMResponse(content="OK")


class FailingLLM(LLMProvider):
    name = "failing"

    def chat(self, messages, tools=None, temperature=0.4):
        return LLMResponse(content="")


class RaisingLLM(LLMProvider):
    name = "raising"

    def chat(self, messages, tools=None, temperature=0.4):
        raise ConnectionError("simulated outage")


def test_initial_state_is_initializing():
    monitor = LLMHealthMonitor(HealthyLLM())
    assert monitor.metrics.status == LLMStatus.INITIALIZING
    assert not monitor.is_available()


def test_success_marks_available():
    monitor = LLMHealthMonitor(HealthyLLM())
    m = monitor.check_health()
    assert m.status == LLMStatus.AVAILABLE
    assert m.consecutive_failures == 0
    assert m.successful_requests == 1
    assert monitor.is_available()


def test_empty_response_is_failure():
    monitor = LLMHealthMonitor(FailingLLM())
    m = monitor.check_health()
    assert m.status == LLMStatus.ERROR
    assert m.consecutive_failures == 1
    assert not monitor.is_available()


def test_raised_exception_is_failure():
    monitor = LLMHealthMonitor(RaisingLLM())
    m = monitor.check_health()
    assert m.status == LLMStatus.ERROR
    assert "simulated outage" in m.last_error


def test_unavailable_after_threshold():
    monitor = LLMHealthMonitor(FailingLLM(), consecutive_failure_threshold=3)
    assert monitor.check_health().status == LLMStatus.ERROR
    assert monitor.check_health().status == LLMStatus.ERROR
    m = monitor.check_health()
    assert m.status == LLMStatus.UNAVAILABLE
    assert m.consecutive_failures == 3


def test_success_resets_failure_counter_and_unavailability():
    class WobbleLLM(LLMProvider):
        name = "wobble"
        calls = 0

        def chat(self, messages, tools=None, temperature=0.4):
            type(self).calls += 1
            return LLMResponse(content="OK" if type(self).calls >= 3 else "")

    monitor = LLMHealthMonitor(WobbleLLM(), consecutive_failure_threshold=2)
    monitor.check_health()
    assert not monitor.is_available()
    monitor.check_health()
    assert not monitor.is_available()
    assert monitor.metrics.status == LLMStatus.UNAVAILABLE
    # Third call succeeds → back to AVAILABLE, counter zeroed.
    monitor.check_health()
    assert monitor.is_available()
    assert monitor.metrics.consecutive_failures == 0


def test_average_response_time_evolves_with_ema():
    class SlowLLM(LLMProvider):
        name = "slow"

        def chat(self, messages, tools=None, temperature=0.4):
            time.sleep(0.01)
            return LLMResponse(content="OK")

    monitor = LLMHealthMonitor(SlowLLM(), ema_alpha=0.5)
    monitor.check_health()
    first = monitor.metrics.average_response_time
    assert first > 0.0
    monitor.check_health()
    assert monitor.metrics.average_response_time > 0.0


def test_mark_unavailable_fast_path():
    monitor = LLMHealthMonitor(FailingLLM(), consecutive_failure_threshold=3)
    monitor.mark_unavailable("real request failed")
    assert monitor.metrics.consecutive_failures == 1
    # Failure from a real call without a full probe.
    assert "real request failed" in monitor.metrics.last_error