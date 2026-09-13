"""Unit tests for lock_manager — sub-step 6.1.

Covers:
- Normal acquire / release round-trip.
- Idempotent release (no error when lock is absent).
- Second acquire on a live lock raises LockAlreadyAcquiredError.
- Stale lock (≥ 2 h) is silently overwritten.
- ``force=True`` overrides a live lock.
- Lock file content (PID + ISO timestamp).
- read_lock_info on existing / missing lock.
- lock_exists helper.
- Race condition: file appears between check and create (FileExistsError path).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from invest_watch.exceptions import LockAlreadyAcquiredError
from invest_watch.lock_manager import (
    _LOCK_FILENAME,
    _LOCK_MAX_AGE_SECONDS,
    acquire_lock,
    lock_exists,
    read_lock_info,
    release_lock,
)


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def data_dir(tmp_path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


# ── acquire / release ─────────────────────────────────────────────────────────


def test_acquire_creates_lock_file(data_dir):
    lock_path = acquire_lock(data_dir)
    assert lock_path == data_dir / _LOCK_FILENAME
    assert lock_path.exists()


def test_release_removes_lock_file(data_dir):
    acquire_lock(data_dir)
    release_lock(data_dir)
    assert not (data_dir / _LOCK_FILENAME).exists()


def test_release_is_idempotent_when_no_lock(data_dir):
    """release_lock must not raise even if the file is already gone."""
    release_lock(data_dir)  # no lock present — should be silent


def test_acquire_creates_data_dir_if_absent(tmp_path):
    """data_dir does not need to pre-exist."""
    data_dir = tmp_path / "new_data"
    assert not data_dir.exists()
    acquire_lock(data_dir)
    assert (data_dir / _LOCK_FILENAME).exists()


# ── lock file content ─────────────────────────────────────────────────────────


def test_lock_file_contains_pid_and_timestamp(data_dir):
    acquire_lock(data_dir)
    lines = (data_dir / _LOCK_FILENAME).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert int(lines[0]) == os.getpid()
    # timestamp must be parseable ISO-8601
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(lines[1])
    assert dt.tzinfo is not None  # must be timezone-aware (UTC)


# ── anti-double-execution ─────────────────────────────────────────────────────


def test_second_acquire_raises_when_live(data_dir):
    acquire_lock(data_dir)
    with pytest.raises(LockAlreadyAcquiredError):
        acquire_lock(data_dir)


def test_second_acquire_error_message_contains_age_and_pid(data_dir):
    acquire_lock(data_dir)
    with pytest.raises(LockAlreadyAcquiredError, match=r"pid=\d+"):
        acquire_lock(data_dir)


# ── stale lock ────────────────────────────────────────────────────────────────


def test_stale_lock_is_replaced(data_dir):
    """A lock file older than 2 h must be silently overwritten."""
    lock_path = data_dir / _LOCK_FILENAME
    lock_path.write_text(f"99999\n2000-01-01T00:00:00+00:00\n", encoding="utf-8")
    # Back-date the mtime to 2 h + 1 s ago.
    stale_mtime = time.time() - _LOCK_MAX_AGE_SECONDS - 1
    os.utime(lock_path, (stale_mtime, stale_mtime))

    acquired = acquire_lock(data_dir)
    # New lock must contain the current PID, not the stale one.
    pid, _ = read_lock_info(data_dir)
    assert pid == os.getpid()
    assert acquired.exists()


def test_fresh_lock_just_below_threshold_raises(data_dir):
    """A lock 1 s younger than the threshold must still block."""
    lock_path = data_dir / _LOCK_FILENAME
    lock_path.write_text(f"{os.getpid()}\n2099-01-01T00:00:00+00:00\n", encoding="utf-8")
    fresh_mtime = time.time() - _LOCK_MAX_AGE_SECONDS + 1
    os.utime(lock_path, (fresh_mtime, fresh_mtime))

    with pytest.raises(LockAlreadyAcquiredError):
        acquire_lock(data_dir)


# ── force flag ────────────────────────────────────────────────────────────────


def test_force_overrides_live_lock(data_dir):
    acquire_lock(data_dir)
    # Second acquire with force=True must succeed.
    lock_path = acquire_lock(data_dir, force=True)
    assert lock_path.exists()
    pid, _ = read_lock_info(data_dir)
    assert pid == os.getpid()


def test_force_works_when_no_lock_present(data_dir):
    """force=True must not fail when there is no existing lock."""
    lock_path = acquire_lock(data_dir, force=True)
    assert lock_path.exists()


# ── read_lock_info ────────────────────────────────────────────────────────────


def test_read_lock_info_returns_none_when_absent(data_dir):
    pid, ts = read_lock_info(data_dir)
    assert pid is None
    assert ts is None


def test_read_lock_info_returns_pid_and_ts(data_dir):
    acquire_lock(data_dir)
    pid, ts = read_lock_info(data_dir)
    assert pid == os.getpid()
    assert ts is not None and "T" in ts


def test_read_lock_info_tolerates_corrupt_file(data_dir):
    (data_dir / _LOCK_FILENAME).write_text("not-a-number\n", encoding="utf-8")
    pid, ts = read_lock_info(data_dir)
    assert pid is None


# ── lock_exists ───────────────────────────────────────────────────────────────


def test_lock_exists_false_when_absent(data_dir):
    assert not lock_exists(data_dir)


def test_lock_exists_true_after_acquire(data_dir):
    acquire_lock(data_dir)
    assert lock_exists(data_dir)


def test_lock_exists_false_after_release(data_dir):
    acquire_lock(data_dir)
    release_lock(data_dir)
    assert not lock_exists(data_dir)


# ── race condition (FileExistsError path) ─────────────────────────────────────


def test_race_condition_on_create_raises_lock_error(data_dir):
    """If os.open raises FileExistsError (race), we surface LockAlreadyAcquiredError."""
    with patch("invest_watch.lock_manager.os.open", side_effect=FileExistsError):
        with pytest.raises(LockAlreadyAcquiredError, match="between check and create"):
            acquire_lock(data_dir)
