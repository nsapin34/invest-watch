"""Clean-shutdown flag — sub-step 6.5.

Implements the ``data/stop.flag`` mechanism from specifications §12.2:

    ``invest-watch stop`` writes ``data/stop.flag``.
    The orchestrator checks the flag between every step and exits as
    ``cancelled`` when it is present, then deletes the flag so a subsequent
    ``invest-watch run`` starts normally.

Public API
----------
``write_stop_flag(data_dir)``
    Create ``data/stop.flag`` (idempotent — no error if already present).

``check_and_clear_stop_flag(data_dir) -> bool``
    Return ``True`` **and delete the flag** if it exists, ``False`` otherwise.
    The deletion is best-effort: an ``OSError`` on removal is logged but does
    not prevent the caller from honouring the cancellation.

``stop_flag_exists(data_dir) -> bool``
    Non-destructive existence test (for diagnostics / ``invest-watch status``).
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_STOP_FLAG_FILENAME = "stop.flag"


def write_stop_flag(data_dir: Path) -> Path:
    """Write ``data/stop.flag`` to request a clean shutdown.

    Parameters
    ----------
    data_dir:
        The ``data/`` directory (parent of ``invest.db``).
        Created if absent.

    Returns
    -------
    Path
        The path of the written flag file.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    flag_path = data_dir / _STOP_FLAG_FILENAME
    flag_path.write_text("stop\n", encoding="utf-8")
    logger.info("stop flag written: %s", flag_path)
    return flag_path


def check_and_clear_stop_flag(data_dir: Path) -> bool:
    """Return ``True`` and delete ``data/stop.flag`` if it exists.

    Called by the orchestrator between each of the 15 steps.  When ``True``
    is returned the orchestrator must set status to ``cancelled`` and exit.

    The deletion is **best-effort**: if ``unlink`` raises an ``OSError`` the
    flag is left on disk (the next check will trigger again) but the
    cancellation proceeds regardless.

    Parameters
    ----------
    data_dir:
        The ``data/`` directory passed to ``acquire_lock``.

    Returns
    -------
    bool
        ``True`` if the flag was present (cancellation requested).
    """
    flag_path = Path(data_dir) / _STOP_FLAG_FILENAME
    if not flag_path.exists():
        return False
    try:
        flag_path.unlink(missing_ok=True)
        logger.info("stop flag detected and cleared — cancelling run")
    except OSError as exc:
        logger.warning("stop flag: could not delete %s — %s", flag_path, exc)
    return True


def stop_flag_exists(data_dir: Path) -> bool:
    """Non-destructive test — ``True`` if ``data/stop.flag`` is present."""
    return (Path(data_dir) / _STOP_FLAG_FILENAME).exists()
