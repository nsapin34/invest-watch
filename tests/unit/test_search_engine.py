"""Search engine: query generation, RSS harvest, cap, engine toggle."""

from pathlib import Path

import httpx

from invest_watch.company_repository import Company
from invest_watch.config_manager import CollectSection
from invest_watch.date_detector import compute_period
from invest_watch.fetcher import Fetcher
from invest_watch.market_repository import Market
from invest_watch.search_engine import SearchEngine, generate_queries, sanitise_query_term
from invest_watch.source_repository import Source, SourceRepository

RSS = Path(__file__).resolve().parent.parent / "fixtures" / "rss"


def _collect(**overrides) -> CollectSection:
    data = dict(
        period_tolerance_hours=6,
        min_content_chars=200,
        max_response_bytes=2_000_000,
        rate_limit_requests_per_sec=100.0,
        max_queries_per_run=40,
        queries_per_company=3,
        queries_per_market=2,
        fetch_user_agent="invest-watch/1.0",
        respect_robots=False,
        use_search_engines=False,
    )
    data.update(overrides)
    return CollectSection(**data)


def test_sanitise_strips_reserved_characters():
    assert '"' not in sanitise_query_term('BYD "EV" (news)')
    assert sanitise_query_term("  BYD   Company ") == "BYD Company"


def test_generate_queries_company_and_market_caps():
    company = Company(name="BYD", ticker="BYDDY", country_code="CN", id=1)
    industry = Market(name="Electric Vehicles", type="industry", id=10)
    market = Market(name="China A", type="market", id=11)
    queries = generate_queries(
        [(company, ["比亚迪", "BYD Company"], [industry])],
        [market],
        queries_per_company=3,
        queries_per_market=2,
        max_queries=40,
    )
    texts = [q.text for q in queries]
    assert "BYD" in texts
    assert "BYDDY" in texts
    assert any("Electric Vehicles" in t for t in texts) or "比亚迪" in texts
    company_q = [q for q in queries if q.entity_type == "company"]
    market_q = [q for q in queries if q.entity_type == "market"]
    assert len(company_q) <= 3
    assert len(market_q) <= 2


def test_generate_queries_respects_global_cap():
    companies = []
    for i in range(20):
        companies.append((Company(name=f"Co{i}", ticker=f"T{i}", id=i + 1), [], []))
    queries = generate_queries(companies, [], max_queries=10)
    assert len(queries) == 10


def test_rss_harvest_filters_old_items(db):
    xml = (RSS / "hkex_sample.xml").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=xml)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False, rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    source = repo.add(Source(name="HKEX", type="rss", url="https://hkex.test/rss.xml", priority=2))
    engine = SearchEngine(_collect(), fetcher, repo, ddg_search=lambda q: [])
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    candidates = engine.collect_candidates([], [source], period)
    urls = {c.url for c in candidates}
    assert "https://www.example.invalid/hkex/byd-results" in urls
    assert "https://www.example.invalid/hkex/old" not in urls
    fetcher.close()


def test_dead_feed_is_isolated(db):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="no")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False, rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    dead = repo.add(Source(name="Dead", type="rss", url="https://dead.test/rss.xml"))
    ok_xml = (RSS / "nbs_sample.xml").read_bytes()

    def mixed(request: httpx.Request) -> httpx.Response:
        if "dead" in str(request.url):
            return httpx.Response(403, text="no")
        return httpx.Response(200, content=ok_xml)

    fetcher.client = httpx.Client(transport=httpx.MockTransport(mixed), follow_redirects=True)
    live = repo.add(Source(name="NBS", type="rss", url="https://nbs.test/rss.xml"))
    engine = SearchEngine(_collect(), fetcher, repo, ddg_search=lambda q: [])
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    candidates = engine.collect_candidates([], [dead, live], period)
    assert any("cpi" in c.url for c in candidates)
    assert engine.errors
    refreshed = repo.get(dead.id)
    assert refreshed.error_count >= 1
    fetcher.close()


def test_google_news_counts_toward_query_cap(db):
    xml = (RSS / "google_news_sample.xml").read_bytes()
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        hits["n"] += 1
        return httpx.Response(200, content=xml)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False, rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    gnews = repo.add(
        Source(
            name="Google News RSS",
            type="search",
            url="https://news.google.com/rss/search?q={query}&hl={lang}",
        )
    )
    from invest_watch.search_engine import SearchQuery

    engine = SearchEngine(
        _collect(use_search_engines=True, max_queries_per_run=2),
        fetcher,
        repo,
        ddg_search=lambda q: [],
        brave_api_key="",
    )
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    queries = [
        SearchQuery(text="BYD", entity_type="company", entity_id=1),
        SearchQuery(text="Tesla", entity_type="company", entity_id=2),
        SearchQuery(text="NIO", entity_type="company", entity_id=3),
    ]
    engine.collect_candidates(queries, [gnews], period)
    assert engine.engine_queries_used == 2
    assert hits["n"] == 2
    fetcher.close()


# ─────────────────────────────────────────────────────────────────────────────
# Additional coverage: Brave Search, DDG harvest, entry_author, load_search_context
# ─────────────────────────────────────────────────────────────────────────────


