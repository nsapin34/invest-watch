"""LLM interface — swappable port (Step 4b).

Implementation: IBM watsonx.ai via ``ibm-watsonx-ai`` SDK (ModelInference).
Replaceable by a test stub or 'off' mode without modifying the rest of the code.

Implements specifications §8.1–§8.8 and architecture Annex A.3 / §8 (Step 4b).

Key behaviours
--------------
- **Off mode** (``llm.enabled = false``): every call raises :exc:`LLMDegradedError`
  immediately — no network, no SDK import.
- **Chat endpoint**: all SDK calls go through ``ModelInference.chat()`` (the
  ``/ml/v1/text/chat`` endpoint) via :class:`_SDKChatModel`.  The legacy
  ``/ml/v1/text/generation`` endpoint is deprecated *and* feeds the raw prompt
  to the model without any chat template — instruct models then continue the
  prompt's rule list instead of answering (run #2, 2026-09-15: 51/51 articles
  degraded, instruction leakage inside digests).
- **Per-run content-hash cache**: second ``generate()`` call with the same
  ``content_hash`` returns the cached result without any network call (§8.6).
- **Retries + backoff**: 5xx / timeout / network errors → up to ``max_retries``
  retries with delays from :class:`~invest_watch.error_manager.ExponentialBackoff`
  (2 s, 8 s, 30 s by default). 4xx / quota errors → no retry (§8.5).
- **Truncation**: ``finish_reason=length`` raises :exc:`LLMTruncatedError`
  immediately — a corrective attempt would truncate again.
- **Robust JSON parsing** (:func:`extract_json_payload`): markdown fences and
  preamble/commentary text around the object are stripped before
  ``json.loads``; on failure the first 300 characters of the raw output are
  logged and kept in :attr:`WatsonxLLMClient.last_raw_output`.
- **Circuit-breaker**: delegates to :class:`~invest_watch.error_manager.CircuitBreaker`;
  ``circuit_breaker_threshold`` consecutive *transport* failures → OPEN, and
  the same number of consecutive ``generate_json()`` unparseable outputs
  manually trips the circuit (prompt/model mismatch guard — spec §8.5).
  :exc:`LLMDegradedError` is then raised for the rest of the run.  Resets on
  :meth:`WatsonxLLMClient.reset_circuit_breaker`.
- **Lazy SDK init**: ``ibm_watsonx_ai`` is imported only on the first real call,
  so the application starts even when the package is absent (degraded mode).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .error_manager import CircuitBreaker, ExponentialBackoff
from .exceptions import CircuitBreakerOpenError

logger = logging.getLogger(__name__)


# ── Public exception ──────────────────────────────────────────────────────────


class LLMDegradedError(Exception):
    """Raised when the LLM is unavailable: off mode, circuit open, or exhausted retries."""


class LLMTruncatedError(LLMDegradedError):
    """Raised when the model hit the token limit (finish_reason=length).

    Deliberately a subclass of :exc:`LLMDegradedError` (callers degrade) but
    with a distinct type so logs/processing_error can tell "output truncated —
    raise llm.max_new_tokens" apart from a transport failure.  Not retried:
    a corrective attempt would truncate again.
    """


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
    # 3000: article-JSON stops at EOS (~600-900 tokens); per-company/market
    # digests (up to 25 article blocks each) legitimately need 1200+.
    max_new_tokens: int = 3000
    temperature: float = 0.1
    top_p: float = 0.9
    timeout_seconds: int = 120
    max_retries: int = 2                           # → 3 total attempts
    circuit_breaker_threshold: int = 3
    # Injected in tests to skip real sleeps
    _backoff_delays: list[float] | None = field(default=None, repr=False)


# ── JSON payload extraction (robust to fences / preamble text) ────────────────


_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\s*")


def extract_json_payload(raw: str) -> dict[str, Any]:
    """Extract and parse a JSON object from an LLM response.

    Handles the common LLM quirks that make ``json.loads`` fail on an
    otherwise-correct payload:
    * Markdown code fences (`` ```json ... ``` ``)
    * Preamble / commentary text before or after the object
    * Trailing commas are NOT fixed — truly malformed JSON still raises.

    Raises:
        json.JSONDecodeError (or ValueError): no balanced JSON object found.
    """
    if raw is None:
        raise ValueError("empty LLM response")
    text = _FENCE_RE.sub("", raw).replace("```", "").strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        result = None
    if isinstance(result, dict):
        return result
    # Locate the outermost balanced {...} block and retry.
    start = text.find("{")
    if start == -1:
        raise ValueError(f"no JSON object found in response: {text[:200]!r}")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                return json.loads(candidate)  # raises JSONDecodeError if broken
    raise ValueError(f"unbalanced JSON object in response: {text[:200]!r}")


# ── SDK chat adapter ──────────────────────────────────────────────────────────


class _SDKChatModel:
    """Adapter exposing a ``generate(prompt=...)`` surface over the SDK chat API.

    The watsonx.ai ``/ml/v1/text/generation`` endpoint is deprecated and — more
    importantly — it feeds the raw prompt to the model *without any chat
    template*.  Instruct models (e.g. Mistral Small 3.1) then continue the
    prompt's "rules" section instead of answering, producing unparseable
    output.  Routing through ``ModelInference.chat()`` (i.e. the
    ``/ml/v1/text/chat`` endpoint with proper system/user messages) fixes both
    the deprecation warning and the degenerate completions.
    """

    def __init__(self, model: Any, config: "LLMClientConfig") -> None:
        self._model = model
        self._cfg = config

    def generate(self, *, prompt: str) -> dict:
        from ibm_watsonx_ai.foundation_models.schema import TextChatParameters  # type: ignore[import-untyped]

        params = TextChatParameters(
            max_tokens=self._cfg.max_new_tokens,
            temperature=self._cfg.temperature,
            top_p=self._cfg.top_p,
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a precise financial-news analyst. Follow the "
                    "user's instructions exactly and never repeat or paraphrase "
                    "the instructions themselves in your answer."
                ),
            },
            {"role": "user", "content": prompt},
        ]
        response = self._model.chat(messages=messages, params=params)
        # Defensive shape check — chat returns {"choices": [{"message": {...}}]}.
        choices = response.get("choices") if isinstance(response, dict) else None
        if not choices:
            raise RuntimeError(f"unexpected chat response shape: {str(response)[:200]}")
        choice = choices[0]
        finish_reason = (choice.get("finish_reason") or "").lower()
        if finish_reason == "length":
            # Raise immediately (no retry): a corrective attempt would truncate again.
            raise LLMTruncatedError(
                "LLM output truncated (finish_reason=length) — "
                f"increase llm.max_new_tokens (current: {self._cfg.max_new_tokens})"
            )
        message = choice.get("message") or {}
        content = message.get("content")
        if content is None:
            raise RuntimeError(f"empty chat message content: {str(response)[:200]}")
        return {"results": [{"generated_text": str(content)}]}


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

        # Consecutive generate_json() failures (both the original and the
        # corrective attempt returned unparseable JSON).  When it reaches the
        # circuit-breaker threshold the circuit is opened — this is the
        # prompt/model mismatch guard (run #2 of 2026-09-15 burned 51 × 2 calls).
        self._json_failure_streak = 0

        # Head of the last unparseable raw response — kept for post-mortem
        # (`invest-watch errors` / log inspection).
        self._last_raw_output: str | None = None

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

        # First parse attempt — tolerant of fences / preamble text
        try:
            parsed = extract_json_payload(raw)
            self._json_failure_streak = 0
            return parsed
        except (json.JSONDecodeError, ValueError):
            logger.warning(
                "LLM returned invalid JSON (hash=%.12s…); sending corrective prompt",
                content_hash or "none",
            )
            self._last_raw_output = raw

        # One corrective attempt — no content_hash to avoid caching the bad result
        corrective_prompt = (
            f"The JSON you returned was invalid. "
            f"Fix it and return ONLY valid JSON, nothing else.\n\n{raw}"
        )
        try:
            corrected = self.generate(corrective_prompt)
            result = extract_json_payload(corrected)
            logger.info(
                "LLM corrective attempt succeeded (hash=%.12s…)", content_hash or "none"
            )
            self._json_failure_streak = 0
            # Cache the corrected result under the original hash
            if content_hash:
                self._cache[content_hash] = corrected
            return result
        except (json.JSONDecodeError, ValueError) as exc:
            self._json_failure_streak += 1
            logger.error(
                "LLM corrective attempt also returned invalid JSON "
                "(hash=%.12s…) — consecutive JSON failures: %d/%d — switching to degraded mode",
                content_hash or "none",
                self._json_failure_streak,
                self._cfg.circuit_breaker_threshold,
            )
            logger.error(
                "LLM raw output head (hash=%.12s…): %.300s",
                content_hash or "none",
                (corrected or "").replace("\n", " "),
            )
            self._last_raw_output = corrected
            if self._json_failure_streak >= self._cfg.circuit_breaker_threshold:
                logger.error(
                    "LLM JSON failures reached circuit-breaker threshold (%d) — opening circuit",
                    self._cfg.circuit_breaker_threshold,
                )
                self._cb.trip_open(
                    f"{self._json_failure_streak} consecutive unparseable JSON responses"
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

    @property
    def last_raw_output(self) -> str | None:
        """Head of the last unparseable raw LLM response (post-mortem aid)."""
        return self._last_raw_output

    @property
    def json_failure_streak(self) -> int:
        """Consecutive generate_json() failures (0 after a success)."""
        return self._json_failure_streak

    # ── orchestrator control ──────────────────────────────────────────────

    def reset_circuit_breaker(self) -> None:
        """Reset circuit-breaker, JSON-failure streak and cache — run start."""
        self._cb.reset()
        self._cache.clear()
        self._json_failure_streak = 0
        self._last_raw_output = None

    # ── lazy SDK init ─────────────────────────────────────────────────────

    def _get_model(self) -> Any:
        """Lazily instantiate the SDK ModelInference object.

        The import is deferred so that the application starts normally even
        when ``ibm-watsonx-ai`` is not installed (degraded / off mode).
        The raw SDK model is wrapped in :class:`_SDKChatModel` so all calls go
        through the ``/ml/v1/text/chat`` endpoint (see that class for why).
        """
        if self._model is not None:
            return self._model

        try:
            from ibm_watsonx_ai import Credentials  # type: ignore[import-untyped]
            from ibm_watsonx_ai.foundation_models import ModelInference  # type: ignore[import-untyped]
        except ImportError as exc:
            raise LLMDegradedError(
                "ibm-watsonx-ai SDK not installed — run: pip install ibm-watsonx-ai"
            ) from exc

        credentials = Credentials(
            url=self._cfg.url,
            api_key=self._cfg.api_key,
        )
        raw_model = ModelInference(
            model_id=self._cfg.model_id,
            credentials=credentials,
            project_id=self._cfg.project_id,
        )
        self._model = _SDKChatModel(raw_model, self._cfg)
        logger.info(
            "LLM initialised: model=%s url=%s (endpoint: /ml/v1/text/chat)",
            self._cfg.model_id,
            self._cfg.url,
        )
        return self._model


# ── Factory ───────────────────────────────────────────────────────────────────


def build_llm_client(config: LLMClientConfig) -> WatsonxLLMClient:
    """Production factory. Replace with a stub in tests."""
    return WatsonxLLMClient(config)


class DisabledLLMClient:
    """LLM client that is unusable from the start (pre-flight detected a bad key).

    Installed by the orchestrator when :func:`~invest_watch.doctor.watsonx_invalid`
    is True.  Every call raises :exc:`LLMDegradedError` carrying the reason —
    the whole run degrades **immediately and silently** instead of burning one
    raw 403 per article.  ``is_degraded`` is always True, so
    :class:`~invest_watch.summarizer.ArticleSummarizer` short-circuits without
    touching the network and without writing a ``processing_error`` row per
    article (the skip is a clean, expected degradation, not a failure).
    """

    def __init__(self, reason: str) -> None:
        #: Why the LLM is unavailable (surfaced in degraded summaries & logs).
        self.degraded_reason = reason

    @property
    def is_degraded(self) -> bool:
        return True

    @property
    def cache_size(self) -> int:
        return 0

    @property
    def last_raw_output(self) -> str | None:
        return None

    def reset_circuit_breaker(self) -> None:
        """No-op — the client is permanently disabled."""

    def generate(self, prompt: str, content_hash: str | None = None) -> str:
        raise LLMDegradedError(self.degraded_reason)

    def generate_json(
        self, prompt: str, content_hash: str | None = None
    ) -> dict[str, Any]:
        raise LLMDegradedError(self.degraded_reason)


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
