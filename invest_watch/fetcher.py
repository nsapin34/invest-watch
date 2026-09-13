"""Polite HTTP retrieval: User-Agent, robots.txt, redirects, size bounds.

All page/feed downloads go through :class:`Fetcher` (specs §6.6–§6.7,
architecture §4). RSS feeds are not subject to robots.txt; web pages are.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import httpx

from .exceptions import FetchError

logger = logging.getLogger(__name__)

_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "igshid", "ref", "ref_src"})
_TRACKING_PREFIXES = ("utm_", "mc_")
_DEFAULT_PAGE_TIMEOUT = 30.0
_DEFAULT_FEED_TIMEOUT = 10.0
_CONNECT_TIMEOUT = 10.0
_MAX_5XX_ATTEMPTS = 2


def normalize_url(url: str) -> str:
    """Lowercase scheme/host, drop tracking params and fragment (dedup layer 1)."""
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    elif netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    path = parts.path or "/"
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        if lower in _TRACKING_PARAMS or any(lower.startswith(p) for p in _TRACKING_PREFIXES):
            continue
        kept.append((key, value))
    query = urlencode(kept)
    return urlunsplit((scheme, netloc, path, query, ""))


def host_of(url: str) -> str:
    return urlsplit(url).netloc.lower()


class HostRateLimiter:
    """Minimum interval between requests to the same host, plus random jitter."""

    def __init__(
        self,
        requests_per_sec: float,
        *,
        jitter: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.interval = 1.0 / requests_per_sec if requests_per_sec > 0 else 0.0
        self.jitter = max(0.0, jitter)
        self._sleep = sleep
        self._clock = clock
        self._rng = rng or random.Random()
        self._last: dict[str, float] = {}

    def wait(self, host: str) -> float:
        """Block until the host may be contacted. Returns seconds slept."""
        if self.interval <= 0 or not host:
            return 0.0
        now = self._clock()
        last = self._last.get(host)
        delay = 0.0
        if last is not None:
            elapsed = now - last
            remaining = self.interval - elapsed
            if remaining > 0:
                jitter = self._rng.uniform(0.0, self.jitter * self.interval)
                delay = remaining + jitter
                self._sleep(delay)
        self._last[host] = self._clock()
        return delay


@dataclass
class FetchResult:
    """Outcome of a single GET (never raises for HTTP status codes)."""

    url: str
    url_final: str
    status_code: int | None
    content: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    disallowed: bool = False
    truncated: bool = False
    too_large: bool = False

    @property
    def ok(self) -> bool:
        return (
            self.status_code is not None
            and 200 <= self.status_code < 400
            and not self.disallowed
            and not self.too_large
            and self.error is None
        )

    @property
    def text(self) -> str:
        if not self.content:
            return ""
        encoding = "utf-8"
        content_type = self.headers.get("content-type", "")
        if "charset=" in content_type.lower():
            encoding = content_type.lower().split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        return self.content.decode(encoding, errors="replace")


class Fetcher:
    """Identified User-Agent client with per-host robots cache and rate limit."""

    def __init__(
        self,
        user_agent: str,
        *,
        max_response_bytes: int = 2_000_000,
        respect_robots: bool = True,
        rate_limit_requests_per_sec: float = 0.5,
        page_timeout: float = _DEFAULT_PAGE_TIMEOUT,
        feed_timeout: float = _DEFAULT_FEED_TIMEOUT,
        client: httpx.Client | None = None,
        limiter: HostRateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.max_response_bytes = max_response_bytes
        self.respect_robots = respect_robots
        self.page_timeout = page_timeout
        self.feed_timeout = feed_timeout
        self._sleep = sleep
        self._owns_client = client is None
        self.client = client or httpx.Client(
            follow_redirects=True,
            headers={"User-Agent": user_agent},
            timeout=httpx.Timeout(page_timeout, connect=_CONNECT_TIMEOUT),
        )
        self.limiter = limiter or HostRateLimiter(rate_limit_requests_per_sec, sleep=sleep)
        self._robots: dict[str, RobotFileParser | None] = {}

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    def __enter__(self) -> "Fetcher":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _ua_token(self) -> str:
        return self.user_agent.split("/", 1)[0].split()[0] or "invest-watch"

    def _robots_for(self, url: str) -> RobotFileParser | None:
        host = host_of(url)
        if not host:
            return None
        if host in self._robots:
            return self._robots[host]
        parts = urlsplit(url)
        robots_url = f"{parts.scheme or 'https'}://{parts.netloc}/robots.txt"
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            self.limiter.wait(host)
            response = self.client.get(
                robots_url,
                timeout=httpx.Timeout(self.feed_timeout, connect=_CONNECT_TIMEOUT),
            )
            if response.status_code >= 400:
                self._robots[host] = None
                return None
            parser.parse(response.text.splitlines())
            self._robots[host] = parser
            return parser
        except httpx.HTTPError as exc:
            logger.warning("robots.txt unreachable for %s: %s — allowing (degraded)", host, exc)
            self._robots[host] = None
            return None

    def is_allowed(self, url: str) -> bool:
        """Return True if robots.txt permits this User-Agent on ``url``."""
        if not self.respect_robots:
            return True
        parser = self._robots_for(url)
        if parser is None:
            return True
        try:
            return bool(parser.can_fetch(self._ua_token(), url))
        except Exception:  # noqa: BLE001 — malformed robots.txt must not stop collection
            return True

    def get(
        self,
        url: str,
        *,
        kind: str = "page",
        extra_headers: dict[str, str] | None = None,
        check_robots: bool | None = None,
    ) -> FetchResult:
        """GET ``url``. ``kind='feed'`` uses the shorter timeout and skips robots.txt."""
        if check_robots is None:
            check_robots = self.respect_robots and kind != "feed"
        if check_robots and not self.is_allowed(url):
            logger.info("robots.txt disallows %s", url)
            return FetchResult(url=url, url_final=url, status_code=None, disallowed=True, error="robots_disallowed")

        timeout_s = self.feed_timeout if kind == "feed" else self.page_timeout
        timeout = httpx.Timeout(timeout_s, connect=_CONNECT_TIMEOUT)
        host = host_of(url)
        headers = {"User-Agent": self.user_agent}
        if extra_headers:
            headers.update(extra_headers)

        last_error = "unspecified"
        attempts = 0
        max_attempts = _MAX_5XX_ATTEMPTS + 1
        while attempts < max_attempts:
            attempts += 1
            self.limiter.wait(host)
            try:
                with self.client.stream("GET", url, headers=headers, timeout=timeout) as response:
                    status = response.status_code
                    hdrs = {k.lower(): v for k, v in response.headers.items()}
                    url_final = str(response.url)
                    if status == 429:
                        retry_after = _retry_after_seconds(hdrs.get("retry-after"))
                        logger.warning("429 from %s — waiting %.1fs", host, retry_after)
                        self._sleep(retry_after)
                        last_error = "http_429"
                        continue
                    if status >= 500 and attempts < max_attempts:
                        backoff = 2 ** attempts
                        logger.warning("%s from %s — backoff %ss", status, host, backoff)
                        self._sleep(backoff)
                        last_error = f"http_{status}"
                        continue
                    body, too_large, truncated = self._read_bounded(response)
                    return FetchResult(
                        url=url,
                        url_final=url_final,
                        status_code=status,
                        content=body,
                        headers=hdrs,
                        too_large=too_large,
                        truncated=truncated,
                        error="too_large" if too_large else (f"http_{status}" if status >= 400 else None),
                    )
            except httpx.TimeoutException as exc:
                last_error = f"timeout:{exc}"
                logger.warning("timeout fetching %s: %s", url, exc)
                if attempts < max_attempts:
                    self._sleep(2 ** attempts)
            except httpx.HTTPError as exc:
                last_error = f"network:{exc}"
                logger.warning("network error fetching %s: %s", url, exc)
                if attempts < max_attempts:
                    self._sleep(2 ** attempts)
        return FetchResult(url=url, url_final=url, status_code=None, error=last_error)

    def _read_bounded(self, response: httpx.Response) -> tuple[bytes, bool, bool]:
        length = response.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > self.max_response_bytes:
                    response.close()
                    return b"", True, False
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > self.max_response_bytes:
                return b"", True, False
            chunks.append(chunk)
        return b"".join(chunks), False, False


def _retry_after_seconds(value: str | None) -> float:
    if not value:
        return 2.0
    try:
        return max(0.0, float(value))
    except ValueError:
        return 2.0


def require_ok(result: FetchResult) -> FetchResult:
    """Raise :class:`FetchError` when the result is not usable HTML/bytes."""
    if result.disallowed:
        raise FetchError(f"robots.txt disallows {result.url}")
    if result.too_large:
        raise FetchError(f"response exceeds size bound: {result.url}")
    if not result.ok:
        raise FetchError(result.error or f"fetch failed: {result.url}")
    return result