def test_brave_harvest_returns_candidates(db):
    """web/search endpoint — payload key is 'web'; freshness=pw in URL."""
    import json as _json

    payload = {
        "web": {
            "results": [
                {"url": "https://brave.test/article", "title": "BYD Brave result", "page_age": "2026-09-11"},
            ]
        }
    }
    captured_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
        captured_urls.append(str(request.url))
        return httpx.Response(200, content=_json.dumps(payload).encode(),
                              headers={"content-type": "application/json"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False,
                      rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    brave_source = repo.add(Source(name="Brave Search", type="search",
                                   url="https://api.search.brave.com/res/v1/web/search"))

    engine = SearchEngine(_collect(use_search_engines=True, max_queries_per_run=5),
                          fetcher, repo, brave_api_key="fake-key")
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = [SearchQuery(text="BYD", entity_type="company", entity_id=1, is_primary=True)]
    candidates = engine.collect_candidates(queries, [brave_source], period)

    assert any(c.origin == "brave" for c in candidates)
    assert any("freshness=pw" in u for u in captured_urls), "freshness=pw missing from Brave URL"
    fetcher.close()


def test_brave_news_endpoint_returns_candidates(db):
    """news/search endpoint — payload key is 'news', not 'web'; freshness=pw in URL."""
    import json as _json

    payload = {
        "news": {
            "results": [
                {"url": "https://brave.test/news-article", "title": "CATL News result", "age": "2026-09-11"},
            ]
        }
    }
    captured_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
        captured_urls.append(str(request.url))
        return httpx.Response(200, content=_json.dumps(payload).encode(),
                              headers={"content-type": "application/json"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False,
                      rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    brave_source = repo.add(Source(name="Brave Search", type="search",
                                   url="https://api.search.brave.com/res/v1/news/search"))

    engine = SearchEngine(_collect(use_search_engines=True, max_queries_per_run=5),
                          fetcher, repo, brave_api_key="fake-key")
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = [SearchQuery(text="CATL", entity_type="company", entity_id=1, is_primary=True)]
    candidates = engine.collect_candidates(queries, [brave_source], period)

    assert any(c.origin == "brave" for c in candidates)
    assert any("freshness=pw" in u for u in captured_urls), "freshness=pw missing from Brave URL"
    fetcher.close()


def test_brave_harvest_no_api_key_skipped(db):
    """Brave source is silently skipped when no API key is configured."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="forbidden")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False,
                      rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    brave_source = repo.add(Source(name="Brave", type="search",
                                   url="https://api.search.brave.com/res/v1/web/search"))

    # use_search_engines=True but no ddg_search stub and no google source
    # so only the brave source is evaluated — and it must be skipped with no API key
    engine = SearchEngine(
        _collect(use_search_engines=True, max_queries_per_run=5),
        fetcher, repo,
        ddg_search=lambda q: [],   # prevent real DDG
        brave_api_key="",
    )
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = [SearchQuery(text="BYD", entity_type="company", entity_id=1, is_primary=True)]
    candidates = engine.collect_candidates(queries, [brave_source], period)
    # Brave skipped — no successful candidates from it; DDG stub returns []
    assert not any(c.origin == "brave" for c in candidates)
    fetcher.close()


def test_ddg_harvest_returns_candidates(db):
    def ddg_stub(query: str) -> list[dict]:
        return [{"href": "https://ddg.test/article", "title": "BYD DDG result",
                 "date": "2026-09-11"}]

    fetcher = Fetcher("invest-watch/1.0", respect_robots=False, rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    ddg_source = repo.add(Source(name="DuckDuckGo", type="search", url=""))

    engine = SearchEngine(_collect(use_search_engines=True, max_queries_per_run=5),
                          fetcher, repo, ddg_search=ddg_stub)
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = [SearchQuery(text="BYD", entity_type="company", entity_id=1)]
    candidates = engine.collect_candidates(queries, [ddg_source], period)

    assert any(c.origin == "ddg" for c in candidates)
    fetcher.close()


def test_ddg_harvest_error_isolated(db):
    """DDG failure is isolated — run does not crash and error is recorded."""

    def ddg_stub(query: str) -> list[dict]:
        raise RuntimeError("network down")

    fetcher = Fetcher("invest-watch/1.0", respect_robots=False, rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    ddg_source = repo.add(Source(name="DuckDuckGo", type="search", url="ddg"))

    engine = SearchEngine(
        _collect(use_search_engines=True, max_queries_per_run=5),
        fetcher, repo,
        ddg_search=ddg_stub,
        brave_api_key="",  # no brave key, so brave block is skipped
    )
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = [SearchQuery(text="BYD", entity_type="company", entity_id=1)]
    # collect_candidates must not raise; DDG error must be recorded
    candidates = engine.collect_candidates(queries, [ddg_source], period)
    # Google News fallback always runs; only assert that the DDG error was captured
    assert engine.errors
    assert any("DuckDuckGo" in e for e in engine.errors)
    fetcher.close()


def test_load_search_context(db):
    from invest_watch.company_repository import Company, CompanyRepository
    from invest_watch.market_repository import MarketRepository
    from invest_watch.search_engine import load_search_context

    market_repo = MarketRepository(db)
    company_repo = CompanyRepository(db)
    market = market_repo.add("Electric Vehicles", "industry")
    company = company_repo.add(Company(name="BYD", ticker="BYDDY", country_code="CN"))
    company_repo.attach_market(company.id, market.id)
    company_repo.add_alias(company.id, "比亚迪")

    packed, markets = load_search_context(company_repo, market_repo)
    assert any(c.name == "BYD" for c, _, _ in packed)
    assert any(m.name == "Electric Vehicles" for m in markets)

# ─────────────────────────────────────────────────────────────────────────────
# Finnhub harvest tests
# ─────────────────────────────────────────────────────────────────────────────


def test_finnhub_harvest_returns_candidates(db):
    """Finnhub JSON response is converted to Candidate objects."""
    import json as _json

    payload = [
        {
            "headline": "BYD Annual Results Beat Estimates",
            "url": "https://example.invalid/byd-results",
            "datetime": 1789113600,   # 2026-09-11 08:00 UTC (D-1 for run_date=2026-09-12)
            "source": "Reuters",
            "summary": "BYD posted strong results.",
            "category": "company news",
            "related": "BYDDY",
        }
    ]
    empty_rss = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "finnhub.io" in url:
            # Validate Finnhub-specific params
            assert "symbol=BYDDY" in url
            assert "token=test-key" in url
            return httpx.Response(200, content=_json.dumps(payload).encode(),
                                  headers={"content-type": "application/json"})
        # Google News RSS fallback — return empty feed
        return httpx.Response(200, content=empty_rss,
                              headers={"content-type": "application/rss+xml"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False,
                      rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    source = repo.add(Source(name="Finnhub Company News", type="api",
                             url="https://finnhub.io/api/v1/company-news"))

    engine = SearchEngine(
        _collect(use_search_engines=True, max_queries_per_run=5),
        fetcher, repo,
        ddg_search=lambda q: [],
        brave_api_key="",
        finnhub_api_key="test-key",
    )
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = [
        SearchQuery(text="BYDDY", entity_type="company", entity_id=1,
                    finnhub_symbol="BYDDY")
    ]
    candidates = engine.collect_candidates(queries, [source], period)

    finnhub_candidates = [c for c in candidates if c.origin == "finnhub"]
    assert finnhub_candidates, "no Finnhub candidates returned"
    c = finnhub_candidates[0]
    assert c.title == "BYD Annual Results Beat Estimates"
    assert c.url == "https://example.invalid/byd-results"
    assert c.author == "Reuters"
    assert c.date_str is not None
    assert "2026" in c.date_str
    fetcher.close()


def test_finnhub_harvest_no_api_key_skipped(db):
    """Finnhub is silently skipped when FINNHUB_API_KEY is absent."""
    empty_rss = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'

    def handler(request: httpx.Request) -> httpx.Response:
        if "finnhub.io" in str(request.url):
            raise AssertionError("Finnhub must not be called without API key")
        return httpx.Response(200, content=empty_rss)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False,
                      rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    source = repo.add(Source(name="Finnhub Company News", type="api",
                             url="https://finnhub.io/api/v1/company-news"))

    engine = SearchEngine(
        _collect(use_search_engines=True, max_queries_per_run=5),
        fetcher, repo,
        ddg_search=lambda q: [],
        brave_api_key="",
        finnhub_api_key="",   # no key
    )
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = [
        SearchQuery(text="BYDDY", entity_type="company", entity_id=1,
                    finnhub_symbol="BYDDY")
    ]
    candidates = engine.collect_candidates(queries, [source], period)
    assert not any(c.origin == "finnhub" for c in candidates)
    fetcher.close()


def test_finnhub_harvest_http_error_isolated(db):
    """HTTP error from Finnhub is isolated: error recorded, no exception raised."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text='{"error":"Invalid API key"}')

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False,
                      rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    source = repo.add(Source(name="Finnhub Company News", type="api",
                             url="https://finnhub.io/api/v1/company-news"))

    engine = SearchEngine(
        _collect(use_search_engines=True, max_queries_per_run=5),
        fetcher, repo,
        ddg_search=lambda q: [],
        brave_api_key="",
        finnhub_api_key="bad-key",
    )
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = [
        SearchQuery(text="BYDDY", entity_type="company", entity_id=1,
                    finnhub_symbol="BYDDY")
    ]
    candidates = engine.collect_candidates(queries, [source], period)
    assert not any(c.origin == "finnhub" for c in candidates)
    assert engine.errors
    assert any("Finnhub" in e for e in engine.errors)
    fetcher.close()


def test_finnhub_harvest_empty_response(db):
    """Empty array from Finnhub (no news for the period) returns no candidates."""
    import json as _json
    empty_rss = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'

    def handler(request: httpx.Request) -> httpx.Response:
        if "finnhub.io" in str(request.url):
            return httpx.Response(200, content=_json.dumps([]).encode(),
                                  headers={"content-type": "application/json"})
        return httpx.Response(200, content=empty_rss)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False,
                      rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    source = repo.add(Source(name="Finnhub Company News", type="api",
                             url="https://finnhub.io/api/v1/company-news"))

    engine = SearchEngine(
        _collect(use_search_engines=True, max_queries_per_run=5),
        fetcher, repo,
        ddg_search=lambda q: [],
        brave_api_key="",
        finnhub_api_key="test-key",
    )
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = [
        SearchQuery(text="BYDDY", entity_type="company", entity_id=1,
                    finnhub_symbol="BYDDY")
    ]
    candidates = engine.collect_candidates(queries, [source], period)
    assert not any(c.origin == "finnhub" for c in candidates)
    assert not engine.errors
    fetcher.close()


def test_finnhub_counts_toward_query_cap(db):
    """Each Finnhub company-news call consumes one slot from the query cap."""
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_json.dumps([]).encode())

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False,
                      rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    source = repo.add(Source(name="Finnhub Company News", type="api",
                             url="https://finnhub.io/api/v1/company-news"))

    engine = SearchEngine(
        _collect(use_search_engines=True, max_queries_per_run=2),
        fetcher, repo,
        ddg_search=lambda q: [],
        brave_api_key="",
        finnhub_api_key="test-key",
    )
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = [
        SearchQuery(text="BYDDY", entity_type="company", entity_id=1,
                    finnhub_symbol="BYDDY"),
        SearchQuery(text="TSLA",  entity_type="company", entity_id=2,
                    finnhub_symbol="TSLA"),
        SearchQuery(text="NIO",   entity_type="company", entity_id=3,
                    finnhub_symbol="NIO"),
    ]
    engine.collect_candidates(queries, [source], period)
    assert engine.engine_queries_used == 2   # cap = 2, third query not executed
    fetcher.close()


# ─────────────────────────────────────────────────────────────────────────────
# Engines are opt-in: no silent fallback (regression: Google News used to run
# unconditionally and burn the whole query budget on robots-blocked URLs)
# ─────────────────────────────────────────────────────────────────────────────


def test_no_engine_without_configured_search_or_api_sources(db):
    """With no active search/api source, no engine runs and no queries are spent."""
    fetcher = Fetcher("invest-watch/1.0", respect_robots=False, rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    engine = SearchEngine(
        _collect(use_search_engines=True, max_queries_per_run=40),
        fetcher, repo,
        ddg_search=lambda q: [{"href": "https://ddg.test/article", "title": "BYD DDG result"}],
        brave_api_key="real-key",   # a key alone must NOT start an engine
        finnhub_api_key="real-key",
    )
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = [SearchQuery(text="BYD", entity_type="company", entity_id=1)]
    candidates = engine.collect_candidates(queries, [], period)
    assert engine.engine_queries_used == 0
    assert candidates == []
    fetcher.close()


def test_deactivated_google_source_is_fully_ignored(db):
    """A google-news source with active=False must not be harvested at all."""
    fetcher = Fetcher("invest-watch/1.0", respect_robots=False, rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    inactive = repo.add(Source(name="google-news-rss", type="search", active=False))
    engine = SearchEngine(
        _collect(use_search_engines=True, max_queries_per_run=40),
        fetcher, repo,
        ddg_search=lambda q: [],
    )
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = [SearchQuery(text="BYD", entity_type="company", entity_id=1)]
    candidates = engine.collect_candidates(queries, [inactive], period)
    assert engine.engine_queries_used == 0
    assert candidates == []
    fetcher.close()


def test_finnhub_runs_before_google_when_both_configured(db):
    """Budget priority: the Finnhub API (real article URLs) precedes Google News."""
    import json as _json

    def handler(request: httpx.Request) -> httpx.Response:
        if "finnhub.io" in str(request.url):
            return httpx.Response(200, content=_json.dumps([]).encode(),
                                  headers={"content-type": "application/json"})
        return httpx.Response(200, content=b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>')

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False,
                      rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    finnhub = repo.add(Source(name="Finnhub Company News", type="api",
                              url="https://finnhub.io/api/v1/company-news"))
    google = repo.add(Source(name="google-news-rss", type="search", active=True,
                             url="https://news.google.com/rss/search?q={query}&hl={lang}"))
    engine = SearchEngine(
        _collect(use_search_engines=True, max_queries_per_run=2),
        fetcher, repo,
        ddg_search=lambda q: [],
        brave_api_key="",
        finnhub_api_key="test-key",
    )
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = [
        SearchQuery(text="BYDDY", entity_type="company", entity_id=1,
                    finnhub_symbol="BYDDY"),
        SearchQuery(text="TSLA",  entity_type="company", entity_id=2,
                    finnhub_symbol="TSLA"),
        SearchQuery(text="NIO",   entity_type="company", entity_id=3,
                    finnhub_symbol="NIO"),
    ]
    candidates = engine.collect_candidates(queries, [finnhub, google], period)
    # Finnhub consumed the whole budget (2/2); Google News ran zero queries.
    assert engine.engine_queries_used == 2
    assert not any(c.origin == "google_news" for c in candidates)
    fetcher.close()


# ─────────────────────────────────────────────────────────────────────────────
# Curated Finnhub symbols (P1): `symbol=` must be an exchange symbol, never the
# free-text search query.  Measured on the live API (2026-09-20):
#   symbol=SpaceX        → 403 "You don't have access to this resource"
#   symbol=Air Liquide   → 200 + [] (a wasted query slot)
#   symbol=AI            → 200 but C3.ai headlines (wrong company!)
#   symbol=AIQUY         → 200 with the real Air Liquide news
# ─────────────────────────────────────────────────────────────────────────────


def _symbol_engine(db, handler, *, max_queries_per_run=40):
    """Build a SearchEngine with one active Finnhub source and a mock transport."""
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False,
                      rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    source = repo.add(Source(name="Finnhub Company News", type="api",
                             url="https://finnhub.io/api/v1/company-news"))
    engine = SearchEngine(
        _collect(use_search_engines=True, max_queries_per_run=max_queries_per_run),
        fetcher, repo,
        ddg_search=lambda q: [],
        brave_api_key="",
        finnhub_api_key="test-key",
    )
    return engine, fetcher, source, repo


def test_generate_queries_carries_curated_finnhub_symbol():
    """The curated symbol rides on the company's first query; markets carry none."""
    company = Company(name="Air Liquide", ticker="AI", country_code="FR",
                      finnhub_symbol="AIQUY", id=1)
    market = Market(name="Industrial Gases", type="industry", id=10)
    queries = generate_queries([(company, ["AIQUY"], [market])], [market], max_queries=40)

    company_q = [q for q in queries if q.entity_type == "company"]
    assert company_q, "no company query generated"
    assert company_q[0].finnhub_symbol == "AIQUY"
    assert all(q.finnhub_symbol is None for q in company_q[1:])
    assert all(q.finnhub_symbol is None for q in queries if q.entity_type == "market")


def test_finnhub_uses_curated_symbol_not_free_text(db):
    """The request carries `symbol=AIQUY` — never the display ticker or the name."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "finnhub.io" in url:
            seen.append(url)
            return httpx.Response(200, content=b"[]",
                                  headers={"content-type": "application/json"})
        return httpx.Response(200, content=b'<?xml version="1.0"?><rss version="2.0"></rss>')

    engine, fetcher, source, _ = _symbol_engine(db, handler)
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = [
        SearchQuery(text="Air Liquide", entity_type="company", entity_id=1,
                    finnhub_symbol="AIQUY"),
        SearchQuery(text="AI", entity_type="company", entity_id=1),
        SearchQuery(text="Industrial Gases news", entity_type="market", entity_id=10),
    ]
    engine.collect_candidates(queries, [source], period)

    assert len(seen) == 1, f"expected exactly one Finnhub call, got {seen}"
    assert "symbol=AIQUY" in seen[0]
    assert "symbol=Air" not in seen[0]        # no free-text symbol
    assert "symbol=AI&" not in seen[0]        # neither the display ticker
    assert "Industrial" not in seen[0]        # nor a market query
    fetcher.close()


def test_finnhub_skips_company_without_symbol(db):
    """No curated symbol → no request at all (the budget goes to other engines)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "finnhub.io" in str(request.url):
            raise AssertionError("Finnhub must not be called without finnhub_symbol")
        return httpx.Response(200, content=b'<?xml version="1.0"?><rss version="2.0"></rss>')

    engine, fetcher, source, _ = _symbol_engine(db, handler)
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = [
        SearchQuery(text="CATL", entity_type="company", entity_id=1),
        SearchQuery(text="EV Batteries news", entity_type="market", entity_id=10),
    ]
    candidates = engine.collect_candidates(queries, [source], period)

    assert candidates == []
    assert engine.engine_queries_used == 0     # nothing was spent
    assert not engine.error_records
    fetcher.close()


def test_finnhub_symbol_queried_once_however_many_queries(db):
    """Three queries for the same company → exactly one API call."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "finnhub.io" in str(request.url):
            calls.append(str(request.url))
            return httpx.Response(200, content=b"[]",
                                  headers={"content-type": "application/json"})
        return httpx.Response(200, content=b'<?xml version="1.0"?><rss version="2.0"></rss>')

    engine, fetcher, source, _ = _symbol_engine(db, handler)
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = [
        SearchQuery(text="SpaceX", entity_type="company", entity_id=1,
                    finnhub_symbol="SPCX"),
        SearchQuery(text="SPCX", entity_type="company", entity_id=1),
        SearchQuery(text="Aerospace & Defense news", entity_type="company", entity_id=1),
    ]
    engine.collect_candidates(queries, [source], period)

    assert len(calls) == 1
    assert engine.engine_queries_used == 1
    fetcher.close()



# ─────────────────────────────────────────────────────────────────────────────
# ST3: Brave cap global + calcul auto market_cap
# ST4: is_primary flag + generate_queries
# ─────────────────────────────────────────────────────────────────────────────


def test_generate_queries_marks_first_company_query_as_primary():
    """Exactly 1 query per company has is_primary=True — always the company name."""
    companies = [
        (Company(name="CATL", ticker="300750", country_code="CN", id=1), [], []),
        (Company(name="SpaceX", ticker=None, country_code="US", id=2), [], []),
    ]
    queries = generate_queries(companies, [], queries_per_company=3, max_queries=40)
    for company_id in (1, 2):
        company_qs = [q for q in queries if q.entity_type == "company" and q.entity_id == company_id]
        primary_qs = [q for q in company_qs if q.is_primary]
        assert len(primary_qs) == 1, f"company {company_id}: expected 1 primary, got {len(primary_qs)}"
        assert primary_qs[0] == company_qs[0], "primary must be the first query (company.name)"


def test_generate_queries_market_queries_not_primary():
    """Market queries must never be is_primary."""
    market = Market(name="Copper", type="market", id=5)
    queries = generate_queries([], [market], max_queries=40)
    market_qs = [q for q in queries if q.entity_type == "market"]
    assert market_qs
    assert not any(q.is_primary for q in market_qs)


def _brave_engine(db, handler, *, cap=23):
    """Build a SearchEngine with one active Brave source and a mock transport."""
    import json as _json
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False,
                      rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    source = repo.add(Source(name="Brave Search", type="search",
                             url="https://api.search.brave.com/res/v1/web/search"))
    engine = SearchEngine(
        _collect(use_search_engines=True, max_queries_per_run=100, max_brave_queries_per_run=cap),
        fetcher, repo,
        ddg_search=lambda q: [],
        brave_api_key="fake-key",
    )
    return engine, fetcher, source


def test_brave_market_cap_auto_adjusted_11_companies(db):
    """11 companies → market_cap = 23-11 = 12 → 12 market queries are sent."""
    import json as _json
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
        calls.append(str(request.url))
        return httpx.Response(200, content=_json.dumps({"web": {"results": []}}).encode(),
                              headers={"content-type": "application/json"})

    engine, fetcher, source = _brave_engine(db, handler, cap=23)
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = (
        [SearchQuery(text=f"Co{i}", entity_type="company", entity_id=i, is_primary=True) for i in range(11)]
        + [SearchQuery(text=f"Mkt{i} news", entity_type="market", entity_id=100 + i) for i in range(15)]
    )
    engine.collect_candidates(queries, [source], period)
    assert len(calls) == 23  # 11 companies + 12 markets
    fetcher.close()


def test_brave_market_cap_auto_adjusted_20_companies(db):
    """20 companies → market_cap = 23-20 = 3 → only 3 market queries are sent."""
    import json as _json
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
        calls.append(str(request.url))
        return httpx.Response(200, content=_json.dumps({"web": {"results": []}}).encode(),
                              headers={"content-type": "application/json"})

    engine, fetcher, source = _brave_engine(db, handler, cap=23)
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = (
        [SearchQuery(text=f"Co{i}", entity_type="company", entity_id=i, is_primary=True) for i in range(20)]
        + [SearchQuery(text=f"Mkt{i} news", entity_type="market", entity_id=100 + i) for i in range(15)]
    )
    engine.collect_candidates(queries, [source], period)
    assert len(calls) == 23  # 20 companies + 3 markets
    fetcher.close()


def test_brave_market_cap_zero_at_23_companies(db, caplog):
    """23 companies → market_cap = 0 → no market queries sent, no warning."""
    import json as _json
    import logging
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
        calls.append(str(request.url))
        return httpx.Response(200, content=_json.dumps({"web": {"results": []}}).encode(),
                              headers={"content-type": "application/json"})

    engine, fetcher, source = _brave_engine(db, handler, cap=23)
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = (
        [SearchQuery(text=f"Co{i}", entity_type="company", entity_id=i, is_primary=True) for i in range(23)]
        + [SearchQuery(text=f"Mkt{i} news", entity_type="market", entity_id=100 + i) for i in range(5)]
    )
    with caplog.at_level(logging.WARNING, logger="invest_watch.search_engine"):
        engine.collect_candidates(queries, [source], period)
    assert len(calls) == 23  # 23 companies + 0 markets
    assert not any(r.levelno == logging.WARNING for r in caplog.records), \
        "unexpected warning when companies == cap"
    fetcher.close()


def test_brave_warning_when_companies_exceed_cap(db, caplog):
    """25 companies with cap=23 → warning listing 2 skipped companies."""
    import json as _json
    import logging
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
        calls.append(str(request.url))
        return httpx.Response(200, content=_json.dumps({"web": {"results": []}}).encode(),
                              headers={"content-type": "application/json"})

    engine, fetcher, source = _brave_engine(db, handler, cap=23)
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = (
        [SearchQuery(text=f"Co{i}", entity_type="company", entity_id=i, is_primary=True) for i in range(25)]
        + [SearchQuery(text=f"Mkt{i} news", entity_type="market", entity_id=100 + i) for i in range(5)]
    )
    with caplog.at_level(logging.WARNING, logger="invest_watch.search_engine"):
        engine.collect_candidates(queries, [source], period)
    assert len(calls) == 23  # cap enforced
    assert any("25" in r.message and "23" in r.message and "2" in r.message
               for r in caplog.records if r.levelno == logging.WARNING), (
        "expected warning mentioning 25 companies, cap 23, 2 skipped"
    )
    fetcher.close()


def test_brave_only_primary_queries_in_pass1(db):
    """Non-primary company queries (ticker, name+industry) must NOT be sent to Brave."""
    import json as _json
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
        calls.append(str(request.url))
        return httpx.Response(200, content=_json.dumps({"web": {"results": []}}).encode(),
                              headers={"content-type": "application/json"})

    engine, fetcher, source = _brave_engine(db, handler, cap=23)
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    # 1 primary + 2 non-primary company queries + 1 market
    queries = [
        SearchQuery(text="CATL", entity_type="company", entity_id=1, is_primary=True),
        SearchQuery(text="300750", entity_type="company", entity_id=1),   # ticker — not primary
        SearchQuery(text="CATL Battery", entity_type="company", entity_id=1),  # industry — not primary
        SearchQuery(text="EV news", entity_type="market", entity_id=10),
    ]
    engine.collect_candidates(queries, [source], period)
    assert len(calls) == 2  # 1 primary + 1 market
    fetcher.close()


# -- v006: per-company brave_query override ----------------------------------

def test_brave_query_becomes_primary_query():
    from invest_watch.company_repository import Company
    from invest_watch.search_engine import generate_queries
    comp = Company(name="CATL", ticker="3750", id=1, brave_query='"CATL" battery')
    qs = generate_queries([(comp, [], [])], [], queries_per_company=3)
    assert [q.text for q in qs if q.is_primary] == ['"CATL" battery']
    assert "CATL" in [q.text for q in qs]  # name still queried (non-Brave)


def test_no_brave_query_keeps_name_as_primary():
    from invest_watch.company_repository import Company
    from invest_watch.search_engine import generate_queries
    comp = Company(name="Hesai", ticker="HSAI", id=2)
    qs = generate_queries([(comp, [], [])], [], queries_per_company=3)
    assert [q.text for q in qs if q.is_primary] == ["Hesai"]


def test_brave_query_persisted_in_db(db):
    from invest_watch.company_repository import Company, CompanyRepository
    repo = CompanyRepository(db)
    c = repo.add(Company(name="Wasion", brave_query='"Wasion Holdings"'))
    assert repo.get_by_name("Wasion").brave_query == '"Wasion Holdings"'
    repo.update(c.id, brave_query='"Wasion Group"')
    assert repo.get_by_name("Wasion").brave_query == '"Wasion Group"'


def test_config_reads_brave_query(project):
    from invest_watch.config_manager import load_config
    cfg_path = project / "conf" / "config.toml"
    cfg_path.write_text(cfg_path.read_text(encoding="utf-8") + (
        '\n[[companies]]\nname = "Wasion"\nbrave_query = \'"Wasion Holdings"\'\n'
    ), encoding="utf-8")
    cfg = load_config(cfg_path)
    seed = next(s for s in cfg.companies if s.name == "Wasion")
    assert seed.brave_query == '"Wasion Holdings"'


# -- v007: Chinese sources (HKEXnews, East Money) -----------------------------

def _engine_with(handler, db, source):
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False,
                      rate_limit_requests_per_sec=100)
    repo = SourceRepository(db)
    src = repo.add(source)
    engine = SearchEngine(_collect(use_search_engines=True, max_queries_per_run=0),
                          fetcher, repo, ddg_search=lambda q: [], brave_api_key="",
                          finnhub_api_key="")
    return engine, src, fetcher


def test_hkex_harvest_decodes_nested_json(db):
    import json as _json
    from invest_watch.search_engine import SearchQuery
    rows = [{"TITLE": "\u6d77\u5916\u76e3\u7ba1\u516c\u544a", "DATE_TIME": "11/09/2026 19:10",
             "STOCK_NAME": "\u5be7\u5fb7\u6642\u4ee3",
             "FILE_LINK": "/listedco/listconews/sehk/2026/0911/x_c.pdf"}]
    body = _json.dumps({"result": _json.dumps(rows), "recordCnt": 1}).encode()

    def handler(request):
        assert "stockId=1000259940" in str(request.url)
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    engine, src, fetcher = _engine_with(handler, db, Source(
        name="HKEXnews Announcements", type="api",
        url="https://www1.hkexnews.hk/search/titleSearchServlet.do"))
    q = SearchQuery(text="CATL", entity_type="company", entity_id=1, is_primary=True,
                    hkex_stock_id="1000259940", company_name="CATL")
    cands = engine.collect_candidates([q], [src], compute_period("Europe/Paris", run_date="2026-09-12"))
    assert len(cands) == 1
    c = cands[0]
    assert c.origin == "hkex"
    assert c.url == "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0911/x_c.pdf"
    assert c.title.startswith("CATL - ")
    assert c.date_str == "2026-09-11T19:10:00+08:00"
    assert engine.engine_queries_used == 0  # free source: no paid budget used
    fetcher.close()


def test_eastmoney_harvest_strips_jsonp(db):
    import json as _json
    from invest_watch.search_engine import SearchQuery
    payload = {"code": 0, "result": {"cmsArticleWebOld": [
        {"date": "2026-09-11 15:00:00", "title": "<em>\u5b81\u5fb7\u65f6\u4ee3</em>\u53d1\u5e03",
         "url": "http://finance.eastmoney.com/a/1.html", "mediaName": "\u8bc1\u5238\u65f6\u62a5"}]}}
    body = ("x(" + _json.dumps(payload) + ")").encode()

    def handler(request):
        assert "keyword" in str(request.url)
        return httpx.Response(200, content=body, headers={"content-type": "application/javascript"})

    engine, src, fetcher = _engine_with(handler, db, Source(
        name="East Money News Search", type="api",
        url="https://search-api-web.eastmoney.com/search/jsonp"))
    q = SearchQuery(text="CATL", entity_type="company", entity_id=1, is_primary=True,
                    zh_term="\u5b81\u5fb7\u65f6\u4ee3", company_name="CATL")
    cands = engine.collect_candidates([q], [src], compute_period("Europe/Paris", run_date="2026-09-12"))
    assert len(cands) == 1
    assert cands[0].title == "\u5b81\u5fb7\u65f6\u4ee3\u53d1\u5e03"  # <em> stripped
    assert cands[0].date_str == "2026-09-11T15:00:00+08:00"
    assert cands[0].origin == "eastmoney"
    fetcher.close()


def test_eastmoney_bad_payload_fails_cleanly(db):
    from invest_watch.search_engine import SearchQuery

    def handler(request):
        return httpx.Response(200, content=b"x(not json)")

    engine, src, fetcher = _engine_with(handler, db, Source(
        name="East Money News Search", type="api",
        url="https://search-api-web.eastmoney.com/search/jsonp"))
    q = SearchQuery(text="CATL", entity_type="company", entity_id=1, is_primary=True,
                    zh_term="\u5b81\u5fb7\u65f6\u4ee3")
    assert engine.collect_candidates([q], [src], compute_period("Europe/Paris", run_date="2026-09-12")) == []
    assert engine.errors
    fetcher.close()


def test_primary_query_carries_chinese_fields():
    comp = Company(name="CATL", ticker="3750", id=1, hkex_stock_id="1000259940")
    qs = generate_queries([(comp, ["\u5b81\u5fb7\u65f6\u4ee3"], [])], [], queries_per_company=3)
    primary = [q for q in qs if q.is_primary][0]
    assert primary.hkex_stock_id == "1000259940"
    assert primary.zh_term == "\u5b81\u5fb7\u65f6\u4ee3"
    assert primary.company_name == "CATL"
    assert all(q.hkex_stock_id is None for q in qs if not q.is_primary)


# -- v011: topic queries share the Brave cap, before markets, Brave only --------

def test_brave_topic_queries_between_companies_and_markets(db):
    import json as _json
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
        calls.append(str(request.url))
        return httpx.Response(200, content=_json.dumps({"web": {"results": []}}).encode(),
                              headers={"content-type": "application/json"})

    engine, fetcher, source = _brave_engine(db, handler, cap=5)
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = (
        [SearchQuery(text=f"Co{i}", entity_type="company", entity_id=i, is_primary=True) for i in range(2)]
        + [SearchQuery(text=f"Topic{i}", entity_type="topic", entity_id=i) for i in range(2)]
        + [SearchQuery(text=f"Mkt{i} news", entity_type="market", entity_id=100 + i) for i in range(5)]
    )
    engine.collect_candidates(queries, [source], period)
    assert len(calls) == 5  # 2 companies + 2 topics + 1 market
    assert "Topic0" in calls[2] and "Topic1" in calls[3] and "Mkt0" in calls[4]
    fetcher.close()


def test_topic_queries_capped_by_brave_budget(db):
    import json as _json
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=_json.dumps({"web": {"results": []}}).encode(),
                              headers={"content-type": "application/json"})

    engine, fetcher, source = _brave_engine(db, handler, cap=3)
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    from invest_watch.search_engine import SearchQuery
    queries = (
        [SearchQuery(text=f"Co{i}", entity_type="company", entity_id=i, is_primary=True) for i in range(2)]
        + [SearchQuery(text=f"Topic{i}", entity_type="topic", entity_id=i) for i in range(3)]
    )
    engine.collect_candidates(queries, [source], period)
    assert len(calls) == 3
    fetcher.close()


def test_ddg_skips_topic_queries(db):
    seen: list[str] = []
    repo = SourceRepository(db)
    ddg_source = repo.add(Source(name="DuckDuckGo", type="search", url="https://duckduckgo.com"))
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    fetcher = Fetcher("invest-watch/1.0", client=client, respect_robots=False, rate_limit_requests_per_sec=100)
    engine = SearchEngine(_collect(use_search_engines=True, max_queries_per_run=100), fetcher, repo,
                          ddg_search=lambda q: seen.append(q) or [])
    from invest_watch.search_engine import SearchQuery
    period = compute_period("Europe/Paris", run_date="2026-09-12")
    engine.collect_candidates([SearchQuery(text="CATL", entity_type="company", entity_id=1, is_primary=True),
                               SearchQuery(text="battery", entity_type="topic", entity_id=1)],
                              [ddg_source], period)
    assert not any("battery" in q for q in seen)
    fetcher.close()
