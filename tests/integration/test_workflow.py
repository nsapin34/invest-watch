"""Integration tests — full Step 2 workflow with mocked sources (specs §15).

Validates the end-to-end collection pipeline:
  - daily_run row created and finished with correct status
  - articles stored (title, url, language, excerpt, date)
  - no duplicate articles on a second identical run (idempotency)
  - source errors are isolated; other sources still yield candidates
  - robots.txt disallowed URLs are skipped (counted, not stored)
  - articles outside the D-1 window are not stored
  - empty run (no companies, no sources) completes cleanly
  - CLI `invest-watch run` exits 0 and prints summary stats
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from invest_watch.article_repository import ArticleRepository
from invest_watch.bootstrap import seed_from_config
from invest_watch.cli import EXIT_OK, main
from invest_watch.collector import run_collection
from invest_watch.company_repository import Company, CompanyRepository
from invest_watch.fetcher import Fetcher
from invest_watch.source_repository import Source, SourceRepository

from tests.conftest import make_config

# ─────────────────────────────────────────────────────────────────────────────
# Fixture paths
# ─────────────────────────────────────────────────────────────────────────────

HTML = Path(__file__).resolve().parent.parent / "fixtures" / "html"
RSS  = Path(__file__).resolve().parent.parent / "fixtures" / "rss"

# ─────────────────────────────────────────────────────────────────────────────
# Shared transport helpers
# ─────────────────────────────────────────────────────────────────────────────


def _multi_feed_handler(feeds: dict[str, bytes], article_html: bytes):
    """Return an httpx handler that serves feeds by URL key and article HTML."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        for key, content in feeds.items():
            if key in url:
                return httpx.Response(200, content=content)
        return httpx.Response(200, content=article_html,
                              headers={"content-type": "text/html; charset=utf-8"})

    return handler


def _make_fetcher(handler, *, respect_robots: bool = False) -> Fetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    return Fetcher("invest-watch/1.0", client=client,
                   respect_robots=respect_robots, rate_limit_requests_per_sec=100)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Happy path — single RSS source, company seeded
# ─────────────────────────────────────────────────────────────────────────────


