"""LLM interface — swappable port (Step 4b).

Implementation: IBM watsonx.ai via ``ibm-watsonx-ai`` SDK (ModelInference).
Replaceable by a test stub or 'off' mode without modifying the rest of the code.

Implements specifications §8.1–§8.8 and architecture Annex A.3 / §8 (Step 4b).

Key behaviours
--------------
- **Off mode** (``llm.enabled = false``): every call raises :exc:`LLMDegradedError`
  immediately — no network, no SDK import.
- **Per-run content-hash cache**: second ``generate()`` call with the same
  ``content_hash`` returns the cached result without any network call (§8.6).
- **Retries + backoff**: 5xx / timeout / network errors → up to ``max_retries``
  retries with delays from :class:`~invest_watch.error_manager.ExponentialBackoff`
  (2 s, 8 s, 30 s by default). 4xx / quota errors → no retry (§8.5).
- **Circuit-breaker**: delegates to :class:`~invest_watch.error_manager.CircuitBreaker`;
  ``circuit_breaker_threshold`` consecutive failures → OPEN →
  :exc:`LLMDegradedError` for the rest of the run.  Resets on
  :meth:`WatsonxLLMClient.reset_circuit_breaker`.
- **Lazy SDK init**: ``ibm_watsonx_ai`` is imported only on the first real call,
  so the application starts even when the package is absent (degraded mode).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .error_manager import CircuitBreaker, ExponentialBackoff
from .exceptions import CircuitBreakerOpenError

logger = logging.getLogger(__name__)


# ── Public exception ──────────────────────────────────────────────────────────


class LLMDegradedError(Exception):
    """Raised when the LLM is unavailable: off mode, circuit open, or exhausted retries."""


# ── Configuration ─────────────────────────────────────────────────────────────


@dataclass
class LLMClientConfig:
    """All runtime settings for the LLM client.

    Built from :class:`~invest_watch.config_manager.LLMSection` by
    :func:`llm_client_config_from_section`.  Kept separate so the client
    module has no import dependency on config_manager.
    """

    enabled: bool = True
    model_id: str = "ibm/granite-3-8b-instruct"
    url: str = "https://eu-de.ml.cloud.ibm.com"
    project_id: str = ""
    api_key: str = ""                              # from env only — never TOML
    max_new_tokens: int = 700
    temperature: float = 0.1
    top_p: float = 0.9
    timeout_seconds: int = 120
    max_retries: int = 2                           # → 3 total attempts
    circuit_breaker_threshold: int = 3
    # Injected in tests to skip real sleeps
    _backoff_delays: list[float] | None = field(default=None, repr=False)


# ── WatsonxLLMClient ──────────────────────────────────────────────────────────


class WatsonxLLMClient:
    """Concrete LLM implementation via the ibm-watsonx-ai SDK (ModelInference).

    The class owns a :class:`~invest_watch.error_manager.CircuitBreaker` and
    an in-run content-hash cache.  Both are reset when
    :meth:`reset_circuit_breaker` is called (orchestrator: start of each run).
    """

    def __init__(
        self,
        config: LLMClientConfig,
        _sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._cfg = config
        self._sleep = _sleep or __import__("time").sleep
        self._model: Any = None  # lazy — imported on first real call

        # Per-run memory cache: content_hash → raw generated text
        self._cache: dict[str, str] = {}

        # Circuit-breaker — threshold from config, no auto half-open in V1
        self._cb = CircuitBreaker(
            threshold=config.circuit_breaker_threshold,
            name="llm",
        )

    # ── public interface ──────────────────────────────────────────────────

    def generate(self, prompt: str, content_hash: str | None = None) -> str:
        """Send *prompt* to watsonx.ai and return the raw generated text.

        Args:
            prompt:       Full prompt string (system + user combined or plain).
            content_hash: SHA-256 hex of the article content (§8.6 cache key).
                          When provided and already cached, returns immediately
                          without a network call.

        Returns:
            Raw text string as returned by the model.

        Raises:
            LLMDegradedError: LLM is disabled, circuit is open, or all retries
                              exhausted.
        """
        # Off mode
        if not self._cfg.enabled:
            raise LLMDegradedError("LLM disabled (llm.enabled = false)")

        # Circuit-breaker gate
        try:
            self._cb.before_call()
        except CircuitBreakerOpenError as exc:
            raise LLMDegradedError(str(exc)) from exc

        # Content-hash cache hit
        if content_hash and content_hash in self._cache:
            logger.debug("LLM cache hit for hash %.12s…", content_hash)
            return self._cache[content_hash]

        # Attempt loop: initial attempt + max_retries retry slots
        backoff_delays = self._cfg._backoff_delays or [2.0, 8.0, 30.0]
        backoff = ExponentialBackoff(delays=backoff_delays)
        backoff_iter = iter(backoff)

        last_exc: Exception | None = None
        total_attempts = self._cfg.max_retries + 1  # e.g. max_retries=2 → 3 attempts

        for attempt in range(total_attempts):
            try:
                model = self._get_model()
                response = model.generate(prompt=prompt)
                result: str = response["results"][0]["generated_text"]

                self._cb.on_success()
                if content_hash:
                    self._cache[content_hash] = result
                logger.debug(
                    "LLM call succeeded on attempt %d/%d", attempt + 1, total_attempts
                )
                return result

            except (CircuitBreakerOpenError, LLMDegradedError):
                raise  # already handled upstream — do not retry

            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "LLM error (attempt %d/%d): %s", attempt + 1, total_attempts, exc
                )

                # No retry on 4xx / quota errors
                status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
                if status is not None and 400 <= int(status) < 500:
                    logger.info("LLM 4xx error — not retrying: %s", exc)
                    break

                # Record failure in circuit-breaker (may raise CircuitBreakerOpenError)
                try:
                    self._cb.on_failure(exc)
                except CircuitBreakerOpenError as cb_exc:
                    raise LLMDegradedError(str(cb_exc)) from exc

                # Sleep before next attempt (skip on last attempt)
                if attempt < total_attempts - 1:
                    try:
                        delay = next(backoff_iter)
                        logger.info("Backoff %.1f s before attempt %d", delay, attempt + 2)
                        self._sleep(delay)
                    except StopIteration:
                        pass  # no more delay slots

        raise LLMDegradedError(
            f"LLM unavailable after {total_attempts} attempt(s)"
        ) from last_exc

    def generate_json(
        self,
        prompt: str,
        content_hash: str | None = None,
    ) -> dict[str, Any]:
        """Call the model and parse the result as JSON (spec §8.4).

        Workflow:
        1. Call :meth:`generate` → raw text.
        2. Attempt ``json.loads`` on the raw text.
        3. If parsing fails, send one corrective follow-up prompt:
           ``"The JSON was invalid: fix it and return only valid JSON."``
        4. If the corrective attempt also fails (parse error or
           :exc:`LLMDegradedError`), log an error and raise
           :exc:`LLMDegradedError` so callers switch to degraded mode.

        Args:
            prompt:       Full prompt string.
            content_hash: Optional cache key (see :meth:`generate`).

        Returns:
            Parsed ``dict``.

        Raises:
            LLMDegradedError: LLM unavailable, or JSON could not be fixed.
        """
        raw = self.generate(prompt, content_hash=content_hash)

        # First parse attempt
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "LLM returned invalid JSON (hash=%.12s…); sending corrective prompt",
                content_hash or "none",
            )

        # One corrective attempt — no content_hash to avoid caching the bad result
        corrective_prompt = (
            f"The JSON you returned was invalid. "
            f"Fix it and return ONLY valid JSON, nothing else.\n\n{raw}"
        )
        try:
            corrected = self.generate(corrective_prompt)
            result = json.loads(corrected)
            logger.info(
                "LLM corrective attempt succeeded (hash=%.12s…)", content_hash or "none"
            )
            # Cache the corrected result under the original hash
            if content_hash:
                self._cache[content_hash] = corrected
            return result
        except json.JSONDecodeError as exc:
            logger.error(
                "LLM corrective attempt also returned invalid JSON "
                "(hash=%.12s…) — switching to degraded mode",
                content_hash or "none",
            )
            raise LLMDegradedError(
                "LLM returned unparseable JSON after corrective attempt"
            ) from exc
        except LLMDegradedError:
            logger.error(
                "LLM unavailable during corrective attempt (hash=%.12s…)",
                content_hash or "none",
            )
            raise

    # ── properties ────────────────────────────────────────────────────────

    @property
    def is_degraded(self) -> bool:
        """True when the circuit-breaker is OPEN (rest of the run is degraded)."""
        return self._cb.is_open

    @property
    def cache_size(self) -> int:
        """Number of entries currently in the per-run content-hash cache."""
        return len(self._cache)

    # ── orchestrator control ──────────────────────────────────────────────

    def reset_circuit_breaker(self) -> None:
        """Reset circuit-breaker and cache — called by the orchestrator at run start."""
        self._cb.reset()
        self._cache.clear()

    # ── lazy SDK init ─────────────────────────────────────────────────────

    def _get_model(self) -> Any:
        """Lazily instantiate the SDK ModelInference object.

        The import is deferred so that the application starts normally even
        when ``ibm-watsonx-ai`` is not installed (degraded / off mode).
        """
        if self._model is not None:
            return self._model

        try:
            from ibm_watsonx_ai import Credentials  # type: ignore[import-untyped]
            from ibm_watsonx_ai.foundation_models import ModelInference  # type: ignore[import-untyped]
            from ibm_watsonx_ai.metanames import GenTextParamsMetaNames as GenParams  # type: ignore[import-untyped]
        except ImportError as exc:
            raise LLMDegradedError(
                "ibm-watsonx-ai SDK not installed — run: pip install ibm-watsonx-ai"
            ) from exc

        credentials = Credentials(
            url=self._cfg.url,
            api_key=self._cfg.api_key,
        )
        params = {
            GenParams.MAX_NEW_TOKENS: self._cfg.max_new_tokens,
            GenParams.TEMPERATURE: self._cfg.temperature,
            GenParams.TOP_P: self._cfg.top_p,
        }
        self._model = ModelInference(
            model_id=self._cfg.model_id,
            credentials=credentials,
            params=params,
            project_id=self._cfg.project_id,
        )
        logger.info(
            "LLM initialised: model=%s url=%s", self._cfg.model_id, self._cfg.url
        )
        return self._model


# ── Factory ───────────────────────────────────────────────────────────────────


def build_llm_client(config: LLMClientConfig) -> WatsonxLLMClient:
    """Production factory. Replace with a stub in tests."""
    return WatsonxLLMClient(config)


def llm_client_config_from_section(section: Any) -> LLMClientConfig:
    """Build an :class:`LLMClientConfig` from a :class:`~invest_watch.config_manager.LLMSection`.

    This adapter keeps ``llm_client`` free of any import dependency on
    ``config_manager``.
    """
    return LLMClientConfig(
        enabled=section.enabled,
        model_id=section.model,
        url=section.endpoint,
        project_id=section.project_id,
        api_key=section.api_key,
        max_new_tokens=section.max_new_tokens,
        temperature=section.temperature,
        top_p=section.top_p,
        timeout_seconds=section.timeout_seconds,
        max_retries=section.max_retries,
        circuit_breaker_threshold=section.circuit_breaker_threshold,
    )
