"""Unit tests for doctor.py — pre-flight pings (P1 item 8).

Every HTTP call goes through an :class:`httpx.MockTransport`, so the whole
module is exercised offline (the ``clean_env`` fixture wipes ``BRAVE_API_KEY`` /
``FINNHUB_API_KEY`` / ``WATSONX_*`` for every test).

Covers:
- watsonx: off mode, missing/placeholder key, 200, 401, 5xx, network failure.
- brave / finnhub: missing key, 200, 429 (rate-limited but valid), 401/403.
- run_preflight relevance rules (config hints, db fallback, skipped engines).
- disabled_engines / watsonx_invalid helpers used by the orchestrator.
- format_doctor_report glyphs.
"""

from __future__ import annotations

import httpx

from invest_watch.config_manager import SourceSeed
from invest_watch.doctor import (
    ST_INVALID_KEY,
    ST_MISSING_KEY,
    ST_OFF,
    ST_OK,
    ST_SKIPPED,
    ST_UNREACHABLE,
    ServicePing,
    disabled_engines,
    format_doctor_report,
    ping_brave,
    ping_finnhub,
    ping_watsonx,
    run_preflight,
    watsonx_invalid,
)
from invest_watch.source_repository import Source, SourceRepository


# ── helpers ───────────────────────────────────────────────────────────────────


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _status_handler(status: int):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={})

    return handler


