"""Fetcher: robots.txt cache, size bound, 429/5xx handling, URL normalisation, rate limit."""

from __future__ import annotations

import httpx
import pytest

from invest_watch.fetcher import Fetcher, HostRateLimiter, normalize_url


def test_normalize_url_strips_tracking_and_fragment():
    url = "HTTPS://News.Example.COM:443/a/b?utm_source=x&id=1&fbclid=zz#section"
    assert normalize_url(url) == "https://news.example.com/a/b?id=1"


def test_normalize_url_drops_utm_and_keeps_others():
    assert "utm_campaign" not in normalize_url("https://ex.test/p?q=1&utm_campaign=a&mc_cid=2")
    assert "q=1" in normalize_url("https://ex.test/p?q=1&utm_campaign=a")


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def test_rate_limiter_enforces_interval_with_jitter():
    clock = _Clock()
    limiter = HostRateLimiter(
        0.5,
        jitter=0.0,
        sleep=clock.sleep,
        clock=clock.monotonic,
    )
    assert limiter.wait("a.test") == 0.0
    clock.now += 0.5
    slept = limiter.wait("a.test")
    assert slept == pytest.approx(1.5)
    assert clock.slept == [pytest.approx(1.5)]
    # different host is independent
    assert limiter.wait("b.test") == 0.0


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_robots_forbidden_path_rejected_and_cached():
    calls = {"robots": 0, "page": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/robots.txt"):
            calls["robots"] += 1
            body = "User-agent: invest-watch\nDisallow: /private\nAllow: /\n"
            return httpx.Response(200, text=body)
        calls["page"] += 1
        return httpx.Response(200, text="<html>ok</html>")

    fetcher = Fetcher("invest-watch/1.0 (+test)", client=_client(handler), rate_limit_requests_per_sec=100)
    denied = fetcher.get("https://news.test/private/secret")
    assert denied.disallowed is True
    assert denied.ok is False
    allowed = fetcher.get("https://news.test/public")
    assert allowed.ok
    fetcher.get("https://news.test/private/other")
    assert calls["robots"] == 1
    assert calls["page"] == 1
    fetcher.close()


def test_robots_disabled_skips_check():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/robots.txt"):
            raise AssertionError("robots.txt should not be fetched")
        return httpx.Response(200, text="ok")

    fetcher = Fetcher(
        "invest-watch/1.0",
        respect_robots=False,
        client=_client(handler),
        rate_limit_requests_per_sec=100,
    )
    assert fetcher.get("https://news.test/anything").ok
    fetcher.close()


def test_response_over_2mb_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(200, content=b"x" * 100, headers={"content-length": "3000000"})

    fetcher = Fetcher(
        "invest-watch/1.0",
        max_response_bytes=2_000_000,
        client=_client(handler),
        rate_limit_requests_per_sec=100,
    )
    result = fetcher.get("https://news.test/huge")
    assert result.too_large
    assert not result.ok
    fetcher.close()


def test_404_is_not_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(404, text="missing")

    fetcher = Fetcher("invest-watch/1.0", client=_client(handler), rate_limit_requests_per_sec=100)
    result = fetcher.get("https://news.test/missing")
    assert result.status_code == 404
    assert not result.ok
    fetcher.close()


def test_429_honours_retry_after(monkeypatch):
    sleeps: list[float] = []
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
        hits["n"] += 1
        if hits["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "1.5"})
        return httpx.Response(200, text="ok")

    fetcher = Fetcher(
        "invest-watch/1.0",
        client=_client(handler),
        rate_limit_requests_per_sec=100,
        sleep=lambda s: sleeps.append(s),
    )
    result = fetcher.get("https://news.test/article")
    assert result.ok
    assert 1.5 in sleeps
    fetcher.close()


def test_5xx_retries_then_returns_error():
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
        hits["n"] += 1
        return httpx.Response(503, text="down")

    fetcher = Fetcher(
        "invest-watch/1.0",
        client=_client(handler),
        rate_limit_requests_per_sec=100,
        sleep=lambda _s: None,
    )
    result = fetcher.get("https://news.test/article")
    assert hits["n"] >= 2
    assert result.status_code == 503 or result.error
    fetcher.close()


def test_redirect_captures_final_url():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/robots.txt"):
            return httpx.Response(404)
        if request.url.path == "/go":
            return httpx.Response(302, headers={"Location": "https://news.test/final"})
        return httpx.Response(200, text="landed")

    fetcher = Fetcher("invest-watch/1.0", client=_client(handler), rate_limit_requests_per_sec=100)
    result = fetcher.get("https://news.test/go")
    assert result.ok
    assert str(result.url_final).rstrip("/") == "https://news.test/final"
    fetcher.close()
