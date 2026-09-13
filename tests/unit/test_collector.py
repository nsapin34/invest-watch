"""End-to-end Step 2 collection: mocked RSS + HTML → articles in SQLite."""

from pathlib import Path

import httpx

from invest_watch.article_repository import ArticleRepository
from invest_watch.collector import run_collection
from invest_watch.company_repository import Company, CompanyRepository
from invest_watch.fetcher import Fetcher
from invest_watch.source_repository import Source, SourceRepository

HTML = Path(__file__).resolve().parent.parent / "fixtures" / "html"
RSS = Path(__file__).resolve().parent.parent / "fixtures" / "rss"


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

    stats = run_collection(
        config,
        db,
        run_date="2026-09-12",
        fetcher=fetcher,
        ddg_search=lambda q: [],
        brave_api_key="",
    )
    assert stats.stored >= 1
    assert stats.status in {"completed", "partial"}
    articles = ArticleRepository(db).list_for_run(stats.run_id)
    assert articles
    assert all(a.status == "new" for a in articles)
    assert any("BYD" in a.title or "byd" in a.url_original.lower() for a in articles)
    run = ArticleRepository(db).get_run(stats.run_id)
    assert run is not None
    assert run.period_start
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
    first = run_collection(config, db, run_date="2026-09-12", fetcher=fetcher, ddg_search=lambda q: [], brave_api_key="")
    second = run_collection(config, db, run_date="2026-09-12", fetcher=fetcher, ddg_search=lambda q: [], brave_api_key="")
    assert second.stored == 0
    assert ArticleRepository(db).count_for_run(first.run_id) == first.stored
    fetcher.close()
