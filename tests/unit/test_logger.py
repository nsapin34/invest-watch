"""Unit tests for logger: file handlers, UTF-8, UTC format, idempotency."""

import logging

import pytest

from invest_watch.logger import (
    LOGGER_NAME,
    get_logger,
    run_log_path,
    setup_logging,
)


def _close_loggers():
    root = logging.getLogger(LOGGER_NAME)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


@pytest.fixture(autouse=True)
def _teardown_logging():
    yield
    _close_loggers()


def test_setup_creates_global_log_and_handlers(tmp_path, capsys):
    logger = setup_logging(logs_dir=tmp_path, level="WARNING", max_bytes=1024, backup_count=2)
    assert logger.name == LOGGER_NAME
    assert (tmp_path / "invest.log").exists()
    assert len([h for h in logger.handlers if getattr(h, "baseFilename", None)]) == 1

    # The file handler records everything (DEBUG); the console filters at level.
    logger.info("hidden")
    logger.warning("visible")
    content = (tmp_path / "invest.log").read_text(encoding="utf-8")
    assert "visible" in content
    assert "hidden" in content
    console = capsys.readouterr().err
    assert "visible" in console
    assert "hidden" not in console


def test_per_run_log_handler(tmp_path):
    logger = setup_logging(logs_dir=tmp_path, run_date="2026-09-01")
    path = run_log_path(tmp_path, "2026-09-01")
    assert path == tmp_path / "run-2026-09-01.log"
    assert path.exists()

    logger.info("per-run message")
    assert "per-run message" in path.read_text(encoding="utf-8")
    # both files (global + per-run) hold the message
    assert "per-run message" in (tmp_path / "invest.log").read_text(encoding="utf-8")


def test_setup_is_idempotent(tmp_path):
    setup_logging(logs_dir=tmp_path)
    first_handlers = set(logging.getLogger(LOGGER_NAME).handlers)
    setup_logging(logs_dir=tmp_path)
    second_handlers = set(logging.getLogger(LOGGER_NAME).handlers)
    assert len(first_handlers) == len(second_handlers) == 2  # file + console


def test_get_logger_nesting():
    child = get_logger("orchestrator")
    assert child.name == "invest_watch.orchestrator"
    assert get_logger().name == LOGGER_NAME


def test_utc_timestamps_render_z_suffix(tmp_path):
    logger = setup_logging(logs_dir=tmp_path, console=False)
    logger.info("hello")
    content = (tmp_path / "invest.log").read_text(encoding="utf-8")
    assert "Z |" in content  # UTC marker in the first field


def test_unicode_content_roundtrip(tmp_path):
    logger = setup_logging(logs_dir=tmp_path, console=False)
    logger.info("比亚迪 – Chinese aliases")
    content = (tmp_path / "invest.log").read_text(encoding="utf-8")
    assert "比亚迪" in content