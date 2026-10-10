"""Orchestrator — sub-steps 6.3 + 6.4 + 6.5: 15-step workflow, resume, shutdown.

Implements the complete ``invest-watch run`` pipeline described in
specifications §10 and architecture §8 Step 6.

Architecture
------------
The orchestrator is intentionally thin: it drives the 15 steps in order,
persists the checkpoint after each one, isolates per-source / per-step
failures without crashing the run, and delegates all domain logic to the
modules built in Steps 1–5.

Step map (specs §10)
---------------------
1.  PERIOD      — compute D-1 interval in configured timezone
2.  LOAD_CONFIG — load companies, markets, sources from DB
3.  QUERIES     — generate deduplicated search query queue
4.  HARVEST     — RSS cascade → Google News → engines → raw candidates
5.  FILTER_DATE — keep only candidates inside the date window
6.  FETCH       — HTTP fetch + trafilatura content extraction
7.  CLEAN       — boilerplate removal, normalisation, SHA-256 hash
8.  DEDUP       — 4-layer dedup vs DB + intra-run event clustering
9.  CLASSIFY    — language, categories, companies/markets, scores
10. IMPORTANCE  — importance_level scoring rules
11. SUMMARISE   — LLM article summaries (or degraded)
12. SAVE        — idempotent upserts already performed by steps 6–11
13. DIGESTS     — company → market → global digests (LLM or degraded)
14. REPORT      — write reports/YYYY-MM-DD.md + .json
15. WRAP_UP     — stats, 90-day purge, daily VACUUM INTO backup

Failure handling
----------------
* Each step that can fail partially (HARVEST, FETCH, SUMMARISE, DIGESTS)
  catches per-item exceptions, accumulates them in ``RunStats.errors``, and
  marks the run ``partial`` — the run never crashes on a single bad source.
* Every caught exception is also written to ``processing_error`` (step=step
  name, status='new') so ``invest-watch errors`` can surface it and the next
  ``--resume`` can retry it.
* Truly unrecoverable errors (PERIOD, LOAD_CONFIG) re-raise after setting the
  run status to ``failed``.
* On any uncaught exception the ``finally`` block closes the run as ``failed``
  and releases the lock.

Resume (sub-step 6.4)
---------------------
* ``steps_to_run(checkpoint)`` skips steps already completed.
* Step 11 (SUMMARISE) is additionally filtered to only articles that have no
  existing ``summary`` row — so a resume never re-calls the LLM for content
  already summarised.
* ``processing_error`` rows in status ``'new'`` or ``'retrying'`` (up to 3
  attempts) are retried on the next matching step.

Clean shutdown (sub-step 6.5)
------------------------------
* ``data/stop.flag`` is checked between every step.  When present: flag is
  deleted, run status is set to ``cancelled``, lock is released, exit code 1.
* ``KeyboardInterrupt`` (Ctrl-C) is caught in the outer ``run()`` guard: any
  open SQLite transaction is rolled back, the run is closed as ``cancelled``,
  the lock is released, and the process exits with code 1.
"""

from __future__ import annotations

import logging
import traceback
from datetime import date as _date
from pathlib import Path
from typing import Any

from .article_repository import ArticleRepository
from .bootstrap import seed_from_config
from .classifier import Classifier
from .collector import run_collection
from .config_manager import AppConfig
from .date_detector import compute_period
from .db_manager import DatabaseManager, utc_now_iso
from .dedup import Deduplicator
from .doctor import (
    disabled_engines as preflight_disabled_engines,
    run_preflight,
    watsonx_invalid,
)
from .exceptions import LockAlreadyAcquiredError
from .fetcher import Fetcher
from .lock_manager import acquire_lock, release_lock
from .source_repository import SourceRepository
from .stop_flag import check_and_clear_stop_flag
from .report_generator import ReportConfig, ReportGenerator
from .run_manager import (
    EXIT_COMPLETED,
    EXIT_FAILED,
    EXIT_PARTIAL,
    RunManager,
    RunStats,
    RunStep,
    steps_to_run,
)
from .summarizer import (
    ArticleSummarizer,
    DigestSummarizer,
    summarizer_config_from_section,
)

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────


