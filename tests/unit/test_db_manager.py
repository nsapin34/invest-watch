"""Unit tests for db_manager: migrations, WAL, transactions, app_meta."""

import re
import sqlite3

import pytest

from invest_watch.db_manager import DatabaseManager, utc_now_iso
from invest_watch.exceptions import MigrationError


def test_no_schema_before_migrations():
    db = DatabaseManager(":memory:")
    assert db.schema_version == 0
    db.close()


def test_migrations_apply_fully(db):
    assert db.schema_version == 3
    assert db.latest_migration_version() == 3


def test_migrations_are_idempotent(db):
    assert db.apply_migrations() == []
    assert db.schema_version == 3


def test_migrations_up_to_v1(db_v1):
    assert db_v1.schema_version == 1
    tables = {row["name"] for row in db_v1.query("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "company" in tables
    assert "article_version" not in tables


def test_v1_to_v2_preserves_data(db_v1):
    now = "2026-09-01T00:00:00Z"
    db_v1.execute(
        "INSERT INTO market (name, type, created_at, updated_at) VALUES ('China A', 'market', ?, ?)",
        (now, now),
    )
    market_id = db_v1.query_one("SELECT id FROM market WHERE name = 'China A'")["id"]

    # v1 -> v2 upgrade keeps the data and bumps db_version
    applied = db_v1.apply_migrations()
    assert applied == ["v002_article_version.sql", "v003_daily_run_checkpoint.sql"]
    assert db_v1.schema_version == 3

    still_there = db_v1.query_one("SELECT id FROM market WHERE id = ?", (market_id,))
    assert still_there is not None

    # and is idempotent on the second pass
    assert db_v1.apply_migrations() == []


def test_sqlite_version_fts5_supported(db):
    version = db.query_one("SELECT sqlite_version() AS v")["v"]
    assert tuple(int(part) for part in version.split(".")) >= (3, 9)
    # FTS5 table was created by v001
    row = db.query_one("SELECT name FROM sqlite_master WHERE name = 'article_fts'")
    assert row is not None


def test_foreign_keys_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO company_market (company_id, market_id) VALUES (999, 999)"
        )


def test_check_constraint_enforced(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO market (name, type, created_at, updated_at)"
            " VALUES ('Bogus', 'constellation', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')"
        )


def test_file_database_uses_wal(tmp_path):
    path = tmp_path / "data" / "wal.db"
    db = DatabaseManager(path)
    assert db.is_file_db
    row = db.query_one("PRAGMA journal_mode")
    assert row[0].lower() == "wal"
    db.close()


def test_in_memory_is_not_file_db():
    db = DatabaseManager(":memory:")
    assert not db.is_file_db
    db.close()


def test_app_meta_get_set(db):
    assert db.get_meta("missing") is None
    assert db.get_meta("missing", "m") == "m"
    db.set_meta("last_run", "2026-09-01")
    db.set_meta("last_run", "2026-09-02")  # upsert
    assert db.get_meta("last_run") == "2026-09-02"


def test_transaction_rolls_back_on_error(db):
    now = "2026-01-01T00:00:00Z"
    with pytest.raises(RuntimeError):
        with db.transaction():
            db.execute(
                "INSERT INTO market (name, type, created_at, updated_at) VALUES ('X', 'market', ?, ?)",
                (now, now),
            )
            raise RuntimeError("boom")
    assert db.query_one("SELECT COUNT(*) AS n FROM market")["n"] == 0


def test_transaction_commits(db):
    now = "2026-01-01T00:00:00Z"
    with db.transaction():
        db.execute(
            "INSERT INTO market (name, type, created_at, updated_at) VALUES ('X', 'market', ?, ?)",
            (now, now),
        )
    assert db.query_one("SELECT COUNT(*) AS n FROM market")["n"] == 1


def test_bad_migration_script_raises_and_preserves_version(db, tmp_path):
    (tmp_path / "v004_broken.sql").write_text("THIS IS NOT VALID SQL;", encoding="utf-8")
    db.migrations_dir = tmp_path
    with pytest.raises(MigrationError, match="v004"):
        db.apply_migrations()
    assert db.schema_version == 3  # version untouched


def test_missing_migrations_dir_raises(db, tmp_path):
    db.migrations_dir = tmp_path / "does-not-exist"
    with pytest.raises(MigrationError, match="migrations directory"):
        db.apply_migrations()


def test_duplicate_migration_versions_detected(db, tmp_path):
    (tmp_path / "v001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "v001_b.sql").write_text("SELECT 2;", encoding="utf-8")
    db.migrations_dir = tmp_path
    with pytest.raises(MigrationError, match="duplicate migration"):
        db.apply_migrations()


def test_utc_now_iso_format():
    stamp = utc_now_iso()
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", stamp)
    assert stamp.endswith("Z")


def test_context_manager_closes_connection(tmp_path):
    path = tmp_path / "data" / "ctx.db"
    with DatabaseManager(path) as db:
        db.execute("SELECT 1")
        assert db._conn is not None
    assert db._conn is None