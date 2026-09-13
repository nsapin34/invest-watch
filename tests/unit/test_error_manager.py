"""Unit tests for error_manager module (Step 4a).

Covers:
- ProcessingError dataclass and as_dict()
- ExponentialBackoff iterator protocol (default spec delays + custom)
- CircuitBreaker state machine: CLOSED → OPEN → HALF_OPEN → CLOSED
- Per-source isolation: independent CircuitBreaker instances
- reset() and manual control
"""

from __future__ import annotations

import pytest

from invest_watch.error_manager import (
    CircuitBreaker,
    CircuitState,
    ExponentialBackoff,
    ProcessingError,
)
from invest_watch.exceptions import CircuitBreakerOpenError


# ── ProcessingError ───────────────────────────────────────────────────────────


class TestProcessingError:
    def test_defaults(self):
        err = ProcessingError(source="rss://hkex.test", error_type="timeout", message="read timed out")
        assert err.retries == 0
        assert err.recovered is False

    def test_as_dict_roundtrip(self):
        err = ProcessingError(
            source="llm",
            error_type="http_5xx",
            message="503 Service Unavailable",
            retries=3,
            recovered=True,
        )
        d = err.as_dict()
        assert d == {
            "source": "llm",
            "error_type": "http_5xx",
            "message": "503 Service Unavailable",
            "retries": 3,
            "recovered": True,
        }

    def test_per_source_isolation(self):
        """Two ProcessingError instances for different sources are independent."""
        e1 = ProcessingError(source="source_a", error_type="timeout", message="a")
        e2 = ProcessingError(source="source_b", error_type="http_5xx", message="b")
        assert e1.source != e2.source
        e1.recovered = True
        assert e2.recovered is False


# ── ExponentialBackoff ────────────────────────────────────────────────────────


class TestExponentialBackoff:
    def test_default_delays_match_spec(self):
        """Spec §8.5: delays must be 2 s, 8 s, 30 s."""
        backoff = ExponentialBackoff()
        assert list(backoff) == [2.0, 8.0, 30.0]

    def test_custom_delays(self):
        backoff = ExponentialBackoff(delays=[0.0, 0.0])
        assert list(backoff) == [0.0, 0.0]

    def test_iterator_exhaustion_stops(self):
        backoff = ExponentialBackoff(delays=[1.0])
        it = iter(backoff)
        assert next(it) == 1.0
        with pytest.raises(StopIteration):
            next(it)

    def test_reset_restarts_from_beginning(self):
        backoff = ExponentialBackoff(delays=[1.0, 2.0])
        it = iter(backoff)
        next(it)  # consume first
        backoff.reset()
        assert list(backoff) == [1.0, 2.0]

    def test_remaining_decrements(self):
        backoff = ExponentialBackoff(delays=[1.0, 2.0, 3.0])
        it = iter(backoff)
        assert backoff.remaining == 3
        next(it)
        assert backoff.remaining == 2
        next(it)
        assert backoff.remaining == 1
        next(it)
        assert backoff.remaining == 0

    def test_reuse_via_iter(self):
        """Calling iter() again on the same instance restarts cleanly."""
        backoff = ExponentialBackoff(delays=[5.0, 10.0])
        assert list(backoff) == [5.0, 10.0]
        assert list(backoff) == [5.0, 10.0]  # second pass


# ── CircuitBreaker — basic state machine ──────────────────────────────────────


class TestCircuitBreakerStateMachine:
    def test_starts_closed(self):
        cb = CircuitBreaker(threshold=3, name="test")
        assert cb.state is CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_success_keeps_closed(self):
        cb = CircuitBreaker(threshold=3)
        cb.before_call()
        cb.on_success()
        assert cb.state is CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_failures_below_threshold_stay_closed(self):
        cb = CircuitBreaker(threshold=3)
        for _ in range(2):
            cb.on_failure()
        assert cb.state is CircuitState.CLOSED
        assert cb.failure_count == 2

    def test_threshold_reached_opens_circuit(self):
        cb = CircuitBreaker(threshold=3)
        cb.on_failure()
        cb.on_failure()
        with pytest.raises(CircuitBreakerOpenError):
            cb.on_failure()  # 3rd failure → OPEN + raises
        assert cb.state is CircuitState.OPEN

    def test_open_circuit_refuses_calls(self):
        cb = CircuitBreaker(threshold=2)
        cb.on_failure()
        with pytest.raises(CircuitBreakerOpenError):
            cb.on_failure()  # opens
        with pytest.raises(CircuitBreakerOpenError):
            cb.before_call()  # still OPEN

    def test_success_resets_failure_counter(self):
        cb = CircuitBreaker(threshold=3)
        cb.on_failure()
        cb.on_failure()
        assert cb.failure_count == 2
        cb.on_success()
        assert cb.failure_count == 0
        assert cb.state is CircuitState.CLOSED

    def test_manual_reset_from_open(self):
        cb = CircuitBreaker(threshold=1)
        with pytest.raises(CircuitBreakerOpenError):
            cb.on_failure()
        assert cb.state is CircuitState.OPEN
        cb.reset()
        assert cb.state is CircuitState.CLOSED
        assert cb.failure_count == 0
        cb.before_call()  # must not raise

    def test_is_open_property(self):
        cb = CircuitBreaker(threshold=1)
        assert cb.is_open is False
        with pytest.raises(CircuitBreakerOpenError):
            cb.on_failure()
        assert cb.is_open is True
        cb.reset()
        assert cb.is_open is False