def run(
    cfg: AppConfig,
    db: DatabaseManager,
    *,
    run_date: str | _date | None = None,
    resume_run_id: int | None = None,
    force: bool = False,
    trigger: str = "manual",
    retry_degraded: bool = False,
    # Injectable test seams — None means "use the real implementation"
    fetcher: Fetcher | None = None,
    llm_client: Any | None = None,
) -> int:
    """Execute the 15-step daily workflow and return an exit code.

    Parameters
    ----------
    cfg:
        Loaded application configuration.
    db:
        Open database connection (migrations already applied).
    run_date:
        ISO date string or ``date`` object for the run.  Defaults to today;
        the collection window is D-1 relative to this date.
    resume_run_id:
        If set, resume a previously interrupted run (sub-step 6.4).
        Steps already completed (checkpoint) are skipped.
    force:
        When True, remove a stale/live ``run.lock`` and proceed anyway.
    trigger:
        ``scheduler`` | ``manual`` | ``retry`` — stored in ``daily_run.trigger``
        (P1 item 9) so the operator can tell a scheduled run from a manual one.
    retry_degraded:
        With ``resume_run_id``: re-run SUMMARISE for articles whose summary has
        ``llm_used=0`` (degraded), regenerate digests + report, and restore an
        honest ``llm_status`` (P1 item 11).  The run row's trigger becomes
        ``'retry'``.
    fetcher:
        Injected for tests; ``None`` creates a real :class:`~.fetcher.Fetcher`.
    llm_client:
        Injected for tests; ``None`` creates a real
        :class:`~.llm_client.WatsonxLLMClient` (or off-mode if disabled).

    Returns
    -------
    int
        ``0`` completed, ``1`` partial / cancelled, ``2`` failed.
    """
    data_dir = cfg.storage.db_path.parent

    # ── 0. Lock ───────────────────────────────────────────────────────────
    try:
        acquire_lock(data_dir, force=force)
    except LockAlreadyAcquiredError as exc:
        logger.info("invest-watch already running — exiting: %s", exc)
        return EXIT_COMPLETED  # not an error; exit 0 per spec §10

    run_mgr = RunManager(db, data_dir, llm_enabled=cfg.llm.enabled)

    # Surface misconfiguration before a run burns its query budget on engines
    # that will silently degrade (ENF-01): active API/search sources whose key
    # is missing or still a placeholder.
    _warn_on_missing_api_keys(cfg)

    try:
        return _execute(
            cfg, db, run_mgr, data_dir,
            run_date=run_date,
            resume_run_id=resume_run_id,
            trigger=trigger,
            retry_degraded=retry_degraded,
            fetcher=fetcher,
            llm_client=llm_client,
        )
    except KeyboardInterrupt:
        # Ctrl-C: rollback any open transaction, close run as cancelled.
        logger.warning("orchestrator: Ctrl-C received — cancelling run")
        try:
            db.connection.execute("ROLLBACK")
        except Exception:
            pass
        _finish_cancelled(run_mgr)
        return EXIT_PARTIAL
    except Exception as exc:
        logger.exception("orchestrator: unhandled exception — %s", exc)
        _finish_cancelled(run_mgr, status="failed")
        return EXIT_FAILED
    finally:
        release_lock(data_dir)


# ─────────────────────────────────────────────────────────────────────────────
# Internal orchestration
# ─────────────────────────────────────────────────────────────────────────────