def _boom_handler(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("simulated network failure", request=request)


def _enable_llm(config, api_key: str = "real-key"):
    """Flip the frozen LLMSection to an enabled, keyed state."""
    object.__setattr__(config.llm, "enabled", True)
    object.__setattr__(config.llm, "api_key", api_key)
    return config


def _with_sources(config, *seeds: SourceSeed):
    object.__setattr__(config, "sources", tuple(seeds))
    return config


FINNHUB_SEED = SourceSeed(
    name="Finnhub Company News", type="api",
    url="https://finnhub.io/api/v1/company-news",
)
BRAVE_SEED = SourceSeed(
    name="Brave News Search", type="search",
    url="https://api.search.brave.com/res/v1/news/search",
)


# ── ping_watsonx ──────────────────────────────────────────────────────────────


class TestPingWatsonx:
    def test_llm_disabled_is_off_and_ok(self, config):
        ping = ping_watsonx(config)
        assert ping.service == "watsonx"
        assert (ping.status, ping.ok) == (ST_OFF, True)

    def test_missing_key(self, config):
        _enable_llm(config, api_key="")
        ping = ping_watsonx(config, client=_client(_status_handler(200)))
        assert (ping.status, ping.ok) == (ST_MISSING_KEY, False)

    def test_placeholder_key_not_pinged(self, config):
        _enable_llm(config, api_key="your-watsonx-key")
        ping = ping_watsonx(config, client=_client(_boom_handler))
        assert (ping.status, ping.ok) == (ST_MISSING_KEY, False)

    def test_iam_200_means_valid_key(self, config):
        _enable_llm(config)
        ping = ping_watsonx(config, client=_client(_status_handler(200)))
        assert (ping.status, ping.ok) == (ST_OK, True)

    def test_iam_401_means_invalid_key(self, config):
        _enable_llm(config)
        ping = ping_watsonx(config, client=_client(_status_handler(401)))
        assert (ping.status, ping.ok) == (ST_INVALID_KEY, False)
        assert "http_401" in ping.message

    def test_iam_500_is_unreachable(self, config):
        _enable_llm(config)
        ping = ping_watsonx(config, client=_client(_status_handler(500)))
        assert (ping.status, ping.ok) == (ST_UNREACHABLE, False)

    def test_network_failure_is_unreachable(self, config):
        _enable_llm(config)
        ping = ping_watsonx(config, client=_client(_boom_handler))
        assert (ping.status, ping.ok) == (ST_UNREACHABLE, False)
        assert "réseau" in ping.message


#  ping_brave ────────────────────────────────────────────────────────────────


class TestPingBrave:
    def test_missing_key(self, config):
        ping = ping_brave(config)
        assert (ping.status, ping.ok) == (ST_MISSING_KEY, False)

    def test_200_means_valid_key(self, config):
        ping = ping_brave(config, client=_client(_status_handler(200)), key="brave-key")
        assert (ping.status, ping.ok) == (ST_OK, True)

    def test_429_is_rate_limited_but_valid(self, config):
        ping = ping_brave(config, client=_client(_status_handler(429)), key="brave-key")
        assert (ping.status, ping.ok) == (ST_OK, True)
        assert "429" in ping.message

    def test_403_means_invalid_key(self, config):
        ping = ping_brave(config, client=_client(_status_handler(403)), key="brave-key")
        assert (ping.status, ping.ok) == (ST_INVALID_KEY, False)

    def test_503_is_unreachable(self, config):
        ping = ping_brave(config, client=_client(_status_handler(503)), key="brave-key")
        assert (ping.status, ping.ok) == (ST_UNREACHABLE, False)

    def test_network_failure_is_unreachable(self, config):
        ping = ping_brave(config, client=_client(_boom_handler), key="brave-key")
        assert (ping.status, ping.ok) == (ST_UNREACHABLE, False)


# ─ ping_finnhub ──────────────────────────────────────────────────────────────


class TestPingFinnhub:
    def test_missing_key(self, config):
        ping = ping_finnhub(config)
        assert (ping.status, ping.ok) == (ST_MISSING_KEY, False)

    def test_placeholder_key_in_config_is_missing(self, config):
        object.__setattr__(config.collect, "finnhub_api_key", "changeme")
        ping = ping_finnhub(config, client=_client(_boom_handler))
        assert (ping.status, ping.ok) == (ST_MISSING_KEY, False)

    def test_200_means_valid_key(self, config):
        ping = ping_finnhub(
            config, client=_client(_status_handler(200)), key="finnhub-key"
        )
        assert (ping.status, ping.ok) == (ST_OK, True)

    def test_401_means_invalid_key(self, config):
        ping = ping_finnhub(
            config, client=_client(_status_handler(401)), key="finnhub-key"
        )
        assert (ping.status, ping.ok) == (ST_INVALID_KEY, False)

    def test_key_from_config_is_used(self, config):
        object.__setattr__(config.collect, "finnhub_api_key", "from-config")
        ping = ping_finnhub(config, client=_client(_status_handler(200)))
        assert (ping.status, ping.ok) == (ST_OK, True)


# ─ run_preflight ─────────────────────────────────────────────────────────────


class TestRunPreflight:
    def test_no_configured_source_skips_finnhub_and_brave(self, config):
        pings = run_preflight(config)
        assert [p.service for p in pings] == ["watsonx", "finnhub", "brave"]
        assert pings[0].status == ST_OFF
        assert pings[1].status == ST_SKIPPED and pings[1].ok
        assert pings[2].status == ST_SKIPPED and pings[2].ok

    def test_config_hint_triggers_engine_pings(self, config, monkeypatch):
        _enable_llm(config)
        _with_sources(config, FINNHUB_SEED, BRAVE_SEED)
        # Engine keys come from the environment (clean_env wipes them per test).
        monkeypatch.setenv("BRAVE_API_KEY", "brave-key")
        monkeypatch.setenv("FINNHUB_API_KEY", "finnhub-key")
        pings = run_preflight(config, client=_client(_status_handler(200)))
        assert {p.service for p in pings} == {"watsonx", "finnhub", "brave"}
        assert all(p.status == ST_OK for p in pings)

    def test_db_rows_trigger_engine_pings(self, config, db):
        SourceRepository(db).add(
            Source(name="Finnhub Company News", type="api",
                   url="https://finnhub.io/api/v1/company-news")
        )
        pings = run_preflight(config, db=db, client=_client(_status_handler(200)))
        finnhub = next(p for p in pings if p.service == "finnhub")
        brave = next(p for p in pings if p.service == "brave")
        # Finnhub is hinted by the source row; Brave has no source → skipped.
        assert finnhub.status == ST_MISSING_KEY
        assert brave.status == ST_SKIPPED

    def test_unreadable_source_table_is_non_fatal(self, config):
        class _BoomDB:
            def query(self, *_args, **_kwargs):
                raise RuntimeError("no such table: source")

        pings = run_preflight(config, db=_BoomDB())
        assert len(pings) == 3  # config hints still applied


# ─ helpers consumed by the orchestrator ──────────────────────────────────────


class TestPingHelpers:
    def test_disabled_engines_only_invalid_api_keys(self):
        pings = [
            ServicePing("watsonx", False, ST_INVALID_KEY),
            ServicePing("brave", False, ST_INVALID_KEY),
            ServicePing("finnhub", False, ST_MISSING_KEY),
        ]
        assert disabled_engines(pings) == frozenset({"brave"})

    def test_watsonx_invalid(self):
        assert watsonx_invalid([ServicePing("watsonx", False, ST_INVALID_KEY)])
        assert not watsonx_invalid([ServicePing("watsonx", True, ST_OK)])
        assert not watsonx_invalid([ServicePing("watsonx", False, ST_UNREACHABLE)])


# ─ formatter ─────────────────────────────────────────────────────────────────


class TestFormatDoctorReport:
    def test_lists_every_service_with_a_glyph(self):
        report = format_doctor_report([
            ServicePing("watsonx", True, ST_OK, "clé valide"),
            ServicePing("finnhub", False, ST_INVALID_KEY, "clé invalide"),
            ServicePing("brave", True, ST_SKIPPED, "aucune source Brave active"),
        ])
        assert report.startswith("Pré-vol des services (doctor):")
        assert "[ok]" in report
        assert "[FAIL]" in report
        assert "[skip]" in report
        for service in ("watsonx", "finnhub", "brave"):
            assert service in report

    def test_unknown_status_uses_fallback_glyph(self):
        report = format_doctor_report([ServicePing("watsonx", False, "weird")])
        assert "[?]" in report

