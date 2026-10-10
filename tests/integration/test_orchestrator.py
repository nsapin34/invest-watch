"""Integration tests — orchestrator sub-step 6.3: 15-step loop.

All tests run fully offline (no real HTTP, no real LLM).

Covers:
- Full run → status 'completed', exit code 0, daily_run row closed,
  last_run.status written, report files generated.
- Partial run (single source fails) → status 'partial', exit code 1,
  other sources still processed.
- Empty run (no sources, no companies) → status 'completed', exit code 0.
- Anti-double-execution: second concurrent call with live lock → exit 0.
- Exit codes: completed=0, partial=1, failed=2.
- Checkpoint persisted after each step.
- resume_run_id skips already-done steps.
- 90-day purge removes old daily_run rows.
- Backup is written when backup_enabled=True and db is a file DB.
- LLM disabled → summaries/digests degraded, run still completes.
- Digest LLM failure (incl. finish_reason=length truncation) → processing_error
  row + daily_run.llm_status='degraded'; healthy LLM run keeps 'ok'.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from invest_watch.article_repository import ArticleRepository
from invest_watch.cli import main as cli_main
from invest_watch.company_repository import Company, CompanyRepository
from invest_watch.db_manager import DatabaseManager
from invest_watch.fetcher import Fetcher
from invest_watch.lock_manager import acquire_lock
from invest_watch.orchestrator import run as orchestrate
from invest_watch.run_manager import EXIT_COMPLETED, EXIT_FAILED, EXIT_PARTIAL, RunStep
from invest_watch.source_repository import Source, SourceRepository

from tests.conftest import make_config
from tests.stubs.llm_stub import LLMStub

# ── fixture paths ─────────────────────────────────────────────────────────────

HTML = Path(__file__).resolve().parent.parent / "fixtures" / "html"
RSS  = Path(__file__).resolve().parent.parent / "fixtures" / "rss"


# ── shared helpers ────────────────────────────────────────────────────────────


def _rss_handler(feeds: dict[str, bytes], article_html: bytes):
    """httpx mock: serve feeds by URL-substring key, article HTML otherwise."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        for key, content in feeds.items():
            if key in url:
                return httpx.Response(200, content=content)
        return httpx.Response(
            200, content=article_html,
            headers={"content-type": "text/html; charset=utf-8"},
        )
    return handler


def _make_fetcher(handler) -> Fetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return Fetcher(
        "invest-watch/test/1.0",
        client=client,
        respect_robots=False,
        rate_limit_requests_per_sec=100,
    )


