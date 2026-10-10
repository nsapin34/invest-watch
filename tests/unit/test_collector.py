"""End-to-end Step 2 collection: mocked RSS + HTML → articles in SQLite.

P1 items 7 + 9 — the collector no longer creates/finishes the ``daily_run``
row (the orchestrator owns it: ``run_id`` + ``period`` are passed in) and it
persists fetch/source errors into ``processing_error`` with a cap.
"""

from pathlib import Path

import httpx

from invest_watch.article_repository import ArticleRepository
from invest_watch.collector import run_collection
from invest_watch.company_repository import Company, CompanyRepository
from invest_watch.date_detector import compute_period
from invest_watch.fetcher import Fetcher
from invest_watch.source_repository import Source, SourceRepository

HTML = Path(__file__).resolve().parent.parent / "fixtures" / "html"
RSS = Path(__file__).resolve().parent.parent / "fixtures" / "rss"


def _open_run(db, config, run_date: str = "2026-09-12") -> tuple[int, object]:
    """Create the ``daily_run`` row the way the orchestrator does (P1 item 9)."""
    period = compute_period(
        config.app.timezone,
        run_date=run_date,
        tolerance_hours=config.collect.period_tolerance_hours,
    )
    run = ArticleRepository(db).create_run(
        period.run_date.isoformat(),
        period.start_iso,
        period.end_iso,
        trigger="manual",
        llm_status="off",
    )
    return run.id, period


