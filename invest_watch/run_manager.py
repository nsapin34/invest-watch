"""daily_run lifecycle management — sub-step 6.2.

Owns the CREATE → checkpoint → FINISH lifecycle of a ``daily_run`` row and
the ``data/last_run.status`` file that Windows Task Scheduler (and monitoring
scripts) can read after each run.

Responsibilities
----------------
* Open a ``daily_run`` row in status ``'running'`` at run start.
* Persist the current **checkpoint** (last completed step name) after each step
  so that ``--resume`` can skip already-done work (sub-step 6.4).
* Close the row with a final status (``completed / partial / failed /
  cancelled``) and a ``stats_json`` blob.
* Write ``data/last_run.status`` (one-line plain text) for the scheduler.

Step enum
---------
:class:`RunStep` lists the 15 workflow steps in execution order (specs §10).
Each step has a string id (its ``value``) that is stored in the ``checkpoint``
column so the DB is self-documenting.

Interaction with ArticleRepository
-----------------------------------
``RunManager`` wraps :class:`~invest_watch.article_repository.ArticleRepository`
instead of duplicating its SQL.  ``create_run`` / ``finish_run`` delegate to it;
``set_checkpoint`` does a targeted UPDATE that the repository doesn't expose.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from .article_repository import ArticleRepository, DailyRun
from .db_manager import DatabaseManager, utc_now_iso

logger = logging.getLogger(__name__)

_LAST_RUN_FILENAME = "last_run.status"

# ── Exit codes (specs §10 / §12.2) ───────────────────────────────────────────

EXIT_COMPLETED = 0   # all steps succeeded
EXIT_PARTIAL   = 1   # at least one recoverable error
EXIT_FAILED    = 2   # unrecoverable failure


# ── Workflow step definitions ─────────────────────────────────────────────────


class RunStep(str, Enum):
    """The 15 orchestrator steps in execution order (specs §10).

    Each value is the string stored in ``daily_run.checkpoint``.
    The ordering is meaningful: ``step_index()`` returns the 0-based position
    so the orchestrator can compare steps for --resume.
    """

    PERIOD      = "period"       # 1  compute D-1 interval
    LOAD_CONFIG = "load_config"  # 2  load companies / markets / sources
    QUERIES     = "queries"      # 3  generate search queries
    HARVEST     = "harvest"      # 4  query sources → raw candidates
    FILTER_DATE = "filter_date"  # 5  filter by date window
    FETCH       = "fetch"        # 6  fetcher + trafilatura
    CLEAN       = "clean"        # 7  boilerplate removal, hash
    DEDUP       = "dedup"        # 8  4-layer dedup vs DB
    CLASSIFY    = "classify"     # 9  language, categories, companies
    IMPORTANCE  = "importance"   # 10 importance scoring rules
    SUMMARISE   = "summarise"    # 11 LLM summaries (or degraded)
    SAVE        = "save"         # 12 idempotent upserts
    DIGESTS     = "digests"      # 13 company → market → global digests
    REPORT      = "report"       # 14 .md + .json report files
    WRAP_UP     = "wrap_up"      # 15 stats, 90-day purge, backup

    def step_index(self) -> int:
        """0-based position in the canonical step order."""
        return list(RunStep).index(self)


# ── RunStats dataclass ────────────────────────────────────────────────────────


@dataclass
class RunStats:
    """Mutable counter bag collected throughout the orchestrator run."""

    run_id: int
    run_date: str
    period_start: str = ""
    period_end: str = ""
    status: str = "running"

    # collection counters
    queries: int = 0
    harvested: int = 0
    fetched: int = 0
    stored: int = 0
    skipped_date: int = 0
    skipped_robots: int = 0
    fetch_errors: int = 0

    # processing counters
    deduped: int = 0
    classified: int = 0
    summarised: int = 0

    # error tracking
    error_count: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "queries": self.queries,
            "harvested": self.harvested,
            "fetched": self.fetched,
            "stored": self.stored,
            "skipped_date": self.skipped_date,
            "skipped_robots": self.skipped_robots,
            "fetch_errors": self.fetch_errors,
            "deduped": self.deduped,
            "classified": self.classified,
            "summarised": self.summarised,
            "error_count": self.error_count,
        }

    def as_json(self) -> str:
        return json.dumps(self.as_dict())

    def exit_code(self) -> int:
        if self.status == "completed":
            return EXIT_COMPLETED
        if self.status in ("partial", "cancelled"):
            return EXIT_PARTIAL
        return EXIT_FAILED


# ── RunManager ────────────────────────────────────────────────────────────────


class RunManager:
    """Owns the ``daily_run`` lifecycle for one orchestrator execution.

    Parameters
    ----------
    db:
        Open :class:`~invest_watch.db_manager.DatabaseManager`.
    data_dir:
        ``data/`` directory (parent of ``invest.db``), used to write
        ``last_run.status``.
    llm_enabled:
        Passed to :meth:`start` to set the initial ``llm_status`` in the row.
    """

    def __init__(
        self,
        db: DatabaseManager,
        data_dir: Path,
        *,
        llm_enabled: bool = False,
    ) -> None:
        self._repo = ArticleRepository(db)
        self._data_dir = Path(data_dir)
        self._llm_enabled = llm_enabled
        self._run: DailyRun | None = None

    # ── properties ────────────────────────────────────────────────────────

    @property
    def run(self) -> DailyRun:
        """The active ``daily_run`` row.  Raises ``RuntimeError`` before :meth:`start`."""
        if self._run is None:
            raise RuntimeError("RunManager.start() has not been called")
        return self._run

    @property
    def run_id(self) -> int:
        return self.run.id

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(
        self,
        run_date: str,
        period_start: str,
        period_end: str,
        *,
        trigger: str = "manual",
    ) -> DailyRun:
        """INSERT (or re-open) a ``daily_run`` row in status ``'running'``.

        If a row for *run_date* already exists (resume scenario) the existing
        row is updated to ``'running'`` again — the checkpoint column is **not**
        cleared so sub-step 6.4 can read it.

        Returns the :class:`~invest_watch.article_repository.DailyRun` dataclass.
        """
        llm_status = "ok" if self._llm_enabled else "off"
        self._run = self._repo.create_run(
            run_date,
            period_start,
            period_end,
            trigger=trigger,
            llm_status=llm_status,
        )
        logger.info(
            "RunManager: daily_run #%d opened  date=%s  trigger=%s",
            self._run.id,
            run_date,
            trigger,
        )
        return self._run

    def set_checkpoint(self, step: RunStep) -> None:
        """Persist *step* as the last completed step in the DB row.

        This is the only write that ``RunManager`` makes outside of
        :meth:`start` / :meth:`finish` — a targeted UPDATE so it is cheap
        enough to call after every step.
        """
        self._repo.db.execute(
            "UPDATE daily_run SET checkpoint = ? WHERE id = ?",
            (step.value, self.run_id),
        )
        logger.debug("checkpoint → %s (run #%d)", step.value, self.run_id)

    def get_checkpoint(self) -> RunStep | None:
        """Return the last persisted checkpoint for the current run, or ``None``."""
        row = self._repo.db.query_one(
            "SELECT checkpoint FROM daily_run WHERE id = ?", (self.run_id,)
        )
        if row is None or row["checkpoint"] is None:
            return None
        try:
            return RunStep(row["checkpoint"])
        except ValueError:
            logger.warning("unknown checkpoint value '%s' in daily_run #%d", row["checkpoint"], self.run_id)
            return None

    def finish(self, stats: RunStats) -> None:
        """UPDATE ``daily_run`` with final status + stats and write ``last_run.status``.

        Parameters
        ----------
        stats:
            The :class:`RunStats` collected during the run.  ``stats.status``
            must already be set to the final value before calling this method.
        """
        self._repo.finish_run(
            self.run_id,
            status=stats.status,
            error_count=stats.error_count,
            stats_json=stats.as_json(),
        )
        self._write_last_run_status(stats.status)
        logger.info(
            "RunManager: daily_run #%d finished  status=%s  errors=%d",
            self.run_id,
            stats.status,
            stats.error_count,
        )

    def load_existing(self, run_id: int) -> DailyRun:
        """Load an existing ``daily_run`` row by id (for ``--resume``).

        Raises :exc:`~invest_watch.exceptions.NotFoundError` if absent.
        """
        from .article_repository import require_run
        self._run = require_run(self._repo, run_id)
        return self._run

    # ── helpers ───────────────────────────────────────────────────────────

    def _write_last_run_status(self, status: str) -> None:
        """Write *status* to ``data/last_run.status`` (one-line plain text)."""
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            path = self._data_dir / _LAST_RUN_FILENAME
            path.write_text(status + "\n", encoding="utf-8")
        except OSError as exc:
            # Non-fatal: the DB already has the status; the file is convenience only.
            logger.warning("could not write last_run.status: %s", exc)


# ── module-level helpers ──────────────────────────────────────────────────────


def steps_to_run(checkpoint: RunStep | None) -> list[RunStep]:
    """Return the subset of :class:`RunStep` members not yet completed.

    When *checkpoint* is ``None`` (fresh run) all 15 steps are returned.
    On resume, steps whose index is <= *checkpoint*'s index are skipped.
    """
    all_steps = list(RunStep)
    if checkpoint is None:
        return all_steps
    cutoff = checkpoint.step_index()
    return [s for s in all_steps if s.step_index() > cutoff]
