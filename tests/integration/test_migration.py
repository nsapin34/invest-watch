"""Integration tests — database migrations (specs §15, architecture §8 Step 2).

Validates:
- v001 schema applies cleanly to a fresh database.
- v002 (article_version) applies over v001, data is preserved, idempotent.
- apply_migrations() is safe to call multiple times without re-applying.
- db_version in app_meta tracks the highest applied migration.
"""

from __future__ import annotations

import sqlite3

import pytest

from invest_watch.db_manager import DatabaseManager


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _table_names(db: DatabaseManager) -> set[str]:
    rows = db.query("SELECT name FROM sqlite_master WHERE type='table'")
    return {row["name"] for row in rows}


def _column_names(db: DatabaseManager, table: str) -> set[str]:
    rows = db.query(f"PRAGMA table_info({table})")
    return {row["col_name"] if "col_name" in row.keys() else row[1] for row in rows}


def _columns(db: DatabaseManager, table: str) -> set[str]:
    """Return the set of column names for *table*."""
    cur = db.connection.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in cur.fetchall()}


# ─────────────────────────────────────────────────────────────────────────────
# v001 baseline
# ─────────────────────────────────────────────────────────────────────────────


def test_v001_creates_all_core_tables():
    db = DatabaseManager(":memory:")
    db.apply_migrations(up_to=1)
    tables = _table_names(db)
    for expected in (
        "market", "company", "company_market", "source", "search_alias",
        "daily_run", "article", "category", "article_company", "article_market",
        "article_category", "summary", "processing_error", "short_link",
        "app_meta", "article_fts",
    ):
        assert expected in tables, f"table '{expected}' missing after v001"
    assert db.schema_version == 1
    db.close()


def test_v001_article_required_columns():
    db = DatabaseManager(":memory:")
    db.apply_migrations(up_to=1)
    cols = _columns(db, "article")
    for col in (
        "id", "run_id", "url_original", "url_normalized", "title",
        "content_hash", "short_code", "status", "date_certaine", "collected_at",
    ):
        assert col in cols, f"article.{col} missing after v001"
    db.close()


def test_v001_daily_run_status_constraint():
    """daily_run.status must be one of the enumerated values."""
    db = DatabaseManager(":memory:")
    db.apply_migrations(up_to=1)
    from invest_watch.db_manager import utc_now_iso
    now = utc_now_iso()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO daily_run (run_date, period_start, period_end, status, error_count, created_at)"
            " VALUES (?,?,?,?,?,?)",
            ("2026-09-11", now, now, "bogus_status", 0, now),
        )
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# v002 upgrade: article_version table
# ─────────────────────────────────────────────────────────────────────────────


def test_v002_adds_article_version_table():
    db = DatabaseManager(":memory:")
    db.apply_migrations(up_to=1)
    assert "article_version" not in _table_names(db)

    db.apply_migrations(up_to=2)
    assert "article_version" in _table_names(db)
    assert db.schema_version == 2
    db.close()


def test_v002_article_version_columns():
    db = DatabaseManager(":memory:")
    db.apply_migrations(up_to=2)
    cols = _columns(db, "article_version")
    for col in ("id", "article_id", "content_hash", "title", "excerpt", "changed_at", "change_reason"):
        assert col in cols, f"article_version.{col} missing after v002"
    db.close()


def test_v001_data_preserved_after_v002():
    """Rows inserted under v001 survive the v002 migration."""
    db = DatabaseManager(":memory:")
    db.apply_migrations(up_to=1)

    from invest_watch.db_manager import utc_now_iso
    now = utc_now_iso()
    db.execute(
        "INSERT INTO daily_run (run_date, period_start, period_end, status, error_count, created_at)"
        " VALUES (?,?,?,?,?,?)",
        ("2026-09-11", now, now, "completed", 0, now),
    )
    run_id = db.query_one("SELECT id FROM daily_run WHERE run_date='2026-09-11'")["id"]

    db.apply_migrations(up_to=2)

    row = db.query_one("SELECT * FROM daily_run WHERE id=?", (run_id,))
    assert row is not None
    assert row["run_date"] == "2026-09-11"
    assert row["status"] == "completed"
    db.close()


# ─────────────────────────────────────────────────────────────────────────────
# Idempotency
# ─────────────────────────────────────────────────────────────────────────────


def test_apply_migrations_is_idempotent():
    """Calling apply_migrations() twice applies nothing on the second call."""
    db = DatabaseManager(":memory:")
    first = db.apply_migrations()
    second = db.apply_migrations()
    assert len(first) == 11  # v001 .. v011
    assert second == []
    assert db.schema_version == 11
    db.close()


def test_schema_version_matches_latest_migration():
    db = DatabaseManager(":memory:")
    db.apply_migrations()
    assert db.schema_version == db.latest_migration_version()
    db.close()


def test_partial_up_to_respected():
    """apply_migrations(up_to=1) stops at v001 even when v002 is on disk."""
    db = DatabaseManager(":memory:")
    applied = db.apply_migrations(up_to=1)
    assert applied == ["v001_initial_schema.sql"]
    assert db.schema_version == 1
    assert "article_version" not in _table_names(db)
    db.close()
