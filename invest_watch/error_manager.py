"""Resilience primitives: ProcessingError, ExponentialBackoff, CircuitBreaker.

Implements specifications §8.5 and architecture §4 / §8 (Step 4a).

These classes are **source-agnostic**: they work for LLM calls (Step 4b),
HTTP fetches, or any other operation that needs retry + circuit-breaker logic.
They carry no business knowledge — they only count failures and enforce timing.

States of a CircuitBreaker
--------------------------
CLOSED    — normal operation; failures are counted.
OPEN      — threshold reached; calls refused immediately (raises
             CircuitBreakerOpenError) until the recovery timeout elapses.
HALF_OPEN — one probe call is allowed; success → CLOSED, failure → OPEN again.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable

from .exceptions import CircuitBreakerOpenError

logger = logging.getLogger(__name__)


# ── ProcessingError ───────────────────────────────────────────────────────────


@dataclass
class ProcessingError:
    """Records a single processing failure for a named source or component.

    Used by the orchestrator to populate ``processing_error`` in the database
    and to decide whether to mark a run as ``partial`` or ``failed``.

    Attributes:
        source:     Logical name of the failing component (e.g. source URL,
                    "llm", "dedup").
        error_type: Short error category (e.g. "timeout", "http_5xx",
                    "json_invalid", "circuit_open").
        message:    Human-readable detail (never contains secrets).
        retries:    Number of retry attempts made before giving up.
        recovered:  True if the run continued despite this error (degraded mode).
    """

    source: str
    error_type: str
    message: str
    retries: int = 0
    recovered: bool = False

    def as_dict(self) -> dict:
        return {
            "source": self.source,
            "error_type": self.error_type,
            "message": self.message,
            "retries": self.retries,
            "recovered": self.recovered,
        }


# ── ExponentialBackoff ────────────────────────────────────────────────────────


@dataclass
class ExponentialBackoff:
    """Iterator-style exponential backoff with a configurable schedule.

    Spec §8.5: 3 retries with inter-attempt delays 2 s, 8 s, 30 s.
    The default ``delays`` match the spec exactly.  Pass a custom list to
    override (e.g. ``[0, 0, 0]`` in tests to avoid real sleeps).

    Usage::

        backoff = ExponentialBackoff()
        for delay in backoff:
            time.sleep(delay)
            try:
                result = do_work()
                break
            except RecoverableError:
                continue  # exhausts the iterator after len(delays) rounds
    """

    delays: list[float] = field(default_factory=lambda: [2.0, 8.0, 30.0])
    _index: int = field(default=0, init=False, repr=False)

    def __iter__(self) -> ExponentialBackoff:
        self._index = 0
        return self

    def __next__(self) -> float:
        if self._index >= len(self.delays):
            raise StopIteration
        delay = self.delays[self._index]
        self._index += 1
        return delay

    @property
    def remaining(self) -> int:
        """Number of delay slots not yet consumed."""
        return max(0, len(self.delays) - self._index)

    def reset(self) -> None:
        """Restart the iterator from the first delay."""
        self._index = 0


# ── CircuitBreaker ────────────────────────────────────────────────────────────


class CircuitState(Enum):
    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


class CircuitBreaker:
    """Three-state circuit breaker (CLOSED → OPEN → HALF_OPEN → CLOSED).

    Spec §8.5:
    - ``threshold`` consecutive failures → OPEN.
    - Any success resets the failure counter → CLOSED.
    - ``recovery_timeout > 0``: after this many seconds in OPEN state the
      breaker promotes automatically to HALF_OPEN to allow one probe call.
      Set to ``0`` (default) to disable automatic promotion (manual reset only).

    Typical integration::

        cb = CircuitBreaker(threshold=3, name="llm")
        try:
            cb.before_call()          # raises CircuitBreakerOpenError if OPEN
            result = do_work()
            cb.on_success()
        except CircuitBreakerOpenError:
            # handle degraded mode without re-raising
        except Exception as exc:
            cb.on_failure(exc)        # may raise CircuitBreakerOpenError
            raise
    """

    def __init__(
        self,
        threshold: int = 3,
        recovery_timeout: float = 0.0,
        name: str = "unnamed",
        _clock: Callable[[], float] | None = None,
    ) -> None:
        self.threshold = threshold
        self.recovery_timeout = recovery_timeout
        self.name = name
        self._clock = _clock or time.monotonic

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._opened_at: float | None = None

    # ── public state ──────────────────────────────────────────────────────

    @property
    def state(self) -> CircuitState:
        """Current state, with automatic HALF_OPEN promotion when due."""
        if (
            self._state is CircuitState.OPEN
            and self.recovery_timeout > 0
            and self._opened_at is not None
            and self._clock() - self._opened_at >= self.recovery_timeout
        ):
            self._state = CircuitState.HALF_OPEN
            logger.info("CircuitBreaker[%s] → HALF_OPEN (probe allowed)", self.name)
        return self._state

    @property
    def is_open(self) -> bool:
        """True only when state is OPEN (not HALF_OPEN)."""
        return self.state is CircuitState.OPEN

    @property
    def failure_count(self) -> int:
        return self._failure_count

    # ── call hooks ────────────────────────────────────────────────────────

    def before_call(self) -> None:
        """Call before every protected operation.

        Raises :exc:`CircuitBreakerOpenError` when OPEN.
        In HALF_OPEN state the call proceeds (probe).
        """
        current = self.state  # triggers automatic half-open promotion
        if current is CircuitState.OPEN:
            raise CircuitBreakerOpenError(
                f"CircuitBreaker[{self.name}] is OPEN after "
                f"{self._failure_count} consecutive failures"
            )

    def on_success(self) -> None:
        """Record a successful call — resets the breaker to CLOSED."""
        if self._state is not CircuitState.CLOSED:
            logger.info(
                "CircuitBreaker[%s] → CLOSED (recovered from %s)",
                self.name,
                self._state.name,
            )
        self._failure_count = 0
        self._opened_at = None
        self._state = CircuitState.CLOSED

    def on_failure(self, exc: BaseException | None = None) -> None:
        """Record a failed call.

        Increments the failure counter.  When ``failure_count`` reaches
        ``threshold`` the circuit opens and :exc:`CircuitBreakerOpenError`
        is raised so the caller can switch to degraded mode immediately.
        """
        self._failure_count += 1
        logger.warning(
            "CircuitBreaker[%s] failure %d/%d: %s",
            self.name,
            self._failure_count,
            self.threshold,
            exc,
        )

        if self._failure_count >= self.threshold:
            self._state = CircuitState.OPEN
            self._opened_at = self._clock()
            logger.error(
                "CircuitBreaker[%s] → OPEN after %d consecutive failures",
                self.name,
                self._failure_count,
            )
            raise CircuitBreakerOpenError(
                f"CircuitBreaker[{self.name}] opened after "
                f"{self._failure_count} consecutive failures"
            ) from exc

    # ── manual control ────────────────────────────────────────────────────

    def reset(self) -> None:
        """Manually reset to CLOSED — called by the orchestrator at run start."""
        self._failure_count = 0
        self._opened_at = None
        self._state = CircuitState.CLOSED
        logger.info("CircuitBreaker[%s] manually reset → CLOSED", self.name)
