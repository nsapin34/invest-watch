"""Query generation and source cascade: RSS → Finnhub → Brave → DDG → Google News.

Engines (Finnhub/Brave/DDG/Google) are all opt-in: each runs only when an
active ``source`` row declares itself for that engine (by name/URL hint). No
silent fallback engine exists, so deactivating a source fully disables it.
Engine queries are capped at ``max_queries_per_run`` (default 40). RSS feeds
do not consume that quota. Partial failure is isolated per source (ENF-01).

Finnhub (type ``api``, priority 2): free Company News endpoint, filtered by
D-1 date range using the **curated exchange symbol** of the company
(``Company.finnhub_symbol``, e.g. ``AIQUY``, ``SPCX``, ``03750.HK``).  The
endpoint takes a symbol, never free text — so a company without a symbol is
skipped without any request, and exactly one request per company is issued
regardless of how many queries that company generated.  Requires
``FINNHUB_API_KEY`` in the environment or ``.env``; silently skipped
(degraded) when absent.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Callable, Iterable
from urllib.parse import quote_plus

import feedparser

from .company_repository import Company, CompanyRepository
from .config_manager import CollectSection
from .date_detector import CollectionPeriod, DateGuess, detect_date
from .fetcher import FetchResult, Fetcher, normalize_url
from .market_repository import Market, MarketRepository
from .source_repository import Source, SourceRepository

logger = logging.getLogger(__name__)

_ENGINE_SPECIAL = re.compile(r"""[<>"'`(){}[\]\\|!?]""")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_GOOGLE_HINT = ("news.google.com", "google-news", "google news")
_FINNHUB_HINT = ("finnhub", "finnhub.io")
_BRAVE_HINT = ("brave", "api.search.brave.com")
_DDG_HINT = ("duckduckgo", "ddg")
_HKEX_HINT = ("hkexnews",)
_EASTMONEY_HINT = ("eastmoney",)
HKEX_SEARCH_URL = "https://www1.hkexnews.hk/search/titleSearchServlet.do"
HKEX_BASE_URL = "https://www1.hkexnews.hk"
EASTMONEY_SEARCH_URL = "https://search-api-web.eastmoney.com/search/jsonp"
EASTMONEY_PAGE_SIZE = 20
_CN_TZ = "+08:00"  # HKEX and East Money timestamps are Hong Kong / Beijing time
_TAG_RE = re.compile(r"<[^>]+>")
FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/company-news"
GOOGLE_NEWS_TEMPLATE = "https://news.google.com/rss/search?q={query}&hl={lang}&gl=US&ceid=US:{lang}"
LANG_HL = {"fr": "fr", "en": "en", "zh": "zh-CN", "zh-cn": "zh-CN"}


@dataclass(frozen=True)
class SearchQuery:
    """One sanitised engine query, attributed to a company or market.

    ``finnhub_symbol`` carries the curated API symbol of the company the query
    belongs to (``Company.finnhub_symbol``) and is ``None`` for markets or for
    companies whose listing is not covered by the subscription.  The Finnhub
    engine **only** issues a request when this field is set: the endpoint takes
    an exchange symbol, so sending ``query.text`` (a name such as ``SpaceX``)
    returned HTTP 403 or an empty list.

    ``is_primary`` marks the first (and only) query sent to Brave for each
    company — always ``company.name``.  Market queries and all other engine
    queries (Finnhub, DDG, Google) are not affected by this flag.
    """

    text: str
    entity_type: str
    entity_id: int | None
    lang: str = "*"
    finnhub_symbol: str | None = None
    is_primary: bool = False
    # Chinese sources (v007): carried by the primary query of each company only.
    hkex_stock_id: str | None = None
    zh_term: str | None = None
    company_name: str | None = None


@dataclass
class Candidate:
    """A raw discovery hit before page fetch (architecture §4 search_engine)."""

    title: str
    url: str
    date_str: str | None = None
    source_id: int | None = None
    guid: str | None = None
    author: str | None = None
    rss_struct: tuple | None = None
    date_guess: DateGuess | None = None
    origin: str = "rss"
    summary: str | None = None  # Finnhub ``summary`` (stored as-is when the link is a Finnhub redirect)

    @property
    def url_normalized(self) -> str:
        return normalize_url(self.url)