def _execute(
    cfg: AppConfig,
    db: DatabaseManager,
    run_mgr: RunManager,
    data_dir: Path,
    *,
    run_date: str | _date | None,
    resume_run_id: int | None,
    trigger: str,
    retry_degraded: bool,
    fetcher: Fetcher | None,
    llm_client: Any | None,
) -> int:
    """Drive the 15 steps; called inside the lock/finally guard of :func:`run`."""

    # ── Step 1: PERIOD ────────────────────────────────────────────────────
    period = compute_period(
        cfg.app.timezone,
        run_date=run_date,
        tolerance_hours=cfg.collect.period_tolerance_hours,
    )
    run_date_str = period.run_date.isoformat()

    # ── Open (or reopen) the daily_run row ────────────────────────────────
    if resume_run_id is not None:
        daily_run = run_mgr.load_existing(resume_run_id)
        run_date_str = daily_run.run_date
        # The resumed run's date may differ from today: recompute the D-1
        # window from the ROW's date (the collector no longer computes it).
        period = compute_period(
            cfg.app.timezone,
            run_date=run_date_str,
            tolerance_hours=cfg.collect.period_tolerance_hours,
        )
        checkpoint = run_mgr.get_checkpoint()
        logger.info(
            "orchestrator: resuming run #%d (checkpoint=%s)",
            resume_run_id,
            checkpoint.value if checkpoint else "none",
        )
        if retry_degraded:
            # A resume with --retry-degraded IS a retry: tag the row so
            # scheduler/manual/retry stay distinguishable (P1 items 9+11).
            run_mgr.set_trigger("retry")
    else:
        # Seed config entities (companies, sources) before opening the run.
        seed_from_config(db, cfg)
        daily_run = run_mgr.start(
            run_date_str,
            period.start_iso,
            period.end_iso,
            trigger=trigger,
        )
        checkpoint = None

    # ── Pre-flight doctor (P1 item 8): ping watsonx / Finnhub / Brave ─────
    # Best-effort and bounded (3 HTTP calls): an invalid key must produce a
    # clean "clé invalide" skip, not one raw 403 per article/query.
    preflight_bad_engines: frozenset[str] = frozenset()
    watsonx_key_invalid = False
    try:
        pings = run_preflight(cfg, db=db)
        for p in pings:
            (logger.warning if not p.ok else logger.info)(
                "doctor %s: %s — %s", p.service, p.status, p.message
            )
        preflight_bad_engines = preflight_disabled_engines(pings)
        watsonx_key_invalid = watsonx_invalid(pings)
    except Exception as exc:  # non-fatal — never block a run on the doctor
        logger.warning("orchestrator: pre-flight skipped — %s", exc)

    todo = steps_to_run(checkpoint)
    if retry_degraded and resume_run_id is not None:
        # Force the LLM pipeline back on even if the checkpoint already passed
        # it: the whole point is to re-summarise the degraded articles, then
        # rebuild digests and the report from them (P1 item 11).
        forced = {RunStep.SUMMARISE, RunStep.DIGESTS, RunStep.REPORT}
        todo = sorted(set(todo) | forced, key=lambda s: s.step_index())

    stats = RunStats(
        run_id=run_mgr.run_id,
        run_date=run_date_str,
        period_start=period.start_iso,
        period_end=period.end_iso,
        status="running",
    )

    # Steps 1+2 are implicit (done above); mark them if they're in todo.
    if RunStep.PERIOD in todo:
        run_mgr.set_checkpoint(RunStep.PERIOD)
    if RunStep.LOAD_CONFIG in todo:
        run_mgr.set_checkpoint(RunStep.LOAD_CONFIG)

    if check_and_clear_stop_flag(data_dir):
        return _cancel(run_mgr, stats)

    # ── Steps 3–7: collection (HARVEST → CLEAN) ────────────────────────────
    # We reuse `run_collection` from collector.py which already handles steps
    # 3-7 atomically.  On resume these steps are re-executed only when
    # HARVEST or later steps are still in todo (idempotent article inserts).
    if RunStep.HARVEST in todo:
        own_fetcher = fetcher is None
        effective_fetcher = fetcher or Fetcher(
            cfg.collect.fetch_user_agent,
            max_response_bytes=cfg.collect.max_response_bytes,
            respect_robots=cfg.collect.respect_robots,
            rate_limit_requests_per_sec=cfg.collect.rate_limit_requests_per_sec,
        )
        try:
            coll_stats = run_collection(
                cfg,
                db,
                run_id=run_mgr.run_id,
                period=period,
                fetcher=effective_fetcher,
                disabled_engines=preflight_bad_engines,
            )
            stats.queries        = coll_stats.queries
            stats.harvested      = coll_stats.harvested
            stats.fetched        = coll_stats.fetched
            stats.stored         = coll_stats.stored
            stats.skipped_date   = coll_stats.skipped_date
            stats.skipped_robots = coll_stats.skipped_robots
            stats.skipped_junk   = coll_stats.skipped_junk
            stats.fetch_errors   = coll_stats.fetch_errors
            stats.errors_persisted = coll_stats.errors_persisted
            stats.errors_dropped   = coll_stats.errors_dropped
            if coll_stats.errors:
                # P1 item 7 — CollectionStats.errors merge into RunStats.errors:
                # the details (URL, host, type) survive inside stats_json.
                stats.errors.extend(coll_stats.errors)
                stats.error_count += len(coll_stats.errors)
                stats.status = "partial"
        except Exception as exc:
            logger.exception("orchestrator: collection failed — %s", exc)
            stats.errors.append(f"collection: {exc}")
            stats.error_count += 1
            stats.status = "partial"
            # P1 item 7 — a crashed collection step must ALSO leave a
            # processing_error row (previously only logged → `invest-watch
            # errors` showed nothing).
            _record_error(db, run_mgr.run_id, RunStep.HARVEST, "run_collection", exc)
        finally:
            if own_fetcher:
                effective_fetcher.close()

        for step in (RunStep.QUERIES, RunStep.HARVEST, RunStep.FILTER_DATE,
                     RunStep.FETCH, RunStep.CLEAN):
            run_mgr.set_checkpoint(step)

    if check_and_clear_stop_flag(data_dir):
        return _cancel(run_mgr, stats)

    # ── Step 8: DEDUP ──────────────────────────────────────────────────────
    if RunStep.DEDUP in todo:
        try:
            deduplicator = Deduplicator(db, cfg.dedup)
            results = deduplicator.deduplicate_run(run_mgr.run_id)
            stats.deduped = len(results)
        except Exception as exc:
            logger.error("orchestrator: dedup failed — %s", exc)
            stats.errors.append(f"dedup: {exc}")
            stats.error_count += 1
            stats.status = "partial"
            _record_error(db, run_mgr.run_id, RunStep.DEDUP, "deduplicate_run", exc)
        run_mgr.set_checkpoint(RunStep.DEDUP)

    if check_and_clear_stop_flag(data_dir):
        return _cancel(run_mgr, stats)

    # ── Steps 9+10: CLASSIFY + IMPORTANCE ─────────────────────────────────
    if RunStep.CLASSIFY in todo:
        try:
            classifier = Classifier(db, cfg.scoring)
            results = classifier.classify_run(run_mgr.run_id)
            stats.classified = len(results)
        except Exception as exc:
            logger.error("orchestrator: classify failed — %s", exc)
            stats.errors.append(f"classify: {exc}")
            stats.error_count += 1
            stats.status = "partial"
            _record_error(db, run_mgr.run_id, RunStep.CLASSIFY, "classify_run", exc)
        run_mgr.set_checkpoint(RunStep.CLASSIFY)

    if RunStep.IMPORTANCE in todo:
        run_mgr.set_checkpoint(RunStep.IMPORTANCE)  # performed inline by classifier

    if check_and_clear_stop_flag(data_dir):
        return _cancel(run_mgr, stats)

    # Shared LLM client (steps 11 + 13): built at most once per run and shared
    # by SUMMARISE and DIGESTS so the per-run content-hash cache and the
    # circuit-breaker state are common to both steps (run #2 of 2026-09-15
    # built two independent clients, losing cache and breaker continuity).
    shared_llm: Any | None = None

    def _get_shared_llm() -> Any:
        nonlocal shared_llm
        if shared_llm is None:
            if watsonx_key_invalid:
                # P1 item 8 — pre-flight proved the key invalid (IAM 401/403):
                # degrade immediately with an honest reason instead of burning
                # one raw 403 per article.
                from .llm_client import DisabledLLMClient
                shared_llm = DisabledLLMClient(
                    "clé watsonx invalide (pré-vol: 401/403) — corrigez WATSONX_API_KEY dans .env"
                )
            else:
                shared_llm = _build_llm_client(cfg, llm_client)
                reset = getattr(shared_llm, "reset_circuit_breaker", None)
                if callable(reset):
                    reset()  # spec §8.5: breaker/counter resets at each run start
        return shared_llm

    # ── Step 11: SUMMARISE ─────────────────────────────────────────────────
    # On resume: only articles that have no existing summary row are processed
    # (idempotent — the LLM is never called twice for the same article/run).
    # With retry_degraded (P1 item 11): articles whose summary has llm_used=0
    # are re-processed so a fixed key / recovered service repairs the run.
    if RunStep.SUMMARISE in todo:
        if ArticleRepository(db).count_for_run(run_mgr.run_id) == 0:
            # Nothing stored → a paid LLM call would only re-float an empty run.
            logger.info(
                "orchestrator: 0 articles in run #%d — skipping LLM summarisation",
                run_mgr.run_id,
            )
            run_mgr.set_checkpoint(RunStep.SUMMARISE)
        else:
            try:
                if retry_degraded:
                    # Previous summarise/digest failures are being re-attempted:
                    # resolve the stale rows so a fully successful retry is not
                    # misread as degraded by the llm_status check below.
                    db.execute(
                        "UPDATE processing_error SET status = 'resolved', updated_at = ?"
                        " WHERE run_id = ? AND step IN ('summarise', 'digest')"
                        "   AND status != 'resolved'",
                        (utc_now_iso(), run_mgr.run_id),
                    )
                llm = _get_shared_llm()
                sum_cfg = summarizer_config_from_section(
                    cfg.llm, relevance_min=cfg.scoring.relevance_min
                )
                article_summarizer = ArticleSummarizer(db, llm, sum_cfg)
                summaries = article_summarizer.summarise_run(
                    run_mgr.run_id,
                    resume=resume_run_id is not None,
                    retry_degraded=retry_degraded,
                )
                stats.summarised = len(summaries)
                stats.llm_used = sum(1 for s in summaries if s.llm_used)
                stats.llm_degraded = len(summaries) - stats.llm_used
            except Exception as exc:
                logger.error("orchestrator: summarise failed — %s", exc)
                stats.errors.append(f"summarise: {exc}")
                stats.error_count += 1
                stats.status = "partial"
                _record_error(db, run_mgr.run_id, RunStep.SUMMARISE, "summarise_run", exc)
            run_mgr.set_checkpoint(RunStep.SUMMARISE)

    if check_and_clear_stop_flag(data_dir):
        return _cancel(run_mgr, stats)

    # ── Step 12: SAVE — idempotent upserts already done by prior steps ─────
    if RunStep.SAVE in todo:
        run_mgr.set_checkpoint(RunStep.SAVE)

    if check_and_clear_stop_flag(data_dir):
        return _cancel(run_mgr, stats)

    # ── Step 13: DIGESTS ───────────────────────────────────────────────────
    if RunStep.DIGESTS in todo:
        if ArticleRepository(db).count_for_run(run_mgr.run_id) == 0:
            # No articles → nothing to digest (also avoids a second LLM init).
            logger.info(
                "orchestrator: 0 articles in run #%d — skipping digests",
                run_mgr.run_id,
            )
            run_mgr.set_checkpoint(RunStep.DIGESTS)
        else:
            try:
                llm = _get_shared_llm()
                sum_cfg = summarizer_config_from_section(
                    cfg.llm, relevance_min=cfg.scoring.relevance_min
                )
                digest_summarizer = DigestSummarizer(db, llm, sum_cfg)
                digest_summarizer.digest_run(run_mgr.run_id, run_date=run_date_str)
            except Exception as exc:
                logger.error("orchestrator: digests failed — %s", exc)
                stats.errors.append(f"digests: {exc}")
                stats.error_count += 1
                stats.status = "partial"
                _record_error(db, run_mgr.run_id, RunStep.DIGESTS, "digest_run", exc)
            run_mgr.set_checkpoint(RunStep.DIGESTS)

    if check_and_clear_stop_flag(data_dir):
        return _cancel(run_mgr, stats)

    # ── llm_status honesty: circuit OR persisted LLM failures degrade it ──
    # The breaker alone does not cover digest truncations (LLMTruncatedError
    # deliberately does NOT trip the circuit): if unresolved 'digest'/'summarise'
    # errors were persisted for this run, the report must show ⚠ degraded, not ✔.
    # Resolved rows (a successful --retry-degraded) no longer count — P1 item 11.
    degraded = bool(getattr(shared_llm, "is_degraded", False)) if shared_llm is not None else False
    if not degraded and shared_llm is not None:
        try:
            row = db.query_one(
                "SELECT COUNT(*) AS n FROM processing_error"
                " WHERE run_id = ? AND step IN ('summarise', 'digest')"
                "   AND status != 'resolved'",
                (run_mgr.run_id,),
            )
            degraded = bool(row and int(row["n"]) > 0)
        except Exception as exc:  # non-fatal — status best-effort
            logger.warning("orchestrator: could not check processing_error — %s", exc)
    if degraded:
        try:
            db.execute(
                "UPDATE daily_run SET llm_status = 'degraded' WHERE id = ?",
                (run_mgr.run_id,),
            )
        except Exception as exc:  # non-fatal — status best-effort
            logger.warning("orchestrator: could not set llm_status=degraded — %s", exc)
    elif cfg.llm.enabled:
        # A fully successful retry restores an honest 'ok' (P1 item 11):
        # a run previously marked degraded is repaired without a manual SQL fix.
        try:
            row = db.query_one(
                "SELECT llm_status FROM daily_run WHERE id = ?", (run_mgr.run_id,)
            )
            if row and row["llm_status"] == "degraded":
                db.execute(
                    "UPDATE daily_run SET llm_status = 'ok' WHERE id = ?",
                    (run_mgr.run_id,),
                )
                logger.info(
                    "orchestrator: run #%d llm_status restored to 'ok' (no unresolved LLM errors)",
                    run_mgr.run_id,
                )
        except Exception as exc:  # non-fatal — status best-effort
            logger.warning("orchestrator: could not restore llm_status — %s", exc)

    # ── Step 14: REPORT ────────────────────────────────────────────────────
    if RunStep.REPORT in todo:
        try:
            report_cfg = ReportConfig(
                reports_dir=cfg.storage.reports_dir,
                model_id=cfg.llm.model,
                prompt_version="1.0",
                timezone_name=cfg.app.timezone,
            )
            generator = ReportGenerator(db, report_cfg)
            generator.generate(run_mgr.run_id)
        except Exception as exc:
            logger.error("orchestrator: report generation failed — %s", exc)
            stats.errors.append(f"report: {exc}")
            stats.error_count += 1
            stats.status = "partial"
            _record_error(db, run_mgr.run_id, RunStep.REPORT, "generate", exc)
        run_mgr.set_checkpoint(RunStep.REPORT)

    # ── Step 15: WRAP_UP ───────────────────────────────────────────────────
    if RunStep.WRAP_UP in todo:
        _purge_old_articles(db, cfg.storage.retention_days)
        _vacuum_backup(db, cfg)
        run_mgr.set_checkpoint(RunStep.WRAP_UP)

    # ── Finalise ───────────────────────────────────────────────────────────
    if stats.status == "running":
        if stats.stored == 0:
            if not SourceRepository(db).list(include_inactive=False):
                # Fresh/empty setup: nothing is configured to collect, so an
                # article-free run is legitimate (ENF-01 — the app never stops).
                stats.status = "completed"
            else:
                # Sources ARE configured but yielded nothing — flag the run so
                # the operator sees it instead of a green "completed" empty run.
                stats.status = "degraded"
                logger.warning(
                    "orchestrator: run #%d stored 0 articles (harvested=%d, skipped_robots=%d, "
                    "skipped_junk=%d, fetch_errors=%d) — marked degraded; check sources/API keys",
                    run_mgr.run_id,
                    stats.harvested,
                    stats.skipped_robots,
                    stats.skipped_junk,
                    stats.fetch_errors,
                )
        else:
            stats.status = "completed"

    run_mgr.finish(stats)

    logger.info(
        "orchestrator: run #%d %s  stored=%d  errors=%d",
        run_mgr.run_id,
        stats.status,
        stats.stored,
        stats.error_count,
    )
    return stats.exit_code()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# Shutdown helpers
