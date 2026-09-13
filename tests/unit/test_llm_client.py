"""Unit tests for llm_client module (Step 4b).

Covers all spec §8.5 / §8.6 / architecture Step 4 requirements:
- LLM stub: all tests offline, without API key
- Timeout / 5xx / quota exhausted → retries → circuit-breaker activated
- No retry on 4xx errors
- Per-content_hash cache: second call on same hash → no LLM call
- llm.enabled = false → LLMDegradedError, zero network calls
- is_degraded reflects circuit-breaker state
- reset_circuit_breaker() restores CLOSED state and clears cache
- LLMStub interface alignment
"""

from __future__ import annotations

import pytest

from invest_watch.llm_client import (
    LLMClientConfig,
    LLMDegradedError,
    WatsonxLLMClient,
    build_llm_client,
    llm_client_config_from_section,
)
from tests.stubs.llm_stub import LLMStub


# ── helpers ───────────────────────────────────────────────────────────────────


def _fake_model(responses: list, *, fail_status: int | None = None):
    """Return a mock model whose generate() pops from *responses*.

    If *fail_status* is set, raises an exception with that status_code attribute
    instead of returning a response.
    """

    class _FakeResp:
        def __init__(self, text: str):
            self._text = text

        def __getitem__(self, key):
            if key == "results":
                return [{"generated_text": self._text}]
            raise KeyError(key)

    class _FakeModel:
        def __init__(self):
            self._responses = list(responses)
            self._calls = 0

        def generate(self, *, prompt: str):
            self._calls += 1
            if fail_status is not None:
                exc = RuntimeError(f"HTTP {fail_status}")
                exc.status_code = fail_status  # type: ignore[attr-defined]
                raise exc
            if not self._responses:
                raise RuntimeError("5xx Service Unavailable")
            return _FakeResp(self._responses.pop(0))

        @property
        def call_count(self):
            return self._calls

    return _FakeModel()


def _client(
    *,
    enabled: bool = True,
    max_retries: int = 2,
    threshold: int = 3,
    backoff: list[float] | None = None,
    sleep_fn=None,
) -> WatsonxLLMClient:
    """Build a WatsonxLLMClient with instant backoff and a no-op sleep by default."""
    cfg = LLMClientConfig(
        enabled=enabled,
        max_retries=max_retries,
        circuit_breaker_threshold=threshold,
        _backoff_delays=backoff if backoff is not None else [0.0, 0.0, 0.0],
    )
    return WatsonxLLMClient(cfg, _sleep=sleep_fn or (lambda _: None))


# ── Off mode ──────────────────────────────────────────────────────────────────


class TestOffMode:
    def test_disabled_raises_immediately(self):
        client = _client(enabled=False)
        with pytest.raises(LLMDegradedError, match="disabled"):
            client.generate("prompt")

    def test_disabled_no_model_init(self):
        """SDK must never be touched when llm.enabled = false."""
        client = _client(enabled=False)
        with pytest.raises(LLMDegradedError):
            client.generate("prompt")
        assert client._model is None

    def test_disabled_is_not_degraded(self):
        """Off mode is a configuration choice, not a circuit-breaker state."""
        client = _client(enabled=False)
        # is_degraded reflects the circuit-breaker, which has never been touched
        assert client.is_degraded is False


# ── Content-hash cache ────────────────────────────────────────────────────────


class TestContentHashCache:
    def test_second_call_same_hash_no_model_call(self):
        """Spec §8.6: second generate() with same content_hash must skip the model."""
        client = _client()
        model = _fake_model(["first response", "second response"])
        client._model = model

        r1 = client.generate("prompt A", content_hash="abc123")
        r2 = client.generate("prompt B", content_hash="abc123")

        assert r1 == r2 == "first response"
        assert model.call_count == 1   # model called only once

    def test_different_hashes_call_model_each_time(self):
        client = _client()
        model = _fake_model(["resp1", "resp2"])
        client._model = model

        r1 = client.generate("prompt A", content_hash="hash1")
        r2 = client.generate("prompt B", content_hash="hash2")

        assert r1 == "resp1"
        assert r2 == "resp2"
        assert model.call_count == 2

    def test_no_hash_skips_cache(self):
        """Without content_hash each call goes to the model."""
        client = _client()
        model = _fake_model(["a", "b"])
        client._model = model

        client.generate("prompt")
        client.generate("prompt")
        assert model.call_count == 2

    def test_cache_cleared_on_reset(self):
        client = _client()
        model = _fake_model(["resp1", "resp2"])
        client._model = model

        client.generate("p", content_hash="h1")
        assert client.cache_size == 1

        client.reset_circuit_breaker()
        assert client.cache_size == 0

        # After reset, same hash must call model again
        client.generate("p", content_hash="h1")
        assert model.call_count == 2


