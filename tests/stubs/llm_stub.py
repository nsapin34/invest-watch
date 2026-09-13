"""LLM stub — zero-network, configurable raise_after (specs §15, PROJECT_STRUCTURE).

Drop-in replacement for :class:`~invest_watch.llm_client.WatsonxLLMClient`.
Every test that touches the LLM path imports :class:`LLMStub` and injects it
instead of opening a real watsonx.ai connection.

Usage::

    stub = LLMStub(responses=["...json...", "...json..."])
    result = stub.generate("some prompt", content_hash="abc123")

    # Force a failure on the 3rd call:
    stub = LLMStub(raise_after=2)

    # Force immediate failure:
    stub = LLMStub(raise_after=0)

Interface contract
------------------
``LLMStub`` mirrors the **full public surface** of :class:`WatsonxLLMClient`:
- :meth:`generate(prompt, content_hash=None) -> str`
- :meth:`generate_json(prompt, content_hash=None) -> dict`
- :attr:`is_degraded` → bool
- :attr:`cache_size` → int   (always 0 — stub has no real cache)
- :meth:`reset_circuit_breaker()` — resets call counter and degraded flag
- :attr:`call_count` → int   (test-only helper)
- :attr:`call_log`   → list  (test-only helper, each entry has ``prompt`` +
  ``content_hash`` keys)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from invest_watch.llm_client import LLMDegradedError


@dataclass
class LLMStub:
    """Deterministic stand-in for :class:`~invest_watch.llm_client.WatsonxLLMClient`.

    Args:
        responses:   Ordered list of raw text responses to return on successive
                     :meth:`generate` calls.  When exhausted the stub returns
                     ``"null"`` (degraded-mode fallback).
        raise_after: If set, raise :exc:`~invest_watch.llm_client.LLMDegradedError`
                     on every call **after** this many successful ones
                     (``0`` = fail on the very first call).
        call_log:    Accumulates every call received (populated automatically).
    """

    responses: list[str] = field(default_factory=list)
    raise_after: int | None = None
    call_log: list[dict[str, Any]] = field(default_factory=list)
    _call_count: int = field(default=0, init=False, repr=False)
    _is_degraded: bool = field(default=False, init=False, repr=False)

    # ── public interface (mirrors WatsonxLLMClient) ───────────────────────────

    def generate(self, prompt: str, content_hash: str | None = None) -> str:
        """Return the next canned response or raise after the configured limit."""
        self.call_log.append({"prompt": prompt, "content_hash": content_hash})
        self._call_count += 1
        if self.raise_after is not None and self._call_count > self.raise_after:
            self._is_degraded = True
            raise LLMDegradedError(
                f"LLMStub: forced failure on call #{self._call_count}"
            )
        idx = self._call_count - 1
        if idx < len(self.responses):
            return self.responses[idx]
        return "null"

    @property
    def is_degraded(self) -> bool:
        return self._is_degraded

    @property
    def cache_size(self) -> int:
        """Always 0 — stub has no real per-hash cache."""
        return 0

    def generate_json(
        self, prompt: str, content_hash: str | None = None
    ) -> dict[str, Any]:
        """Call :meth:`generate` and parse as JSON.

        If the canned response is not valid JSON, raises
        :exc:`~invest_watch.llm_client.LLMDegradedError` (mirrors the real
        client's behaviour after the corrective attempt fails).
        """
        raw = self.generate(prompt, content_hash=content_hash)
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            self._is_degraded = True
            raise LLMDegradedError(
                f"LLMStub: response is not valid JSON: {raw!r}"
            ) from exc

    def reset_circuit_breaker(self) -> None:
        """Reset call counter, log and degraded flag (mirrors real client)."""
        self._call_count = 0
        self._is_degraded = False
        self.call_log.clear()

    # ── test-only helpers ─────────────────────────────────────────────────────

    @property
    def call_count(self) -> int:
        return self._call_count

    def reset(self) -> None:
        """Alias for :meth:`reset_circuit_breaker` — kept for backward compat."""
        self.reset_circuit_breaker()
