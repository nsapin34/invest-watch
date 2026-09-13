"""Lock management for invest-watch — sub-step 6.1.

Implements the ``data/run.lock`` anti-double-execution mechanism from
specifications §10 (preamble) and §12.2.

Contract
--------
* ``acquire_lock(data_dir)`` creates the lock file atomically.
  - If the file already exists **and is less than 2 hours old**, raises
    :exc:`LockAlreadyAcquiredError` — the caller (CLI) should exit with
    code 0 (not an error, just a concurrent run guard).
  - If the file is stale (≥ 2 h) or ``force=True``, it is removed and
    re-created normally.
* ``release_lock(data_dir)`` deletes the lock file (idempotent — no error
  if already absent).
* ``read_lock_info(data_dir)`` returns ``(pid, timestamp)`` for diagnostics.

The lock file format is plain text::

    <PID>\\n<ISO-8601 UTC timestamp>\\n

This keeps it human-readable and parseable by external monitoring scripts.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .exceptions import LockAlreadyAcquiredError

# Maximum age of a lock that is considered "live" (seconds = 2 h).
_LOCK_MAX_AGE_SECONDS: int = 2 * 60 * 60
_LOCK_FILENAME = "run.lock"


# ── public API ────────────────────────────────────────────────────────────────


def acquire_lock(data_dir: Path, *, force: bool = False) -> Path:
    """Create ``data/run.lock`` exclusively.

    Parameters
    ----------
    data_dir:
        The ``data/`` directory configured in ``[storage].db_path``'s parent
        (typically ``cfg.storage.db_path.parent``).  Created if absent.
    force:
        When ``True`` any existing lock (even a fresh one) is removed and
        re-created — escape hatch for ``invest-watch run --force``.

    Returns
    -------
    Path
        The path of the created lock file.

    Raises
    ------
    LockAlreadyAcquiredError
        When the lock file exists and is younger than 2 hours (and
        ``force=False``).  The caller should exit with code 0.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / _LOCK_FILENAME

    if lock_path.exists():
        if force:
            lock_path.unlink(missing_ok=True)
        else:
            age = _lock_age_seconds(lock_path)
            if age < _LOCK_MAX_AGE_SECONDS:
                pid, ts = _parse_lock(lock_path)
                raise LockAlreadyAcquiredError(
                    f"invest-watch is already running (lock age {int(age)}s, "
                    f"pid={pid}, started={ts})"
                )
            # Stale lock — safe to remove.
            lock_path.unlink(missing_ok=True)

    _write_lock(lock_path)
    return lock_path


def release_lock(data_dir: Path) -> None:
    """Delete ``data/run.lock`` (idempotent — no error if already absent).

    Parameters
    ----------
    data_dir:
        Same ``data/`` directory passed to :func:`acquire_lock`.
    """
    lock_path = Path(data_dir) / _LOCK_FILENAME
    lock_path.unlink(missing_ok=True)


def read_lock_info(data_dir: Path) -> tuple[int | None, str | None]:
    """Return ``(pid, iso_timestamp)`` from the lock file, or ``(None, None)``.

    Useful for ``invest-watch status`` and error messages.
    """
    lock_path = Path(data_dir) / _LOCK_FILENAME
    if not lock_path.exists():
        return None, None
    return _parse_lock(lock_path)


def lock_exists(data_dir: Path) -> bool:
    """Return ``True`` if a lock file is present (regardless of age)."""
    return (Path(data_dir) / _LOCK_FILENAME).exists()


# ── internal helpers ──────────────────────────────────────────────────────────


def _write_lock(lock_path: Path) -> None:
    """Write PID + UTC timestamp to *lock_path* (exclusive create on POSIX,
    normal write on Windows where O_EXCL is less reliable for files)."""
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    content = f"{os.getpid()}\n{now_iso}\n"
    # On POSIX, use O_CREAT|O_EXCL for true atomicity; on Windows the plain
    # write is sufficient because Task Scheduler never runs two instances.
    try:
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, content.encode())
        os.close(fd)
    except FileExistsError:
        # Race condition: another process won — treat as live lock.
        raise LockAlreadyAcquiredError("lock file appeared between check and create")


def _parse_lock(lock_path: Path) -> tuple[int | None, str | None]:
    """Return ``(pid, iso_timestamp)`` parsed from *lock_path*."""
    try:
        lines = lock_path.read_text(encoding="utf-8").splitlines()
        pid = int(lines[0]) if lines else None
        ts = lines[1] if len(lines) > 1 else None
        return pid, ts
    except (OSError, ValueError):
        return None, None


def _lock_age_seconds(lock_path: Path) -> float:
    """Return the age of *lock_path* in seconds (based on mtime)."""
    try:
        return time.time() - lock_path.stat().st_mtime
    except OSError:
        # File disappeared between the exists() check and stat() — treat as gone.
        return float("inf")
