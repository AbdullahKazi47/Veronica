import time

import pytest

from veronica.runtime.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)


def test_closed_runs_call():
    breaker = CircuitBreaker(failure_threshold=2)
    assert breaker.call(lambda: "result") == "result"
    assert breaker.state == CircuitState.CLOSED
    assert breaker.total_calls == 1


def test_opens_after_threshold_failures():
    breaker = CircuitBreaker(failure_threshold=2)
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert breaker.state == CircuitState.CLOSED  # one failure, not yet open
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert breaker.state == CircuitState.OPEN
    assert breaker.failure_count == 2


def test_open_refuses_calls():
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=60)
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: "should never run")


def test_success_resets_failure_count():
    breaker = CircuitBreaker(failure_threshold=2)
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert breaker.call(lambda: "ok") == "ok"
    assert breaker.failure_count == 0
    assert breaker.state == CircuitState.CLOSED


def test_half_open_trial_succeeds_closes():
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=0.01)
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert breaker.state == CircuitState.OPEN

    # First refusal happens before the timeout elapses.
    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: "nope")

    # After the tiny timeout, a trial runs; success flips back to CLOSED.
    time.sleep(0.02)
    assert breaker.call(lambda: "recovered") == "recovered"
    assert breaker.state == CircuitState.CLOSED


def test_half_open_trial_failure_returns_to_open():
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=0.01)
    breaker.reset_timeout_secs_for_test = None  # no-op, keep simple
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    time.sleep(0.02)
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("still bad")))
    assert breaker.state == CircuitState.OPEN
    # Now refuses again.
    with pytest.raises(CircuitOpenError):
        breaker.call(lambda: "nope")


def test_reset_force_closes():
    breaker = CircuitBreaker(failure_threshold=1, reset_timeout_s=60)
    with pytest.raises(RuntimeError):
        breaker.call(lambda: (_ for _ in ()).throw(RuntimeError("x")))
    assert breaker.state == CircuitState.OPEN
    breaker.reset()
    assert breaker.state == CircuitState.CLOSED
    assert breaker.call(lambda: "fine") == "fine"


def test_manual_record_modes():
    breaker = CircuitBreaker(failure_threshold=2)
    breaker.record_failure()
    assert breaker.state == CircuitState.CLOSED
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN
    # Force the reset timeout to have elapsed; the successful trial then closes it.
    breaker._opened_at = time.monotonic() - 100.0
    assert breaker.call(lambda: "x") == "x"
    assert breaker.state == CircuitState.CLOSED
    assert breaker.total_calls == 3  # 2 manual failures + 1 successful trial
    assert breaker.failed_calls == 2