"""Unit tests for stop_flag — sub-step 6.5.

Covers:
- write_stop_flag creates the flag file with content.
- check_and_clear_stop_flag returns False when absent.
- check_and_clear_stop_flag returns True and deletes the file when present.
- check_and_clear_stop_flag returns True even when unlink raises OSError.
- stop_flag_exists reflects presence / absence.
- write_stop_flag creates data_dir if absent.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from invest_watch.stop_flag import (
    check_and_clear_stop_flag,
    stop_flag_exists,
    write_stop_flag,
)

_FLAG = "stop.flag"


@pytest.fixture()
def data_dir(tmp_path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


# ── write_stop_flag ───────────────────────────────────────────────────────────


def test_write_creates_flag_file(data_dir):
    path = write_stop_flag(data_dir)
    assert path == data_dir / _FLAG
    assert path.exists()


def test_write_flag_has_content(data_dir):
    write_stop_flag(data_dir)
    content = (data_dir / _FLAG).read_text(encoding="utf-8")
    assert content.strip() == "stop"


def test_write_is_idempotent(data_dir):
    write_stop_flag(data_dir)
    write_stop_flag(data_dir)  # second call must not raise
    assert (data_dir / _FLAG).exists()


def test_write_creates_data_dir_if_absent(tmp_path):
    new_dir = tmp_path / "new_data"
    assert not new_dir.exists()
    write_stop_flag(new_dir)
    assert (new_dir / _FLAG).exists()


# ── check_and_clear_stop_flag ─────────────────────────────────────────────────


def test_check_returns_false_when_absent(data_dir):
    assert check_and_clear_stop_flag(data_dir) is False


def test_check_returns_true_when_present(data_dir):
    write_stop_flag(data_dir)
    assert check_and_clear_stop_flag(data_dir) is True


def test_check_deletes_flag_after_detection(data_dir):
    write_stop_flag(data_dir)
    check_and_clear_stop_flag(data_dir)
    assert not (data_dir / _FLAG).exists()


def test_check_second_call_returns_false_after_clear(data_dir):
    write_stop_flag(data_dir)
    check_and_clear_stop_flag(data_dir)
    assert check_and_clear_stop_flag(data_dir) is False


def test_check_returns_true_even_if_unlink_raises(data_dir):
    """An OSError on unlink must not prevent the cancellation from being signalled."""
    write_stop_flag(data_dir)
    with patch("invest_watch.stop_flag.Path.unlink", side_effect=OSError("locked")):
        result = check_and_clear_stop_flag(data_dir)
    assert result is True  # still signals cancellation


# ── stop_flag_exists ──────────────────────────────────────────────────────────


def test_exists_false_when_absent(data_dir):
    assert not stop_flag_exists(data_dir)


def test_exists_true_after_write(data_dir):
    write_stop_flag(data_dir)
    assert stop_flag_exists(data_dir)


def test_exists_false_after_check_and_clear(data_dir):
    write_stop_flag(data_dir)
    check_and_clear_stop_flag(data_dir)
    assert not stop_flag_exists(data_dir)
