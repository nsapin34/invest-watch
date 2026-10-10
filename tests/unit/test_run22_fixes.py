"""Run #22 fixes: Finnhub redirects, cleared symbols, slow hosts, error cap, blocked domains."""

import json
from types import SimpleNamespace

import httpx
import pytest

from invest_watch.collector import (
    _fetch_with_retry, _is_slow_host, _persist_collection_errors, is_finnhub_redirect, run_collection,
)
from invest_watch.company_repository import CompanyRepository
from invest_watch.date_detector import compute_period
from invest_watch.fetcher import FetchResult, Fetcher
from invest_watch.source_repository import Source, SourceRepository


def test_is_finnhub_redirect():
    assert is_finnhub_redirect("https://finnhub.io/api/news?id=abc")
    assert is_finnhub_redirect("https://www.finnhub.io/api/news?id=abc")
    assert not is_finnhub_redirect("https://finance.yahoo.com/news/x.html")
    assert not is_finnhub_redirect("https://finnhub.io/api/v1/company-news?symbol=X")
    assert not is_finnhub_redirect(None)


def test_finnhub_redirect_stored_from_summary_without_fetch(project, monkeypatch):
    from dataclasses import replace
    from invest_watch.article_repository import ArticleRepository
    from invest_watch.bootstrap import open_database
    from invest_watch.config_manager import load_config
    cfg = load_config(project / "conf" / "config.toml")
    db = open_database(cfg, migrate=True)
    period = compute_period("Europe/Paris", run_date="2026-10-10")
    ts = int(period.start_utc.timestamp()) + 3600
    news = [
        {"headline": "Wolfspeed wins SiC deal", "url": "https://finnhub.io/api/news?id=deadbeef",
         "datetime": ts, "source": "Yahoo", "summary": "Wolfspeed signed a silicon carbide supply deal."},
    ]
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        if "company-news" in str(request.url):
            return httpx.Response(200, content=json.dumps(news).encode(), headers={"content-type": "application/json"})
        return httpx.Response(403)

    fetcher = Fetcher("t/1", client=httpx.Client(transport=httpx.MockTransport(handler)), respect_robots=False,
                      rate_limit_requests_per_sec=1000)
    try:
        CompanyRepository(db).add(__import__("invest_watch.company_repository", fromlist=["Company"]).Company(
            name="Wolfspeed", finnhub_symbol="WOLF"))
        SourceRepository(db).add(Source(name="Finnhub Company News", type="api",
                                        url="https://finnhub.io/api/v1/company-news"))
        run = ArticleRepository(db).create_run("2026-10-10", period.start_iso, period.end_iso)
        cfg2 = replace(cfg, collect=replace(cfg.collect, use_search_engines=True))
        stats = run_collection(cfg2, db, run_id=run.id, period=period, fetcher=fetcher,
                               finnhub_api_key="k", translator=None, ddg_search=lambda q: [])
        assert not any("api/news?id=" in u for u in fetched)
        assert stats.fetch_errors == 0 and stats.finnhub_summary_only == 1 and stats.stored == 1
        row = db.query_one("SELECT title, content, author FROM article")
        assert row["title"] == "Wolfspeed wins SiC deal" and "silicon carbide" in row["content"]
        assert row["author"] == "Yahoo"
    finally:
        fetcher.close()
        db.close()


def test_persist_errors_unlimited_when_cap_zero(db):
    recs = [{"step": "fetch", "error_type": "http_403", "message": f"u{i}"} for i in range(60)]
    assert _persist_collection_errors(db, None, recs, cap=0) == (60, 0)
    assert _persist_collection_errors(db, None, recs, cap=10) == (10, 50)


def test_slow_host_match():
    assert _is_slow_host("http://stock.eastmoney.com/a/1.html", ("eastmoney.com",))
    assert not _is_slow_host("http://noteastmoney.com/a", ("eastmoney.com",))


class _FakeFetcher:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(kw)
        return self.results.pop(0)


def _cfg(**kw):
    base = dict(slow_hosts=("eastmoney.com",), slow_host_timeout_seconds=45, slow_host_retries=1)
    base.update(kw)
    return SimpleNamespace(collect=SimpleNamespace(**base))


def _res(ok, error=None):
    return FetchResult(url="u", url_final="u", status_code=200 if ok else None, error=error,
                       **({"content": b"x"} if ok else {}))