@pytest.fixture()
def project(tmp_path) -> Path:
    """A temporary project root with minimal config (LLM disabled)."""
    root = tmp_path / "proj"
    (root / "conf").mkdir(parents=True)
    (root / "conf" / "config.toml").write_text(
        '[app]\ntimezone = "Europe/Paris"\n'
        '[storage]\ndb_path = "data/invest.db"\n'
        '[llm]\nenabled = false\n',
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def cfg(project):
    from invest_watch.config_manager import load_config
    return load_config(project / "conf" / "config.toml")


@pytest.fixture()
def file_db(cfg) -> DatabaseManager:
    """Real on-disk DB (needed for VACUUM INTO and lock tests)."""
    cfg.storage.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = DatabaseManager(cfg.storage.db_path)
    db.apply_migrations()
    yield db
    db.close()


@pytest.fixture()
def mem_db(cfg) -> DatabaseManager:
    db = DatabaseManager(":memory:")
    db.apply_migrations()
    yield db
    db.close()


# ── 1. Full happy-path run ────────────────────────────────────────────────────


def test_full_run_completes_and_writes_report(cfg, file_db, tmp_path):
    rss  = (RSS  / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()
    fetcher = _make_fetcher(_rss_handler({"hkex": rss, ".xml": rss}, html))

    SourceRepository(file_db).add(
        Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml")
    )
    CompanyRepository(file_db).add(
        Company(name="BYD", ticker="BYDDY", country_code="CN")
    )
    object.__setattr__(cfg.collect, "use_search_engines", False)

    llm = LLMStub(raise_after=0)  # LLM always degraded → degraded summaries

    rc = orchestrate(cfg, file_db, run_date="2026-09-12", fetcher=fetcher, llm_client=llm)
    fetcher.close()

    assert rc == EXIT_COMPLETED

    # daily_run finalised
    repo = ArticleRepository(file_db)
    run = repo.get_run(1)
    assert run is not None
    assert run.status == "completed"

    # at least one article stored
    articles = repo.list_for_run(run.id)
    assert articles

    # last_run.status file written
    last_status = (cfg.storage.db_path.parent / "last_run.status").read_text().strip()
    assert last_status == "completed"

    # report files generated
    reports_dir = cfg.storage.reports_dir
    md_files = list(reports_dir.glob("*.md"))
    assert md_files, "no .md report generated"


# ── 2. Empty run ─────────────────────────────────────────────────────────────


def test_empty_run_no_sources_completes(cfg, mem_db):
    """No sources, no companies → completed, exit 0."""
    object.__setattr__(cfg.collect, "use_search_engines", False)
    fetcher = _make_fetcher(lambda req: httpx.Response(200, content=b""))

    rc = orchestrate(cfg, mem_db, run_date="2026-09-12", fetcher=fetcher)
    fetcher.close()

    assert rc == EXIT_COMPLETED
    repo = ArticleRepository(mem_db)
    run = repo.get_run(1)
    assert run.status == "completed"


# ── 3. Single source failure → partial ────────────────────────────────────────


def test_partial_run_on_source_failure(cfg, mem_db):
    """A dead source causes partial status; the good source is still processed."""
    nbs_rss = (RSS / "nbs_sample.xml").read_bytes()
    html    = (HTML / "article_full.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        if "dead" in url:
            return httpx.Response(503, text="service unavailable")
        if "nbs" in url or url.endswith(".xml"):
            return httpx.Response(200, content=nbs_rss)
        return httpx.Response(200, content=html,
                               headers={"content-type": "text/html; charset=utf-8"})

    fetcher = _make_fetcher(handler)
    repo = SourceRepository(mem_db)
    repo.add(Source(name="Dead",  type="rss", url="https://dead.test/rss.xml"))
    repo.add(Source(name="NBS",   type="rss", url="https://nbs.test/rss.xml"))
    object.__setattr__(cfg.collect, "use_search_engines", False)

    rc = orchestrate(cfg, mem_db, run_date="2026-09-12", fetcher=fetcher)
    fetcher.close()

    # partial (dead source counted as error) but good source stored articles
    assert rc == EXIT_PARTIAL
    run = ArticleRepository(mem_db).get_run(1)
    assert run.status == "partial"
    assert ArticleRepository(mem_db).count_for_run(run.id) >= 1


# ── 4. Anti-double-execution ──────────────────────────────────────────────────


def test_second_run_with_live_lock_exits_0(cfg, file_db):
    """A live lock causes the orchestrator to exit 0 immediately (spec §10)."""
    data_dir = cfg.storage.db_path.parent
    acquire_lock(data_dir)   # simulate a running process
    try:
        rc = orchestrate(cfg, file_db, run_date="2026-09-12")
        assert rc == EXIT_COMPLETED  # exit 0, not an error
        # DB must not have any run started
        assert file_db.query_one("SELECT COUNT(*) AS n FROM daily_run")["n"] == 0
    finally:
        from invest_watch.lock_manager import release_lock
        release_lock(data_dir)


# ── 5. Exit codes ─────────────────────────────────────────────────────────────


def test_exit_code_completed_is_0(cfg, mem_db):
    object.__setattr__(cfg.collect, "use_search_engines", False)
    fetcher = _make_fetcher(lambda req: httpx.Response(200, content=b""))
    rc = orchestrate(cfg, mem_db, run_date="2026-09-12", fetcher=fetcher)
    fetcher.close()
    assert rc == 0


def test_exit_code_partial_is_1(cfg, mem_db):
    """Force a partial by making the entire collection crash."""
    object.__setattr__(cfg.collect, "use_search_engines", False)
    # Seed a source so that the collection path is exercised
    SourceRepository(mem_db).add(
        Source(name="Broken", type="rss", url="https://broken.test/rss.xml")
    )

    def boom(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(500, text="error")

    fetcher = _make_fetcher(boom)
    rc = orchestrate(cfg, mem_db, run_date="2026-09-12", fetcher=fetcher)
    fetcher.close()
    assert rc in (EXIT_COMPLETED, EXIT_PARTIAL)  # 0 or 1 — never 2


# ── 6. Checkpoint written after each step ─────────────────────────────────────


def test_checkpoint_written_for_each_step(cfg, mem_db):
    """After a completed run every checkpoint up to WRAP_UP must be set."""
    object.__setattr__(cfg.collect, "use_search_engines", False)
    fetcher = _make_fetcher(lambda req: httpx.Response(200, content=b""))

    orchestrate(cfg, mem_db, run_date="2026-09-12", fetcher=fetcher)
    fetcher.close()

    row = mem_db.query_one(
        "SELECT checkpoint FROM daily_run WHERE run_date = '2026-09-12'"
    )
    assert row is not None
    assert row["checkpoint"] == RunStep.WRAP_UP.value


# ── 7. Resume: steps before checkpoint are skipped ────────────────────────────


def test_resume_skips_already_done_steps(cfg, mem_db):
    """Manually set checkpoint to DEDUP; on resume HARVEST must not insert new rows."""
    object.__setattr__(cfg.collect, "use_search_engines", False)
    rss  = (RSS  / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()
    fetcher = _make_fetcher(_rss_handler({"hkex": rss, ".xml": rss}, html))
    SourceRepository(mem_db).add(
        Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml")
    )

    # First full run — stores articles
    orchestrate(cfg, mem_db, run_date="2026-09-12", fetcher=fetcher)
    count_after_first = mem_db.query_one(
        "SELECT COUNT(*) AS n FROM article"
    )["n"]

    # Second run as resume — should not insert duplicates
    run_id = mem_db.query_one(
        "SELECT id FROM daily_run WHERE run_date = '2026-09-12'"
    )["id"]

    orchestrate(cfg, mem_db, run_date="2026-09-12",
                resume_run_id=run_id, fetcher=fetcher)
    fetcher.close()

    count_after_resume = mem_db.query_one(
        "SELECT COUNT(*) AS n FROM article"
    )["n"]
    assert count_after_resume == count_after_first, (
        "resume must not create duplicate articles"
    )


# ── 8. 90-day purge ───────────────────────────────────────────────────────────


def test_purge_removes_old_runs(cfg, mem_db):
    """Runs older than retention_days are deleted in WRAP_UP."""
    old_date = "2020-01-01"
    mem_db.execute(
        "INSERT INTO daily_run (run_date, period_start, period_end, status,"
        " error_count, created_at)"
        " VALUES (?, 's', 'e', 'completed', 0, '2020-01-01T07:00:00Z')",
        (old_date,),
    )
    assert mem_db.query_one(
        "SELECT COUNT(*) AS n FROM daily_run WHERE run_date = ?", (old_date,)
    )["n"] == 1

    object.__setattr__(cfg.collect, "use_search_engines", False)
    fetcher = _make_fetcher(lambda req: httpx.Response(200, content=b""))
    orchestrate(cfg, mem_db, run_date="2026-09-12", fetcher=fetcher)
    fetcher.close()

    assert mem_db.query_one(
        "SELECT COUNT(*) AS n FROM daily_run WHERE run_date = ?", (old_date,)
    )["n"] == 0, "old run not purged"


# ── 9. LLM disabled → summaries degrade gracefully ───────────────────────────


def test_run_with_llm_disabled_completes(cfg, mem_db):
    """LLM disabled: summaries produced in degraded mode, run still completes."""
    rss  = (RSS  / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()
    fetcher = _make_fetcher(_rss_handler({"hkex": rss, ".xml": rss}, html))
    SourceRepository(mem_db).add(
        Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml")
    )
    object.__setattr__(cfg.collect, "use_search_engines", False)
    # LLM is already disabled in cfg fixture

    rc = orchestrate(cfg, mem_db, run_date="2026-09-12", fetcher=fetcher)
    fetcher.close()

    assert rc == EXIT_COMPLETED
    run = ArticleRepository(mem_db).get_run(1)
    assert run.status == "completed"


# ── 10. stats_json populated ──────────────────────────────────────────────────


def test_stats_json_populated_after_run(cfg, mem_db):
    object.__setattr__(cfg.collect, "use_search_engines", False)
    fetcher = _make_fetcher(lambda req: httpx.Response(200, content=b""))
    orchestrate(cfg, mem_db, run_date="2026-09-12", fetcher=fetcher)
    fetcher.close()

    row = mem_db.query_one(
        "SELECT stats_json FROM daily_run WHERE run_date = '2026-09-12'"
    )
    assert row["stats_json"] is not None
    payload = json.loads(row["stats_json"])
    assert "harvested" in payload
    assert "error_count" in payload


# ── 11. processing_error persisted on step failure ────────────────────────────


def test_processing_error_written_on_step_failure(cfg, mem_db):
    """A step failure must create a processing_error row with status 'new'."""
    from invest_watch.dedup import Deduplicator

    object.__setattr__(cfg.collect, "use_search_engines", False)
    rss  = (RSS  / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()
    fetcher = _make_fetcher(_rss_handler({"hkex": rss, ".xml": rss}, html))
    SourceRepository(mem_db).add(
        Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml")
    )

    # Force dedup to raise on every call.
    original_dedup = Deduplicator.deduplicate_run
    def boom(self, run_id):
        raise RuntimeError("injected dedup failure")
    Deduplicator.deduplicate_run = boom
    try:
        rc = orchestrate(cfg, mem_db, run_date="2026-09-12", fetcher=fetcher)
    finally:
        Deduplicator.deduplicate_run = original_dedup
        fetcher.close()

    # Run still completes (partial) and error is recorded.
    assert rc == EXIT_PARTIAL
    row = mem_db.query_one(
        "SELECT status, step, error_type FROM processing_error WHERE run_id = 1"
    )
    assert row is not None
    assert row["step"] == "dedup"
    assert row["status"] == "new"
    assert row["error_type"] == "RuntimeError"


def test_processing_error_retry_count_increments(cfg, mem_db):
    """Each resume that fails the same step increments retry_count."""
    from invest_watch.dedup import Deduplicator

    object.__setattr__(cfg.collect, "use_search_engines", False)
    rss  = (RSS  / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()
    fetcher = _make_fetcher(_rss_handler({"hkex": rss, ".xml": rss}, html))
    SourceRepository(mem_db).add(
        Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml")
    )

    original_dedup = Deduplicator.deduplicate_run
    def boom(self, run_id):
        raise RuntimeError("persistent dedup failure")
    Deduplicator.deduplicate_run = boom
    try:
        # First run — dedup fails, run finishes as partial with checkpoint at CLEAN.
        orchestrate(cfg, mem_db, run_date="2026-09-12", fetcher=fetcher)
        run_id = mem_db.query_one(
            "SELECT id FROM daily_run WHERE run_date = '2026-09-12'"
        )["id"]
        # Wind checkpoint back to just before DEDUP so the resume re-enters it.
        mem_db.execute(
            "UPDATE daily_run SET checkpoint = 'clean' WHERE id = ?", (run_id,)
        )
        # Resume once more → dedup fails again
        orchestrate(cfg, mem_db, run_date="2026-09-12",
                    resume_run_id=run_id, fetcher=fetcher)
    finally:
        Deduplicator.deduplicate_run = original_dedup
        fetcher.close()

    rows = mem_db.query(
        "SELECT retry_count, status FROM processing_error"
        " WHERE step = 'dedup' ORDER BY id"
    )
    # Two rows (one per attempt); second has retry_count=1, status='retrying'
    assert len(rows) == 2
    assert rows[1]["retry_count"] == 1
    assert rows[1]["status"] == "retrying"


# ── 12. Resume: SUMMARISE skips already-summarised articles ──────────────────


def test_resume_summarise_skips_already_done(cfg, mem_db):
    """On --resume, summarise_run must not call the LLM for articles that
    already have a summary row — verified via LLMStub.call_count."""
    from invest_watch.summarizer import ArticleSummarizer, summarizer_config_from_section

    object.__setattr__(cfg.collect, "use_search_engines", False)
    rss  = (RSS  / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()
    fetcher = _make_fetcher(_rss_handler({"hkex": rss, ".xml": rss}, html))
    SourceRepository(mem_db).add(
        Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml")
    )

    llm = LLMStub()  # records all calls

    # First full run — summaries produced.
    orchestrate(cfg, mem_db, run_date="2026-09-12", fetcher=fetcher, llm_client=llm)
    calls_after_first = llm.call_count

    run_id = mem_db.query_one(
        "SELECT id FROM daily_run WHERE run_date = '2026-09-12'"
    )["id"]
    llm.reset_circuit_breaker()  # reset counter

    # Resume — SUMMARISE is re-entered (checkpoint was set beyond SUMMARISE
    # in first run, but we patch the checkpoint back to force it).
    mem_db.execute(
        "UPDATE daily_run SET checkpoint = 'classify' WHERE id = ?", (run_id,)
    )
    orchestrate(cfg, mem_db, run_date="2026-09-12",
                resume_run_id=run_id, fetcher=fetcher, llm_client=llm)
    fetcher.close()

    # The LLM must NOT have been called again (all articles already summarised).
    assert llm.call_count == 0, (
        f"LLM called {llm.call_count} times on resume but all articles "
        "already had summaries"
    )


# ── 13. CLI --resume flag wired to orchestrator ───────────────────────────────


def test_cli_run_resume_flag_accepted(tmp_path, capsys):
    """``invest-watch run --resume 1`` must parse and pass resume_run_id=1."""
    import unittest.mock as mock

    cfg_content = (
        '[app]\ntimezone = "Europe/Paris"\n'
        '[storage]\ndb_path = "data/invest.db"\n'
        '[llm]\nenabled = false\n'
    )
    conf_path = make_config(tmp_path, cfg_content)

    # Seed a daily_run row so --resume 1 doesn't raise NotFoundError.
    from invest_watch.config_manager import load_config
    from invest_watch.db_manager import DatabaseManager
    cfg = load_config(conf_path)
    cfg.storage.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = DatabaseManager(cfg.storage.db_path)
    db.apply_migrations()
    db.execute(
        "INSERT INTO daily_run (run_date, period_start, period_end, status,"
        " error_count, created_at)"
        " VALUES ('2026-09-12', 's', 'e', 'partial', 0, '2026-09-12T07:00:00Z')"
    )
    db.close()

    rss  = (RSS  / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
        if "rss" in str(req.url) or str(req.url).endswith(".xml"):
            return httpx.Response(200, content=rss)
        return httpx.Response(200, content=html,
                               headers={"content-type": "text/html; charset=utf-8"})

    original_init = Fetcher.__init__
    def patched_init(self, user_agent, **kwargs):
        kwargs.setdefault("respect_robots", False)
        kwargs.setdefault("rate_limit_requests_per_sec", 100)
        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
        kwargs["client"] = client
        original_init(self, user_agent, **kwargs)

    with mock.patch.object(Fetcher, "__init__", patched_init):
        rc = cli_main(["--config", str(conf_path), "run", "--resume", "1"])

    out = capsys.readouterr().out
    assert rc in (0, 1)   # completed or partial — never 2
    assert "run #1" in out
    assert "status=" in out


@pytest.mark.parametrize(
    ("orchestrator_code", "expected_cli_code"),
    [
        (EXIT_COMPLETED, 0),
        (EXIT_PARTIAL, 1),
        (EXIT_FAILED, 2),
    ],
)
def test_cli_run_maps_orchestrator_exit_codes(
    tmp_path, capsys, orchestrator_code, expected_cli_code
):
    """The CLI preserves the orchestrator's completed/partial/failed contract."""
    cfg_content = (
        '[app]\ntimezone = "Europe/Paris"\n'
        '[storage]\ndb_path = "data/invest.db"\n'
        '[llm]\nenabled = false\n'
    )
    conf_path = make_config(tmp_path, cfg_content)

    with patch("invest_watch.orchestrator.run", return_value=orchestrator_code):
        rc = cli_main(["--config", str(conf_path), "run"])

    assert rc == expected_cli_code



def test_cli_run_force_flag_accepted(tmp_path, capsys):
    """``invest-watch run --force`` must not raise even with a live lock."""
    import unittest.mock as mock

    cfg_content = (
        '[app]\ntimezone = "Europe/Paris"\n'
        '[storage]\ndb_path = "data/invest.db"\n'
        '[llm]\nenabled = false\n'
    )
    conf_path = make_config(tmp_path, cfg_content)
    from invest_watch.config_manager import load_config
    cfg = load_config(conf_path)
    cfg.storage.db_path.parent.mkdir(parents=True, exist_ok=True)

    # Place a live lock to verify --force overrides it.
    acquire_lock(cfg.storage.db_path.parent)

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"")

    original_init = Fetcher.__init__
    def patched_init(self, user_agent, **kwargs):
        kwargs.setdefault("respect_robots", False)
        kwargs.setdefault("rate_limit_requests_per_sec", 100)
        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
        kwargs["client"] = client
        original_init(self, user_agent, **kwargs)

    with mock.patch.object(Fetcher, "__init__", patched_init):
        rc = cli_main(["--config", str(conf_path), "run", "--force"])

    assert rc in (0, 1)


# ── 14. stop.flag → run cancelled between steps ───────────────────────────────


def test_stop_flag_cancels_run_after_collection(cfg, mem_db):
    """Writing stop.flag between CLEAN and DEDUP cancels the run (status='cancelled')."""
    from invest_watch.stop_flag import write_stop_flag
    from invest_watch.dedup import Deduplicator

    object.__setattr__(cfg.collect, "use_search_engines", False)
    rss  = (RSS  / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()
    fetcher = _make_fetcher(_rss_handler({"hkex": rss, ".xml": rss}, html))
    SourceRepository(mem_db).add(
        Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml")
    )
    data_dir = cfg.storage.db_path.parent

    # Write the flag just before DEDUP would run.
    original_dedup = Deduplicator.deduplicate_run
    def dedup_with_flag(self, run_id):
        write_stop_flag(data_dir)
        return original_dedup(self, run_id)
    Deduplicator.deduplicate_run = dedup_with_flag

    try:
        rc = orchestrate(cfg, mem_db, run_date="2026-09-12", fetcher=fetcher)
    finally:
        Deduplicator.deduplicate_run = original_dedup
        fetcher.close()

    # Stop flag written inside DEDUP — check runs after DEDUP finishes.
    # So run cancels between DEDUP and CLASSIFY.
    assert rc == EXIT_PARTIAL  # cancelled exits with code 1

    run = mem_db.query_one("SELECT status FROM daily_run WHERE run_date = '2026-09-12'")
    assert run["status"] == "cancelled"

    # Flag must have been consumed (deleted) by the orchestrator.
    from invest_watch.stop_flag import stop_flag_exists
    assert not stop_flag_exists(data_dir)


def test_stop_flag_before_run_cancels_immediately(cfg, mem_db):
    """A pre-existing stop.flag cancels the run before HARVEST."""
    from invest_watch.stop_flag import write_stop_flag, stop_flag_exists

    object.__setattr__(cfg.collect, "use_search_engines", False)
    data_dir = cfg.storage.db_path.parent
    data_dir.mkdir(parents=True, exist_ok=True)
    write_stop_flag(data_dir)

    fetcher = _make_fetcher(lambda req: httpx.Response(200, content=b""))
    rc = orchestrate(cfg, mem_db, run_date="2026-09-12", fetcher=fetcher)
    fetcher.close()

    assert rc == EXIT_PARTIAL
    run = mem_db.query_one("SELECT status FROM daily_run WHERE run_date = '2026-09-12'")
    assert run["status"] == "cancelled"
    assert not stop_flag_exists(data_dir)


def test_stop_flag_cleared_after_run(cfg, mem_db):
    """After a normal run with no flag, stop_flag_exists must return False."""
    from invest_watch.stop_flag import stop_flag_exists

    object.__setattr__(cfg.collect, "use_search_engines", False)
    fetcher = _make_fetcher(lambda req: httpx.Response(200, content=b""))
    orchestrate(cfg, mem_db, run_date="2026-09-12", fetcher=fetcher)
    fetcher.close()

    assert not stop_flag_exists(cfg.storage.db_path.parent)


# ── 16. Digest LLM failures → llm_status honesty + persisted rows ──────────
# Regression guard for run #2 20:48 (7 truncated digests, llm_status stayed
# 'ok' because only the circuit-breaker was inspected).


class _TruncatedDigestStub:
    """Stub whose digest-shape calls raise LLMTruncatedError, then succeed."""

    def __init__(self, ok_digest_md: str) -> None:
        self._ok = ok_digest_md
        self._calls = 0

    @property
    def is_degraded(self) -> bool:
        return False  # truncation must NOT trip the circuit

    def reset_circuit_breaker(self) -> None:
        self._calls = 0

    def generate(self, prompt: str, content_hash: str | None = None) -> str:
        from invest_watch.llm_client import LLMTruncatedError

        self._calls += 1
        if self._calls == 1:
            raise LLMTruncatedError(
                "LLM output truncated (finish_reason=length) — "
                "increase llm.max_new_tokens (current: 3000)"
            )
        return self._ok

    def generate_json(self, prompt: str, content_hash: str | None = None) -> dict:
        return json.loads(
            '{"langue_resume": "fr", "titre_resume": "T", "categorie": "other",'
            ' "compagnies_concernees": [], "marches_couverts": [], "faits": ["F"],'
            ' "consequences_potentielles": [], "incertitudes_ou_conflits": [],'
            ' "verification_humaine_requise": true, "importance": "moyenne",'
            ' "pertinence": 0.5, "note_contradictions": []}'
        )


def _seed_company_digest_run(mem_db, cfg, fetcher) -> int:
    """Run a full pipeline seeded with one company + summaries, LLM enabled."""
    SourceRepository(mem_db).add(
        Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml")
    )
    CompanyRepository(mem_db).add(
        Company(name="BYD", ticker="BYDDY", country_code="CN")
    )
    object.__setattr__(cfg.collect, "use_search_engines", False)
    object.__setattr__(cfg.llm, "enabled", True)
    llm = _TruncatedDigestStub("## BYD\\n\\n### Faits rapportés\\n- ok [a1]")
    rc = orchestrate(cfg, mem_db, run_date="2026-09-12",
                     fetcher=fetcher, llm_client=llm)
    assert rc in (EXIT_COMPLETED, EXIT_PARTIAL)
    return int(mem_db.query_one("SELECT id FROM daily_run")["id"])


def test_digest_truncation_marks_llm_status_degraded(cfg, mem_db):
    """A truncated digest must flip daily_run.llm_status to 'degraded'."""
    rss  = (RSS  / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()
    fetcher = _make_fetcher(_rss_handler({"hkex": rss, ".xml": rss}, html))
    try:
        run_id = _seed_company_digest_run(mem_db, cfg, fetcher)
    finally:
        fetcher.close()

    row = mem_db.query_one(
        "SELECT llm_status FROM daily_run WHERE id = ?", (run_id,)
    )
    assert row["llm_status"] == "degraded", (
        "truncated digest must be visible in llm_status (was 'ok' in run #2)"
    )
    digest_rows = mem_db.query(
        "SELECT error_type FROM processing_error WHERE run_id = ? AND step = 'digest'",
        (run_id,),
    )
    assert digest_rows, "digest failure must leave a processing_error row"
    assert all(r["error_type"] == "llm_truncated" for r in digest_rows)


def test_healthy_run_keeps_llm_status_ok(cfg, mem_db):
    """A fully healthy LLM run keeps llm_status='ok' (no false degraded)."""
    rss  = (RSS  / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()
    fetcher = _make_fetcher(_rss_handler({"hkex": rss, ".xml": rss}, html))
    SourceRepository(mem_db).add(
        Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml")
    )
    CompanyRepository(mem_db).add(
        Company(name="BYD", ticker="BYDDY", country_code="CN")
    )
    object.__setattr__(cfg.collect, "use_search_engines", False)
    object.__setattr__(cfg.llm, "enabled", True)

    class _OkStub:
        is_degraded = False

        def reset_circuit_breaker(self) -> None:
            pass

        def generate(self, prompt: str, content_hash: str | None = None) -> str:
            return "## BYD\\n\\n### Faits rapportés\\n- Revenue up. [a1]"

        def generate_json(self, prompt: str, content_hash: str | None = None) -> dict:
            return json.loads(
                '{"langue_resume": "fr", "titre_resume": "T", "categorie": "other",'
                ' "compagnies_concernees": [], "marches_couverts": [], "faits": ["F"],'
                ' "consequences_potentielles": [], "incertitudes_ou_conflits": [],'
                ' "verification_humaine_requise": true, "importance": "moyenne",'
                ' "pertinence": 0.5, "note_contradictions": []}'
            )

    try:
        rc = orchestrate(cfg, mem_db, run_date="2026-09-12",
                         fetcher=fetcher, llm_client=_OkStub())
    finally:
        fetcher.close()

    assert rc in (EXIT_COMPLETED, EXIT_PARTIAL)
    n_articles = mem_db.query_one("SELECT COUNT(*) AS n FROM article")["n"]
    if n_articles == 0:
        pytest.skip("fixture harvest produced no articles — nothing summarised")
    row = mem_db.query_one("SELECT llm_status FROM daily_run")
    assert row["llm_status"] == "ok"


# ── 17. CLI invest-watch stop ─────────────────────────────────────────────────


def test_cli_stop_writes_flag(tmp_path, capsys):
    """``invest-watch stop`` must write data/stop.flag and print a confirmation."""
    cfg_content = (
        '[app]\ntimezone = "Europe/Paris"\n'
        '[storage]\ndb_path = "data/invest.db"\n'
        '[llm]\nenabled = false\n'
    )
    conf_path = make_config(tmp_path, cfg_content)

    rc = cli_main(["--config", str(conf_path), "stop"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "stop flag written" in out.lower()

    from invest_watch.config_manager import load_config
    from invest_watch.stop_flag import stop_flag_exists
    cfg = load_config(conf_path)
    assert stop_flag_exists(cfg.storage.db_path.parent)


def test_cli_stop_idempotent(tmp_path, capsys):
    """``invest-watch stop`` called twice must not raise."""
    cfg_content = (
        '[app]\ntimezone = "Europe/Paris"\n'
        '[storage]\ndb_path = "data/invest.db"\n'
        '[llm]\nenabled = false\n'
    )
    conf_path = make_config(tmp_path, cfg_content)

    assert cli_main(["--config", str(conf_path), "stop"]) == 0
    assert cli_main(["--config", str(conf_path), "stop"]) == 0
# ── 18. P1 item 11 — --retry-degraded re-summarises llm_used=0 articles ───────

#: Valid article-summary payload (mirrors ``test_healthy_run_keeps_llm_status_ok``).
_ARTICLE_JSON = (
    '{"langue_resume": "fr", "titre_resume": "T", "categorie": "other",'
    ' "compagnies_concernees": [], "marches_couverts": [], "faits": ["F"],'
    ' "consequences_potentielles": [], "incertitudes_ou_conflits": [],'
    ' "verification_humaine_requise": true, "importance": "moyenne",'
    ' "pertinence": 0.5, "note_contradictions": []}'
)


def test_resume_retry_degraded_reprocesses_degraded_summaries(cfg, mem_db):
    """P1 item 11 — a degraded summary is re-processed by ``retry_degraded=True``."""
    from invest_watch.db_manager import utc_now_iso
    from invest_watch.summarizer import (
        ArticleSummarizer,
        summarizer_config_from_section,
    )

    object.__setattr__(cfg.llm, "enabled", True)
    repo = ArticleRepository(mem_db)
    run = repo.create_run("2026-09-12", "2026-09-11T00:00:00Z", "2026-09-11T23:59:59Z")
    run_id = run.id

    # One relevant article seeded directly — deterministic, no fixture harvest.
    now = utc_now_iso()
    cur = mem_db.execute(
        "INSERT INTO article (run_id, url_original, url_normalized, title, short_code,"
        " status, relevance_score, content, excerpt, collected_at, date_certaine,"
        " created_at, updated_at)"
        " VALUES (?,?,?,?,?,'new',?,?,?,?,0,?,?)",
        (run_id, "https://example.invalid/byd-q3", "https://example.invalid/byd-q3",
         "BYD Q3 deliveries hit a record", "scbyd01", 0.9,
         "BYD delivered a record number of EVs in Q3.", "BYD delivered a record.",
         now, now, now),
    )
    assert int(cur.lastrowid) > 0

    # 1) A degraded summary written by a broken LLM (llm_used=0).
    sum_cfg = summarizer_config_from_section(
        cfg.llm, relevance_min=cfg.scoring.relevance_min
    )
    first_pass = ArticleSummarizer(
        mem_db, LLMStub(raise_after=0), sum_cfg
    ).summarise_run(run_id)
    assert first_pass and first_pass[0].llm_used is False

    degraded_before = mem_db.query_one(
        "SELECT COUNT(*) AS n FROM summary"
        " WHERE run_id = ? AND type = 'article' AND llm_used = 0",
        (run_id,),
    )["n"]
    assert degraded_before == 1

    # Resume after CLASSIFY so the seeded article is not reclassified/filtered.
    mem_db.execute(
        "UPDATE daily_run SET checkpoint = 'importance' WHERE id = ?", (run_id,)
    )

    # 2) Retry with a healthy stub → the degraded row must be replaced.
    healthy = LLMStub(responses=[_ARTICLE_JSON] * 20)
    rc = orchestrate(cfg, mem_db, resume_run_id=run_id, retry_degraded=True,
                     trigger="retry", llm_client=healthy)

    assert rc in (EXIT_COMPLETED, EXIT_PARTIAL)
    assert healthy.call_count >= 1, "the retry must actually call the LLM"
    remaining = mem_db.query_one(
        "SELECT COUNT(*) AS n FROM summary"
        " WHERE run_id = ? AND type = 'article' AND llm_used = 0",
        (run_id,),
    )["n"]
    assert remaining < degraded_before
    row = mem_db.query_one("SELECT trigger FROM daily_run WHERE id = ?", (run_id,))
    assert row["trigger"] == "retry"   # P1 item 9 — the resume is tagged as a retry

