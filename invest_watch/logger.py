"""Application logging: UTC timestamps, rotating global file + per-run file.

Files (see PROJECT_STRUCTURE):
- ``logs/invest.log``          rotating global log (max_bytes, backup_count)
- ``logs/run-YYYY-MM-DD.log``  one file per execution

Console output goes to stderr and stays concise.
"""

from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "invest_watch"

_FILE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_CONSOLE_FORMAT = "%(levelname)-8s | %(message)s"


class _UTCFormatter(logging.Formatter):
    """Formatter rendering timestamps in UTC (suffix ``Z``)."""

    converter = time.gmtime

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:  # noqa: N802
        return super().formatTime(record, datefmt or "%Y-%m-%d %H:%M:%SZ")


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger nested under the ``invest_watch`` root logger."""
    if name:
        return logging.getLogger(f"{LOGGER_NAME}.{name}")
    return logging.getLogger(LOGGER_NAME)


def run_log_path(logs_dir: Path | str, run_date: str) -> Path:
    """Return the per-run log file path ``logs/run-YYYY-MM-DD.log``."""
    return Path(logs_dir) / f"run-{run_date}.log"


def setup_logging(
    logs_dir: Path | str,
    *,
    level: str = "INFO",
    max_bytes: int = 5_000_000,
    backup_count: int = 5,
    run_date: str | None = None,
    console: bool = True,
) -> logging.Logger:
    """(Re)configure the ``invest_watch`` logger (idempotent).

    Existing handlers are closed and replaced, so calling twice never
    duplicates output.
    """
    logs_dir = Path(logs_dir)
    logs_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger(LOGGER_NAME)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    root.setLevel(logging.DEBUG)
    root.propagate = False

    file_handler = RotatingFileHandler(
        logs_dir / "invest.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_UTCFormatter(_FILE_FORMAT))
    root.addHandler(file_handler)

    if run_date:
        run_handler = RotatingFileHandler(
            run_log_path(logs_dir, run_date),
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        run_handler.setLevel(logging.DEBUG)
        run_handler.setFormatter(_UTCFormatter(_FILE_FORMAT))
        root.addHandler(run_handler)

    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(getattr(logging, str(level).upper(), logging.INFO))
        console_handler.setFormatter(_UTCFormatter(_CONSOLE_FORMAT))
        root.addHandler(console_handler)

    return root


def attach_run_handler(logs_dir: Path | str, run_date: str, *, max_bytes: int = 5_000_000) -> Path:
    """Attach the per-run file handler to an already-configured logger.

    Used by the orchestrator (Step 6) once the run-specific file should be
    written. Returns the created log file path.
    """
    path = run_log_path(Path(logs_dir), run_date)
    handler = RotatingFileHandler(path, maxBytes=max_bytes, backupCount=1, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(_UTCFormatter(_FILE_FORMAT))
    logging.getLogger(LOGGER_NAME).addHandler(handler)
    return path