def test_run_collection_stores_raw_candidates(db, config):
    rss = (RSS / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()
    paywall = (HTML / "article_paywall.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith(".xml") or "rss" in url:
            return httpx.Response(200, content=rss)
        if "old" in url:
            return httpx.Response(200, content=paywall)
        return httpx.Response(200, content=html, headers={"content-type": "text/html; charset=utf-8"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher(
        "invest-watch/1.0",
        client=client,
        respect_robots=False,
        rate_limit_requests_per_sec=100,
    )
    SourceRepository(db).add(Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml"))
    CompanyRepository(db).add(Company(name="BYD", ticker="BYDDY", country_code="CN"))

    # Force RSS-only so Google News / DDG are not called.
    object.__setattr__(config.collect, "use_search_engines", False)

    run_id, period = _open_run(db, config)
    stats = run_collection(
        config,
        db,
        run_id=run_id,
        period=period,
        fetcher=fetcher,
        ddg_search=lambda q: [],
        brave_api_key="",
    )
    assert stats.stored >= 1
    assert stats.status in {"completed", "partial"}
    articles = ArticleRepository(db).list_for_run(run_id)
    assert articles
    assert all(a.status == "new" for a in articles)
    assert any("BYD" in a.title or "byd" in a.url_original.lower() for a in articles)
    run = ArticleRepository(db).get_run(run_id)
    assert run is not None
    assert run.period_start
    # P1 item 9 — the collector never touches the daily_run status: it stays
    # 'running' (the orchestrator's finish_run sets the final value).
    assert run.status == "running"
    fetcher.close()


def test_collection_idempotent_on_url(db, config):
    rss = (RSS / "nbs_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if "rss" in str(request.url) or str(request.url).endswith(".xml"):
            return httpx.Response(200, content=rss)
        return httpx.Response(200, content=html)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False, rate_limit_requests_per_sec=100)
    SourceRepository(db).add(Source(name="NBS", type="rss", url="https://nbs.test/rss.xml"))
    object.__setattr__(config.collect, "use_search_engines", False)
    run_id, period = _open_run(db, config)
    first = run_collection(
        config, db, run_id=run_id, period=period, fetcher=fetcher,
        ddg_search=lambda q: [], brave_api_key="",
    )
    second = run_collection(
        config, db, run_id=run_id, period=period, fetcher=fetcher,
        ddg_search=lambda q: [], brave_api_key="",
    )
    assert second.stored == 0
    assert ArticleRepository(db).count_for_run(first.run_id) == first.stored
    fetcher.close()


# ─────────────────────────────────────────────────────────────────────────────
# Junk-page guard: consent / JS-challenge interstitials are never articles
# (regression: Google's "Before you continue" page used to be stored as an
# article — 213 words of boilerplate sailing past min_content_chars=200)
# ─────────────────────────────────────────────────────────────────────────────


def test_is_junk_page_blocklist():
    from invest_watch.collector import _is_junk_page

    assert _is_junk_page("https://news.google.com/rss/articles/CBMi123", "An article", "text")
    assert _is_junk_page("https://consent.google.com/m", "Consent", "text")
    assert _is_junk_page("https://publisher.example/a", "Before you continue", "text")
    assert _is_junk_page("https://publisher.example/a", "Just a moment...", "text")
    assert _is_junk_page("https://publisher.example/a", "Access denied | publisher used Cloudflare", "text")
    # Real articles must never be dropped.
    assert not _is_junk_page("https://publisher.example/a", "BYD beats quarterly expectations", "text")
    assert not _is_junk_page("https://www.stats.gov.cn/english/PressRelease/x", "CPI release", "text")


def test_junk_interstitial_page_is_rejected(db, config):
    """A fetched consent page is counted in skipped_junk and never stored."""
    rss = (RSS / "hkex_sample.xml").read_bytes()
    consent_html = (
        b'<html><head><title>Before you continue</title></head>'
        b'<body><p>Google uses cookies for various purposes. '
        b'You can manage your preferences at any time.</p></body></html>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith(".xml") or "rss" in url:
            return httpx.Response(200, content=rss)
        return httpx.Response(200, content=consent_html,
                              headers={"content-type": "text/html; charset=utf-8"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher(
        "invest-watch/1.0",
        client=client,
        respect_robots=False,
        rate_limit_requests_per_sec=100,
    )
    SourceRepository(db).add(Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml"))
    CompanyRepository(db).add(Company(name="BYD", ticker="BYDDY", country_code="CN"))
    object.__setattr__(config.collect, "use_search_engines", False)

    run_id, period = _open_run(db, config)
    stats = run_collection(
        config, db, run_id=run_id, period=period, fetcher=fetcher,
        ddg_search=lambda q: [], brave_api_key="",
    )
    assert stats.skipped_junk >= 1
    assert stats.stored == 0
    fetcher.close()


# ─────────────────────────────────────────────────────────────────────────────
# P1 item 7 — fetch errors are persisted into processing_error (capped)
# ─────────────────────────────────────────────────────────────────────────────


def test_fetch_error_persisted_with_structured_type(db, config):
    """A 403 page fetch leaves a processing_error row step='fetch' type='http_403'."""
    rss = (RSS / "hkex_sample.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith(".xml") or "rss" in url:
            return httpx.Response(200, content=rss)
        # Every article page is forbidden — the classic publisher 403.
        return httpx.Response(403, content=b"<html>forbidden</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher(
        "invest-watch/1.0",
        client=client,
        respect_robots=False,
        rate_limit_requests_per_sec=100,
    )
    SourceRepository(db).add(Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml"))
    CompanyRepository(db).add(Company(name="BYD", ticker="BYDDY", country_code="CN"))
    object.__setattr__(config.collect, "use_search_engines", False)

    run_id, period = _open_run(db, config)
    stats = run_collection(
        config, db, run_id=run_id, period=period, fetcher=fetcher,
        ddg_search=lambda q: [], brave_api_key="",
    )
    fetcher.close()

    assert stats.fetch_errors >= 1
    assert stats.errors_persisted == stats.fetch_errors
    assert stats.errors_dropped == 0
    rows = db.query(
        "SELECT step, error_type, message, source_id FROM processing_error WHERE run_id = ?",
        (run_id,),
    )
    assert rows and len(rows) == stats.fetch_errors
    assert all(r["step"] == "fetch" for r in rows)
    assert all(r["error_type"] == "http_403" for r in rows)
    # message = URL (P1 item 7) so the host grouping works.
    assert all(str(r["message"]).startswith("http") for r in rows)
    assert all(r["source_id"] is not None for r in rows)
    # The in-memory error strings merged into stats keep the details too.
    assert any("http_403" in e for e in stats.errors)


def test_error_cap_drops_excess_rows(db, config):
    """With error_cap=2 and 3 failures, only 2 rows are written, 1 dropped."""
    rss = (
        b"<?xml version='1.0' encoding='UTF-8'?>"
        b"<rss version='2.0'><channel><title>t</title><link>https://hkex.test</link>"
        b"<description>d</description>"
        b"<item><title>BYD article one</title><link>https://pub1.test/a1</link>"
        b"<pubDate>Fri, 11 Sep 2026 10:00:00 GMT</pubDate><guid>g1</guid></item>"
        b"<item><title>BYD article two</title><link>https://pub2.test/a2</link>"
        b"<pubDate>Fri, 11 Sep 2026 11:00:00 GMT</pubDate><guid>g2</guid></item>"
        b"<item><title>BYD article three</title><link>https://pub3.test/a3</link>"
        b"<pubDate>Fri, 11 Sep 2026 12:00:00 GMT</pubDate><guid>g3</guid></item>"
        b"</channel></rss>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith(".xml") or "rss" in url:
            return httpx.Response(200, content=rss)
        return httpx.Response(403, content=b"<html>forbidden</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher(
        "invest-watch/1.0",
        client=client,
        respect_robots=False,
        rate_limit_requests_per_sec=100,
    )
    SourceRepository(db).add(Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml"))
    CompanyRepository(db).add(Company(name="BYD", ticker="BYDDY", country_code="CN"))
    object.__setattr__(config.collect, "use_search_engines", False)

    run_id, period = _open_run(db, config)
    stats = run_collection(
        config, db, run_id=run_id, period=period, fetcher=fetcher,
        ddg_search=lambda q: [], brave_api_key="", error_cap=2,
    )
    fetcher.close()

    total_errors = stats.fetch_errors + stats.source_errors
    assert total_errors >= 3  # plafond actually tested
    assert stats.errors_persisted == 2
    assert stats.errors_dropped == total_errors - 2
    n_rows = db.query_one(
        "SELECT COUNT(*) AS n FROM processing_error WHERE run_id = ?", (run_id,)
    )["n"]
    assert n_rows == 2


# -- Publisher blacklist ([collect] blocked_domains) ---------------------------

def test_is_blocked_matches_domain_and_subdomains():
    from invest_watch.collector import _is_blocked
    blocked = ("fool.com",)
    assert _is_blocked("https://www.fool.com/investing/2026/10/08/x/", blocked)
    assert _is_blocked("https://fool.com/a", blocked)
    assert _is_blocked("https://m.FOOL.com/a", blocked)
    assert not _is_blocked("https://notfool.com/a", blocked)
    assert not _is_blocked("https://fool.com.example.org/a", blocked)
    assert not _is_blocked("https://www.fool.com/a", ())
    assert not _is_blocked(None, blocked)


def test_normalize_blocked_domains():
    import pytest as _pytest
    from invest_watch.config_manager import normalize_blocked_domains
    from invest_watch.exceptions import ConfigError
    assert normalize_blocked_domains(["https://www.Fool.com/x", "fool.com", " seekingalpha.com "]) == (
        "fool.com", "seekingalpha.com")
    assert normalize_blocked_domains(None) == ()
    with _pytest.raises(ConfigError):
        normalize_blocked_domains(42)


def _blacklist_fetcher(rss: bytes, html: bytes, redirect_to: str | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith(".xml") or "rss" in url:
            return httpx.Response(200, content=rss)
        if redirect_to and "redirected" not in url:
            return httpx.Response(302, headers={"location": redirect_to})
        return httpx.Response(200, content=html, headers={"content-type": "text/html; charset=utf-8"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return Fetcher("invest-watch/1.0", client=client, respect_robots=False,
                   rate_limit_requests_per_sec=100)


def _feed_hosts(rss: bytes) -> tuple[str, ...]:
    import feedparser
    from urllib.parse import urlsplit
    return tuple(sorted({urlsplit(e.link).hostname for e in feedparser.parse(rss).entries}))


def test_blocked_domain_is_never_fetched_nor_stored(db, config):
    rss = (RSS / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()
    fetcher = _blacklist_fetcher(rss, html)
    SourceRepository(db).add(Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml"))
    CompanyRepository(db).add(Company(name="BYD", ticker="BYDDY", country_code="CN"))
    object.__setattr__(config.collect, "use_search_engines", False)
    object.__setattr__(config.collect, "blocked_domains", _feed_hosts(rss))

    run_id, period = _open_run(db, config)
    stats = run_collection(config, db, run_id=run_id, period=period, fetcher=fetcher,
                           ddg_search=lambda q: [], brave_api_key="")
    assert stats.skipped_blocked >= 1
    assert stats.fetched == 0 and stats.stored == 0
    assert '"skipped_blocked"' in stats.as_json()
    fetcher.close()


def test_redirect_to_blocked_domain_is_skipped(db, config):
    rss = (RSS / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()
    fetcher = _blacklist_fetcher(rss, html, redirect_to="https://www.fool.com/redirected/article")
    SourceRepository(db).add(Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml"))
    CompanyRepository(db).add(Company(name="BYD", ticker="BYDDY", country_code="CN"))
    object.__setattr__(config.collect, "use_search_engines", False)
    object.__setattr__(config.collect, "blocked_domains", ("fool.com",))

    run_id, period = _open_run(db, config)
    stats = run_collection(config, db, run_id=run_id, period=period, fetcher=fetcher,
                           ddg_search=lambda q: [], brave_api_key="")
    assert stats.skipped_blocked >= 1
    assert stats.stored == 0
    fetcher.close()