# ─────────────────────────────────────────────────────────────────────────────


def _cancel(run_mgr: RunManager, stats: RunStats) -> int:
    """Set status to 'cancelled', finish the run, return EXIT_PARTIAL."""
    stats.status = "cancelled"
    run_mgr.finish(stats)
    logger.info("orchestrator: run #%d cancelled (stop.flag)", run_mgr.run_id)
    return EXIT_PARTIAL


def _finish_cancelled(run_mgr: RunManager, status: str = "cancelled") -> None:
    """Best-effort run close used by the KeyboardInterrupt / unhandled-exception path."""
    try:
        if run_mgr._run is not None:
            stats = RunStats(
                run_id=run_mgr.run_id,
                run_date=run_mgr.run.run_date,
                status=status,
            )
            run_mgr.finish(stats)
    except Exception as exc:
        logger.warning("_finish_cancelled: could not finalise run — %s", exc)


def _build_llm_client(cfg: AppConfig, injected: Any | None) -> Any:
    """Return the injected client (tests) or build the real one from config."""
    if injected is not None:
        return injected
    from .llm_client import WatsonxLLMClient, llm_client_config_from_section
    return WatsonxLLMClient(llm_client_config_from_section(cfg.llm))


_PLACEHOLDER_MARKERS = (
    "your-",
    "changeme",
    "placeholder",
    "xxx",
    "<your",
    "todo",
)


