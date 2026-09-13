"""Unit tests for run_manager — sub-step 6.2.

Covers:
- RunStep ordering and step_index().
- steps_to_run() for fresh and resumed runs.
- RunManager.start() creates a daily_run row in status 'running'.
- RunManager.start() on an existing run_date reopens it (resume).
- RunManager.set_checkpoint() persists the step in the DB.
- RunManager.get_checkpoint() round-trips correctly.
- RunManager.get_checkpoint() returns None when column is NULL.
- RunManager.get_checkpoint() handles an unknown checkpoint value gracefully.
- RunManager.finish() updates status + stats_json + writes last_run.status.
- last_run.status file content matches the final status.
- last_run.status file is written atomically (OSError is non-fatal).
- RunManager.load_existing() returns a DailyRun for a known run_id.
- RunManager.load_existing() raises NotFoundError for an unknown run_id.
- RunManager.run raises RuntimeError before start().
- RunStats.exit_code() maps status → correct exit code.
- RunStats.as_dict() / as_json() include all counters.
- Migration v003 adds the checkpoint column (idempotent).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from invest_watch.db_manager import DatabaseManager
from invest_watch.exceptions import NotFoundError
from invest_watch.run_manager import (
    EXIT_COMPLETED,
    EXIT_FAILED,
    EXIT_PARTIAL,
    RunManager,
    RunStats,
    RunStep,
    steps_to_run,
)


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def mem_db() -> DatabaseManager:
    db = DatabaseManager(":memory:")
    db.apply_migrations()
    yield db
    db.close()


@pytest.fixture()
def data_dir(tmp_path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


@pytest.fixture()
def mgr(mem_db, data_dir) -> RunManager:
    return RunManager(mem_db, data_dir, llm_enabled=False)


def _start(mgr: RunManager, run_date: str = "2024-01-15") -> None:
    mgr.start(run_date, "2024-01-14T00:00:00Z", "2024-01-14T23:59:59Z")


# ── RunStep ordering ──────────────────────────────────────────────────────────


def test_runstep_order_is_stable():
    steps = list(RunStep)
    assert steps[0] is RunStep.PERIOD
    assert steps[-1] is RunStep.WRAP_UP
    assert len(steps) == 15


def test_runstep_step_index_is_zero_based():
    assert RunStep.PERIOD.step_index() == 0
    assert RunStep.WRAP_UP.step_index() == 14


def test_runstep_indices_are_unique():
    indices = [s.step_index() for s in RunStep]
    assert len(indices) == len(set(indices))


# ── steps_to_run() ────────────────────────────────────────────────────────────


def test_steps_to_run_fresh_returns_all():
    assert steps_to_run(None) == list(RunStep)


def test_steps_to_run_after_period_skips_first():
    remaining = steps_to_run(RunStep.PERIOD)
    assert RunStep.PERIOD not in remaining
    assert RunStep.LOAD_CONFIG in remaining
    assert len(remaining) == 14


def test_steps_to_run_after_wrap_up_returns_empty():
    assert steps_to_run(RunStep.WRAP_UP) == []


def test_steps_to_run_midway():
    remaining = steps_to_run(RunStep.DEDUP)
    assert RunStep.DEDUP not in remaining
    assert RunStep.CLASSIFY in remaining
    # exactly the steps after DEDUP (index 7)
    assert len(remaining) == 14 - RunStep.DEDUP.step_index()


# ── RunManager.start() ────────────────────────────────────────────────────────


def test_start_creates_running_row(mgr, mem_db):
    _start(mgr)
    row = mem_db.query_one("SELECT * FROM daily_run WHERE id = ?", (mgr.run_id,))
    assert row["status"] == "running"
    assert row["run_date"] == "2024-01-15"


def test_start_sets_run_attribute(mgr):
    _start(mgr)
    assert mgr.run.id > 0
    assert mgr.run.status == "running"


def test_start_same_date_reopens_existing_row(mgr, mem_db):
    _start(mgr)
    first_id = mgr.run_id
    # Simulate a second start on the same date (resume scenario).
    mgr2 = RunManager(mem_db, mgr._data_dir, llm_enabled=False)
    mgr2.start("2024-01-15", "2024-01-14T00:00:00Z", "2024-01-14T23:59:59Z")
    assert mgr2.run_id == first_id  # same row, not a new one
    row = mem_db.query_one("SELECT COUNT(*) AS n FROM daily_run WHERE run_date = '2024-01-15'")
    assert row["n"] == 1


def test_start_does_not_clear_checkpoint_on_reopen(mgr, mem_db):
    _start(mgr)
    mgr.set_checkpoint(RunStep.FETCH)
    # Reopen
    mgr2 = RunManager(mem_db, mgr._data_dir, llm_enabled=False)
    mgr2.start("2024-01-15", "2024-01-14T00:00:00Z", "2024-01-14T23:59:59Z")
    # checkpoint must survive the reopen
    assert mgr2.get_checkpoint() == RunStep.FETCH


# ── RunManager.set_checkpoint() / get_checkpoint() ───────────────────────────


def test_set_and_get_checkpoint_round_trip(mgr):
    _start(mgr)
    for step in RunStep:
        mgr.set_checkpoint(step)
        assert mgr.get_checkpoint() == step


def test_get_checkpoint_returns_none_before_any_set(mgr):
    _start(mgr)
    assert mgr.get_checkpoint() is None


def test_get_checkpoint_returns_none_for_null_column(mgr, mem_db):
    _start(mgr)
    mem_db.execute("UPDATE daily_run SET checkpoint = NULL WHERE id = ?", (mgr.run_id,))
    assert mgr.get_checkpoint() is None


def test_get_checkpoint_handles_unknown_value_gracefully(mgr, mem_db):
    _start(mgr)
    mem_db.execute("UPDATE daily_run SET checkpoint = 'nonexistent_step' WHERE id = ?", (mgr.run_id,))
    assert mgr.get_checkpoint() is None  # logs a warning but does not raise


# ── RunManager.finish() ───────────────────────────────────────────────────────


def _make_stats(mgr: RunManager, status: str = "completed") -> RunStats:
    s = RunStats(run_id=mgr.run_id, run_date="2024-01-15", status=status)
    s.harvested = 10
    s.stored = 8
    s.error_count = 0
    return s


def test_finish_updates_status_in_db(mgr, mem_db):
    _start(mgr)
    mgr.finish(_make_stats(mgr, "completed"))
    row = mem_db.query_one("SELECT status, finished_at FROM daily_run WHERE id = ?", (mgr.run_id,))
    assert row["status"] == "completed"
    assert row["finished_at"] is not None


def test_finish_writes_stats_json(mgr, mem_db):
    _start(mgr)
    stats = _make_stats(mgr, "partial")
    stats.harvested = 42
    mgr.finish(stats)
    row = mem_db.query_one("SELECT stats_json FROM daily_run WHERE id = ?", (mgr.run_id,))
    payload = json.loads(row["stats_json"])
    assert payload["harvested"] == 42


def test_finish_writes_last_run_status_file(mgr, data_dir):
    _start(mgr)
    mgr.finish(_make_stats(mgr, "completed"))
    content = (data_dir / "last_run.status").read_text(encoding="utf-8")
    assert content.strip() == "completed"


@pytest.mark.parametrize("status", ["completed", "partial", "failed", "cancelled"])
def test_finish_last_run_status_matches_status(mgr, data_dir, status):
    _start(mgr)
    mgr.finish(_make_stats(mgr, status))
    content = (data_dir / "last_run.status").read_text(encoding="utf-8")
    assert content.strip() == status


def test_finish_tolerates_oserror_on_last_run_file(mgr):
    """OSError when writing last_run.status must not propagate."""
    _start(mgr)
    with patch("invest_watch.run_manager.Path.write_text", side_effect=OSError("disk full")):
        mgr.finish(_make_stats(mgr, "completed"))  # must not raise


# ── RunManager.load_existing() ────────────────────────────────────────────────


def test_load_existing_returns_correct_run(mgr, mem_db, data_dir):
    _start(mgr)
    run_id = mgr.run_id

    mgr2 = RunManager(mem_db, data_dir)
    loaded = mgr2.load_existing(run_id)
    assert loaded.id == run_id


def test_load_existing_raises_not_found_for_unknown_id(mgr, data_dir, mem_db):
    _start(mgr)
    mgr2 = RunManager(mem_db, data_dir)
    with pytest.raises(NotFoundError):
        mgr2.load_existing(9999)


# ── RunManager.run property guard ─────────────────────────────────────────────


def test_run_property_raises_before_start(mgr):
    with pytest.raises(RuntimeError, match="start\\(\\)"):
        _ = mgr.run


# ── RunStats ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status,expected", [
    ("completed", EXIT_COMPLETED),
    ("partial",   EXIT_PARTIAL),
    ("cancelled", EXIT_PARTIAL),
    ("failed",    EXIT_FAILED),
])
def test_runstats_exit_code(status, expected):
    s = RunStats(run_id=1, run_date="2024-01-15", status=status)
    assert s.exit_code() == expected


def test_runstats_as_dict_contains_all_counters():
    s = RunStats(run_id=1, run_date="2024-01-15")
    s.harvested = 5
    s.stored = 3
    d = s.as_dict()
    assert d["harvested"] == 5
    assert d["stored"] == 3
    assert "error_count" in d


def test_runstats_as_json_is_valid():
    s = RunStats(run_id=1, run_date="2024-01-15")
    payload = json.loads(s.as_json())
    assert isinstance(payload, dict)


# ── Migration v003 adds checkpoint column ─────────────────────────────────────


def test_migration_v003_adds_checkpoint_column():
    db = DatabaseManager(":memory:")
    db.apply_migrations()
    # INSERT a row and verify we can read the checkpoint column
    db.execute(
        "INSERT INTO daily_run (run_date, period_start, period_end, status, "
        "error_count, created_at) VALUES (?,?,?,?,?,?)",
        ("2024-01-15", "s", "e", "running", 0, "2024-01-15T07:00:00Z"),
    )
    row = db.query_one("SELECT checkpoint FROM daily_run WHERE run_date = '2024-01-15'")
    assert row is not None
    assert row["checkpoint"] is None  # default NULL
    db.close()


def test_migration_v003_idempotent():
    """Applying all migrations twice must not raise."""
    db = DatabaseManager(":memory:")
    db.apply_migrations()
    # v003 is already applied; applying again does nothing (version tracked)
    applied = db.apply_migrations()
    assert applied == []
    db.close()