# ── Retries and backoff ───────────────────────────────────────────────────────


class TestRetriesAndBackoff:
    def test_5xx_retried_then_succeeds(self):
        """Two 5xx failures then success — no circuit open, result returned."""
        client = _client(max_retries=2, threshold=5)

        fail_count = [0]
        success_response = "OK"

        class _FlakeyModel:
            def generate(self, *, prompt):
                fail_count[0] += 1
                if fail_count[0] <= 2:
                    raise RuntimeError("5xx")
                return {"results": [{"generated_text": success_response}]}

        client._model = _FlakeyModel()
        result = client.generate("prompt")
        assert result == success_response
        assert fail_count[0] == 3

    def test_all_retries_exhausted_raises_degraded(self):
        """max_retries=2 → 3 total attempts all fail → LLMDegradedError, not circuit open yet."""
        client = _client(max_retries=2, threshold=10)  # high threshold so CB stays closed

        class _AlwaysFail:
            def generate(self, *, prompt):
                raise RuntimeError("5xx")

        client._model = _AlwaysFail()
        with pytest.raises(LLMDegradedError, match="unavailable"):
            client.generate("prompt")

    def test_no_retry_on_4xx(self):
        """Spec §8.5: 4xx errors must not trigger a retry."""
        call_count = [0]

        class _FourXXModel:
            def generate(self, *, prompt):
                call_count[0] += 1
                exc = RuntimeError("400 Bad Request")
                exc.status_code = 400  # type: ignore[attr-defined]
                raise exc

        client = _client(max_retries=2, threshold=10)
        client._model = _FourXXModel()

        with pytest.raises(LLMDegradedError):
            client.generate("prompt")

        assert call_count[0] == 1  # exactly one attempt

    def test_sleep_called_between_retries(self):
        """Backoff sleep must be invoked between retries (not after last attempt)."""
        sleeps: list[float] = []
        client = _client(max_retries=2, threshold=10, sleep_fn=sleeps.append)

        class _AlwaysFail:
            def generate(self, *, prompt):
                raise RuntimeError("5xx")

        client._model = _AlwaysFail()
        with pytest.raises(LLMDegradedError):
            client.generate("prompt")

        # 3 attempts → 2 inter-attempt sleeps
        assert len(sleeps) == 2


# ── Circuit-breaker integration ───────────────────────────────────────────────


class TestCircuitBreakerIntegration:
    def test_threshold_failures_open_circuit(self):
        """Spec §8.5: 3 consecutive failures → circuit opens → LLMDegradedError."""
        # threshold=3, max_retries=0: each generate() counts as one failure
        client = _client(max_retries=0, threshold=3)

        class _AlwaysFail:
            def generate(self, *, prompt):
                raise RuntimeError("5xx")

        client._model = _AlwaysFail()

        # Calls 1 and 2: fail but circuit stays CLOSED (< threshold)
        with pytest.raises(LLMDegradedError):
            client.generate("p1")
        with pytest.raises(LLMDegradedError):
            client.generate("p2")
        assert client.is_degraded is False

        # Call 3: reaches threshold → circuit OPENS
        with pytest.raises(LLMDegradedError):
            client.generate("p3")
        assert client.is_degraded is True

    def test_open_circuit_refuses_subsequent_calls(self):
        client = _client(max_retries=0, threshold=1)

        class _AlwaysFail:
            def generate(self, *, prompt):
                raise RuntimeError("5xx")

        client._model = _AlwaysFail()

        with pytest.raises(LLMDegradedError):
            client.generate("p1")  # opens circuit
        assert client.is_degraded is True

        # Subsequent call: refused immediately (no model call)
        call_count = [0]
        class _CountModel:
            def generate(self, *, prompt):
                call_count[0] += 1
                return {"results": [{"generated_text": "ok"}]}

        client._model = _CountModel()
        with pytest.raises(LLMDegradedError):
            client.generate("p2")
        assert call_count[0] == 0  # model was not invoked

    def test_reset_circuit_breaker_restores_closed(self):
        client = _client(max_retries=0, threshold=1)

        class _AlwaysFail:
            def generate(self, *, prompt):
                raise RuntimeError("5xx")

        client._model = _AlwaysFail()
        with pytest.raises(LLMDegradedError):
            client.generate("p1")
        assert client.is_degraded is True

        client.reset_circuit_breaker()
        assert client.is_degraded is False

        # Can call again after reset
        model = _fake_model(["restored"])
        client._model = model
        result = client.generate("p2")
        assert result == "restored"

    def test_success_resets_failure_counter(self):
        """Two failures then a success must reset the counter; circuit stays closed."""
        client = _client(max_retries=0, threshold=3)
        fail_count = [0]

        class _EventuallyOK:
            def generate(self, *, prompt):
                fail_count[0] += 1
                if fail_count[0] <= 2:
                    raise RuntimeError("5xx")
                return {"results": [{"generated_text": "ok"}]}

        client._model = _EventuallyOK()

        with pytest.raises(LLMDegradedError):
            client.generate("p1")  # failure 1
        with pytest.raises(LLMDegradedError):
            client.generate("p2")  # failure 2
        result = client.generate("p3")  # success → resets counter
        assert result == "ok"
        assert client.is_degraded is False
        assert client._cb.failure_count == 0