def sanitise_query_term(term: str) -> str:
    """Strip engine-reserved characters; templates never take raw free text."""
    cleaned = _ENGINE_SPECIAL.sub(" ", term or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _is_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def generate_queries(
    companies: Iterable[tuple[Company, list[str], list[Market]]],
    markets: Iterable[Market],
    *,
    queries_per_company: int = 3,
    queries_per_market: int = 2,
    max_queries: int = 40,
) -> list[SearchQuery]:
    """Build up to 3 queries/company + 2/active market, then apply the global cap."""
    seen: set[str] = set()
    queries: list[SearchQuery] = []

    def _add(
        text: str,
        entity_type: str,
        entity_id: int | None,
        lang: str = "*",
        finnhub_symbol: str | None = None,
        is_primary: bool = False,
        raw: bool = False,
        hkex_stock_id: str | None = None,
        zh_term: str | None = None,
        company_name: str | None = None,
    ) -> bool:
        """Append a query; returns False when it was empty or already seen.

        ``raw=True`` (curated ``brave_query`` only) keeps the user's search
        operators - quotes, OR - and just normalises whitespace.
        """
        term = re.sub(r"\s+", " ", text or "").strip() if raw else sanitise_query_term(text)
        if not term:
            return False
        key = term.casefold()
        if key in seen:
            return False
        seen.add(key)
        queries.append(
            SearchQuery(
                text=term,
                entity_type=entity_type,
                entity_id=entity_id,
                lang=lang,
                finnhub_symbol=finnhub_symbol,
                is_primary=is_primary,
                hkex_stock_id=hkex_stock_id,
                zh_term=zh_term,
                company_name=company_name,
            )
        )
        return True

    companies_list = list(companies)
    for company, aliases, linked_markets in companies_list:
        symbol = getattr(company, "finnhub_symbol", None)
        symbol_attached = False
        slots: list[tuple[str, str]] = []
        # v006: a curated Brave query, when set, becomes the primary (Brave) query.
        brave_query = getattr(company, "brave_query", None)
        if brave_query:
            slots.append((brave_query, "en"))  # kept raw (operators preserved)
        slots.append((company.name, "*"))
        if company.ticker:
            slots.append((company.ticker, "en"))
        industry = next((m.name for m in linked_markets if m.type == "industry"), None)
        if industry:
            slots.append((f"{company.name} {industry}", "*"))
        elif company.name:
            slots.append((f"{company.name} news", "*"))
        cjk_alias = next((a for a in aliases if _is_cjk(a)), None)
        if cjk_alias:
            slots.append((cjk_alias, "zh"))
        for extra in aliases:
            if extra.casefold() != company.name.casefold():
                slots.append((extra, "zh" if _is_cjk(extra) else "*"))
        added = 0
        for text, lang in slots:
            # The curated Finnhub symbol rides on the first query that is
            # actually kept for this company — one API call per company, never
            # one per query (P1: the engine used to spray 40 free-text symbols).
            carry = symbol if (symbol and not symbol_attached) else None
            primary = added == 0  # first kept query per company → Brave primary
            is_raw = bool(brave_query) and text == brave_query
            extra = (
                dict(
                    hkex_stock_id=getattr(company, "hkex_stock_id", None),
                    zh_term=cjk_alias,
                    company_name=company.name,
                )
                if primary
                else {}
            )
            if _add(text, "company", company.id, lang, finnhub_symbol=carry, is_primary=primary, raw=is_raw, **extra):
                added += 1
                if carry:
                    symbol_attached = True
            if added >= queries_per_company:
                break

    # v011: one topic (sector) query per company, raw, Brave only. Placed after
    # the company queries and before markets so the global cap trims markets first.
    for company, _aliases, _markets in companies_list:
        topic_query = getattr(company, "topic_query", None)
        if topic_query:
            _add(topic_query, "topic", company.id, "en", raw=True, company_name=company.name)

    for market in (markets if queries_per_market > 0 else ()):
        market_slots = [f"{market.name} news", f"{market.name} regulation"]
        added = 0
        for text in market_slots:
            before = len(queries)
            _add(text, "market", market.id)
            if len(queries) > before:
                added += 1
            if added >= queries_per_market:
                break

    return queries[:max_queries]


def _source_name_url(source: Source) -> str:
    return f"{source.name} {source.url}".lower()


class SearchEngine:
    """Harvests candidates from configured sources; never raises on a dead source."""

    def __init__(
        self,
        collect: CollectSection,
        fetcher: Fetcher,
        source_repo: SourceRepository,
        *,
        ddg_search: Callable[[str], list[dict]] | None = None,
        brave_api_key: str | None = None,
        finnhub_api_key: str | None = None,
        disabled_engines: Iterable[str] | None = None,
    ) -> None:
        self.collect = collect
        self.fetcher = fetcher
        self.source_repo = source_repo
        self._ddg_search = ddg_search
        self._brave_api_key = brave_api_key if brave_api_key is not None else os.environ.get("BRAVE_API_KEY", "")
        self._finnhub_api_key = finnhub_api_key if finnhub_api_key is not None else os.environ.get("FINNHUB_API_KEY", "")
        # Engines whose API key the doctor's pre-flight found invalid (401/403):
        # their sources are skipped WITHOUT any network call and recorded once
        # with error_type='invalid_key' (P1 item 8 — clean skip, no 403 spray).
        self._disabled_engines = frozenset(disabled_engines or ())
        self.errors: list[str] = []
        # Structured error records for processing_error persistence (P1 item 7):
        # {"source_id", "source_name", "error_type", "message"}.
        self.error_records: list[dict] = []
        self.engine_queries_used = 0

    def collect_candidates(
        self,
        queries: list[SearchQuery],
        sources: list[Source],
        period: CollectionPeriod,
        *,
        languages: Iterable[str] = ("en", "fr", "zh"),
    ) -> list[Candidate]:
        """Cascade RSS → Google News RSS → Finnhub → Brave → DuckDuckGo."""
        by_url: dict[str, Candidate] = {}
        rss_sources = [s for s in sources if s.type == "rss" and s.active]
        website_sources = [s for s in sources if s.type == "website" and s.active]
        search_sources = [s for s in sources if s.type == "search" and s.active]
        api_sources = [s for s in sources if s.type == "api" and s.active]

        for source in sorted(rss_sources, key=lambda s: s.priority):
            self._ingest(by_url, self._harvest_rss(source, period), period)
        for source in website_sources:
            self._ingest(by_url, self._harvest_website(source), period)

        if not self.collect.use_search_engines:
            return list(by_url.values())

        google = [s for s in search_sources if any(h in _source_name_url(s) for h in _GOOGLE_HINT)]
        brave = [s for s in search_sources if any(h in _source_name_url(s) for h in _BRAVE_HINT)]
        ddg = [s for s in search_sources if any(h in _source_name_url(s) for h in _DDG_HINT)]
        finnhub = [s for s in api_sources if any(h in _source_name_url(s) for h in _FINNHUB_HINT)]
        hkex = [s for s in api_sources if any(h in _source_name_url(s) for h in _HKEX_HINT)]
        eastmoney = [s for s in api_sources if any(h in _source_name_url(s) for h in _EASTMONEY_HINT)]
        langs = [lang for lang in languages if lang]
        if not langs:
            langs = ["en"]

        # Engines run in priority order: sources that yield real article URLs
        # (Finnhub API, Brave) first, then DuckDuckGo, then Google News RSS.
        # Every engine is OPT-IN: nothing runs unless an *active* source declares
        # itself for it.  Google News RSS is therefore never a silent fallback —
        # its /rss/articles/ links are robots-blocked on the intermediate domain
        # and resolve to a consent/interstitial page when fetched content
        # (see the junk filter in collector.py), so it pays no budget and yields
        # no usable article unless explicitly configured.
        # Finnhub is driven by curated exchange symbols only (Company.finnhub_symbol):
        # one request per company, never one per query.  The endpoint rejects free
        # text — `symbol=SpaceX` → HTTP 403, `symbol=Air Liquide` → empty list — so
        # a company without a symbol is skipped *before* any request is issued.
        # Chinese sources (v007): free, no key, one request per company, and
        # they do NOT consume the paid-engine budget (max_queries_per_run).
        primaries = [q for q in queries if q.is_primary]
        for source in hkex:
            for query in primaries:
                if query.hkex_stock_id:
                    self._ingest(by_url, self._harvest_hkex(source, query, period), period)
        for source in eastmoney:
            for query in primaries:
                if query.zh_term:
                    self._ingest(by_url, self._harvest_eastmoney(source, query), period)

        tried_symbols: set[str] = set()
        for source in finnhub:
            if not self._finnhub_api_key:
                logger.info("Finnhub skipped — FINNHUB_API_KEY missing (degraded)")
                continue
            if "finnhub" in self._disabled_engines:
                # Pre-flight found an invalid key (401/403) — skip cleanly
                # instead of firing one raw 403 per query (P1 item 8).
                self._fail_source(source, None, extra="clé invalide (pré-vol: 401/403)")
                continue
            for query in queries:
                symbol = query.finnhub_symbol
                if not symbol or symbol in tried_symbols:
                    continue
                if self.engine_queries_used >= self.collect.max_queries_per_run:
                    return list(by_url.values())
                tried_symbols.add(symbol)
                self._ingest(by_url, self._harvest_finnhub(source, symbol, period), period)

        for source in brave:
            if not self._brave_api_key:
                logger.info("Brave Search skipped — BRAVE_API_KEY missing (degraded)")
                continue
            if "brave" in self._disabled_engines:
                self._fail_source(source, None, extra="clé invalide (pré-vol: 401/403)")
                continue
            company_queries = [q for q in queries if q.is_primary]
            topic_queries = [q for q in queries if q.entity_type == "topic"]
            market_queries = [q for q in queries if q.entity_type == "market"]
            n_companies = len(company_queries)
            cap = self.collect.max_brave_queries_per_run
            if n_companies > cap:
                logger.warning(
                    "Brave: %d companies exceed max_brave_queries_per_run=%d"
                    " — %d companies will be skipped."
                    " Increase max_brave_queries_per_run in [collect] config.",
                    n_companies,
                    cap,
                    n_companies - cap,
                )
            # Pass 1 — companies (is_primary): always served first, up to cap
            brave_used = 0
            for query in company_queries:
                if brave_used >= cap:
                    break
                self._ingest(by_url, self._harvest_brave(source, query), period)
                brave_used += 1
            # Pass 1b - topic (sector) queries, v011: same Brave cap, before markets.
            # No company link is attached: the classifier decides on the content.
            for query in topic_queries:
                if brave_used >= cap or self.engine_queries_used >= self.collect.max_queries_per_run:
                    break
                self._ingest(by_url, self._harvest_brave(source, query), period)
                brave_used += 1
            market_cap = max(0, cap - brave_used)
            # Pass 2 — markets: consume remaining budget (auto-adjusted)
            market_used = 0
            for query in market_queries:
                if market_used >= market_cap:
                    break
                self._ingest(by_url, self._harvest_brave(source, query), period)
                market_used += 1

        for source in ddg:
            for query in queries:
                if query.entity_type == "topic":  # topic queries are Brave-only
                    continue
                if self.engine_queries_used >= self.collect.max_queries_per_run:
                    return list(by_url.values())
                self._ingest(by_url, self._harvest_ddg(source, query), period)

        for source in google:
            for query in queries:
                if query.entity_type == "topic":  # topic queries are Brave-only
                    continue
                if self.engine_queries_used >= self.collect.max_queries_per_run:
                    return list(by_url.values())
                hl = LANG_HL.get(query.lang if query.lang != "*" else langs[0], "en")
                self._ingest(by_url, self._harvest_google_news(source, query, hl, period), period)

        return list(by_url.values())

    def _ingest(self, by_url: dict[str, Candidate], batch: list[Candidate], period: CollectionPeriod) -> None:
        for candidate in batch:
            url = candidate.url_normalized
            if not url or not candidate.title.strip():
                continue
            guess = candidate.date_guess or detect_date(
                rss_published=candidate.date_str,
                rss_struct=candidate.rss_struct,
                tz_name=period.timezone,
            )
            candidate.date_guess = guess
            if guess.published_at is not None and not period.contains(guess.published_at):
                continue
            by_url.setdefault(url, candidate)

    def _harvest_rss(self, source: Source, period: CollectionPeriod) -> list[Candidate]:
        if not source.url:
            return []
        result = self.fetcher.get(source.url, kind="feed")
        if not result.ok:
            self._fail_source(source, result)
            return []
        parsed = feedparser.parse(result.content)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            self._fail_source(source, result, extra=str(getattr(parsed, "bozo_exception", "malformed feed")))
            return []
        if source.id:
            self.source_repo.record_success(source.id)
        out: list[Candidate] = []
        for entry in parsed.entries:
            link = entry.get("link") or ""
            title = entry.get("title") or ""
            if not link or not title:
                continue
            published = entry.get("published") or entry.get("updated")
            struct = entry.get("published_parsed") or entry.get("updated_parsed")
            out.append(
                Candidate(
                    title=str(title),
                    url=str(link),
                    date_str=str(published) if published else None,
                    source_id=source.id,
                    guid=str(entry.get("id") or entry.get("guid") or "") or None,
                    author=_entry_author(entry),
                    rss_struct=tuple(struct) if struct else None,
                    origin="rss",
                )
            )
        return out

    def _harvest_website(self, source: Source) -> list[Candidate]:
        if not source.url:
            return []
        return [
            Candidate(title=source.name, url=source.url, source_id=source.id, origin="website")
        ]

    def _harvest_google_news(
        self, source: Source, query: SearchQuery, hl: str, period: CollectionPeriod
    ) -> list[Candidate]:
        template = source.url or GOOGLE_NEWS_TEMPLATE
        after_date = period.start_utc.date().isoformat()
        before_date = period.end_utc.date().isoformat()
        q = f"{query.text} after:{after_date} before:{before_date}"
        try:
            url = template.format(query=quote_plus(q), lang=hl, hl=hl)
        except (KeyError, IndexError, ValueError):
            url = GOOGLE_NEWS_TEMPLATE.format(query=quote_plus(q), lang=hl)
        self.engine_queries_used += 1
        fake = Source(name=source.name, type="rss", url=url, id=source.id)
        batch = self._harvest_rss(fake, period)
        for item in batch:
            item.origin = "google_news"
            item.source_id = source.id
        return batch

    def _harvest_brave(self, source: Source, query: SearchQuery) -> list[Candidate]:
        endpoint = source.url or "https://api.search.brave.com/res/v1/web/search"
        if "{" in endpoint:
            url = endpoint.format(query=quote_plus(query.text), lang=query.lang)
        else:
            sep = "&" if "?" in endpoint else "?"
            url = f"{endpoint}{sep}q={quote_plus(query.text)}&freshness=pw"
        self.engine_queries_used += 1
        result = self.fetcher.get(
            url,
            kind="page",
            extra_headers={"Accept": "application/json", "X-Subscription-Token": self._brave_api_key},
            check_robots=False,
        )
        if not result.ok:
            self._fail_source(source, result)
            return []
        try:
            import json

            payload = json.loads(result.text)
        except ValueError as exc:
            self._fail_source(source, result, extra=str(exc))
            return []
        if source.id:
            self.source_repo.record_success(source.id)
        out: list[Candidate] = []
        bucket = payload.get("news") or payload.get("web") or {}
        for item in bucket.get("results") or []:
            link = item.get("url") or ""
            title = item.get("title") or ""
            if not link or not title:
                continue
            out.append(
                Candidate(
                    title=title,
                    url=link,
                    date_str=item.get("page_age") or item.get("age"),
                    source_id=source.id,
                    origin="brave",
                )
            )
        return out

    def _harvest_finnhub(
        self, source: Source, symbol: str, period: CollectionPeriod
    ) -> list[Candidate]:
        """Fetch company news from Finnhub REST API (free tier, GET, date-filtered).

        ``symbol`` is the curated **exchange symbol** carried by the query
        (``Company.finnhub_symbol``, e.g. ``AIQUY``, ``SPCX``, ``03750.HK``) —
        never a search phrase: `/company-news` takes a symbol, so a company name
        yields an empty list and an unknown name yields HTTP 403.

        Finnhub ``/company-news`` accepts the symbol and a ``from``/``to`` date
        range — the D-1 window is passed directly so server-side filtering does
        the heavy lifting (no client-side date discard needed).  The response's
        ``headline``/``url``/``datetime``/``source`` fields are mapped to a
        :class:`Candidate`.
        """
        endpoint = source.url or FINNHUB_NEWS_URL
        from_date = period.start_utc.date().isoformat()
        to_date = period.end_utc.date().isoformat()
        sep = "&" if "?" in endpoint else "?"
        url = (
            f"{endpoint}{sep}symbol={quote_plus(symbol)}"
            f"&from={from_date}&to={to_date}"
            f"&token={self._finnhub_api_key}"
        )
        self.engine_queries_used += 1
        result = self.fetcher.get(url, kind="page", check_robots=False)
        if not result.ok:
            self._fail_symbol(source, symbol, result)
            return []
        try:
            items = json.loads(result.text) or []
        except ValueError as exc:
            self._fail_source(source, result, extra=str(exc))
            return []
        if not isinstance(items, list):
            self._fail_source(source, result, extra="unexpected response shape")
            return []
        if source.id:
            self.source_repo.record_success(source.id)
        out: list[Candidate] = []
        for item in items:
            link = item.get("url") or ""
            title = item.get("headline") or ""
            if not link or not title:
                continue
            ts = item.get("datetime")
            date_str: str | None = None
            if ts:
                try:
                    date_str = datetime.datetime.fromtimestamp(
                        int(ts), tz=datetime.timezone.utc
                    ).isoformat()
                except (ValueError, OSError, OverflowError):
                    date_str = None
            out.append(
                Candidate(
                    title=title,
                    url=link,
                    date_str=date_str,
                    author=item.get("source"),
                    source_id=source.id,
                    origin="finnhub",
                    summary=(item.get("summary") or "").strip() or None,
                )
            )
        return out

    def _harvest_hkex(
        self, source: Source, query: SearchQuery, period: CollectionPeriod
    ) -> list[Candidate]:
        """Official HKEX announcements for one company (HKEXnews title search).

        The endpoint returns ``{"result": "<JSON string>"}`` - the list is
        JSON-encoded *inside* the JSON, hence the double decode.  Each row maps to
        a PDF announcement.  ``lang=en`` is used on purpose: the Chinese PDFs use
        CID fonts that pypdf cannot decode (garbled text), the English ones extract
        cleanly - the LLM writes the French summary anyway.  The company name is
        prefixed to the title so the classifier can attribute it.
        """
        endpoint = source.url or HKEX_SEARCH_URL
        from_date = (period.start_utc.date() - datetime.timedelta(days=1)).strftime("%Y%m%d")
        to_date = (period.end_utc.date() + datetime.timedelta(days=1)).strftime("%Y%m%d")
        url = (
            f"{endpoint}?sortDir=0&sortByOptions=DateTime&category=0&market=SEHK"
            f"&stockId={quote_plus(str(query.hkex_stock_id))}&documentType=-1"
            f"&fromDate={from_date}&toDate={to_date}&title=&searchType=0"
            f"&t1code=-2&t2Gcode=-2&t2code=-2&rowRange=50&lang=en"
        )
        result = self.fetcher.get(url, kind="page", check_robots=False)
        if not result.ok:
            self._fail_source(source, result)
            return []
        try:
            payload = json.loads(result.text)
            rows = payload.get("result") or "[]"
            items = json.loads(rows) if isinstance(rows, str) else rows
        except (ValueError, AttributeError) as exc:
            self._fail_source(source, result, extra=str(exc))
            return []
        if not isinstance(items, list):
            self._fail_source(source, result, extra="unexpected response shape")
            return []
        if source.id:
            self.source_repo.record_success(source.id)
        out: list[Candidate] = []
        for item in items:
            link = str(item.get("FILE_LINK") or "")
            title = _TAG_RE.sub("", str(item.get("TITLE") or "")).strip()
            if not link or not title:
                continue
            if link.startswith("/"):
                link = HKEX_BASE_URL + link
            prefix = query.company_name or str(item.get("STOCK_NAME") or "")
            out.append(
                Candidate(
                    title=f"{prefix} - {title}" if prefix else title,
                    url=link,
                    date_str=_hkex_date(item.get("DATE_TIME")),
                    author="HKEXnews",
                    source_id=source.id,
                    origin="hkex",
                )
            )
        return out

    def _harvest_eastmoney(self, source: Source, query: SearchQuery) -> list[Candidate]:
        """Chinese news search on East Money by the company's Chinese alias.

        Undocumented JSONP web API: the ``x(...)`` wrapper is stripped before
        decoding.  Results are fuzzy (related stocks, sector news) - the
        classifier's alias matching filters out unrelated ones downstream.
        """
        endpoint = source.url or EASTMONEY_SEARCH_URL
        param = {
            "uid": "",
            "keyword": query.zh_term,
            "type": ["cmsArticleWebOld"],
            "client": "web",
            "clientType": "web",
            "clientVersion": "curr",
            "param": {
                "cmsArticleWebOld": {
                    "searchScope": "default",
                    "sort": "time",
                    "pageIndex": 1,
                    "pageSize": EASTMONEY_PAGE_SIZE,
                    "preTag": "",
                    "postTag": "",
                }
            },
        }
        url = f"{endpoint}?cb=x&param={quote_plus(json.dumps(param, ensure_ascii=False))}"
        result = self.fetcher.get(url, kind="page", check_robots=False)
        if not result.ok:
            self._fail_source(source, result)
            return []
        try:
            items = _parse_eastmoney(result.text)
        except ValueError as exc:
            self._fail_source(source, result, extra=str(exc))
            return []
        if source.id:
            self.source_repo.record_success(source.id)
        out: list[Candidate] = []
        for item in items:
            link = str(item.get("url") or "")
            title = _TAG_RE.sub("", str(item.get("title") or "")).strip()
            if not link or not title:
                continue
            date_raw = str(item.get("date") or "").strip()
            out.append(
                Candidate(
                    title=title,
                    url=link,
                    date_str=f"{date_raw.replace(' ', 'T')}{_CN_TZ}" if date_raw else None,
                    author=item.get("mediaName") or None,
                    source_id=source.id,
                    origin="eastmoney",
                )
            )
        return out

    def _harvest_ddg(self, source: Source, query: SearchQuery) -> list[Candidate]:
        self.engine_queries_used += 1
        try:
            search = self._ddg_search or _default_ddg
            rows = search(query.text)
        except Exception as exc:  # noqa: BLE001 — last-resort engine, never fatal
            self._fail_source(source, None, extra=str(exc))
            return []
        if source.id:
            self.source_repo.record_success(source.id)
        out: list[Candidate] = []
        for item in rows:
            link = item.get("href") or item.get("url") or ""
            title = item.get("title") or ""
            if not link or not title:
                continue
            out.append(
                Candidate(
                    title=title,
                    url=link,
                    date_str=item.get("date") or item.get("published"),
                    source_id=source.id,
                    origin="ddg",
                )
            )
        return out

    def _fail_source(self, source: Source, result: FetchResult | None, extra: str = "") -> None:
        message = extra or (result.error if result else "unknown")
        logger.warning("source '%s' failed: %s", source.name, message)
        self.errors.append(f"{source.name}: {message}")
        self.error_records.append(
            {
                "source_id": source.id,
                "source_name": source.name,
                "error_type": _source_error_type(result),
                "message": message,
            }
        )
        if source.id:
            self.source_repo.record_error(source.id)

    #: Statuses that mean "this symbol is not covered", not "the source is down".
    _SYMBOL_UNAVAILABLE_STATUS = frozenset({401, 403, 404})

    def _fail_symbol(self, source: Source, symbol: str, result: FetchResult) -> None:
        """Record one unusable Finnhub symbol, once per run.

        A 403/404 on ``symbol=`` means the key and the endpoint are healthy but
        the listing is not served by the subscription (``You don't have access
        to this resource`` for every ``.HK`` / ``.SZ`` / ``.SS`` / ``.T`` /
        ``.TO`` symbol on the free tier).  That is a *configuration* gap, so
        ``source.error_count`` is deliberately left untouched — otherwise
        ``invest-watch sources`` / ``doctor`` would report a perfectly working
        source as broken.  Transport failures (timeout, 5xx, network) still go
        through :meth:`_fail_source`.
        """
        status = result.status_code
        if status not in self._SYMBOL_UNAVAILABLE_STATUS:
            self._fail_source(source, result)
            return
        message = f"symbole '{symbol}' non couvert par l'abonnement (http_{status})"
        logger.warning("finnhub symbol '%s' unavailable: http_%s", symbol, status)
        self.errors.append(f"{source.name}: {message}")
        self.error_records.append(
            {
                "source_id": source.id,
                "source_name": source.name,
                "error_type": "symbol_unavailable",
                "message": message,
            }
        )


def _source_error_type(result: FetchResult | None) -> str:
    """Normalise a FetchResult error into a short ``processing_error.error_type``.

    ``http_403`` stays ``http_403``; ``timeout:...`` → ``timeout``;
    ``network:...`` → ``network_error``; no result → ``harvest_failed``.
    """
    if result is None:
        return "harvest_failed"
    error = result.error or ""
    if error.startswith("timeout"):
        return "timeout"
    if error.startswith("network"):
        return "network_error"
    if not error:
        return "harvest_failed"
    return error[:64]


def _entry_author(entry: dict) -> str | None:
    if entry.get("author"):
        return str(entry["author"])
    authors = entry.get("authors")
    if authors and isinstance(authors, list) and authors:
        name = authors[0].get("name") if isinstance(authors[0], dict) else authors[0]
        return str(name) if name else None
    return None


def _default_ddg(query: str) -> list[dict]:
    try:
        from ddgs import DDGS  # ddgs >= 6.x (renamed from duckduckgo_search)
    except ImportError:  # pragma: no cover - fallback for older installs
        from duckduckgo_search import DDGS  # type: ignore

    with DDGS() as client:
        news = list(client.news(query, max_results=8, timelimit="d") or [])
        if news:
            return news
        return list(client.text(query, max_results=8) or [])


def load_search_context(
    company_repo: CompanyRepository,
    market_repo: MarketRepository,
) -> tuple[list[tuple[Company, list[str], list[Market]]], list[Market]]:
    """Active companies with aliases and linked markets, plus all active markets."""
    packed: list[tuple[Company, list[str], list[Market]]] = []
    for company in company_repo.list(include_inactive=False):
        aliases = [a.term for a in company_repo.aliases_for(company.id) if a.active]
        linked = [m for m, _ in company_repo.markets_for(company.id)]
        packed.append((company, aliases, linked))
    markets = market_repo.list(include_inactive=False)
    return packed, markets


def _hkex_date(value: object) -> str | None:
    """HKEX ``DATE_TIME`` ('08/10/2026 19:10', HK time) to ISO 8601."""
    try:
        parsed = datetime.datetime.strptime(str(value).strip(), "%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return None
    return parsed.strftime("%Y-%m-%dT%H:%M:00") + _CN_TZ


def _parse_eastmoney(text: str) -> list[dict]:
    """Strip the JSONP wrapper and return the article list (raises ValueError)."""
    start, end = text.find("("), text.rfind(")")
    body = text[start + 1 : end] if start != -1 and end > start else text
    payload = json.loads(body)
    items = ((payload.get("result") or {}).get("cmsArticleWebOld")) or []
    if not isinstance(items, list):
        raise ValueError("unexpected response shape")
    return items