# ── CircuitBreaker — HALF_OPEN promotion ──────────────────────────────────────


class TestCircuitBreakerHalfOpen:
    def _make_clock(self, start: float = 0.0):
        """Returns a mutable clock callable."""
        state = [start]

        def clock():
            return state[0]

        def advance(seconds: float):
            state[0] += seconds

        return clock, advance

    def test_half_open_after_recovery_timeout(self):
        clock, advance = self._make_clock()
        cb = CircuitBreaker(threshold=1, recovery_timeout=10.0, _clock=clock)

        with pytest.raises(CircuitBreakerOpenError):
            cb.on_failure()  # → OPEN
        assert cb.state is CircuitState.OPEN

        advance(9.9)
        assert cb.state is CircuitState.OPEN  # not yet

        advance(0.2)  # total 10.1 s ≥ 10.0 s
        assert cb.state is CircuitState.HALF_OPEN

    def test_half_open_probe_success_closes(self):
        clock, advance = self._make_clock()
        cb = CircuitBreaker(threshold=1, recovery_timeout=5.0, _clock=clock)

        with pytest.raises(CircuitBreakerOpenError):
            cb.on_failure()
        advance(6.0)
        assert cb.state is CircuitState.HALF_OPEN

        cb.before_call()   # probe allowed
        cb.on_success()
        assert cb.state is CircuitState.CLOSED

    def test_half_open_probe_failure_reopens(self):
        clock, advance = self._make_clock()
        cb = CircuitBreaker(threshold=1, recovery_timeout=5.0, _clock=clock)

        with pytest.raises(CircuitBreakerOpenError):
            cb.on_failure()
        advance(6.0)
        assert cb.state is CircuitState.HALF_OPEN

        # Probe fails → back to OPEN immediately (threshold already met)
        with pytest.raises(CircuitBreakerOpenError):
            cb.on_failure()
        assert cb.state is CircuitState.OPEN

    def test_no_auto_halfopen_when_timeout_zero(self):
        clock, advance = self._make_clock()
        cb = CircuitBreaker(threshold=1, recovery_timeout=0.0, _clock=clock)

        with pytest.raises(CircuitBreakerOpenError):
            cb.on_failure()
        advance(9999.0)
        assert cb.state is CircuitState.OPEN  # still OPEN, must reset manually


# ── CircuitBreaker — per-source isolation ────────────────────────────────────


class TestCircuitBreakerIsolation:
    def test_two_breakers_are_independent(self):
        """Failures on one breaker must not affect another (spec §8.5)."""
        cb_llm = CircuitBreaker(threshold=3, name="llm")
        cb_rss = CircuitBreaker(threshold=3, name="rss")

        cb_llm.on_failure()
        cb_llm.on_failure()
        with pytest.raises(CircuitBreakerOpenError):
            cb_llm.on_failure()  # llm opens

        # rss breaker completely unaffected
        assert cb_rss.state is CircuitState.CLOSED
        assert cb_rss.failure_count == 0
        cb_rss.before_call()  # must not raise

    def test_reset_on_one_does_not_affect_other(self):
        cb_a = CircuitBreaker(threshold=1, name="a")
        cb_b = CircuitBreaker(threshold=1, name="b")

        with pytest.raises(CircuitBreakerOpenError):
            cb_a.on_failure()
        with pytest.raises(CircuitBreakerOpenError):
            cb_b.on_failure()

        cb_a.reset()
        assert cb_a.state is CircuitState.CLOSED
        assert cb_b.state is CircuitState.OPEN  # b still open


# ── Integration: backoff + circuit-breaker together ───────────────────────────


class TestBackoffWithCircuitBreaker:
    def test_three_retries_then_circuit_opens(self):
        """Simulate spec §8.5 scenario: 3 attempts → circuit-breaker activates."""
        cb = CircuitBreaker(threshold=3, name="llm")
        backoff = ExponentialBackoff(delays=[0.0, 0.0, 0.0])  # instant in tests

        circuit_opened = False
        attempts = 0

        for delay in backoff:
            # delay would normally be time.sleep(delay)
            attempts += 1
            try:
                cb.before_call()
                # simulate a 5xx failure every time
                try:
                    raise RuntimeError("503 Service Unavailable")
                except RuntimeError as exc:
                    cb.on_failure(exc)
            except CircuitBreakerOpenError:
                circuit_opened = True
                break

        assert circuit_opened
        assert attempts == 3
        assert cb.state is CircuitState.OPEN

    def test_success_on_second_attempt_resets_counter(self):
        cb = CircuitBreaker(threshold=3, name="llm")
        backoff = ExponentialBackoff(delays=[0.0, 0.0])

        for i, _delay in enumerate(backoff):
            try:
                cb.before_call()
                if i == 0:
                    cb.on_failure(RuntimeError("transient"))
                else:
                    cb.on_success()
                    break
            except CircuitBreakerOpenError:
                pytest.fail("Circuit opened prematurely")

        assert cb.state is CircuitState.CLOSED
        assert cb.failure_count == 0