# ── SDK not installed ─────────────────────────────────────────────────────────


class TestSDKMissing:
    def test_missing_sdk_raises_degraded_not_import_error(self, monkeypatch):
        """If ibm_watsonx_ai is absent, LLMDegradedError is raised (not ImportError)."""
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "ibm_watsonx_ai":
                raise ImportError("No module named 'ibm_watsonx_ai'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        client = _client()
        with pytest.raises(LLMDegradedError, match="ibm-watsonx-ai SDK not installed"):
            client.generate("prompt")


# ── build_llm_client factory ──────────────────────────────────────────────────


class TestBuildLLMClient:
    def test_returns_watsonx_client(self):
        cfg = LLMClientConfig()
        client = build_llm_client(cfg)
        assert isinstance(client, WatsonxLLMClient)

    def test_llm_client_config_from_section(self):
        """Adapter must map every LLMSection field correctly."""
        from types import SimpleNamespace
        section = SimpleNamespace(
            enabled=False,
            model="ibm/granite-3-8b-instruct",
            endpoint="https://eu-de.ml.cloud.ibm.com",
            project_id="proj-123",
            api_key="key-abc",
            max_new_tokens=500,
            temperature=0.2,
            top_p=0.8,
            timeout_seconds=60,
            max_retries=1,
            circuit_breaker_threshold=2,
        )
        cfg = llm_client_config_from_section(section)
        assert cfg.enabled is False
        assert cfg.model_id == "ibm/granite-3-8b-instruct"
        assert cfg.url == "https://eu-de.ml.cloud.ibm.com"
        assert cfg.project_id == "proj-123"
        assert cfg.api_key == "key-abc"
        assert cfg.max_new_tokens == 500
        assert cfg.max_retries == 1
        assert cfg.circuit_breaker_threshold == 2


# ── LLMStub alignment ────────────────────────────────────────────────────────


class TestLLMStubAlignment:
    def test_stub_returns_canned_responses(self):
        stub = LLMStub(responses=["response A", "response B"])
        assert stub.generate("p1") == "response A"
        assert stub.generate("p2") == "response B"
        assert stub.generate("p3") == "null"  # exhausted

    def test_stub_raise_after(self):
        stub = LLMStub(raise_after=1)
        stub.generate("p1")  # call 1 — ok
        with pytest.raises(LLMDegradedError):
            stub.generate("p2")  # call 2 — raises
        assert stub.is_degraded is True

    def test_stub_raise_after_zero_fails_immediately(self):
        stub = LLMStub(raise_after=0)
        with pytest.raises(LLMDegradedError):
            stub.generate("p1")

    def test_stub_call_log_captures_content_hash(self):
        stub = LLMStub(responses=["ok"])
        stub.generate("my prompt", content_hash="deadbeef")
        assert stub.call_log[0]["prompt"] == "my prompt"
        assert stub.call_log[0]["content_hash"] == "deadbeef"

    def test_stub_reset_circuit_breaker(self):
        stub = LLMStub(raise_after=0)
        with pytest.raises(LLMDegradedError):
            stub.generate("p")
        assert stub.is_degraded is True
        stub.reset_circuit_breaker()
        assert stub.is_degraded is False
        assert stub.call_count == 0
        assert stub.call_log == []

    def test_stub_cache_size_always_zero(self):
        stub = LLMStub(responses=["a"])
        stub.generate("p", content_hash="h1")
        assert stub.cache_size == 0


# ── generate_json — invalid JSON handling (spec §8.4) ────────────────────────


class TestGenerateJSON:
    """Spec §8.4: invalid JSON → 1 corrective attempt → degraded mode + error logged."""

    def _client_with_responses(self, responses: list[str]) -> WatsonxLLMClient:
        """Build a client whose model returns each string in *responses* in order."""
        client = _client(max_retries=0, threshold=10)

        resp_iter = iter(responses)

        class _SeqModel:
            def generate(self, *, prompt):
                text = next(resp_iter)
                return {"results": [{"generated_text": text}]}

        client._model = _SeqModel()
        return client

    def test_valid_json_returned_directly(self):
        payload = '{"importance": "haute", "pertinence": 0.9}'
        client = self._client_with_responses([payload])
        result = client.generate_json("prompt", content_hash="h1")
        assert result == {"importance": "haute", "pertinence": 0.9}

    def test_invalid_then_valid_corrective_succeeds(self):
        """First response bad JSON → corrective prompt → valid JSON → no exception."""
        bad = "Sure! Here is your JSON: {importance: haute}"   # not valid JSON
        good = '{"importance": "haute"}'
        client = self._client_with_responses([bad, good])
        result = client.generate_json("prompt", content_hash="h2")
        assert result == {"importance": "haute"}

    def test_invalid_then_invalid_corrective_raises_degraded(self):
        """Both responses bad JSON → LLMDegradedError (spec §8.4 'degraded fallback')."""
        client = self._client_with_responses(["bad json {", "also bad {"])
        with pytest.raises(LLMDegradedError, match="unparseable JSON"):
            client.generate_json("prompt")

    def test_invalid_corrective_attempt_is_exactly_one(self):
        """The corrective attempt must be exactly 1 — not more."""
        calls: list[str] = []

        client = _client(max_retries=0, threshold=10)

        class _RecordModel:
            def generate(self, *, prompt):
                calls.append(prompt)
                # always return bad JSON
                return {"results": [{"generated_text": "bad {json"}]}

        client._model = _RecordModel()
        with pytest.raises(LLMDegradedError):
            client.generate_json("original prompt")

        # Exactly 2 calls: the original + 1 corrective
        assert len(calls) == 2

    def test_corrective_caches_fixed_result(self):
        """After a corrective success, the fixed JSON must be cached under original hash."""
        bad = "oops {bad}"
        good = '{"key": "value"}'
        client = self._client_with_responses([bad, good])
        client.generate_json("prompt", content_hash="myhash")
        # Cache must now hold the corrected raw text
        assert "myhash" in client._cache
        assert client._cache["myhash"] == good

    def test_llm_unavailable_during_corrective_reraises_degraded(self):
        """If the LLM goes down mid-correction, LLMDegradedError propagates."""
        client = _client(max_retries=0, threshold=1)
        call_n = [0]

        class _FailOnSecond:
            def generate(self, *, prompt):
                call_n[0] += 1
                if call_n[0] == 1:
                    return {"results": [{"generated_text": "bad json {"}]}
                # Second call: 5xx → circuit opens
                raise RuntimeError("5xx")

        client._model = _FailOnSecond()
        with pytest.raises(LLMDegradedError):
            client.generate_json("prompt")

    # ── LLMStub generate_json ────────────────────────────────────────────

    def test_stub_generate_json_valid(self):
        stub = LLMStub(responses=['{"score": 0.8}'])
        result = stub.generate_json("prompt")
        assert result == {"score": 0.8}

    def test_stub_generate_json_invalid_raises_degraded(self):
        stub = LLMStub(responses=["not json {"])
        with pytest.raises(LLMDegradedError):
            stub.generate_json("prompt")
        assert stub.is_degraded is True

