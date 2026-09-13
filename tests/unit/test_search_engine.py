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
    import json as _json

    payload = {
        "web": {
            "results": [
                {"url": "https://brave.test/article", "title": "BYD Brave result", "page_age": "2026-09-11"},
            ]
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
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
    queries = [SearchQuery(text="BYD", entity_type="company", entity_id=1)]
    candidates = engine.collect_candidates(queries, [brave_source], period)

    assert any(c.origin == "brave" for c in candidates)
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
    queries = [SearchQuery(text="BYD", entity_type="company", entity_id=1)]
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
    queries = [SearchQuery(text="BYDDY", entity_type="company", entity_id=1)]
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
    queries = [SearchQuery(text="BYDDY", entity_type="company", entity_id=1)]
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
    queries = [SearchQuery(text="BYDDY", entity_type="company", entity_id=1)]
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
    queries = [SearchQuery(text="BYDDY", entity_type="company", entity_id=1)]
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
        SearchQuery(text="BYDDY", entity_type="company", entity_id=1),
        SearchQuery(text="TSLA",  entity_type="company", entity_id=2),
        SearchQuery(text="NIO",   entity_type="company", entity_id=3),
    ]
    engine.collect_candidates(queries, [source], period)
    assert engine.engine_queries_used == 2   # cap = 2, third query not executed
    fetcher.close()