def test_workflow_stores_articles_and_finalises_run(db, config):
    rss  = (RSS  / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()

    fetcher = _make_fetcher(_multi_feed_handler({"rss": rss, ".xml": rss}, html))
    SourceRepository(db).add(Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml"))
    CompanyRepository(db).add(Company(name="BYD", ticker="BYDDY", country_code="CN"))
    object.__setattr__(config.collect, "use_search_engines", False)

    stats = run_collection(config, db, run_date="2026-09-12", fetcher=fetcher,
                           ddg_search=lambda q: [], brave_api_key="")
    fetcher.close()

    assert stats.status in {"completed", "partial"}
    assert stats.stored >= 1
    assert stats.harvested >= 1

    articles = ArticleRepository(db).list_for_run(stats.run_id)
    assert articles, "no articles stored"
    for art in articles:
        assert art.title
        assert art.url_original
        assert art.status == "new"

    run = ArticleRepository(db).get_run(stats.run_id)
    assert run is not None
    assert run.status in {"completed", "partial"}
    assert run.period_start
    assert run.period_end


def test_workflow_article_has_language_and_excerpt(db, config):
    rss  = (RSS  / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()
    fetcher = _make_fetcher(_multi_feed_handler({"rss": rss, ".xml": rss}, html))
    SourceRepository(db).add(Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml"))
    object.__setattr__(config.collect, "use_search_engines", False)

    stats = run_collection(config, db, run_date="2026-09-12", fetcher=fetcher,
                           ddg_search=lambda q: [], brave_api_key="")
    fetcher.close()

    articles = ArticleRepository(db).list_for_run(stats.run_id)
    assert articles
    art = articles[0]
    assert art.language == "en"
    assert art.excerpt
    assert art.content_hash


# ─────────────────────────────────────────────────────────────────────────────
# 2. Idempotency — second run on same date stores nothing new
# ─────────────────────────────────────────────────────────────────────────────


def test_workflow_idempotent_second_run(db, config):
    rss  = (RSS  / "nbs_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()
    fetcher = _make_fetcher(_multi_feed_handler({"rss": rss, ".xml": rss}, html))
    SourceRepository(db).add(Source(name="NBS", type="rss", url="https://nbs.test/rss.xml"))
    object.__setattr__(config.collect, "use_search_engines", False)

    first = run_collection(config, db, run_date="2026-09-12", fetcher=fetcher,
                           ddg_search=lambda q: [], brave_api_key="")
    second = run_collection(config, db, run_date="2026-09-12", fetcher=fetcher,
                            ddg_search=lambda q: [], brave_api_key="")
    fetcher.close()

    assert second.stored == 0, "second run must not insert duplicate articles"
    assert ArticleRepository(db).count_for_run(first.run_id) == first.stored


# ─────────────────────────────────────────────────────────────────────────────
# 3. Source isolation — dead feed does not block good feed
# ─────────────────────────────────────────────────────────────────────────────


def test_workflow_dead_source_isolated(db, config):
    nbs_rss = (RSS / "nbs_sample.xml").read_bytes()
    html    = (HTML / "article_full.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "dead" in url:
            return httpx.Response(403, text="forbidden")
        if "nbs" in url or ".xml" in url:
            return httpx.Response(200, content=nbs_rss)
        return httpx.Response(200, content=html,
                              headers={"content-type": "text/html; charset=utf-8"})

    fetcher = _make_fetcher(handler)
    repo = SourceRepository(db)
    repo.add(Source(name="Dead Feed", type="rss", url="https://dead.test/rss.xml"))
    repo.add(Source(name="NBS",       type="rss", url="https://nbs.test/rss.xml"))
    object.__setattr__(config.collect, "use_search_engines", False)

    stats = run_collection(config, db, run_date="2026-09-12", fetcher=fetcher,
                           ddg_search=lambda q: [], brave_api_key="")
    fetcher.close()

    # run should not crash; at least the NBS article comes through
    assert stats.stored >= 1
    assert stats.source_errors >= 1  # dead feed counted as error
    # dead source error_count incremented
    dead = repo.get_by_name("Dead Feed")
    assert dead.error_count >= 1


# ─────────────────────────────────────────────────────────────────────────────
# 4. Date filtering — old articles are not stored
# ─────────────────────────────────────────────────────────────────────────────


def test_workflow_out_of_window_articles_skipped(db, config):
    """hkex_sample.xml has one current and one old article; only the current is stored."""
    rss  = (RSS  / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()
    fetcher = _make_fetcher(_multi_feed_handler({"rss": rss, ".xml": rss}, html))
    SourceRepository(db).add(Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml"))
    object.__setattr__(config.collect, "use_search_engines", False)

    stats = run_collection(config, db, run_date="2026-09-12", fetcher=fetcher,
                           ddg_search=lambda q: [], brave_api_key="")
    fetcher.close()

    articles = ArticleRepository(db).list_for_run(stats.run_id)
    urls = {a.url_original for a in articles}
    # The August article must NOT have been stored
    assert not any("old" in u for u in urls), "old article was stored — date filter failed"


# ─────────────────────────────────────────────────────────────────────────────
# 5. robots.txt — disallowed URLs are skipped and counted
# ─────────────────────────────────────────────────────────────────────────────


def test_workflow_robots_disallowed_skipped(db, config):
    rss  = (RSS / "hkex_sample.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/robots.txt"):
            return httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        if "rss" in str(request.url) or path.endswith(".xml"):
            return httpx.Response(200, content=rss)
        return httpx.Response(200, content=b"<html><body>blocked</body></html>")

    fetcher = _make_fetcher(handler, respect_robots=True)
    SourceRepository(db).add(Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml"))
    object.__setattr__(config.collect, "use_search_engines", False)

    stats = run_collection(config, db, run_date="2026-09-12", fetcher=fetcher,
                           ddg_search=lambda q: [], brave_api_key="")
    fetcher.close()

    # All pages disallowed → nothing stored, robots counter incremented
    assert stats.stored == 0
    assert stats.skipped_robots >= 1


# ─────────────────────────────────────────────────────────────────────────────
# 6. Empty run — no sources and no companies → completes cleanly
# ─────────────────────────────────────────────────────────────────────────────


def test_workflow_empty_run_completes(db, config):
    object.__setattr__(config.collect, "use_search_engines", False)
    fetcher = _make_fetcher(lambda req: httpx.Response(200, content=b""))

    stats = run_collection(config, db, run_date="2026-09-12", fetcher=fetcher,
                           ddg_search=lambda q: [], brave_api_key="")
    fetcher.close()

    assert stats.status == "completed"
    assert stats.stored == 0
    assert stats.harvested == 0
    run = ArticleRepository(db).get_run(stats.run_id)
    assert run is not None


# ─────────────────────────────────────────────────────────────────────────────
# 7. Multiple RSS sources — candidates from all are harvested
# ─────────────────────────────────────────────────────────────────────────────


def test_workflow_multiple_sources_all_harvested(db, config):
    hkex = (RSS / "hkex_sample.xml").read_bytes()
    nbs  = (RSS / "nbs_sample.xml").read_bytes()
    pboc = (RSS / "pboC_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()

    feeds = {"hkex": hkex, "nbs": nbs, "pboc": pboc}
    fetcher = _make_fetcher(_multi_feed_handler(feeds, html))

    repo = SourceRepository(db)
    repo.add(Source(name="HKEX", type="rss", url="https://hkex.test/rss.xml"))
    repo.add(Source(name="NBS",  type="rss", url="https://nbs.test/rss.xml"))
    repo.add(Source(name="PBoC", type="rss", url="https://pboc.test/rss.xml"))
    object.__setattr__(config.collect, "use_search_engines", False)

    stats = run_collection(config, db, run_date="2026-09-12", fetcher=fetcher,
                           ddg_search=lambda q: [], brave_api_key="")
    fetcher.close()

    # Each fixture has 1 current item → at least 3 stored (the hkex old one is filtered)
    assert stats.stored >= 3


# ─────────────────────────────────────────────────────────────────────────────
# 8. Paywall articles — stored with metadata_only content
# ─────────────────────────────────────────────────────────────────────────────


def test_workflow_paywall_article_stored_as_metadata(db, config):
    rss     = (RSS  / "hkex_sample.xml").read_bytes()
    paywall = (HTML / "article_paywall.html").read_bytes()
    fetcher = _make_fetcher(_multi_feed_handler({"rss": rss, ".xml": rss}, paywall))
    SourceRepository(db).add(Source(name="HKEX", type="rss", url="https://hkex.test/feed.xml"))
    object.__setattr__(config.collect, "use_search_engines", False)

    stats = run_collection(config, db, run_date="2026-09-12", fetcher=fetcher,
                           ddg_search=lambda q: [], brave_api_key="")
    fetcher.close()

    articles = ArticleRepository(db).list_for_run(stats.run_id)
    assert articles
    # Paywall pages have short/no content, but should still be stored
    for art in articles:
        assert art.title  # title must always be present


# ─────────────────────────────────────────────────────────────────────────────
# 9. Chinese articles — detected and stored
# ─────────────────────────────────────────────────────────────────────────────


def test_workflow_chinese_article_stored(db, config):
    rss  = (RSS  / "nbs_sample.xml").read_bytes()
    html = (HTML / "article_chinese.html").read_bytes()
    fetcher = _make_fetcher(_multi_feed_handler({"rss": rss, ".xml": rss}, html))
    SourceRepository(db).add(Source(name="NBS", type="rss", url="https://nbs.test/rss.xml"))
    CompanyRepository(db).add(Company(name="BYD", ticker="BYDDY", country_code="CN"))
    object.__setattr__(config.collect, "use_search_engines", False)

    stats = run_collection(config, db, run_date="2026-09-12", fetcher=fetcher,
                           ddg_search=lambda q: [], brave_api_key="")
    fetcher.close()

    articles = ArticleRepository(db).list_for_run(stats.run_id)
    assert articles
    langs = {a.language for a in articles if a.language}
    assert "zh" in langs


# ─────────────────────────────────────────────────────────────────────────────
# 10. CLI `invest-watch run` — exit code and printed stats
# ─────────────────────────────────────────────────────────────────────────────


def test_cli_run_command_exits_ok_and_prints_stats(tmp_path, capsys):
    """CLI ``run`` wires config → seed → collection and exits 0 on success."""
    cfg_content = """\
[app]
timezone = "Europe/Paris"

[storage]
db_path = "data/invest.db"

[llm]
enabled = false
"""
    conf_path = make_config(tmp_path, cfg_content)

    rss  = (RSS  / "hkex_sample.xml").read_bytes()
    html = (HTML / "article_full.html").read_bytes()

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
        if "rss" in str(req.url) or str(req.url).endswith(".xml"):
            return httpx.Response(200, content=rss)
        return httpx.Response(200, content=html,
                              headers={"content-type": "text/html; charset=utf-8"})

    import unittest.mock as mock
    from invest_watch import fetcher as fetcher_mod

    original_init = Fetcher.__init__

    def patched_init(self, user_agent, **kwargs):
        kwargs.setdefault("respect_robots", False)
        kwargs.setdefault("rate_limit_requests_per_sec", 100)
        client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
        kwargs["client"] = client
        original_init(self, user_agent, **kwargs)

    with mock.patch.object(Fetcher, "__init__", patched_init):
        rc = main(["--config", str(conf_path), "run", "--date", "2026-09-12"])

    out = capsys.readouterr().out
    assert rc == EXIT_OK
    assert "run #" in out
    assert "status=" in out
    assert "harvested=" in out


# ─────────────────────────────────────────────────────────────────────────────
# 11. Finnhub — intégration dans le pipeline de collecte
# ─────────────────────────────────────────────────────────────────────────────


def test_workflow_finnhub_articles_stored(db, config):
    """Articles from Finnhub API are fetched and stored in the database."""
    import json as _json

    finnhub_payload = [
        {
            "headline": "BYD Q3 EV deliveries hit record",
            "url": "https://example.invalid/finnhub/byd-q3",
            "datetime": 1789113600,   # 2026-09-11 08:00 UTC (D-1 for run_date=2026-09-12)
            "source": "Reuters",
            "summary": "BYD delivered a record number of EVs in Q3.",
            "category": "company news",
            "related": "BYDDY",
        }
    ]
    article_html = (HTML / "article_full.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "finnhub.io" in url and "company-news" in url:
            return httpx.Response(
                200,
                content=_json.dumps(finnhub_payload).encode(),
                headers={"content-type": "application/json"},
            )
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, content=article_html,
                              headers={"content-type": "text/html; charset=utf-8"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client,
                      respect_robots=False, rate_limit_requests_per_sec=100)

    repo = SourceRepository(db)
    repo.add(Source(
        name="Finnhub Company News",
        type="api",
        url="https://finnhub.io/api/v1/company-news",
        priority=2,
    ))
    CompanyRepository(db).add(Company(name="BYD", ticker="BYDDY", country_code="CN"))
    object.__setattr__(config.collect, "use_search_engines", True)
    object.__setattr__(config.collect, "finnhub_api_key", "test-key")

    stats = run_collection(
        config, db,
        run_date="2026-09-12",
        fetcher=fetcher,
        ddg_search=lambda q: [],
        brave_api_key="",
        finnhub_api_key="test-key",
    )
    fetcher.close()

    articles = ArticleRepository(db).list_for_run(stats.run_id)
    finnhub_articles = [
        a for a in articles
        if "byd-q3" in (a.url_original or "")
    ]
    assert finnhub_articles, "no Finnhub article stored"
    # The URL must come from Finnhub; the title may be overridden by HTML extraction
    assert "byd-q3" in finnhub_articles[0].url_original
    assert finnhub_articles[0].source_id is not None  # linked to the Finnhub source
