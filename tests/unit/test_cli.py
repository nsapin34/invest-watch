"""CLI tests — Step 1 deliverable (specs §13.1, architecture §8).

Every command runs against a temporary project root (via ``--config``) with a
real on-disk SQLite database, and is verified through the repositories.
"""

import logging

import pytest

from invest_watch.cli import EXIT_ERROR, EXIT_NOT_IMPLEMENTED, EXIT_OK, main
from invest_watch.config_manager import load_config
from invest_watch.company_repository import CompanyRepository
from invest_watch.db_manager import DatabaseManager
from invest_watch.market_repository import MarketRepository
from invest_watch.source_repository import SourceRepository

from tests.conftest import make_config


@pytest.fixture(autouse=True)
def _close_logging_handlers():
    yield
    root = logging.getLogger("invest_watch")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


def _cfgdb(project):
    cfg = load_config(project / "conf" / "config.toml")
    db = DatabaseManager(cfg.db_path)
    db.apply_migrations()
    return cfg, db


def test_company_add_list_edit_remove_roundtrip(project, capsys):
    assert main(["--config", str(project / "conf" / "config.toml"), "company", "add",
                 "--name", "BYD", "--ticker", "BYDDY", "--country", "CN"]) == EXIT_OK
    assert "added company #1: BYD" in capsys.readouterr().out

    assert main(["--config", str(project / "conf" / "config.toml"), "company", "list"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "BYD" in out and "BYDDY" in out and "CN" in out

    assert main(["--config", str(project / "conf" / "config.toml"), "company", "edit",
                 "byd", "--ticker", "BG", "--rename", "BYD Co"]) == EXIT_OK
    assert "updated company #1: BYD Co" in capsys.readouterr().out

    assert main(["--config", str(project / "conf" / "config.toml"), "company", "remove",
                 "byD co", "--yes"]) == EXIT_OK
    assert "removed company #1: BYD Co" in capsys.readouterr().out

    cfg, db = _cfgdb(project)
    assert CompanyRepository(db).list() == []
    db.close()


def test_company_add_duplicate_exits_1(project, capsys):
    argv = ["--config", str(project / "conf" / "config.toml"), "company", "add", "--name", "BYD"]
    assert main(argv) == EXIT_OK
    assert main(argv) == EXIT_ERROR
    assert "error:" in capsys.readouterr().err


def test_company_add_with_market_and_alias(project, capsys):
    base = ["--config", str(project / "conf" / "config.toml")]
    assert main([*base, "market", "add", "Electric Vehicles", "--type", "industry"]) == EXIT_OK
    assert main([*base, "company", "add", "--name", "BYD", "--ticker", "BYDDY",
                 "--market", "Electric Vehicles=1.5", "--alias", "比亚迪", "--alias", "BYD Company"]) == EXIT_OK

    cfg, db = _cfgdb(project)
    company = CompanyRepository(db).get_by_name("BYD")
    linked = CompanyRepository(db).markets_for(company.id)
    assert [(m.name, w) for m, w in linked] == [("Electric Vehicles", 1.5)]
    terms = [a.term for a in CompanyRepository(db).aliases_for(company.id)]
    assert terms == ["比亚迪", "BYD Company"]
    db.close()


def test_company_edit_aliases_and_markets(project, capsys):
    base = ["--config", str(project / "conf" / "config.toml")]
    main([*base, "market", "add", "EV", "--type", "industry"])
    main([*base, "company", "add", "--name", "BYD"])
    assert main([*base, "company", "edit", "BYD",
                 "--add-alias", "First", "--set-aliases", "A, B",
                 "--attach-market", "EV=2.0", "--detach-market", "EV"]) == EXIT_OK

    cfg, db = _cfgdb(project)
    company = CompanyRepository(db).get_by_name("BYD")
    assert [a.term for a in CompanyRepository(db).aliases_for(company.id)] == ["A", "B"]
    assert CompanyRepository(db).markets_for(company.id) == []
    db.close()


def test_company_remove_requires_confirmation(project, capsys, monkeypatch):
    base = ["--config", str(project / "conf" / "config.toml")]
    main([*base, "company", "add", "--name", "BYD"])
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    assert main([*base, "company", "remove", "BYD"]) == EXIT_ERROR
    assert "aborted" in capsys.readouterr().out

    cfg, db = _cfgdb(project)
    assert CompanyRepository(db).get_by_name("BYD") is not None
    db.close()


def test_market_add_list_and_tree(project, capsys):
    base = ["--config", str(project / "conf" / "config.toml")]
    assert main([*base, "market", "add", "China", "--type", "market", "--code", "CN.A"]) == EXIT_OK
    assert main([*base, "market", "add", "Tech", "--type", "sector", "--parent", "China"]) == EXIT_OK
    assert main([*base, "market", "add", "Semi", "--type", "industry", "--parent", "Tech"]) == EXIT_OK

    assert main([*base, "market", "list", "--tree"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "China (market)" in out
    assert "Tech (sector)" in out
    assert "Semi (industry)" in out


def test_market_remove_requires_force(project, capsys):
    base = ["--config", str(project / "conf" / "config.toml")]
    main([*base, "market", "add", "China", "--type", "market"])
    main([*base, "market", "add", "Tech", "--type", "sector", "--parent", "China"])

    assert main([*base, "market", "remove", "China"]) == EXIT_ERROR
    assert "children" in capsys.readouterr().err

    assert main([*base, "market", "remove", "China", "--force"]) == EXIT_OK
    cfg, db = _cfgdb(project)
    assert MarketRepository(db).get_by_name("China") is None
    assert MarketRepository(db).get_by_name("Tech").parent_id is None  # detached
    db.close()

def test_market_edit_type_change(project, capsys):
    base = ["--config", str(project / "conf" / "config.toml")]
    main([*base, "market", "add", "Tech", "--type", "sector"])
    assert main([*base, "market", "edit", "Tech", "--type", "market"]) == EXIT_OK
    cfg, db = _cfgdb(project)
    assert MarketRepository(db).get_by_name("Tech").type == "market"
    db.close()


def test_source_add_list_edit_remove_roundtrip(project, capsys):
    base = ["--config", str(project / "conf" / "config.toml")]
    assert main([*base, "source", "add", "HKEX", "--type", "rss",
                 "--url", "https://example.com/rss.xml", "--lang", "en", "--priority", "2"]) == EXIT_OK

    assert main([*base, "source", "list"]) == EXIT_OK
    assert "HKEX" in capsys.readouterr().out

    assert main([*base, "source", "edit", "HKEX", "--url", "https://new.example.com/x",
                 "--priority", "1"]) == EXIT_OK
    assert main([*base, "source", "remove", "HKEX", "--yes"]) == EXIT_OK

    cfg, db = _cfgdb(project)
    assert SourceRepository(db).get_by_name("HKEX") is None
    db.close()


def test_db_migrate_and_version(project, capsys):
    base = ["--config", str(project / "conf" / "config.toml")]
    assert main([*base, "db", "migrate"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "applied migrations: v001" in out and "v009" in out

    assert main([*base, "db", "migrate"]) == EXIT_OK
    assert "already at schema version 11" in capsys.readouterr().out

    assert main([*base, "db", "version"]) == EXIT_OK
    assert "schema version:  11" in capsys.readouterr().out


def test_db_migrate_seeds_config_entities(project, capsys):
    make_config(
        project,
        '[app]\ntimezone = "Europe/Paris"\n'
        '\n[[sources]]\nname = "HKEX"\ntype = "rss"\nurl = "https://x/rss.xml"\n'
        '\n[[companies]]\nname = "BYD"\nalias = ["比亚迪"]\n'
        '\n[[companies.markets]]\nname = "EV"\ntype = "industry"\n',
    )
    base = ["--config", str(project / "conf" / "config.toml")]
    assert main([*base, "db", "migrate"]) == EXIT_OK
    assert "1 source(s), 1 company(ies), 1 market(s) created" in capsys.readouterr().out

    cfg, db = _cfgdb(project)
    assert SourceRepository(db).get_by_name("HKEX") is not None
    company = CompanyRepository(db).get_by_name("BYD")
    assert [a.term for a in CompanyRepository(db).aliases_for(company.id)] == ["比亚迪"]
    assert [m.name for m, _ in CompanyRepository(db).markets_for(company.id)] == ["EV"]
    db.close()

def test_cli_status_command(project, capsys):
    base = ["--config", str(project / "conf" / "config.toml")]
    assert main([*base, "company", "add", "--name", "BYD"]) == EXIT_OK
    assert main([*base, "status"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "=== Database Status ===" in out
    assert "1 companies" in out
    assert "=== Execution Locks ===" in out
    assert "=== Last Run ===" in out
    assert "=== Last Run — Articles by Source ===" in out
    assert "=== LLM Service ===" in out
    # Verify no secrets leak
    assert "WATSONX_API_KEY" not in out
    assert "secret" not in out.lower()


def test_cli_status_articles_by_source(project, capsys):
    """Status shows per-source article counts (stored, duplicate, errors)."""
    from invest_watch.article_repository import ArticleRepository
    from invest_watch.db_manager import utc_now_iso

    base = ["--config", str(project / "conf" / "config.toml")]
    cfg, db = _cfgdb(project)

    # Insert a source and a run with articles in different statuses.
    now = utc_now_iso()
    src_id = db.execute(
        "INSERT INTO source (name, type, url, active, priority, created_at, updated_at)"
        " VALUES (?,?,?,1,1,?,?)",
        ("Brave Search", "search", "https://api.search.brave.com", now, now),
    ).lastrowid
    repo = ArticleRepository(db)
    run = repo.create_run("2026-09-20", "2026-09-19T00:00:00Z", "2026-09-19T23:59:59Z")

    def _art(suffix, status):
        url = f"https://example.com/{suffix}"
        db.execute(
            "INSERT INTO article"
            " (run_id, source_id, url_original, url_normalized, title, short_code,"
            "  status, relevance_score, date_certaine, collected_at, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,0.8,0,?,?,?)",
            (run.id, src_id, url, url, f"Title {suffix}", f"sc{suffix}",
             status, now, now, now),
        )

    _art("a1", "new")
    _art("a2", "new")
    _art("a3", "duplicate")

    # Insert a processing error linked to the same source and run.
    db.execute(
        "INSERT INTO processing_error"
        " (run_id, source_id, step, error_type, retry_count, status, message, created_at, updated_at)"
        " VALUES (?,?,'collect','http_403',0,'new','403 Forbidden',?,?)",
        (run.id, src_id, now, now),
    )
    db.close()

    assert main([*base, "status"]) == EXIT_OK
    out = capsys.readouterr().out

    assert "=== Last Run — Articles by Source ===" in out
    assert "Brave Search" in out
    # 2 stored (new), 1 duplicate, 1 error
    lines = [l for l in out.splitlines() if "Brave Search" in l]
    assert lines, "Brave Search row missing from source table"
    row = lines[0]
    assert "2" in row   # stored
    assert "1" in row   # duplicate (and errors share the same count)


def test_cli_errors_command(project, capsys):
    base = ["--config", str(project / "conf" / "config.toml")]
    assert main([*base, "errors"]) == EXIT_OK
    assert "No processing errors recorded." in capsys.readouterr().out

    assert main([*base, "source", "add", "HKEX", "--type", "rss", "--url", "https://hkex.com/rss"]) == EXIT_OK
    capsys.readouterr()

    cfg, db = _cfgdb(project)
    source_row = db.query_one("SELECT id FROM source WHERE name = 'HKEX'")
    db.execute(
        "INSERT INTO processing_error (step, source_id, error_type, retry_count, status, message, created_at, updated_at)"
        " VALUES ('collect', ?, 'ConnectionError', 2, 'retrying', 'Connection timeout', '2026-09-08T07:00:00Z', '2026-09-08T07:00:00Z')",
        (source_row["id"],),
    )
    db.close()

    assert main([*base, "errors", "--limit", "10"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "HKEX" in out
    assert "Connection timeout" in out
    assert "collect" in out


def test_cli_search_command_fts5(project, capsys):
    base = ["--config", str(project / "conf" / "config.toml")]
    from invest_watch.article_repository import ArticleRepository

    cfg, db = _cfgdb(project)
    repo = ArticleRepository(db)
    run = repo.create_run("2026-09-08", "2026-09-07T00:00:00Z", "2026-09-07T23:59:59Z")
    repo.insert_candidate(
        run_id=run.id,
        title="BYD reveals massive expansion in Europe",
        url="https://example.com/byd-europe",
        content="BYD is investing heavily in electric vehicles manufacturing plant in Hungary.",
        excerpt="BYD electric vehicles expansion.",
    )
    repo.insert_candidate(
        run_id=run.id,
        title="Tencent releases new AI model",
        url="https://example.com/tencent-ai",
        content="Tencent cloud announced foundation model updates.",
        excerpt="Tencent AI news.",
    )
    db.close()

    assert main([*base, "search", "electric vehicles"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "Found 1 matching article(s):" in out
    assert "BYD reveals" in out

    assert main([*base, "search", "Tencent"]) == EXIT_OK
    out2 = capsys.readouterr().out
    assert "Tencent releases" in out2

    assert main([*base, "search", "NonExistentWord12345"]) == EXIT_OK
    assert "No articles matching 'NonExistentWord12345' found." in capsys.readouterr().out


def test_cli_search_purge_sync(project, capsys):
    from invest_watch.article_repository import ArticleRepository
    from invest_watch.orchestrator import _purge_old_articles

    cfg, db = _cfgdb(project)
    repo = ArticleRepository(db)
    run = repo.create_run("2020-01-01", "2020-01-01T00:00:00Z", "2020-01-01T23:59:59Z")
    repo.insert_candidate(
        run_id=run.id,
        title="Old obsolete news from 2020",
        url="https://example.com/old",
        content="Old content to be purged.",
        excerpt="Old excerpt.",
    )
    repo.finish_run(run.id, status="completed", error_count=0, stats_json=None)

    # Search finds it before purge
    rows = db.query("SELECT * FROM article_fts WHERE article_fts MATCH 'obsolete'")
    assert len(rows) == 1

    # Run purge
    _purge_old_articles(db, retention_days=90)

    # FTS5 index is clean after purge
    rows_after = db.query("SELECT * FROM article_fts WHERE article_fts MATCH 'obsolete'")
    assert len(rows_after) == 0
    db.close()


def test_cli_report_and_export_commands(project, capsys):
    base = ["--config", str(project / "conf" / "config.toml")]
    from invest_watch.article_repository import ArticleRepository
    from invest_watch.report_generator import ReportConfig, ReportGenerator

    cfg, db = _cfgdb(project)
    repo = ArticleRepository(db)
    run = repo.create_run("2026-09-08", "2026-09-07T00:00:00Z", "2026-09-07T23:59:59Z")
    repo.finish_run(run.id, status="completed", error_count=0, stats_json='{"harvested": 10, "stored": 5}')
    db.close()

    # Report command displays markdown report
    assert main([*base, "report", "--date", "2026-09-08"]) == EXIT_OK
    out_rep = capsys.readouterr().out
    assert "# invest-watch — Report of 2026-09-08" in out_rep

    # Export md
    assert main([*base, "export", "--format", "md", "--date", "2026-09-08"]) == EXIT_OK
    out_exp_md = capsys.readouterr().out
    assert "# invest-watch — Report of 2026-09-08" in out_exp_md

    # Export json
    assert main([*base, "export", "--format", "json", "--date", "2026-09-08"]) == EXIT_OK
    out_exp_json = capsys.readouterr().out
    import json
    data = json.loads(out_exp_json)
    assert data["run_date"] == "2026-09-08"

    # No secrets in output
    assert "WATSONX_API_KEY" not in out_rep
    assert "WATSONX_API_KEY" not in out_exp_json


def test_missing_config_exits_1(tmp_path, capsys):
    assert main(["--config", str(tmp_path / "nope.toml"), "company", "list"]) == EXIT_ERROR
    assert "error:" in capsys.readouterr().err


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    assert "company" in capsys.readouterr().out


def test_alias_via_invest_watch_home(project, capsys, monkeypatch):
    monkeypatch.setenv("INVEST_WATCH_HOME", str(project))
    assert main(["company", "add", "--name", "BYD"]) == EXIT_OK
    cfg, db = _cfgdb(project)
    assert CompanyRepository(db).get_by_name("BYD") is not None
    db.close()
# ─────────────────────────────────────────────────────────────────────────────
# P1 items 9 + 11 — run --trigger / --retry-degraded wiring
# ─────────────────────────────────────────────────────────────────────────────


def test_run_trigger_is_persisted(project, capsys):
    """``run --trigger scheduler`` is recorded in ``daily_run.trigger`` (P1 item 9)."""
    base = ["--config", str(project / "conf" / "config.toml")]

    assert main([*base, "run", "--date", "2026-09-12", "--trigger", "scheduler"]) == EXIT_OK
    capsys.readouterr()

    cfg, db = _cfgdb(project)
    row = db.query_one("SELECT trigger, status FROM daily_run ORDER BY id DESC LIMIT 1")
    assert row is not None
    assert row["trigger"] == "scheduler"
    assert row["status"] in ("completed", "partial", "degraded")
    db.close()


def test_run_retry_degraded_requires_resume(project, capsys):
    """``run --retry-degraded`` without ``--resume`` is rejected (P1 item 11)."""
    base = ["--config", str(project / "conf" / "config.toml")]

    assert main([*base, "run", "--retry-degraded"]) == EXIT_NOT_IMPLEMENTED
    assert "--resume" in capsys.readouterr().err


def test_report_retry_degraded_resumes_latest_run(project, capsys):
    """``report --retry-degraded`` re-runs the LLM pipeline on the latest run."""
    from invest_watch.article_repository import ArticleRepository

    base = ["--config", str(project / "conf" / "config.toml")]
    cfg, db = _cfgdb(project)
    repo = ArticleRepository(db)
    run = repo.create_run("2026-09-12", "2026-09-11T00:00:00Z", "2026-09-11T23:59:59Z")
    repo.finish_run(run.id, status="completed", error_count=0, stats_json="{}")
    db.close()

    assert main([*base, "report", "--retry-degraded"]) == EXIT_OK
    out = capsys.readouterr().out
    assert f"Retrying LLM summarisation of degraded articles (run #{run.id})" in out
    assert "# invest-watch — Report of 2026-09-12" in out

    # A resume with --retry-degraded IS a retry → trigger updated in the DB.
    cfg, db = _cfgdb(project)
    row = db.query_one("SELECT trigger FROM daily_run WHERE id = ?", (run.id,))
    assert row["trigger"] == "retry"
    db.close()
