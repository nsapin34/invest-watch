"""Unit tests for invest_watch/summarizer.py — Step 5a.

Covers (architecture §8 / specs §8.1–§8.8):
- Prompt golden test: all fields present, text truncated at max_input_chars.
- JSON schema validation: valid payload round-trips cleanly.
- Enum out-of-range → defaults, not rejection.
- Score out-of-bounds → clamped to [0, 1].
- verification_humaine_requise as bool / int / missing.
- Out-of-schema LLM response → degraded mode (validation_status='invalid').
- LLMDegradedError → degraded mode (llm_used=False).
- Circuit-breaker open (is_degraded=True) → degraded immediately.
- relevance_score < relevance_min → degraded immediately (no LLM call).
- max_calls_per_run cap reached → degraded (no LLM call).
- content length > max_input_chars → degraded (no chunking in V1).
- LLM disabled (enabled=False) → degraded immediately.
- Persistence: summary row written with correct fields.
- Idempotence: second call for the same article/run overwrites (no duplicate).
- summarise_run: skips duplicate/redundant/unrelated articles.
- summarise_run: call_count incremented only on real LLM calls.
- to_json_payload / to_text_content round-trip.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from invest_watch.llm_client import LLMDegradedError
from invest_watch.summarizer import (
    ArticleSummarizer,
    ArticleSummary,
    SummarizerConfig,
    _degraded_summary,
    build_article_prompt,
    validate_article_json,
)
from tests.stubs.llm_stub import LLMStub


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_VALID_PAYLOAD: dict[str, Any] = {
    "langue_resume": "fr",
    "titre_resume": "BYD annonce des résultats records",
    "categorie": "financial_results",
    "compagnies_concernees": ["BYD"],
    "marches_couverts": ["Electric Vehicles"],
    "faits": ["Le chiffre d'affaires a augmenté de 30 %.", "Le bénéfice net atteint 40 Mds CNY."],
    "consequences_potentielles": [
        {
            "cible": "BYD",
            "impact": "positive",
            "horizon": "court",
            "confidence": "haute",
        }
    ],
    "incertitudes_ou_conflits": ["L'impact sur la marge longue terme reste incertain."],
    "verification_humaine_requise": True,
    "importance": "haute",
    "pertinence": 0.87,
    "note_contradictions": [],
}


def _make_cfg(**overrides: Any) -> SummarizerConfig:
    defaults = dict(
        enabled=True,
        model_id="ibm/granite-3-8b-instruct",
        prompt_version="1.0",
        max_input_chars=8000,
        max_calls_per_run=150,
        relevance_min=0.5,
    )
    defaults.update(overrides)
    return SummarizerConfig(**defaults)


def _insert_article(db, *, run_id: int = 1, relevance: float = 0.8, content: str = "body text", status: str = "new") -> int:
    """Insert a minimal article row and return its id."""
    from invest_watch.db_manager import utc_now_iso
    now = utc_now_iso()
    cur = db.execute(
        "INSERT INTO article"
        " (run_id, url_original, url_normalized, title, short_code, status,"
        "  relevance_score, content, excerpt, content_hash, collected_at,"
        "  date_certaine, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?)",
        (
            run_id,
            f"https://example.com/article-{relevance}-{status}",
            f"https://example.com/article-{relevance}-{status}",
            "Test article title",
            f"sc{int(relevance*100)}{status[:2]}",
            status,
            relevance,
            content,
            "lead excerpt",
            None,
            now,
            now,
            now,
        ),
    )
    return int(cur.lastrowid)


def _insert_run(db) -> int:
    from invest_watch.db_manager import utc_now_iso
    now = utc_now_iso()
    cur = db.execute(
        "INSERT INTO daily_run (run_date, period_start, period_end, status, created_at)"
        " VALUES (?,?,?,?,?)",
        ("2026-09-08", "2026-09-07T00:00:00Z", "2026-09-07T23:59:59Z", "running", now),
    )
    return int(cur.lastrowid)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: build_article_prompt
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildArticlePrompt:
    def test_contains_title_and_text(self):
        prompt = build_article_prompt(
            title="BYD Q3 results",
            text="Revenue up 30%",
            language="en",
            published_at="2026-09-07",
            company_names=["BYD"],
            market_names=["EV"],
        )
        assert "BYD Q3 results" in prompt
        assert "Revenue up 30%" in prompt
        assert "BYD" in prompt
        assert "EV" in prompt

    def test_text_truncated_at_max_input_chars(self):
        long_text = "x" * 10_000
        prompt = build_article_prompt(
            title="T",
            text=long_text,
            language="fr",
            published_at=None,
            company_names=[],
            market_names=[],
            max_input_chars=100,
        )
        # The truncated text must appear but not the full 10 000 chars.
        assert "x" * 100 in prompt
        assert "x" * 101 not in prompt

    def test_no_companies_shows_aucune(self):
        prompt = build_article_prompt(
            title="T", text="body", language="fr", published_at=None,
            company_names=[], market_names=[],
        )
        assert "aucune" in prompt

    def test_json_schema_block_in_prompt(self):
        prompt = build_article_prompt(
            title="T", text="body", language="fr", published_at=None,
            company_names=[], market_names=[],
        )
        assert '"categorie"' in prompt
        assert '"faits"' in prompt
        assert '"pertinence"' in prompt


# ─────────────────────────────────────────────────────────────────────────────
# Tests: validate_article_json
# ─────────────────────────────────────────────────────────────────────────────

class TestValidateArticleJson:
    def test_valid_payload_round_trips(self):
        ok, summary, err = validate_article_json(_VALID_PAYLOAD)
        assert ok is True
        assert err == ""
        assert summary is not None
        assert summary.categorie == "financial_results"
        assert summary.importance == "haute"
        assert summary.pertinence == pytest.approx(0.87, abs=1e-4)
        assert summary.verification_humaine_requise is True
        assert "BYD" in summary.compagnies_concernees
        assert len(summary.faits) == 2
        assert len(summary.consequences_potentielles) == 1

    def test_invalid_categorie_defaults_to_other(self):
        payload = {**_VALID_PAYLOAD, "categorie": "galaxy_brain"}
        ok, summary, _ = validate_article_json(payload)
        assert ok
        assert summary.categorie == "other"

    def test_invalid_importance_defaults_to_moyenne(self):
        payload = {**_VALID_PAYLOAD, "importance": "super-high"}
        ok, summary, _ = validate_article_json(payload)
        assert ok
        assert summary.importance == "moyenne"

    def test_pertinence_clamped_above_one(self):
        payload = {**_VALID_PAYLOAD, "pertinence": 5.0}
        ok, summary, _ = validate_article_json(payload)
        assert ok
        assert summary.pertinence == pytest.approx(1.0)

    def test_pertinence_clamped_below_zero(self):
        payload = {**_VALID_PAYLOAD, "pertinence": -3.0}
        ok, summary, _ = validate_article_json(payload)
        assert ok
        assert summary.pertinence == pytest.approx(0.0)

    def test_pertinence_non_numeric_defaults_zero(self):
        payload = {**_VALID_PAYLOAD, "pertinence": "high"}
        ok, summary, _ = validate_article_json(payload)
        assert ok
        assert summary.pertinence == pytest.approx(0.0)

    def test_vhr_int_coerced_to_bool(self):
        for raw, expected in [(1, True), (0, False)]:
            payload = {**_VALID_PAYLOAD, "verification_humaine_requise": raw}
            ok, summary, _ = validate_article_json(payload)
            assert ok
            assert summary.verification_humaine_requise is expected

    def test_vhr_missing_defaults_false(self):
        payload = {k: v for k, v in _VALID_PAYLOAD.items() if k != "verification_humaine_requise"}
        ok, summary, _ = validate_article_json(payload)
        assert ok
        assert summary.verification_humaine_requise is False

    def test_consequence_bad_impact_defaults_unknown(self):
        payload = {
            **_VALID_PAYLOAD,
            "consequences_potentielles": [{"cible": "X", "impact": "lunar", "horizon": "court", "confidence": "haute"}],
        }
        ok, summary, _ = validate_article_json(payload)
        assert ok
        assert summary.consequences_potentielles[0]["impact"] == "unknown"

    def test_consequence_bad_horizon_defaults_inconnu(self):
        payload = {
            **_VALID_PAYLOAD,
            "consequences_potentielles": [{"cible": "X", "impact": "positive", "horizon": "now", "confidence": "basse"}],
        }
        ok, summary, _ = validate_article_json(payload)
        assert ok
        assert summary.consequences_potentielles[0]["horizon"] == "inconnu"

    def test_non_dict_payload_returns_error(self):
        ok, summary, err = validate_article_json("not a dict")  # type: ignore[arg-type]
        assert ok is False
        assert summary is None
        assert "not a dict" in err

    def test_list_fields_truncated_to_max_items(self):
        payload = {**_VALID_PAYLOAD, "faits": [f"fact {i}" for i in range(50)]}
        ok, summary, _ = validate_article_json(payload)
        assert ok
        assert len(summary.faits) <= 20

    def test_titre_truncated_to_300_chars(self):
        long_title = "T" * 500
        payload = {**_VALID_PAYLOAD, "titre_resume": long_title}
        ok, summary, _ = validate_article_json(payload)
        assert ok
        assert len(summary.titre_resume) <= 300

    def test_null_fields_produce_empty_lists(self):
        payload = {**_VALID_PAYLOAD, "faits": None, "marches_couverts": None}
        ok, summary, _ = validate_article_json(payload)
        assert ok
        assert summary.faits == []
        assert summary.marches_couverts == []


# ─────────────────────────────────────────────────────────────────────────────
# Tests: _degraded_summary
# ─────────────────────────────────────────────────────────────────────────────

class TestDegradedSummary:
    def test_llm_used_false(self):
        s = _degraded_summary(
            title="Test", excerpt="Lead.", article_id=1, run_id=1,
            model_id="model", content_hash=None,
        )
        assert s.llm_used is False
        assert s.validation_status == "degraded"

    def test_uses_excerpt_as_fact(self):
        s = _degraded_summary(
            title="T", excerpt="Key info here.", article_id=1, run_id=1,
            model_id="m", content_hash=None,
        )
        assert "Key info here." in s.faits

    def test_uses_title_when_no_excerpt(self):
        s = _degraded_summary(
            title="Only title", excerpt=None, article_id=1, run_id=1,
            model_id="m", content_hash=None,
        )
        assert "Only title" in s.faits

    def test_content_hash_stored(self):
        s = _degraded_summary(
            title="T", excerpt="E", article_id=1, run_id=1,
            model_id="m", content_hash="abc123",
        )
        assert s.content_hash == "abc123"


# ─────────────────────────────────────────────────────────────────────────────
# Tests: ArticleSummarizer.summarise_article
# ─────────────────────────────────────────────────────────────────────────────

class TestArticleSummarizer:
    def _make_summarizer(self, db, llm, **cfg_overrides):
        cfg = _make_cfg(**cfg_overrides)
        return ArticleSummarizer(db=db, llm=llm, config=cfg)

    # ── happy path ────────────────────────────────────────────────────────

    def test_llm_summary_persisted(self, db):
        run_id = _insert_run(db)
        article_id = _insert_article(db, run_id=run_id, relevance=0.8)
        stub = LLMStub(responses=[json.dumps(_VALID_PAYLOAD)])
        summarizer = self._make_summarizer(db, stub)

        result = summarizer.summarise_article(article_id, run_id)

        assert result.llm_used is True
        assert result.validation_status == "ok"
        assert result.db_id is not None
        assert result.categorie == "financial_results"

    def test_call_count_incremented_on_success(self, db):
        run_id = _insert_run(db)
        a1 = _insert_article(db, run_id=run_id, relevance=0.8)
        a2 = _insert_article(db, run_id=run_id, relevance=0.9)
        stub = LLMStub(responses=[json.dumps(_VALID_PAYLOAD)] * 2)
        summarizer = self._make_summarizer(db, stub)

        summarizer.summarise_article(a1, run_id)
        summarizer.summarise_article(a2, run_id)

        assert summarizer.call_count == 2

    def test_model_id_and_prompt_version_in_db(self, db):
        run_id = _insert_run(db)
        article_id = _insert_article(db, run_id=run_id, relevance=0.8)
        stub = LLMStub(responses=[json.dumps(_VALID_PAYLOAD)])
        summarizer = self._make_summarizer(db, stub, model_id="ibm/granite-test")

        summarizer.summarise_article(article_id, run_id)

        row = db.query_one(
            "SELECT model_id, prompt_version, llm_used, validation_status"
            " FROM summary WHERE type='article' AND ref_id=?",
            (article_id,),
        )
        assert row is not None
        assert row["model_id"] == "ibm/granite-test"
        assert row["prompt_version"] == "1.0"
        assert row["llm_used"] == 1
        assert row["validation_status"] == "ok"

    # ── degraded paths ────────────────────────────────────────────────────

    def test_low_relevance_skips_llm(self, db):
        run_id = _insert_run(db)
        article_id = _insert_article(db, run_id=run_id, relevance=0.2)
        stub = LLMStub(responses=[json.dumps(_VALID_PAYLOAD)])
        summarizer = self._make_summarizer(db, stub, relevance_min=0.5)

        result = summarizer.summarise_article(article_id, run_id)

        assert result.llm_used is False
        assert stub.call_count == 0
        assert summarizer.call_count == 0

    def test_llm_disabled_skips_llm(self, db):
        run_id = _insert_run(db)
        article_id = _insert_article(db, run_id=run_id, relevance=0.9)
        stub = LLMStub(responses=[json.dumps(_VALID_PAYLOAD)])
        summarizer = self._make_summarizer(db, stub, enabled=False)

        result = summarizer.summarise_article(article_id, run_id)

        assert result.llm_used is False
        assert stub.call_count == 0

    def test_circuit_breaker_open_skips_llm(self, db):
        run_id = _insert_run(db)
        article_id = _insert_article(db, run_id=run_id, relevance=0.9)
        stub = LLMStub(raise_after=0)  # first call triggers is_degraded=True
        # Pre-trigger the stub's degraded flag by making one call outside the summarizer.
        try:
            stub.generate("x")
        except LLMDegradedError:
            pass
        assert stub.is_degraded is True

        summarizer = self._make_summarizer(db, stub)
        result = summarizer.summarise_article(article_id, run_id)

        assert result.llm_used is False
        # The stub was called once (pre-trigger), not again by the summarizer.
        assert stub.call_count == 1

    def test_call_cap_reached_skips_llm(self, db):
        run_id = _insert_run(db)
        a1 = _insert_article(db, run_id=run_id, relevance=0.8)
        a2 = _insert_article(db, run_id=run_id, relevance=0.9)
        stub = LLMStub(responses=[json.dumps(_VALID_PAYLOAD)] * 10)
        summarizer = self._make_summarizer(db, stub, max_calls_per_run=1)

        summarizer.summarise_article(a1, run_id)  # uses the 1 allowed call
        result2 = summarizer.summarise_article(a2, run_id)  # cap reached

        assert summarizer.call_count == 1
        assert result2.llm_used is False

    def test_content_too_long_skips_llm(self, db):
        run_id = _insert_run(db)
        long_content = "x" * 9000
        article_id = _insert_article(db, run_id=run_id, relevance=0.9, content=long_content)
        stub = LLMStub(responses=[json.dumps(_VALID_PAYLOAD)])
        summarizer = self._make_summarizer(db, stub, max_input_chars=8000)

        result = summarizer.summarise_article(article_id, run_id)

        assert result.llm_used is False
        assert stub.call_count == 0

    def test_llm_raises_degraded_error_fallback(self, db):
        run_id = _insert_run(db)
        article_id = _insert_article(db, run_id=run_id, relevance=0.8)
        stub = LLMStub(raise_after=0)
        summarizer = self._make_summarizer(db, stub)

        result = summarizer.summarise_article(article_id, run_id)

        assert result.llm_used is False
        assert result.validation_status == "degraded"

    def test_invalid_json_schema_fallback(self, db):
        run_id = _insert_run(db)
        article_id = _insert_article(db, run_id=run_id, relevance=0.8)
        # LLMStub returns a non-dict JSON (array) — validate_article_json will fail.
        stub = LLMStub(responses=['["not", "a", "dict"]'])
        summarizer = self._make_summarizer(db, stub)

        result = summarizer.summarise_article(article_id, run_id)

        assert result.llm_used is False
        assert result.validation_status == "invalid"

    # ── persistence & idempotence ─────────────────────────────────────────

    def test_summary_row_exists_after_call(self, db):
        run_id = _insert_run(db)
        article_id = _insert_article(db, run_id=run_id, relevance=0.8)
        stub = LLMStub(responses=[json.dumps(_VALID_PAYLOAD)])
        summarizer = self._make_summarizer(db, stub)

        summarizer.summarise_article(article_id, run_id)

        row = db.query_one(
            "SELECT * FROM summary WHERE run_id=? AND type='article' AND ref_id=?",
            (run_id, article_id),
        )
        assert row is not None
        assert row["llm_used"] == 1
        assert json.loads(row["json_payload"])["categorie"] == "financial_results"

    def test_idempotent_second_call_no_duplicate(self, db):
        run_id = _insert_run(db)
        article_id = _insert_article(db, run_id=run_id, relevance=0.8)
        stub = LLMStub(responses=[json.dumps(_VALID_PAYLOAD), json.dumps(_VALID_PAYLOAD)])
        summarizer = self._make_summarizer(db, stub)

        summarizer.summarise_article(article_id, run_id)
        summarizer.summarise_article(article_id, run_id)

        rows = db.query(
            "SELECT id FROM summary WHERE run_id=? AND type='article' AND ref_id=?",
            (run_id, article_id),
        )
        assert len(rows) == 1  # no duplicate

    def test_raises_value_error_for_unknown_article(self, db):
        run_id = _insert_run(db)
        stub = LLMStub()
        summarizer = self._make_summarizer(db, stub)
        with pytest.raises(ValueError, match="article #9999 not found"):
            summarizer.summarise_article(9999, run_id)

    # ── reset_run ─────────────────────────────────────────────────────────

    def test_reset_run_resets_call_count(self, db):
        run_id = _insert_run(db)
        article_id = _insert_article(db, run_id=run_id, relevance=0.8)
        stub = LLMStub(responses=[json.dumps(_VALID_PAYLOAD)] * 5)
        summarizer = self._make_summarizer(db, stub)

        summarizer.summarise_article(article_id, run_id)
        assert summarizer.call_count == 1

        summarizer.reset_run()
        assert summarizer.call_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# Tests: summarise_run
# ─────────────────────────────────────────────────────────────────────────────

class TestSummariseRun:
    def _make_summarizer(self, db, llm, **cfg_overrides):
        cfg = _make_cfg(**cfg_overrides)
        return ArticleSummarizer(db=db, llm=llm, config=cfg)

    def test_processes_eligible_articles(self, db):
        run_id = _insert_run(db)
        a1 = _insert_article(db, run_id=run_id, relevance=0.8, status="new")
        a2 = _insert_article(db, run_id=run_id, relevance=0.9, status="updated")
        stub = LLMStub(responses=[json.dumps(_VALID_PAYLOAD)] * 5)
        summarizer = self._make_summarizer(db, stub)

        results = summarizer.summarise_run(run_id)

        assert len(results) == 2
        ids = {r.article_id for r in results}
        assert a1 in ids
        assert a2 in ids

    def test_skips_duplicate_and_redundant(self, db):
        run_id = _insert_run(db)
        _insert_article(db, run_id=run_id, relevance=0.8, status="duplicate")
        _insert_article(db, run_id=run_id, relevance=0.8, status="redundant")
        _insert_article(db, run_id=run_id, relevance=0.8, status="unrelated")
        eligible = _insert_article(db, run_id=run_id, relevance=0.8, status="new")
        stub = LLMStub(responses=[json.dumps(_VALID_PAYLOAD)] * 5)
        summarizer = self._make_summarizer(db, stub)

        results = summarizer.summarise_run(run_id)

        assert len(results) == 1
        assert results[0].article_id == eligible

    def test_call_count_reflects_real_calls(self, db):
        run_id = _insert_run(db)
        # 2 eligible articles, both above relevance_min
        _insert_article(db, run_id=run_id, relevance=0.8, status="new")
        _insert_article(db, run_id=run_id, relevance=0.9, status="new")
        stub = LLMStub(responses=[json.dumps(_VALID_PAYLOAD)] * 5)
        summarizer = self._make_summarizer(db, stub)

        summarizer.summarise_run(run_id)

        assert summarizer.call_count == 2


# ─────────────────────────────────────────────────────────────────────────────
# Tests: ArticleSummary serialisation
# ─────────────────────────────────────────────────────────────────────────────

class TestArticleSummarySerialization:
    def _make(self) -> ArticleSummary:
        ok, s, _ = validate_article_json(_VALID_PAYLOAD)
        assert ok
        s.article_id = 42
        s.run_id = 7
        s.model_id = "ibm/granite-3-8b-instruct"
        return s

    def test_to_json_payload_is_valid_json(self):
        s = self._make()
        raw = s.to_json_payload()
        parsed = json.loads(raw)
        assert parsed["categorie"] == "financial_results"
        assert parsed["pertinence"] == pytest.approx(0.87, abs=1e-4)

    def test_to_text_content_contains_titre(self):
        s = self._make()
        text = s.to_text_content()
        assert "BYD annonce des résultats records" in text

    def test_to_text_content_marks_degraded(self):
        s = _degraded_summary(
            title="T", excerpt="E", article_id=1, run_id=1,
            model_id="m", content_hash=None,
        )
        text = s.to_text_content()
        assert "dégradé" in text
