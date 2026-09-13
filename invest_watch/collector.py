"""Step 2 collection pipeline: discover → date-filter → fetch → clean → store.

The full 15-step orchestrator (lock, resume, reports) arrives in Step 6.
This module is the deliverable for architecture §8 Step 2: ``invest-watch run``
writes raw candidates into ``article`` / ``daily_run``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date

from .article_repository import ArticleRepository
from .cleaner import clean_html
from .company_repository import CompanyRepository
from .config_manager import AppConfig
from .date_detector import compute_period, detect_date
from .db_manager import DatabaseManager
from .fetcher import Fetcher
from .lang_detector import detect_language
from .market_repository import MarketRepository
from .search_engine import SearchEngine, generate_queries, load_search_context
from .source_repository import SourceRepository

logger = logging.getLogger(__name__)


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
    fetch_errors: int = 0
    source_errors: int = 0
    queries: int = 0
    errors: list[str] = field(default_factory=list)

    def as_json(self) -> str:
        return json.dumps(
            {
                "harvested": self.harvested,
                "fetched": self.fetched,
                "stored": self.stored,
                "skipped_date": self.skipped_date,
                "skipped_robots": self.skipped_robots,
                "fetch_errors": self.fetch_errors,
                "source_errors": self.source_errors,
                "queries": self.queries,
            }
        )


def run_collection(
    cfg: AppConfig,
    db: DatabaseManager,
    *,
    run_date: date | str | None = None,
    fetcher: Fetcher | None = None,
    ddg_search=None,
    brave_api_key: str | None = None,
    finnhub_api_key: str | None = None,
) -> CollectionStats:
    """Execute workflow steps 1–7 (period, load, query, harvest, filter, fetch, clean)."""
    period = compute_period(
        cfg.app.timezone,
        run_date=run_date,
        tolerance_hours=cfg.collect.period_tolerance_hours,
    )
    articles = ArticleRepository(db)
    companies_repo = CompanyRepository(db)
    markets_repo = MarketRepository(db)
    sources_repo = SourceRepository(db)

    run = articles.create_run(
        period.run_date.isoformat(),
        period.start_iso,
        period.end_iso,
        trigger="manual",
        llm_status="off" if not cfg.llm.enabled else "ok",
    )

    packed, markets = load_search_context(companies_repo, markets_repo)
    sources = sources_repo.list(include_inactive=False)
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
    )
    try:
        candidates = engine.collect_candidates(queries, sources, period)
        stats = CollectionStats(
            run_id=run.id,
            run_date=period.run_date.isoformat(),
            period_start=period.start_iso,
            period_end=period.end_iso,
            status="running",
            harvested=len(candidates),
            queries=engine.engine_queries_used,
            source_errors=len(engine.errors),
            errors=list(engine.errors),
        )
        for candidate in candidates:
            result = fetcher.get(candidate.url, kind="page")
            if result.disallowed:
                stats.skipped_robots += 1
                continue
            if not result.ok:
                stats.fetch_errors += 1
                stats.errors.append(f"fetch {candidate.url}: {result.error}")
                continue
            stats.fetched += 1
            html = result.text
            cleaned = clean_html(
                html,
                min_content_chars=cfg.collect.min_content_chars,
                url=str(result.url_final),
            )
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
            stored = articles.insert_candidate(
                run_id=run.id,
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
            )
            if stored is not None:
                stats.stored += 1
    finally:
        if own_fetcher:
            fetcher.close()

    error_count = stats.fetch_errors + stats.source_errors
    stats.status = "partial" if error_count else "completed"
    if stats.harvested == 0 and not packed and not sources:
        stats.status = "completed"
    articles.finish_run(run.id, status=stats.status, error_count=error_count, stats_json=stats.as_json())
    logger.info(
        "collection run #%s %s stored=%s harvested=%s errors=%s",
        run.id,
        stats.status,
        stats.stored,
        stats.harvested,
        error_count,
    )
    return stats