def _is_placeholder_key(value: str) -> bool:
    lowered = (value or "").lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _warn_on_missing_api_keys(cfg: AppConfig) -> None:
    """Warn at run start when an active API/search source can't authenticate.

    Only honest-seeming keys silence the warning: empty, unset or clearly
    placeholder values (``your-...``, ``changeme``, ...) are flagged so a
    misconfigured run is caught before it harvests nothing.
    """
    import os

    active = [s for s in cfg.sources if s.active]
    finnhub_sources = [s for s in active if s.type == "api" and "finnhub" in s.name.lower()]
    brave_sources = [s for s in active if s.type == "search" and "brave" in s.name.lower()]

    if finnhub_sources:
        key = os.environ.get("FINNHUB_API_KEY", "")
        if not key or _is_placeholder_key(key):
            logger.warning(
                "%s is active but FINNHUB_API_KEY is %s — Finnhub will be skipped "
                "(degraded). Put a real key in .env.",
                ", ".join(s.name for s in finnhub_sources),
                "missing" if not key else "a placeholder",
            )
    if brave_sources:
        key = os.environ.get("BRAVE_API_KEY", "")
        if not key or _is_placeholder_key(key):
            logger.warning(
                "%s is active but BRAVE_API_KEY is %s — Brave Search will be skipped "
                "(degraded). Put a real key in .env.",
                ", ".join(s.name for s in brave_sources),
                "missing" if not key else "a placeholder",
            )


