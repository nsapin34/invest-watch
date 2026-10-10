"""Step 2 collection pipeline: discover → date-filter → fetch → clean → store.

Executes workflow steps 3–7 of the orchestrator.  The ``daily_run`` row is
owned by the caller (orchestrator): ``run_collection`` receives the
``run_id`` and the computed ``period`` (P1 item 9).  Fetch errors and source
errors are persisted into ``processing_error`` with a per-run cap (P1 item 7).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlsplit

from .article_repository import ArticleRepository
from .cleaner import clean_html, clean_pdf, content_hash, is_pdf
from .company_repository import CompanyRepository
from .config_manager import AppConfig
from .date_detector import CollectionPeriod, detect_date
from .db_manager import DatabaseManager, utc_now_iso
from .fetcher import Fetcher, host_of
from .lang_detector import detect_language
from .market_repository import MarketRepository
from .search_engine import SearchEngine, generate_queries, load_search_context
from .source_repository import SourceRepository

logger = logging.getLogger(__name__)

# ── Junk-page guard (data quality, architecture §4) ─────────────────────────
# Interstitial, consent and JS-challenge pages pass HTML cleaning (they carry
# 200+ words of boilerplate) but are never article content. The canonical
# example is Google News RSS: its /rss/articles/ redirector resolves to the
# consent page "Before you continue" for headless clients — such pages must
# never reach the article table.
_JUNK_HOSTS = frozenset(
    {
        "news.google.com",
        "consent.google.com",
        "consent.youtube.com",
    }
)
_JUNK_TITLE_MARKERS = (
    "before you continue",
    "just a moment",
    "attention required",
    "checking your browser",
    "please enable cookies",
    "access denied",
    "verify you are human",
    "enable javascript and cookies",
    "sorry, you have been blocked",
    "request unsuccessful",
    "are you a robot",
)


def _is_blocked(url: str | None, blocked_domains: tuple[str, ...] | list[str]) -> bool:
    """True when ``url``'s host is a blocked domain or one of its subdomains.

    ``fool.com`` blocks ``fool.com`` and ``www.fool.com`` but not ``notfool.com``.
    """
    if not url or not blocked_domains:
        return False
    from urllib.parse import urlsplit

    host = (urlsplit(str(url)).hostname or "").lower().strip(".")
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in blocked_domains)


def _is_junk_page(url_final: str, title: str | None, text: str | None) -> bool:
    """True when a fetched page looks like an interstitial rather than an article.

    Checks the final (post-redirect) host plus a small blocklist of well-known
    consent / challenge page titles. Deliberately cheap and conservative so a
    real article is never dropped; page content length is enforced separately
    by ``min_content_chars``.
    """
    try:
        host = host_of(url_final or "")
    except ValueError:
        host = ""
    if host in _JUNK_HOSTS:
        return True
    lowered = (title or "").strip().lower()
    return any(marker in lowered for marker in _JUNK_TITLE_MARKERS)


@dataclass
class CollectionStats:
    run_id: int
    run_date: str
    period_start: str
    period_end: str
    status: str
    harvested: int = 0
    fetched: int = 0
    stored: int = 0
    skipped_date: int = 0
    skipped_robots: int = 0
    skipped_junk: int = 0
    skipped_blocked: int = 0
    finnhub_summary_only: int = 0  # Finnhub redirect links stored from headline + summary (no fetch)
    slow_host_retries: int = 0
    translated: int = 0
    translation_chars: int = 0
    fetch_errors: int = 0
    source_errors: int = 0
    queries: int = 0
    errors: list[str] = field(default_factory=list)
    # P1 item 7 — processing_error persistence bookkeeping (plafond):
    errors_persisted: int = 0
    errors_dropped: int = 0

    def as_json(self) -> str:
        return json.dumps(
            {
                "harvested": self.harvested,
                "fetched": self.fetched,
                "stored": self.stored,
                "skipped_date": self.skipped_date,
                "skipped_robots": self.skipped_robots,
                "skipped_junk": self.skipped_junk,
                "skipped_blocked": self.skipped_blocked,
                "finnhub_summary_only": self.finnhub_summary_only,
                "slow_host_retries": self.slow_host_retries,
                "translated": self.translated,
                "translation_chars": self.translation_chars,
                "fetch_errors": self.fetch_errors,
                "source_errors": self.source_errors,
                "queries": self.queries,
                "errors_persisted": self.errors_persisted,
                "errors_dropped": self.errors_dropped,
            }
        )


_MAX_PERSISTED_ERRORS = 0  # 0 = no cap (configurable: [collect] max_errors_persisted)
_FINNHUB_REDIRECT_RE = re.compile(r"^https?://(?:www\.)?finnhub\.io/api/news\?", re.IGNORECASE)


def is_finnhub_redirect(url: str | None) -> bool:
    """Finnhub ``/api/news?id=`` links: opaque redirects that answer 403 to scripts.

    The publisher URL is not exposed by ``/company-news`` (``url`` IS this
    redirect), so these items are stored from the API's headline + summary
    instead of being fetched.
    """
    return bool(url and _FINNHUB_REDIRECT_RE.match(url))


def _is_slow_host(url: str, hosts: Iterable[str]) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in hosts)


def _fetch_with_retry(fetcher, url: str, cfg, stats, *, check_robots: bool):
    """GET a page; slow hosts get a longer timeout and ``slow_host_retries`` retries on timeout."""
    hosts = tuple(getattr(cfg.collect, "slow_hosts", ()) or ())
    if not _is_slow_host(url, hosts):
        return fetcher.get(url, kind="page", check_robots=check_robots)
    timeout = float(getattr(cfg.collect, "slow_host_timeout_seconds", 45) or 45)
    retries = max(0, int(getattr(cfg.collect, "slow_host_retries", 1) or 0))
    result = fetcher.get(url, kind="page", check_robots=check_robots, timeout_seconds=timeout)
    while retries > 0 and not result.ok and (result.error or "").startswith("timeout"):
        retries -= 1
        stats.slow_host_retries += 1
        result = fetcher.get(url, kind="page", check_robots=check_robots, timeout_seconds=timeout)
    return result


def _fetch_error_type(error: str | None) -> str:
    """Normalise a FetchResult error into a short ``processing_error.error_type``.

    ``http_403`` stays ``http_403``; ``timeout:...`` → ``timeout``;
    ``network:...`` → ``network_error``; ``too_large`` stays; unknown →
    ``fetch_failed``.
    """
    if not error:
        return "fetch_failed"
    if error.startswith("timeout"):
        return "timeout"
    if error.startswith("network"):
        return "network_error"
    return error[:64]


def _persist_collection_errors(
    db: DatabaseManager,
    run_id: int,
    records: list[dict],
    *,
    cap: int = _MAX_PERSISTED_ERRORS,
) -> tuple[int, int]:
    """Persist collection errors into ``processing_error`` with a per-run cap.

    Each record becomes one row (``step`` = 'fetch' or 'harvest',
    ``error_type`` = ``http_403`` / ``timeout`` / ``invalid_key`` / ...,
    ``message`` = URL or source detail, ``source_id`` when known).  At most
    *cap* rows are written per run — a host failing 200 times must not flood
    the errors table; the surplus is counted in ``errors_dropped``.

    Returns ``(persisted, dropped)``.
    """
    persisted = 0
    dropped = 0
    now = utc_now_iso()
    for record in records:
        if cap and persisted >= cap:
            dropped += 1
            continue
        try:
            db.execute(
                "INSERT INTO processing_error"
                "  (run_id, step, source_id, error_type, message, retry_count,"
                "   status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 0, 'new', ?, ?)",
                (
                    run_id,
                    record["step"],
                    record.get("source_id"),
                    record["error_type"],
                    record["message"],
                    now,
                    now,
                ),
            )
            persisted += 1
        except Exception as exc:  # non-fatal — stats keep the in-memory copy
            logger.warning("could not persist collection error: %s", exc)
    return persisted, dropped


_DEFAULT_TRANSLATOR = object()  # sentinel: build from cfg.translation


def run_collection(
    cfg: AppConfig,
    db: DatabaseManager,
    *,
    run_id: int,
    period: CollectionPeriod,
    fetcher: Fetcher | None = None,
    ddg_search=None,
    brave_api_key: str | None = None,
    finnhub_api_key: str | None = None,
    disabled_engines: Iterable[str] | None = None,
    error_cap: int | None = None,
    translator=_DEFAULT_TRANSLATOR,
) -> CollectionStats:
    """Execute workflow steps 3–7 (load, query, harvest, filter, fetch, clean).

    The caller (orchestrator) owns the ``daily_run`` row: it passes the
    ``run_id`` and the already-computed ``period`` (P1 item 9 — the collector
    no longer creates nor finishes the row).

    Fetch errors and source (engine) errors are persisted into
    ``processing_error`` (P1 item 7): one capped row per failure with a
    structured ``error_type`` (``http_403``, ``timeout``, ``invalid_key``...)
    and the URL as ``message``.
    """
    articles = ArticleRepository(db)
    companies_repo = CompanyRepository(db)
    markets_repo = MarketRepository(db)
    sources_repo = SourceRepository(db)

    packed, markets = load_search_context(companies_repo, markets_repo)
    sources = sources_repo.list(include_inactive=False)
    search_source_ids = {s.id for s in sources if s.type == "search"}
    queries = generate_queries(
        packed,
        markets,
        queries_per_company=cfg.collect.queries_per_company,
        queries_per_market=cfg.collect.queries_per_market,
        max_queries=cfg.collect.max_queries_per_run,
    )

    own_fetcher = fetcher is None
    fetcher = fetcher or Fetcher(
        cfg.collect.fetch_user_agent,
        max_response_bytes=cfg.collect.max_response_bytes,
        respect_robots=cfg.collect.respect_robots,
        rate_limit_requests_per_sec=cfg.collect.rate_limit_requests_per_sec,
    )
    engine = SearchEngine(
        cfg.collect,
        fetcher,
        sources_repo,
        ddg_search=ddg_search,
        brave_api_key=brave_api_key,
        finnhub_api_key=finnhub_api_key if finnhub_api_key is not None else cfg.collect.finnhub_api_key,
        disabled_engines=disabled_engines,
    )
    try:
        candidates = engine.collect_candidates(queries, sources, period)
        stats = CollectionStats(
            run_id=run_id,
            run_date=period.run_date.isoformat(),
            period_start=period.start_iso,
            period_end=period.end_iso,
            status="running",
            harvested=len(candidates),
            queries=engine.engine_queries_used,
            source_errors=len(engine.errors),
            errors=list(engine.errors),
        )
        fetch_error_records: list[dict] = []
        blocked_domains = tuple(getattr(cfg.collect, "blocked_domains", ()) or ())
        if translator is _DEFAULT_TRANSLATOR:
            from .translator import TranslationSection, build_translator

            translator = build_translator(getattr(cfg, "translation", TranslationSection()))
        for candidate in candidates:
            # Publisher blacklist - checked before any download.
            if _is_blocked(candidate.url, blocked_domains):
                stats.skipped_blocked += 1
                continue
            # Sources of type "search" (e.g. Google News RSS) yield redirect URLs
            # that robots.txt disallows on the intermediate domain. Skip the robots
            # check here — httpx follows the redirect to the final article page.
            if is_finnhub_redirect(candidate.url):
                # No fetch: the redirect answers 403. Headline + summary from the API.
                text = (candidate.summary or "").strip()
                page_date = detect_date(html=None, rss_published=candidate.date_str,
                                        rss_struct=None, tz_name=cfg.app.timezone)
                if page_date.published_at is not None and not period.contains(page_date.published_at):
                    stats.skipped_date += 1
                    continue
                stats.finnhub_summary_only += 1
                stored = articles.insert_candidate(
                    run_id=run_id,
                    title=candidate.title,
                    url=candidate.url,
                    source_id=candidate.source_id,
                    guid=candidate.guid,
                    author=candidate.author,
                    language=detect_language(f"{candidate.title} {text}") or "en",
                    published_at=page_date.iso,
                    date_certaine=page_date.date_certaine,
                    content=text or None,
                    excerpt=text[:300] or None,
                    content_hash=content_hash(text) if text else None,
                    word_count=len(text.split()),
                )
                if stored is not None:
                    stats.stored += 1
                continue
            is_search_source = candidate.source_id in search_source_ids
            result = _fetch_with_retry(fetcher, candidate.url, cfg, stats, check_robots=not is_search_source)
            if result.disallowed:
                stats.skipped_robots += 1
                continue
            if not result.ok:
                stats.fetch_errors += 1
                stats.errors.append(f"fetch {candidate.url}: {result.error}")
                fetch_error_records.append(
                    {
                        "step": "fetch",
                        "error_type": _fetch_error_type(result.error),
                        "message": str(candidate.url),
                        "source_id": candidate.source_id,
                    }
                )
                continue
            # ... and again on the final URL (aggregator / redirect to a blocked site).
            if _is_blocked(str(result.url_final), blocked_domains):
                stats.skipped_blocked += 1
                continue
            stats.fetched += 1
            if is_pdf(result.content, result.headers.get("content-type"), str(result.url_final)):
                # PDF announcements (HKEXnews): text via pypdf, no HTML date hints.
                html = None
                cleaned = clean_pdf(result.content, min_content_chars=cfg.collect.min_content_chars)
            else:
                html = result.text
                cleaned = clean_html(
                    html,
                    min_content_chars=cfg.collect.min_content_chars,
                    url=str(result.url_final),
                )
            # Interstitial / consent / challenge pages are never articles.
            if _is_junk_page(str(result.url_final), cleaned.title, cleaned.text):
                stats.skipped_junk += 1
                continue
            page_date = detect_date(
                html=html,
                rss_published=candidate.date_str,
                rss_struct=candidate.rss_struct,
                tz_name=cfg.app.timezone,
            )
            if page_date.published_at is not None and not period.contains(page_date.published_at):
                stats.skipped_date += 1
                continue
            language = detect_language(cleaned.text) or detect_language(candidate.title)
            title_final = cleaned.title or candidate.title
            title_fr = content_fr = None
            if translator is not None and translator.wants(language):
                title_fr, content_fr = translator.translate_article(title_final, cleaned.text, language)
            stored = articles.insert_candidate(
                run_id=run_id,
                title=cleaned.title or candidate.title,
                url=result.url_final or candidate.url,
                source_id=candidate.source_id,
                guid=candidate.guid,
                author=cleaned.author or candidate.author,
                language=language,
                published_at=page_date.iso,
                date_certaine=page_date.date_certaine,
                content=cleaned.text or None,
                excerpt=cleaned.excerpt or None,
                content_hash=cleaned.content_hash,
                word_count=cleaned.word_count,
                title_fr=title_fr,
                content_fr=content_fr,
            )
            if stored is not None:
                stats.stored += 1
    finally:
        if own_fetcher:
            fetcher.close()

    # ── Persist fetch + source errors into processing_error (P1 item 7) ──────
    records: list[dict] = [
        {
            "step": "harvest",
            "error_type": rec["error_type"],
            "message": f"{rec['source_name']}: {rec['message']}",
            "source_id": rec.get("source_id"),
        }
        for rec in engine.error_records
    ]
    records.extend(fetch_error_records)
    stats.errors_persisted, stats.errors_dropped = _persist_collection_errors(
        db, run_id, records,
        cap=error_cap if error_cap is not None else int(getattr(cfg.collect, "max_errors_persisted", 0) or 0),
    )

    # Informational status only — the orchestrator owns the final daily_run
    # status (P1 item 9: the collector no longer finishes the row).
    error_count = stats.fetch_errors + stats.source_errors
    stats.status = "partial" if error_count else "completed"
    if stats.harvested == 0 and not packed and not sources:
        stats.status = "completed"
    logger.info(
        "collection run #%s stored=%s harvested=%s errors=%s (persisted=%s dropped=%s)",
        run_id,
        stats.stored,
        stats.harvested,
        error_count,
        stats.errors_persisted,
        stats.errors_dropped,
    )
    if translator is not None and translator is not _DEFAULT_TRANSLATOR:
        stats.translated = translator.translated
        stats.translation_chars = translator.chars_used
        translator.close()
    return stats
