"""Unit tests for report_generator.py — Step 5c.

Covers (architecture §8 / specs §13.2–§13.3):

ReportConfig / helpers
- _duration_str: seconds, minutes, missing timestamps.
- _period_display: formats period string.
- _llm_status_line: ok / degraded / off / None.
- _importance_sort_key: critique < haute < moyenne < faible.
- _extract_impact: reads first consequence from json_payload.
- _atomic_write: file written, temp file cleaned up.

ReportGenerator.generate
- Creates the reports/ directory if absent.
- Returns (md_path, json_path) with correct filenames.
- Overwrites existing files (idempotent).
- Raises ValueError for unknown run_id.

Markdown report structure (§13.2)
- Header: run_date, period, LLM status line, execution summary.
- Global summary section present.
- 🚨 Critical information table present (populated / empty).
- By company section present (digest content included).
- By market section present (digest content included).
- Key articles table: all eligible articles listed, correct priority order.
- Unverified section: extracts incertitudes + note_contradictions.
- Errors section: populated / empty.
- Footer: auto-generated line present.
- model_id / prompt_version in header LLM line.

JSON report structure
- Top-level keys: run_id, run_date, articles, companies, markets, errors, short_links.
- articles list: id == short_code, faits/incertitudes from json_payload.
- short_links map: short_code → url_original.
- No duplicate articles.
- Duplicate/redundant/unrelated articles excluded.

Display rules (§13.3)
- Short link Markdown format: [aNN](url).
- Priority: critique first in key-articles table.
- requires_human_check shown as ✔ in critical table.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from invest_watch.db_manager import utc_now_iso
from invest_watch.report_generator import (
    ReportConfig,
    ReportGenerator,
    _atomic_write,
    _duration_str,
    _extract_impact,
    _importance_sort_key,
    _llm_status_line,
    _period_display,
)


# ─────────────────────────────────────────────────────────────────────────────
# DB seeding helpers
# ─────────────────────────────────────────────────────────────────────────────

def _insert_run(db, *, run_date="2026-09-08", status="completed",
                llm_status="ok", error_count=0,
                started_at=None, finished_at=None) -> int:
    now = utc_now_iso()
    cur = db.execute(
        "INSERT INTO daily_run"
        " (run_date, period_start, period_end, status, llm_status, error_count,"
        "  started_at, finished_at, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (run_date, "2026-09-07T00:00:00Z", "2026-09-07T23:59:59Z",
         status, llm_status, error_count, started_at, finished_at, now),
    )
    return int(cur.lastrowid)


def _insert_company(db, name="BYD", country="CN") -> int:
    now = utc_now_iso()
    cur = db.execute(
        "INSERT INTO company (name, country_code, active, created_at, updated_at)"
        " VALUES (?,?,1,?,?)", (name, country, now, now),
    )
    return int(cur.lastrowid)


def _insert_market(db, name="Electric Vehicles", mtype="sector") -> int:
    now = utc_now_iso()
    cur = db.execute(
        "INSERT INTO market (name, type, active, created_at, updated_at)"
        " VALUES (?,?,1,?,?)", (name, mtype, now, now),
    )
    return int(cur.lastrowid)


def _insert_article(db, run_id, *, url_suffix="x", status="new",
                    importance="haute", relevance=0.8,
                    requires_human_check=0) -> int:
    now = utc_now_iso()
    url = f"https://example.com/{url_suffix}"
    cur = db.execute(
        "INSERT INTO article"
        " (run_id, url_original, url_normalized, title, short_code, status,"
        "  relevance_score, importance_level, requires_human_check,"
        "  date_certaine, collected_at, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?)",
        (run_id, url, url, f"Title {url_suffix}", f"a{url_suffix}",
         status, relevance, importance, requires_human_check,
         now, now, now),
    )
    return int(cur.lastrowid)


def _insert_source(db, name="Reuters") -> int:
    now = utc_now_iso()
    cur = db.execute(
        "INSERT INTO source (name, type, url, active, created_at, updated_at)"
        " VALUES (?,?,?,1,?,?)",
        (name, "rss", "https://example.com/feed.xml", now, now),
    )
    return int(cur.lastrowid)


def _link_company(db, article_id, company_id):
    db.execute(
        "INSERT INTO article_company (article_id, company_id, mention_count, relevance_in_article)"
        " VALUES (?,?,1,0.8)", (article_id, company_id),
    )


def _link_market(db, article_id, market_id):
    db.execute(
        "INSERT INTO article_market (article_id, market_id, mention_count, relevance_in_article)"
        " VALUES (?,?,1,0.7)", (article_id, market_id),
    )


def _insert_short_link(db, article_id, short_code, url):
    now = utc_now_iso()
    db.execute(
        "INSERT INTO short_link (short_code, url_original, article_id, created_at)"
        " VALUES (?,?,?,?)", (short_code, url, article_id, now),
    )


def _insert_article_summary(db, run_id, article_id, *,
                             json_payload: dict | None = None,
                             llm_used: int = 1) -> None:
    now = utc_now_iso()
    payload_str = json.dumps(json_payload or {
        "faits": ["Revenue up 30%."],
        "incertitudes_ou_conflits": ["Margin outlook unclear."],
        "consequences_potentielles": [
            {"cible": "BYD", "impact": "positive", "horizon": "court", "confidence": "haute"}
        ],
        "note_contradictions": [],
        "verification_humaine_requise": True,
    })
    db.execute(
        "INSERT INTO summary (run_id, type, ref_id, content, json_payload,"
        " model_id, prompt_version, llm_used, validation_status, article_ids, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT (run_id, type, COALESCE(ref_id, 0)) DO NOTHING",
        (run_id, "article", article_id,
         f"**Title {article_id}**\nFaits :\n- Revenue up 30%.",
         payload_str,
         "ibm/granite-3-8b-instruct", "1.0", llm_used, "ok",
         json.dumps([article_id]), now),
    )


def _insert_company_digest(db, run_id, company_id, content="## BYD\n...") -> None:
    now = utc_now_iso()
    db.execute(
        "INSERT INTO summary (run_id, type, ref_id, content, json_payload,"
        " model_id, prompt_version, llm_used, validation_status, article_ids, created_at)"
        " VALUES (?,?,?,?,NULL,?,?,?,?,?,?)"
        " ON CONFLICT (run_id, type, COALESCE(ref_id, 0)) DO NOTHING",
        (run_id, "company", company_id, content,
         "ibm/granite-3-8b-instruct", "1.0", 1, "ok",
         json.dumps([]), now),
    )


def _insert_market_digest(db, run_id, market_id, content="## EV\n...") -> None:
    now = utc_now_iso()
    db.execute(
        "INSERT INTO summary (run_id, type, ref_id, content, json_payload,"
        " model_id, prompt_version, llm_used, validation_status, article_ids, created_at)"
        " VALUES (?,?,?,?,NULL,?,?,?,?,?,?)"
        " ON CONFLICT (run_id, type, COALESCE(ref_id, 0)) DO NOTHING",
        (run_id, "market", market_id, content,
         "ibm/granite-3-8b-instruct", "1.0", 1, "ok",
         json.dumps([]), now),
    )


def _insert_global_digest(db, run_id, content="## Résumé global\nBonne journée.") -> None:
    now = utc_now_iso()
    db.execute(
        "INSERT INTO summary (run_id, type, ref_id, content, json_payload,"
        " model_id, prompt_version, llm_used, validation_status, article_ids, created_at)"
        " VALUES (?,?,NULL,?,NULL,?,?,?,?,?,?)"
        " ON CONFLICT (run_id, type, COALESCE(ref_id, 0)) DO NOTHING",
        (run_id, "global", content,
         "ibm/granite-3-8b-instruct", "1.0", 1, "ok",
         json.dumps([]), now),
    )


def _insert_error(db, run_id, step="summarise", error_type="timeout", message="timed out"):
    now = utc_now_iso()
    db.execute(
        "INSERT INTO processing_error (run_id, step, error_type, message, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        (run_id, step, error_type, message, now, now),
    )


def _make_cfg(tmp_path) -> ReportConfig:
    return ReportConfig(
        reports_dir=tmp_path / "reports",
        model_id="ibm/granite-3-8b-instruct",
        prompt_version="1.0",
        timezone_name="Europe/Paris",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tests: helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestDurationStr:
    def test_minutes(self):
        assert _duration_str("2026-09-08T06:00:00Z", "2026-09-08T06:34:00Z") == "34 min"

    def test_seconds(self):
        assert _duration_str("2026-09-08T06:00:00Z", "2026-09-08T06:00:45Z") == "45 s"

    def test_missing_returns_question_mark(self):
        assert _duration_str(None, None) == "?"
        assert _duration_str("2026-09-08T06:00:00Z", None) == "?"


class TestPeriodDisplay:
    def test_format(self):
        s = _period_display("2026-09-07T00:00:00Z", "2026-09-07T23:59:59Z", "Europe/Paris")
        assert "2026-09-07 00:00" in s
        assert "23:59" in s
        assert "Europe/Paris" in s


class TestLlmStatusLine:
    def test_ok(self):
        line = _llm_status_line("ok", "ibm/granite-3-8b-instruct", "1.0")
        assert "✔" in line
        assert "ibm/granite-3-8b-instruct" in line
        assert "prompt v1.0" in line

    def test_degraded(self):
        line = _llm_status_line("degraded", "ibm/granite-3-8b-instruct", "1.0")
        assert "degraded" in line

    def test_off(self):
        line = _llm_status_line("off", "m", "1.0")
        assert "disabled" in line

    def test_none(self):
        line = _llm_status_line(None, "m", "1.0")
        assert "disabled" in line


class TestImportanceSortKey:
    def test_critique_before_haute(self):
        a_crit = {"importance_level": "critique", "published_at": "2026-09-07", "relevance_score": 0.9}
        a_haute = {"importance_level": "haute", "published_at": "2026-09-07", "relevance_score": 0.9}
        assert _importance_sort_key(a_crit) < _importance_sort_key(a_haute)

    def test_missing_importance_defaults_moyenne(self):
        a = {"importance_level": None, "published_at": "2026-09-07", "relevance_score": 0.5}
        key = _importance_sort_key(a)
        assert key[0] == 2  # "moyenne" rank


class TestExtractImpact:
    def test_returns_impact_string(self):
        summary = {
            "json_payload": json.dumps({
                "consequences_potentielles": [
                    {"cible": "BYD", "impact": "positive", "horizon": "court", "confidence": "haute"}
                ]
            })
        }
        result = _extract_impact(summary)
        assert "positive" in result
        assert "court" in result

    def test_no_consequences_returns_dash(self):
        summary = {"json_payload": json.dumps({"consequences_potentielles": []})}
        assert _extract_impact(summary) == "—"

    def test_none_summary_returns_dash(self):
        assert _extract_impact(None) == "—"

    def test_invalid_json_returns_dash(self):
        assert _extract_impact({"json_payload": "not json"}) == "—"


class TestAtomicWrite:
    def test_writes_file(self, tmp_path):
        p = tmp_path / "out.md"
        _atomic_write(p, "hello world")
        assert p.read_text(encoding="utf-8") == "hello world"

    def test_overwrites_existing(self, tmp_path):
        p = tmp_path / "out.md"
        p.write_text("old content", encoding="utf-8")
        _atomic_write(p, "new content")
        assert p.read_text(encoding="utf-8") == "new content"

    def test_creates_parent_directory(self, tmp_path):
        p = tmp_path / "sub" / "dir" / "out.md"
        _atomic_write(p, "content")
        assert p.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Tests: ReportGenerator.generate — file I/O
# ─────────────────────────────────────────────────────────────────────────────

class TestReportGeneratorIO:
    def test_creates_reports_directory(self, db, tmp_path):
        run_id = _insert_run(db)
        cfg = _make_cfg(tmp_path)
        gen = ReportGenerator(db=db, config=cfg)
        md, jpath = gen.generate(run_id)
        assert cfg.reports_dir.is_dir()

    def test_returns_correct_filenames(self, db, tmp_path):
        run_id = _insert_run(db, run_date="2026-09-08")
        cfg = _make_cfg(tmp_path)
        gen = ReportGenerator(db=db, config=cfg)
        md, jpath = gen.generate(run_id)
        assert md.name == "2026-09-08.md"
        assert jpath.name == "2026-09-08.json"

    def test_files_exist_after_generate(self, db, tmp_path):
        run_id = _insert_run(db)
        gen = ReportGenerator(db=db, config=_make_cfg(tmp_path))
        md, jpath = gen.generate(run_id)
        assert md.exists()
        assert jpath.exists()

    def test_idempotent_overwrites(self, db, tmp_path):
        run_id = _insert_run(db)
        gen = ReportGenerator(db=db, config=_make_cfg(tmp_path))
        gen.generate(run_id)
        gen.generate(run_id)  # second call must not crash
        assert (tmp_path / "reports" / "2026-09-08.md").exists()

    def test_raises_for_unknown_run_id(self, db, tmp_path):
        gen = ReportGenerator(db=db, config=_make_cfg(tmp_path))
        with pytest.raises(ValueError, match="daily_run #9999"):
            gen.generate(9999)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: Markdown structure (§13.2)
# ─────────────────────────────────────────────────────────────────────────────

class TestMarkdownStructure:
    def _generate(self, db, tmp_path, **run_kwargs) -> str:
        run_id = _insert_run(db, **run_kwargs)
        gen = ReportGenerator(db=db, config=_make_cfg(tmp_path))
        md, _ = gen.generate(run_id)
        return md.read_text(encoding="utf-8")

    def test_header_contains_run_date(self, db, tmp_path):
        text = self._generate(db, tmp_path, run_date="2026-09-08")
        assert "2026-09-08" in text

    def test_header_llm_status_ok(self, db, tmp_path):
        text = self._generate(db, tmp_path, llm_status="ok")
        assert "✔" in text
        assert "ibm/granite-3-8b-instruct" in text

    def test_header_period_present(self, db, tmp_path):
        text = self._generate(db, tmp_path)
        assert "Period covered" in text
        assert "Europe/Paris" in text

    def test_header_execution_line(self, db, tmp_path):
        text = self._generate(db, tmp_path, status="completed", error_count=2)
        assert "Execution" in text
        assert "completed" in text
        assert "2 minor errors" in text

    def test_global_summary_section_present(self, db, tmp_path):
        run_id = _insert_run(db)
        _insert_global_digest(db, run_id, content="## Résumé global\nLa journée s'est bien passée.")
        gen = ReportGenerator(db=db, config=_make_cfg(tmp_path))
        md, _ = gen.generate(run_id)
        text = md.read_text(encoding="utf-8")
        assert "Global summary" in text
        assert "La journée s'est bien passée." in text

    def test_no_global_digest_placeholder(self, db, tmp_path):
        text = self._generate(db, tmp_path)
        assert "Global summary" in text
        assert "No global summary" in text

    def test_critical_section_present(self, db, tmp_path):
        text = self._generate(db, tmp_path)
        assert "Critical information" in text

    def test_critical_article_appears_in_table(self, db, tmp_path):
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        art_id = _insert_article(db, run_id, url_suffix="crit1",
                                  importance="critique", requires_human_check=1)
        _link_company(db, art_id, company_id)
        _insert_article_summary(db, run_id, art_id)
        gen = ReportGenerator(db=db, config=_make_cfg(tmp_path))
        md, _ = gen.generate(run_id)
        text = md.read_text(encoding="utf-8")
        assert "Title crit1" in text
        assert "✔" in text

    def test_by_company_section_present(self, db, tmp_path):
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        _insert_company_digest(db, run_id, company_id, content="## BYD (CN)\nFaits rapportés...")
        gen = ReportGenerator(db=db, config=_make_cfg(tmp_path))
        md, _ = gen.generate(run_id)
        text = md.read_text(encoding="utf-8")
        assert "By company" in text
        assert "BYD (CN)" in text

    def test_by_market_section_present(self, db, tmp_path):
        run_id = _insert_run(db)
        market_id = _insert_market(db)
        _insert_market_digest(db, run_id, market_id, content="## Electric Vehicles\nFaits...")
        gen = ReportGenerator(db=db, config=_make_cfg(tmp_path))
        md, _ = gen.generate(run_id)
        text = md.read_text(encoding="utf-8")
        assert "By market" in text
        assert "Electric Vehicles" in text

    def test_key_articles_table_present(self, db, tmp_path):
        run_id = _insert_run(db)
        _insert_article(db, run_id, url_suffix="ka1")
        gen = ReportGenerator(db=db, config=_make_cfg(tmp_path))
        md, _ = gen.generate(run_id)
        text = md.read_text(encoding="utf-8")
        assert "Key articles" in text
        assert "aka1" in text  # short_code

    def test_key_articles_priority_critique_first(self, db, tmp_path):
        run_id = _insert_run(db)
        _insert_article(db, run_id, url_suffix="faible1", importance="faible", relevance=0.3)
        _insert_article(db, run_id, url_suffix="critique1", importance="critique", relevance=0.9)
        gen = ReportGenerator(db=db, config=_make_cfg(tmp_path))
        md, _ = gen.generate(run_id)
        text = md.read_text(encoding="utf-8")
        pos_crit = text.index("acritique1")
        pos_faib = text.index("afaible1")
        assert pos_crit < pos_faib

    def test_unverified_section_present(self, db, tmp_path):
        run_id = _insert_run(db)
        art_id = _insert_article(db, run_id, url_suffix="u1")
        _insert_article_summary(db, run_id, art_id, json_payload={
            "incertitudes_ou_conflits": ["Margin outlook unclear."],
            "note_contradictions": ["Figure mismatch vs press release."],
            "consequences_potentielles": [],
            "faits": [],
            "verification_humaine_requise": False,
        })
        gen = ReportGenerator(db=db, config=_make_cfg(tmp_path))
        md, _ = gen.generate(run_id)
        text = md.read_text(encoding="utf-8")
        assert "Unverified" in text
        assert "Margin outlook unclear." in text
        assert "Figure mismatch" in text

    def test_errors_section_populated(self, db, tmp_path):
        run_id = _insert_run(db)
        _insert_error(db, run_id, step="fetch", error_type="http_4xx", message="paywall")
        gen = ReportGenerator(db=db, config=_make_cfg(tmp_path))
        md, _ = gen.generate(run_id)
        text = md.read_text(encoding="utf-8")
        assert "Errors" in text
        assert "paywall" in text

    def test_errors_section_empty_placeholder(self, db, tmp_path):
        text = self._generate(db, tmp_path)
        assert "No errors recorded" in text

    def test_footer_present(self, db, tmp_path):
        text = self._generate(db, tmp_path)
        assert "Auto-generated report" in text
        assert "verifiable" in text

    def test_duplicate_articles_excluded(self, db, tmp_path):
        run_id = _insert_run(db)
        _insert_article(db, run_id, url_suffix="dup1", status="duplicate")
        _insert_article(db, run_id, url_suffix="real1", status="new")
        gen = ReportGenerator(db=db, config=_make_cfg(tmp_path))
        md, _ = gen.generate(run_id)
        text = md.read_text(encoding="utf-8")
        assert "adup1" not in text
        assert "areal1" in text

    def test_short_link_url_in_table(self, db, tmp_path):
        """The article URL appears as a link in the Source column (§13.3)."""
        run_id = _insert_run(db)
        art_id = _insert_article(db, run_id, url_suffix="sl1")
        _insert_short_link(db, art_id, "asl1", "https://example.com/sl1")
        gen = ReportGenerator(db=db, config=_make_cfg(tmp_path))
        md, _ = gen.generate(run_id)
        text = md.read_text(encoding="utf-8")
        # short_code present in the # column
        assert "asl1" in text
        # URL appears somewhere in the table row
        assert "https://example.com/sl1" in text


# ─────────────────────────────────────────────────────────────────────────────
# Tests: JSON structure
# ─────────────────────────────────────────────────────────────────────────────

class TestJsonStructure:
    def _generate_json(self, db, tmp_path, run_id) -> dict:
        gen = ReportGenerator(db=db, config=_make_cfg(tmp_path))
        _, jpath = gen.generate(run_id)
        return json.loads(jpath.read_text(encoding="utf-8"))

    def test_top_level_keys_present(self, db, tmp_path):
        run_id = _insert_run(db)
        data = self._generate_json(db, tmp_path, run_id)
        for key in ("run_id", "run_date", "articles", "companies", "markets", "errors", "short_links"):
            assert key in data

    def test_run_id_matches(self, db, tmp_path):
        run_id = _insert_run(db)
        data = self._generate_json(db, tmp_path, run_id)
        assert data["run_id"] == run_id

    def test_articles_have_id_field(self, db, tmp_path):
        run_id = _insert_run(db)
        _insert_article(db, run_id, url_suffix="j1")
        data = self._generate_json(db, tmp_path, run_id)
        assert len(data["articles"]) == 1
        assert data["articles"][0]["id"] == "aj1"

    def test_articles_faits_populated_from_summary(self, db, tmp_path):
        run_id = _insert_run(db)
        art_id = _insert_article(db, run_id, url_suffix="j2")
        _insert_article_summary(db, run_id, art_id, json_payload={
            "faits": ["Revenue up 30%."],
            "incertitudes_ou_conflits": [],
            "consequences_potentielles": [],
            "note_contradictions": [],
            "verification_humaine_requise": False,
        })
        data = self._generate_json(db, tmp_path, run_id)
        article = next(a for a in data["articles"] if a["id"] == "aj2")
        assert "Revenue up 30%." in article["faits"]

    def test_short_links_map_populated(self, db, tmp_path):
        run_id = _insert_run(db)
        art_id = _insert_article(db, run_id, url_suffix="jsl")
        _insert_short_link(db, art_id, "ajsl", "https://example.com/jsl")
        data = self._generate_json(db, tmp_path, run_id)
        # url_original is stored directly on article row
        assert "ajsl" in data["short_links"]

    def test_companies_list_populated(self, db, tmp_path):
        run_id = _insert_run(db)
        company_id = _insert_company(db)
        _insert_company_digest(db, run_id, company_id, "## BYD\nFaits...")
        data = self._generate_json(db, tmp_path, run_id)
        assert len(data["companies"]) == 1
        assert data["companies"][0]["name"] == "BYD"

    def test_markets_list_populated(self, db, tmp_path):
        run_id = _insert_run(db)
        market_id = _insert_market(db)
        _insert_market_digest(db, run_id, market_id, "## Electric Vehicles\nFaits...")
        data = self._generate_json(db, tmp_path, run_id)
        assert len(data["markets"]) == 1
        assert data["markets"][0]["name"] == "Electric Vehicles"

    def test_errors_list_populated(self, db, tmp_path):
        run_id = _insert_run(db)
        _insert_error(db, run_id, message="timeout after 120s")
        data = self._generate_json(db, tmp_path, run_id)
        assert len(data["errors"]) == 1
        assert "timeout" in data["errors"][0]["message"]

    def test_duplicate_articles_excluded_from_json(self, db, tmp_path):
        run_id = _insert_run(db)
        _insert_article(db, run_id, url_suffix="jdup", status="duplicate")
        _insert_article(db, run_id, url_suffix="jreal", status="new")
        data = self._generate_json(db, tmp_path, run_id)
        ids = [a["id"] for a in data["articles"]]
        assert "ajdup" not in ids
        assert "ajreal" in ids

    def test_global_summary_in_json(self, db, tmp_path):
        run_id = _insert_run(db)
        _insert_global_digest(db, run_id, content="## Résumé global\nSynthèse.")
        data = self._generate_json(db, tmp_path, run_id)
        assert data["global_summary"] is not None
        assert "Synthèse" in data["global_summary"]

    def test_json_is_valid_utf8(self, db, tmp_path):
        run_id = _insert_run(db)
        _insert_article(db, run_id, url_suffix="utf")
        gen = ReportGenerator(db=db, config=_make_cfg(tmp_path))
        _, jpath = gen.generate(run_id)
        # read_text should not raise
        content = jpath.read_text(encoding="utf-8")
        json.loads(content)  # must parse cleanly
