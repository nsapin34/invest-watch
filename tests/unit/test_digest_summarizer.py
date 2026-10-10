"""Unit tests for DigestSummarizer — Step 5b.

Covers (architecture §8 / specs §8.1–§8.8, §13.2–§13.3):

Prompt builders
- build_company_digest_prompt: contains company name, article blocks, [aNN] instruction.
- build_market_digest_prompt: contains market name and article blocks.
- build_global_digest_prompt: contains run_date, company and market digests.
- _format_article_block: truncates summary_content at _MAX_ARTICLE_BLOCK_CHARS.

Degraded fallbacks
- _degraded_company_digest: llm_used=False, [aNN] citations present.
- _degraded_market_digest:  llm_used=False, market name in content.
- _degraded_global_digest:  aggregates article_ids from all input digests.

DigestSummarizer.digest_company
- LLM enabled + articles → llm_used=True, db_id set, content stored.
- No articles → placeholder digest, no LLM call.
- LLM disabled → degraded immediately (no LLM call, no error row).
- call cap reached → degraded (no LLM call).
- LLMDegradedError during call → degraded fallback + processing_error row.
- A client flagged is_degraded does NOT block the digest loop (per-digest
  failures are isolated and recorded, not propagated to other digests).
- Idempotent: second call overwrites, no duplicate row.
- article_ids in persisted row map to real article ids.

DigestSummarizer.digest_market
- Same degraded paths as company (LLM disabled, no articles, LLM error).

DigestSummarizer.digest_global
- LLM enabled → llm_used=True, type='global', ref_id IS NULL.
- Degraded → assembles company + market names.
- article_ids union of company + market digests.
- Idempotent: second call overwrites.

DigestSummarizer.digest_run
- Returns (company_digests, market_digests, global_digest).
- run_date fetched from DB when not passed.
- call counter shared: articles + digests draw from same cap.

Shared call counter
- Counter incremented by digest LLM calls.
- Counter shared between ArticleSummarizer and DigestSummarizer.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from invest_watch.llm_client import LLMDegradedError
from invest_watch.summarizer import (
    DIGEST_PROMPT_VERSION,
    Digest,
    DigestSummarizer,
    SummarizerConfig,
    _ArticleBlock,
    _degraded_company_digest,
    _degraded_global_digest,
    _degraded_market_digest,
    _format_article_block,
    _sanitise_digest_content,
    build_company_digest_prompt,
    build_global_digest_prompt,
    build_market_digest_prompt,
)
from tests.stubs.llm_stub import LLMStub


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures / helpers
# ─────────────────────────────────────────────────────────────────────────────

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


def _make_block(article_id: int = 1, summary: str = "Some summary text.") -> _ArticleBlock:
    return _ArticleBlock(
        article_id=article_id,
        short_code=f"a{article_id}",
        title=f"Article {article_id} title",
        published_at="2026-09-07T10:00:00Z",
        source_name="Reuters",
        summary_content=summary,
        importance="haute",
    )


def _insert_run(db, run_date: str = "2026-09-08") -> int:
    from invest_watch.db_manager import utc_now_iso
    now = utc_now_iso()
    cur = db.execute(
        "INSERT INTO daily_run (run_date, period_start, period_end, status, created_at)"
        " VALUES (?,?,?,?,?)",
        (run_date, "2026-09-07T00:00:00Z", "2026-09-07T23:59:59Z", "running", now),
    )
    return int(cur.lastrowid)


def _insert_company(db, name: str = "BYD", country: str = "CN") -> int:
    from invest_watch.db_manager import utc_now_iso
    now = utc_now_iso()
    cur = db.execute(
        "INSERT INTO company (name, country_code, active, created_at, updated_at)"
        " VALUES (?,?,1,?,?)",
        (name, country, now, now),
    )
    return int(cur.lastrowid)


def _insert_market(db, name: str = "Electric Vehicles", mtype: str = "sector") -> int:
    from invest_watch.db_manager import utc_now_iso
    now = utc_now_iso()
    cur = db.execute(
        "INSERT INTO market (name, type, active, created_at, updated_at) VALUES (?,?,1,?,?)",
        (name, mtype, now, now),
    )
    return int(cur.lastrowid)


def _insert_article(
    db,
    run_id: int,
    *,
    status: str = "new",
    relevance: float = 0.8,
    importance: str = "haute",
    url_suffix: str = "",
) -> int:
    from invest_watch.db_manager import utc_now_iso
    now = utc_now_iso()
    url = f"https://example.com/art-{run_id}-{url_suffix or relevance}"
    cur = db.execute(
        "INSERT INTO article"
        " (run_id, url_original, url_normalized, title, short_code, status,"
        "  relevance_score, importance_level, content, excerpt, content_hash,"
        "  date_certaine, collected_at, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            run_id, url, url,
            f"Title {url_suffix or relevance}",
            f"sc{url_suffix or int(relevance*100)}",
            status, relevance, importance,
            "Article body text.", "Lead excerpt.", None,
            0, now, now, now,
        ),
    )
    return int(cur.lastrowid)


def _link_company(db, article_id: int, company_id: int) -> None:
    db.execute(
        "INSERT INTO article_company (article_id, company_id, mention_count, relevance_in_article)"
        " VALUES (?,?,1,0.8)",
        (article_id, company_id),
    )


def _link_market(db, article_id: int, market_id: int) -> None:
    db.execute(
        "INSERT INTO article_market (article_id, market_id, mention_count, relevance_in_article)"
        " VALUES (?,?,1,0.7)",
        (article_id, market_id),
    )


def _insert_article_summary(db, run_id: int, article_id: int) -> None:
    """Insert a minimal summary row for an article (simulates step 5a output)."""
    from invest_watch.db_manager import utc_now_iso
    now = utc_now_iso()
    db.execute(
        "INSERT INTO summary (run_id, type, ref_id, content, json_payload, model_id,"
        " prompt_version, llm_used, validation_status, article_ids, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT (run_id, type, COALESCE(ref_id, 0)) DO NOTHING",
        (
            run_id, "article", article_id,
            f"**Title {article_id}**\n\nFaits :\n- Some fact [a{article_id}]",
            json.dumps({"faits": ["Some fact."]}),
            "ibm/granite-3-8b-instruct", "1.0", 1, "ok",
            json.dumps([article_id]), now,
        ),
    )


def _make_digester(db, llm, counter=None, **overrides):
    # call_counter is a DigestSummarizer kwarg, not a SummarizerConfig kwarg.
    call_counter = overrides.pop("call_counter", counter)
    cfg = _make_cfg(**overrides)
    return DigestSummarizer(db=db, llm=llm, config=cfg, call_counter=call_counter)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: prompt builders
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildCompanyDigestPrompt:
    def test_contains_company_name(self):
        blocks = [_make_block(1)]
        prompt = build_company_digest_prompt("BYD", "CN", blocks)
        assert "BYD" in prompt
        assert "CN" in prompt

    def test_contains_article_id_citation_instruction(self):
        blocks = [_make_block(1)]
        prompt = build_company_digest_prompt("BYD", None, blocks)
        assert "[aNN]" in prompt

    def test_contains_expected_sections(self):
        blocks = [_make_block(1)]
        prompt = build_company_digest_prompt("BYD", None, blocks)
        assert "Faits rapportés" in prompt
        assert "Conséquences potentielles" in prompt
        assert "Points à vérifier" in prompt

    def test_article_block_in_prompt(self):
        blocks = [_make_block(12, summary="BYD revenue up 30%.")]
        prompt = build_company_digest_prompt("BYD", None, blocks)
        assert "a12" in prompt
        assert "BYD revenue up 30%." in prompt

    def test_no_country_code(self):
        blocks = [_make_block(1)]
        prompt = build_company_digest_prompt("BYD", None, blocks)
        # Should not contain " (None)"
        assert "(None)" not in prompt


class TestBuildMarketDigestPrompt:
    def test_contains_market_name_and_type(self):
        blocks = [_make_block(1)]
        prompt = build_market_digest_prompt("Electric Vehicles", "sector", blocks)
        assert "Electric Vehicles" in prompt
        assert "sector" in prompt

    def test_contains_sections(self):
        blocks = [_make_block(1)]
        prompt = build_market_digest_prompt("EV", "sector", blocks)
        assert "Faits" in prompt
        assert "Points à vérifier" in prompt

    def test_article_count_in_prompt(self):
        blocks = [_make_block(i) for i in range(3)]
        prompt = build_market_digest_prompt("EV", "sector", blocks)
        assert "3" in prompt


class TestBuildGlobalDigestPrompt:
    def test_contains_run_date(self):
        prompt = build_global_digest_prompt("2026-09-08", ["## BYD\n..."], ["## EV\n..."])
        assert "2026-09-08" in prompt

    def test_contains_company_and_market_sections(self):
        prompt = build_global_digest_prompt("2026-09-08", ["## BYD\n..."], ["## EV\n..."])
        assert "DIGESTS COMPAGNIES" in prompt
        assert "DIGESTS MARCHÉS" in prompt

    def test_empty_digests_show_placeholder(self):
        prompt = build_global_digest_prompt("2026-09-08", [], [])
        assert "Aucun digest compagnie" in prompt
        assert "Aucun digest marché" in prompt

    def test_contains_required_sections(self):
        prompt = build_global_digest_prompt("2026-09-08", [], [])
        assert "Points critiques" in prompt
        assert "Points à vérifier" in prompt


class TestFormatArticleBlock:
    def test_short_code_in_output(self):
        block = _make_block(42)
        out = _format_article_block(block)
        assert "a42" in out

    def test_summary_truncated(self):
        from invest_watch.summarizer import _MAX_ARTICLE_BLOCK_CHARS
        block = _make_block(1, summary="x" * 2000)
        out = _format_article_block(block)
        assert "x" * _MAX_ARTICLE_BLOCK_CHARS in out
        assert "x" * (_MAX_ARTICLE_BLOCK_CHARS + 1) not in out

    def test_missing_published_at(self):
        block = _make_block(1)
        block.published_at = None
        out = _format_article_block(block)
        assert "?" in out


# ─────────────────────────────────────────────────────────────────────────────
# Tests: degraded fallback builders
# ─────────────────────────────────────────────────────────────────────────────

class TestDegradedCompanyDigest:
    def test_llm_used_false(self):
        d = _degraded_company_digest(
            run_id=1, company_id=1, company_name="BYD", country_code="CN",
            articles=[_make_block(12)], model_id="m",
        )
        assert d.llm_used is False
        assert d.validation_status == "degraded"
        assert d.digest_type == "company"

    def test_citations_in_content(self):
        d = _degraded_company_digest(
            run_id=1, company_id=1, company_name="BYD", country_code=None,
            articles=[_make_block(12)], model_id="m",
        )
        assert "[a12]" in d.content

    def test_article_ids_populated(self):
        d = _degraded_company_digest(
            run_id=1, company_id=1, company_name="BYD", country_code=None,
            articles=[_make_block(12), _make_block(15)], model_id="m",
        )
        assert 12 in d.article_ids
        assert 15 in d.article_ids


class TestDegradedMarketDigest:
    def test_llm_used_false(self):
        d = _degraded_market_digest(
            run_id=1, market_id=1, market_name="EV", market_type="sector",
            articles=[_make_block(5)], model_id="m",
        )
        assert d.llm_used is False
        assert d.digest_type == "market"

    def test_market_name_in_content(self):
        d = _degraded_market_digest(
            run_id=1, market_id=1, market_name="China A-shares", market_type="market",
            articles=[_make_block(5)], model_id="m",
        )
        assert "China A-shares" in d.content


class TestDegradedGlobalDigest:
    def _make_company_digest(self, article_ids: list[int]) -> Digest:
        return Digest(
            run_id=1, digest_type="company", ref_id=1, ref_name="BYD",
            content="## BYD\n...", llm_used=False, validation_status="degraded",
            model_id="m", prompt_version="1.0", article_ids=article_ids,
        )

    def _make_market_digest(self, article_ids: list[int]) -> Digest:
        return Digest(
            run_id=1, digest_type="market", ref_id=2, ref_name="EV",
            content="## EV\n...", llm_used=False, validation_status="degraded",
            model_id="m", prompt_version="1.0", article_ids=article_ids,
        )

    def test_llm_used_false(self):
        d = _degraded_global_digest(
            run_id=1, run_date="2026-09-08",
            company_digests=[], market_digests=[], model_id="m",
        )
        assert d.llm_used is False
        assert d.digest_type == "global"
        assert d.ref_id is None

    def test_aggregates_article_ids(self):
        c = self._make_company_digest([1, 2])
        m = self._make_market_digest([2, 3])
        d = _degraded_global_digest(
            run_id=1, run_date="2026-09-08",
            company_digests=[c], market_digests=[m], model_id="m",
        )
        assert sorted(d.article_ids) == [1, 2, 3]  # deduplicated

    def test_company_and_market_names_in_content(self):
        c = self._make_company_digest([1])
        m = self._make_market_digest([2])
        d = _degraded_global_digest(
            run_id=1, run_date="2026-09-08",
            company_digests=[c], market_digests=[m], model_id="m",
        )
        assert "BYD" in d.content
        assert "EV" in d.content


# ─────────────────────────────────────────────────────────────────────────────
# Tests: DigestSummarizer.digest_company
# ─────────────────────────────────────────────────────────────────────────────

class TestDigestCompany:
    def test_llm_digest_persisted(self, db):
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        article_id = _insert_article(db, run_id, url_suffix="c1")
        _link_company(db, article_id, company_id)
        _insert_article_summary(db, run_id, article_id)

        stub = LLMStub(responses=["## BYD (CN)\n### Faits rapportés\n- Revenue up. [a1]"])
        digester = _make_digester(db, stub)
        digest = digester.digest_company(run_id, company_id, "BYD", country_code="CN")

        assert digest.llm_used is True
        assert digest.validation_status == "ok"
        assert digest.db_id is not None
        row = db.query_one(
            "SELECT * FROM summary WHERE run_id=? AND type='company' AND ref_id=?",
            (run_id, company_id),
        )
        assert row is not None
        assert row["llm_used"] == 1

    def test_no_articles_produces_placeholder(self, db):
        run_id = _insert_run(db)
        company_id = _insert_company(db, name="Empty Co")
        stub = LLMStub()
        digester = _make_digester(db, stub)
        digest = digester.digest_company(run_id, company_id, "Empty Co")

        assert digest.llm_used is False
        assert stub.call_count == 0
        assert "Aucun article" in digest.content

    def test_llm_disabled_degrades(self, db):
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        article_id = _insert_article(db, run_id, url_suffix="d1")
        _link_company(db, article_id, company_id)
        _insert_article_summary(db, run_id, article_id)

        stub = LLMStub(responses=["## BYD\n..."])
        digester = _make_digester(db, stub, enabled=False)
        digest = digester.digest_company(run_id, company_id, "BYD")

        assert digest.llm_used is False
        assert stub.call_count == 0

    def test_circuit_breaker_open_does_not_block_digests(self, db):
        """A stub flagged degraded must NOT poison the remaining digests.

        Regression guard (run #2, 2026-09-15): the real client's
        ``is_degraded`` mirrors the *transport* circuit-breaker; per-digest
        failures (truncation, timeout) are recorded per digest but the
        digest loop keeps trying the following digests. Gating the whole
        DIGESTS step on one flag turned one bad digest into N degraded ones.
        """
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        article_id = _insert_article(db, run_id, url_suffix="cb1")
        _link_company(db, article_id, company_id)
        _insert_article_summary(db, run_id, article_id)

        stub = LLMStub(raise_after=0)
        try:
            stub.generate("x")
        except LLMDegradedError:
            pass
        assert stub.is_degraded is True  # global flag is set…

        # …but the digester still attempts the call (and records the outcome).
        digester = _make_digester(db, stub)
        digest = digester.digest_company(run_id, company_id, "BYD")

        assert digest.llm_used is False  # stub(raise_after=0) fails every call
        rows = db.query(
            "SELECT error_type FROM processing_error WHERE run_id=? AND step='digest'",
            (run_id,),
        )
        assert [r["error_type"] for r in rows] == ["llm_call_failed"]

    def test_call_cap_reached_degrades(self, db):
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        article_id = _insert_article(db, run_id, url_suffix="cap1")
        _link_company(db, article_id, company_id)
        _insert_article_summary(db, run_id, article_id)

        stub = LLMStub(responses=["## BYD\n..."] * 10)
        counter = [150]  # already at cap
        digester = _make_digester(db, stub, max_calls_per_run=150, call_counter=counter)
        digest = digester.digest_company(run_id, company_id, "BYD")

        assert digest.llm_used is False
        assert stub.call_count == 0

    def test_llm_error_falls_back(self, db):
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        article_id = _insert_article(db, run_id, url_suffix="err1")
        _link_company(db, article_id, company_id)
        _insert_article_summary(db, run_id, article_id)

        stub = LLMStub(raise_after=0)
        digester = _make_digester(db, stub)
        digest = digester.digest_company(run_id, company_id, "BYD")

        assert digest.llm_used is False
        assert digest.validation_status == "degraded"

    def test_idempotent_no_duplicate_row(self, db):
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        article_id = _insert_article(db, run_id, url_suffix="idem1")
        _link_company(db, article_id, company_id)
        _insert_article_summary(db, run_id, article_id)

        stub = LLMStub(responses=["## BYD\n..."] * 5)
        digester = _make_digester(db, stub)
        digester.digest_company(run_id, company_id, "BYD")
        digester.digest_company(run_id, company_id, "BYD")

        rows = db.query(
            "SELECT id FROM summary WHERE run_id=? AND type='company' AND ref_id=?",
            (run_id, company_id),
        )
        assert len(rows) == 1

    def test_article_ids_stored_in_db(self, db):
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        a1 = _insert_article(db, run_id, url_suffix="aid1")
        a2 = _insert_article(db, run_id, url_suffix="aid2")
        _link_company(db, a1, company_id)
        _link_company(db, a2, company_id)
        _insert_article_summary(db, run_id, a1)
        _insert_article_summary(db, run_id, a2)

        stub = LLMStub(responses=["## BYD\n..."])
        digester = _make_digester(db, stub)
        digest = digester.digest_company(run_id, company_id, "BYD")

        stored_ids = sorted(json.loads(
            db.query_one(
                "SELECT article_ids FROM summary WHERE run_id=? AND type='company' AND ref_id=?",
                (run_id, company_id),
            )["article_ids"]
        ))
        assert a1 in stored_ids
        assert a2 in stored_ids


# ─────────────────────────────────────────────────────────────────────────────
# Tests: DigestSummarizer.digest_market
# ─────────────────────────────────────────────────────────────────────────────

class TestDigestMarket:
    def test_llm_digest_persisted(self, db):
        run_id = _insert_run(db)
        market_id = _insert_market(db)
        article_id = _insert_article(db, run_id, url_suffix="m1")
        _link_market(db, article_id, market_id)
        _insert_article_summary(db, run_id, article_id)

        stub = LLMStub(responses=["## Electric Vehicles\n### Faits\n- EV sales up. [a1]"])
        digester = _make_digester(db, stub)
        digest = digester.digest_market(run_id, market_id, "Electric Vehicles", market_type="sector")

        assert digest.llm_used is True
        assert digest.db_id is not None

    def test_no_articles_placeholder(self, db):
        run_id = _insert_run(db)
        market_id = _insert_market(db, name="Empty Market")
        stub = LLMStub()
        digester = _make_digester(db, stub)
        digest = digester.digest_market(run_id, market_id, "Empty Market")

        assert digest.llm_used is False
        assert stub.call_count == 0

    def test_llm_disabled_degrades(self, db):
        run_id = _insert_run(db)
        market_id = _insert_market(db)
        article_id = _insert_article(db, run_id, url_suffix="md1")
        _link_market(db, article_id, market_id)
        _insert_article_summary(db, run_id, article_id)

        stub = LLMStub(responses=["## EV\n..."])
        digester = _make_digester(db, stub, enabled=False)
        digest = digester.digest_market(run_id, market_id, "EV")

        assert digest.llm_used is False

    def test_llm_error_falls_back(self, db):
        run_id = _insert_run(db)
        market_id = _insert_market(db)
        article_id = _insert_article(db, run_id, url_suffix="merr1")
        _link_market(db, article_id, market_id)
        _insert_article_summary(db, run_id, article_id)

        stub = LLMStub(raise_after=0)
        digester = _make_digester(db, stub)
        digest = digester.digest_market(run_id, market_id, "EV")

        assert digest.llm_used is False

    def test_excludes_company_articles(self, db):
        """When exclude_article_ids contains the only article, the digest is a
        placeholder (no LLM call, empty article_ids)."""
        run_id = _insert_run(db)
        market_id = _insert_market(db)
        article_id = _insert_article(db, run_id, url_suffix="excl1")
        _link_market(db, article_id, market_id)
        _insert_article_summary(db, run_id, article_id)

        stub = LLMStub(responses=["## EV\n..."])
        digester = _make_digester(db, stub)
        digest = digester.digest_market(
            run_id, market_id, "Electric Vehicles",
            exclude_article_ids=frozenset({article_id}),
        )

        assert digest.llm_used is False
        assert stub.call_count == 0
        assert digest.article_ids == []


# ─────────────────────────────────────────────────────────────────────────────
# Tests: DigestSummarizer.digest_global
# ─────────────────────────────────────────────────────────────────────────────

class TestDigestGlobal:
    def _make_digest(self, digest_type: str, ref_id: int | None, article_ids: list[int]) -> Digest:
        return Digest(
            run_id=1,
            digest_type=digest_type,
            ref_id=ref_id,
            ref_name="test",
            content="## test\n...",
            llm_used=True,
            validation_status="ok",
            model_id="m",
            prompt_version="1.0",
            article_ids=article_ids,
        )

    def test_global_digest_persisted(self, db):
        run_id = _insert_run(db)
        c = self._make_digest("company", 1, [10, 11])
        m = self._make_digest("market", 2, [12])

        stub = LLMStub(responses=["## Résumé global\nSynthèse de la journée."])
        digester = _make_digester(db, stub)
        digest = digester.digest_global(run_id, run_date="2026-09-08",
                                        company_digests=[c], market_digests=[m])

        assert digest.llm_used is True
        assert digest.digest_type == "global"
        assert digest.ref_id is None
        assert digest.db_id is not None

        row = db.query_one(
            "SELECT * FROM summary WHERE run_id=? AND type='global' AND ref_id IS NULL",
            (run_id,),
        )
        assert row is not None
        assert row["llm_used"] == 1

    def test_global_aggregates_article_ids(self, db):
        run_id = _insert_run(db)
        c = self._make_digest("company", 1, [1, 2])
        m = self._make_digest("market", 2, [2, 3])

        stub = LLMStub(responses=["## Résumé global\n..."])
        digester = _make_digester(db, stub)
        digest = digester.digest_global(run_id, run_date="2026-09-08",
                                        company_digests=[c], market_digests=[m])

        assert sorted(digest.article_ids) == [1, 2, 3]

    def test_degraded_global_when_disabled(self, db):
        run_id = _insert_run(db)
        stub = LLMStub(responses=["## Résumé global\n..."])
        digester = _make_digester(db, stub, enabled=False)
        digest = digester.digest_global(run_id, run_date="2026-09-08",
                                        company_digests=[], market_digests=[])
        assert digest.llm_used is False

    def test_idempotent_global_no_duplicate(self, db):
        run_id = _insert_run(db)
        stub = LLMStub(responses=["## Résumé global\n..."] * 5)
        digester = _make_digester(db, stub)
        digester.digest_global(run_id, run_date="2026-09-08", company_digests=[], market_digests=[])
        digester.digest_global(run_id, run_date="2026-09-08", company_digests=[], market_digests=[])

        rows = db.query(
            "SELECT id FROM summary WHERE run_id=? AND type='global'",
            (run_id,),
        )
        assert len(rows) == 1


# ─────────────────────────────────────────────────────────────────────────────
# Tests: DigestSummarizer.digest_run
# ─────────────────────────────────────────────────────────────────────────────

class TestDigestRun:
    def test_returns_three_level_tuple(self, db):
        # Article linked to both a company and a market.
        # After deduplication, the market digest receives 0 articles (company takes
        # priority), so the LLM is only called for the company and global digests.
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        market_id = _insert_market(db)
        a1 = _insert_article(db, run_id, url_suffix="r1")
        _link_company(db, a1, company_id)
        _link_market(db, a1, market_id)
        _insert_article_summary(db, run_id, a1)

        stub = LLMStub(responses=["## BYD\n...", "## Global\n..."])
        digester = _make_digester(db, stub)
        company_d, market_d, global_d = digester.digest_run(run_id, run_date="2026-09-08")

        assert len(company_d) == 1
        # Market digest is produced but with no articles (placeholder, llm_used=False)
        # because the only article was already covered by the company digest.
        assert len(market_d) == 1
        assert market_d[0].llm_used is False
        assert market_d[0].article_ids == []
        assert global_d.digest_type == "global"

    def test_run_date_fetched_from_db_when_none(self, db):
        run_id = _insert_run(db, run_date="2026-10-01")
        stub = LLMStub(responses=["## Global\n..."])
        digester = _make_digester(db, stub)
        _, _, global_d = digester.digest_run(run_id)  # no run_date argument

        assert "2026-10-01" in global_d.content or global_d.validation_status in ("ok", "degraded")

    def test_skips_duplicate_articles(self, db):
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        dup = _insert_article(db, run_id, status="duplicate", url_suffix="dup")
        _link_company(db, dup, company_id)

        stub = LLMStub(responses=["## Global\n..."])
        digester = _make_digester(db, stub)
        company_d, _, _ = digester.digest_run(run_id, run_date="2026-09-08")

        # The only article is duplicate → company digest has no articles → placeholder
        assert len(company_d) == 0 or all(len(d.article_ids) == 0 for d in company_d)

    def test_market_digest_excludes_company_articles(self, db):
        """An article linked to both a company and a market must only appear in the
        company digest; the market digest must be empty (placeholder)."""
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        market_id = _insert_market(db)
        article_id = _insert_article(db, run_id, url_suffix="mx1")
        _link_company(db, article_id, company_id)
        _link_market(db, article_id, market_id)
        _insert_article_summary(db, run_id, article_id)

        stub = LLMStub(responses=["## BYD\n...", "## Global\n..."])
        digester = _make_digester(db, stub)
        company_d, market_d, _ = digester.digest_run(run_id, run_date="2026-09-08")

        # Company digest received the article.
        assert len(company_d) == 1
        assert article_id in company_d[0].article_ids

        # Market digest was excluded → no articles → placeholder (llm_used=False).
        assert len(market_d) == 1
        assert article_id not in market_d[0].article_ids
        assert market_d[0].llm_used is False

    def test_market_keeps_unlinked_articles(self, db):
        """An article linked only to a market (no company) must still appear in the
        market digest even when other articles were excluded."""
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        market_id = _insert_market(db)

        # Article 1: linked to company + market → will be excluded from market digest.
        a_company = _insert_article(db, run_id, url_suffix="mk-co")
        _link_company(db, a_company, company_id)
        _link_market(db, a_company, market_id)
        _insert_article_summary(db, run_id, a_company)

        # Article 2: linked only to market → must appear in market digest.
        a_market_only = _insert_article(db, run_id, url_suffix="mk-only")
        _link_market(db, a_market_only, market_id)
        _insert_article_summary(db, run_id, a_market_only)

        stub = LLMStub(responses=["## BYD\n...", "## EV\n...", "## Global\n..."])
        digester = _make_digester(db, stub)
        company_d, market_d, _ = digester.digest_run(run_id, run_date="2026-09-08")

        # Company digest has the company-linked article.
        assert a_company in company_d[0].article_ids

        # Market digest has only the market-only article.
        assert len(market_d) == 1
        assert a_market_only in market_d[0].article_ids
        assert a_company not in market_d[0].article_ids


# ─────────────────────────────────────────────────────────────────────────────
# Tests: shared call counter
# ─────────────────────────────────────────────────────────────────────────────

class TestSharedCallCounter:
    def test_counter_incremented_on_digest_call(self, db):
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        article_id = _insert_article(db, run_id, url_suffix="cnt1")
        _link_company(db, article_id, company_id)
        _insert_article_summary(db, run_id, article_id)

        stub = LLMStub(responses=["## BYD\n..."] * 5)
        counter = [0]
        digester = _make_digester(db, stub, call_counter=counter)
        digester.digest_company(run_id, company_id, "BYD")

        assert counter[0] == 1

    def test_shared_counter_caps_both_article_and_digest_calls(self, db):
        """When the shared counter is at the cap, digests must degrade."""
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        article_id = _insert_article(db, run_id, url_suffix="shared1")
        _link_company(db, article_id, company_id)
        _insert_article_summary(db, run_id, article_id)

        stub = LLMStub(responses=["## BYD\n..."] * 5)
        counter = [150]   # already at max_calls_per_run=150
        digester = _make_digester(db, stub, max_calls_per_run=150, call_counter=counter)
        digest = digester.digest_company(run_id, company_id, "BYD")

        assert digest.llm_used is False
        assert counter[0] == 150  # unchanged


# ─────────────────────────────────────────────────────────────────────────────
# Tests: digest content hygiene (P0 — run #2 instruction leakage)
# ─────────────────────────────────────────────────────────────────────────────


class TestSanitiseDigestContent:
    LEAK = ' Ne pas utiliser de phrases comme "aujourd\'hui". Les faits doivent être uniques.\n\n'

    def test_strips_leaking_instructions(self):
        content = self.LEAK + "## BYD\n### Faits rapportés\n- Revenue up. [a1]"
        cleaned = _sanitise_digest_content(content)
        assert cleaned.startswith("## BYD")
        assert "Ne pas utiliser" not in cleaned

    def test_clean_content_untouched(self):
        content = "## BYD\n### Faits rapportés\n- Revenue up. [a1]"
        assert _sanitise_digest_content(content) == content

    def test_content_without_heading_untouched(self):
        content = "texte sans titre de section"
        assert _sanitise_digest_content(content) == content

    def test_empty_content_untouched(self):
        assert _sanitise_digest_content("") == ""
        assert _sanitise_digest_content("   ") == "   "

    def test_h3_heading_also_strips_prefix(self):
        content = self.LEAK + "### Faits rapportés\n- ok [a1]"
        cleaned = _sanitise_digest_content(content)
        assert cleaned.startswith("### Faits")


# ─────────────────────────────────────────────────────────────────────────────
# Tests: digest LLM failures persist to processing_error (P0 follow-up)
# Run #2 20:48: 7 truncated digests produced ONLY log lines — llm_status
# stayed 'ok'. Every digest LLM failure must leave a visible row.
# ─────────────────────────────────────────────────────────────────────────────

from invest_watch.llm_client import LLMTruncatedError  # noqa: E402


class TestDigestLLMErrorPersistence:
    def test_company_digest_truncation_persists_llm_truncated(self, db):
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        article_id = _insert_article(db, run_id, url_suffix="digerr1")
        _link_company(db, article_id, company_id)
        _insert_article_summary(db, run_id, article_id)

        class _TruncStub:
            is_degraded = False

            def generate(self, prompt: str) -> str:
                raise LLMTruncatedError(
                    "LLM output truncated (finish_reason=length) — "
                    "increase llm.max_new_tokens (current: 3000)"
                )

        digest = _make_digester(db, _TruncStub()).digest_company(run_id, company_id, "BYD")

        assert digest.llm_used is False
        assert digest.validation_status == "degraded"
        rows = db.query(
            "SELECT step, article_id, error_type FROM processing_error WHERE run_id=?",
            (run_id,),
        )
        assert len(rows) == 1
        assert rows[0]["step"] == "digest"
        assert rows[0]["article_id"] is None  # digest aggregates many articles
        assert rows[0]["error_type"] == "llm_truncated"

    def test_market_digest_transport_failure_persists_llm_call_failed(self, db):
        run_id = _insert_run(db)
        market_id = _insert_market(db)
        article_id = _insert_article(db, run_id, url_suffix="digerr2")
        _link_market(db, article_id, market_id)
        _insert_article_summary(db, run_id, article_id)

        stub = LLMStub(raise_after=0)  # every generate() raises LLMDegradedError
        digest = _make_digester(db, stub).digest_market(run_id, market_id, "EV")

        assert digest.llm_used is False
        rows = db.query(
            "SELECT error_type FROM processing_error WHERE run_id=? AND step='digest'",
            (run_id,),
        )
        assert [r["error_type"] for r in rows] == ["llm_call_failed"]

    def test_global_digest_failure_references_global_ref(self, db):
        run_id = _insert_run(db)
        stub = LLMStub(raise_after=0)
        digest = _make_digester(db, stub).digest_global(
            run_id, run_date="2026-09-08", company_digests=[], market_digests=[]
        )

        assert digest.llm_used is False
        rows = db.query(
            "SELECT error_type, message FROM processing_error"
            " WHERE run_id=? AND step='digest'",
            (run_id,),
        )
        assert len(rows) == 1
        assert rows[0]["error_type"] == "llm_call_failed"
        assert "global" in rows[0]["message"]

    def test_digest_errors_capped_per_run(self, db):
        run_id = _insert_run(db)
        stub = LLMStub(raise_after=0)
        digester = _make_digester(db, stub)
        for i in range(12):
            cid = _insert_company(db, name=f"C{i:02d}")
            aid = _insert_article(db, run_id, url_suffix=f"digcap{i:02d}")
            _link_company(db, aid, cid)
            _insert_article_summary(db, run_id, aid)
            digester.digest_company(run_id, cid, f"C{i:02d}")

        rows = db.query(
            "SELECT id FROM processing_error WHERE run_id=? AND step='digest'",
            (run_id,),
        )
        assert len(rows) == 10  # _MAX_LLM_ERROR_ROWS_PER_RUN


class TestDigestLeakStrippingEndToEnd:
    def test_company_digest_content_starts_with_heading(self, db):
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        article_id = _insert_article(db, run_id, url_suffix="leak1")
        _link_company(db, article_id, company_id)
        _insert_article_summary(db, run_id, article_id)

        leaked = (
            ' Si un fait est mentionné dans plusieurs articles, ne le citer qu\'une seule fois.\n\n'
            "## BYD\n### Faits rapportés\n- Revenue up. [a1]"
        )
        stub = LLMStub(responses=[leaked])
        digester = _make_digester(db, stub)
        digest = digester.digest_company(run_id, company_id, "BYD")

        assert digest.llm_used is True
        assert digest.validation_status == "ok"
        assert digest.content.startswith("## BYD")
        assert "ne le citer qu" not in digest.content

        row = db.query_one(
            "SELECT content FROM summary WHERE run_id=? AND type='company' AND ref_id=?",
            (run_id, company_id),
        )
        assert row is not None
        assert row["content"].startswith("## BYD")