def test_slow_host_longer_timeout_and_one_retry():
    stats = SimpleNamespace(slow_host_retries=0)
    f = _FakeFetcher([_res(False, "timeout: read"), _res(True)])
    r = _fetch_with_retry(f, "http://finance.eastmoney.com/a/1.html", _cfg(), stats, check_robots=True)
    assert r.ok and len(f.calls) == 2 and stats.slow_host_retries == 1
    assert all(c["timeout_seconds"] == 45 for c in f.calls)
    f = _FakeFetcher([_res(False, "timeout"), _res(False, "timeout")])
    assert not _fetch_with_retry(f, "http://finance.eastmoney.com/a", _cfg(), stats, check_robots=True).ok
    assert len(f.calls) == 2  # only one retry
    f = _FakeFetcher([_res(False, "http_404")])
    _fetch_with_retry(f, "http://finance.eastmoney.com/a", _cfg(), stats, check_robots=True)
    assert len(f.calls) == 1  # no retry on non-timeout


def test_normal_host_default_fetch():
    f = _FakeFetcher([_res(False, "timeout")])
    _fetch_with_retry(f, "https://example.com/a", _cfg(), SimpleNamespace(slow_host_retries=0), check_robots=False)
    assert f.calls == [{"kind": "page", "check_robots": False}]


def test_fetcher_timeout_override():
    seen = {}

    def handler(request):
        seen["t"] = request.extensions.get("timeout")
        return httpx.Response(200, content=b"ok")

    f = Fetcher("t/1", client=httpx.Client(transport=httpx.MockTransport(handler)), respect_robots=False,
                rate_limit_requests_per_sec=1000)
    f.get("https://example.com/", timeout_seconds=45)
    assert seen["t"]["read"] == 45
    f.close()


def test_empty_finnhub_symbol_clears_db(project):
    from invest_watch.bootstrap import open_database, seed_from_config
    from invest_watch.config_manager import load_config
    path = project / "conf" / "config.toml"
    base = path.read_text(encoding="utf-8")
    path.write_text(base + '\n[[companies]]\nname = "CATL"\nfinnhub_symbol = "03750.HK"\n', encoding="utf-8")
    cfg = load_config(path)
    db = open_database(cfg, migrate=True)
    try:
        seed_from_config(db, cfg)
        assert CompanyRepository(db).get_by_name("CATL").finnhub_symbol == "03750.HK"
        path.write_text(base + '\n[[companies]]\nname = "CATL"\nfinnhub_symbol = ""\n', encoding="utf-8")
        seed_from_config(db, load_config(path))
        assert CompanyRepository(db).get_by_name("CATL").finnhub_symbol is None
        path.write_text(base + '\n[[companies]]\nname = "CATL"\n', encoding="utf-8")  # absent: untouched
        CompanyRepository(db).update(CompanyRepository(db).get_by_name("CATL").id, finnhub_symbol="03750.HK")
        seed_from_config(db, load_config(path))
        assert CompanyRepository(db).get_by_name("CATL").finnhub_symbol == "03750.HK"
    finally:
        db.close()


def test_real_config_blocked_domains_and_aliases():
    import pathlib
    from invest_watch.config_manager import load_config
    root = pathlib.Path(__file__).resolve().parents[2]
    path = root / "conf" / "config.toml"
    if not path.exists():
        pytest.skip("no local config.toml")
    cfg = load_config(path)
    for d in ("onsemi.com", "tradingeconomics.com", "coherentmarketinsights.com"):
        assert d in cfg.collect.blocked_domains
    total = [c for c in cfg.companies if c.name == "TotalEnergies"][0]
    assert "Total" not in (total.aliases or ())
    hk = {c.name: c for c in cfg.companies}
    for n in ("CATL", "Wasion", "Zijin Mining"):
        assert hk[n].finnhub_symbol is None and "finnhub_symbol" in hk[n].clear_fields


def test_total_word_no_longer_links_totalenergies(db):
    from invest_watch.classifier import Classifier
    from invest_watch.company_repository import Company
    CompanyRepository(db).add(Company(name="TotalEnergies", ticker="TTE"), aliases=["TotalEnergies SE"])
    comps, *_ = Classifier(db).classify_text("Top 10 SiC Ceramics", "a 30% drop in total losses")
    assert not comps
    comps, *_ = Classifier(db).classify_text("TotalEnergies SE results", "")
    assert comps[0].company.name == "TotalEnergies"