def _record_error(
    db: DatabaseManager,
    run_id: int,
    step: "RunStep",
    source: str,
    exc: BaseException,
) -> None:
    """Insert a row into ``processing_error`` for a caught step exception.

    Each call produces one row (AUTOINCREMENT PK).  The ``source`` parameter
    is stored in the ``message`` prefix so it is human-readable in
    ``invest-watch errors``.
    Non-fatal: if the insert itself fails we log and continue.
    """
    tb = traceback.format_exc()
    now = _utc_now()
    # Count existing rows for this run+step to derive the retry_count.
    existing = db.query_one(
        "SELECT COUNT(*) AS n FROM processing_error WHERE run_id = ? AND step = ?",
        (run_id, step.value),
    )
    retry_count = int(existing["n"]) if existing else 0
    status = "failed" if retry_count >= 2 else ("retrying" if retry_count > 0 else "new")
    try:
        db.execute(
            "INSERT INTO processing_error"
            "  (run_id, step, error_type, message, traceback, retry_count,"
            "   status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id, step.value, type(exc).__name__,
                f"{source}: {exc}", tb,
                retry_count, status, now, now,
            ),
        )
    except Exception as db_exc:
        logger.warning("_record_error: could not persist processing_error — %s", db_exc)


def _utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _purge_old_articles(db: DatabaseManager, retention_days: int) -> None:
    """Delete articles (and cascade) older than *retention_days* (spec §10 step 15)."""
    try:
        with db.transaction():
            db.execute(
                "DELETE FROM daily_run"
                " WHERE run_date < date('now', ? || ' days')"
                " AND status IN ('completed', 'partial', 'failed', 'cancelled')",
                (f"-{retention_days}",),
            )
            # Synchronise FTS5: remove entries whose article no longer exists
            db.execute(
                "DELETE FROM article_fts WHERE article_id NOT IN (SELECT id FROM article)"
            )
        logger.debug("purge: removed daily_run rows older than %d days and synchronised FTS5", retention_days)
    except Exception as exc:
        logger.warning("purge failed (non-fatal): %s", exc)


def _vacuum_backup(db: DatabaseManager, cfg: AppConfig) -> None:
    """Write a daily VACUUM INTO backup (spec §10 step 15)."""
    if not cfg.storage.backup_enabled or not db.is_file_db:
        return
    try:
        backup_dir = cfg.storage.backups_dir
        backup_dir.mkdir(parents=True, exist_ok=True)
        # Timestamped name so repeated same-day runs never collide (VACUUM INTO
        # refuses to overwrite an existing file) and every run keeps its backup.
        now_utc = utc_now_iso()  # 2026-09-13T12:03:30Z
        stamp = f"{now_utc[:10]}-{now_utc[11:19].replace(':', '')}"
        backup_path = backup_dir / f"invest-{stamp}.db"
        # VACUUM INTO makes a clean, compacted copy — no WAL, no locks.
        db.execute(f"VACUUM INTO '{backup_path}'")
        logger.info("backup written: %s", backup_path)
    except Exception as exc:
        logger.warning("backup failed (non-fatal): %s", exc)
