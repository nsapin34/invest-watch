"""Startup helpers: directory bootstrap, database opening, config seeding.

Shared by the CLI (Step 1) and later by the orchestrator (Step 6). Seeding
upserts the ``[[sources]]`` / ``[[companies]]`` declarations of
``conf/config.toml`` into the database (PROJECT_STRUCTURE: "loaded at startup
and upserted into the database").
"""

from __future__ import annotations

from .company_repository import Company, CompanyRepository
from .config_manager import AppConfig
from .db_manager import DatabaseManager
from .market_repository import MarketRepository
from .source_repository import Source, SourceRepository


def ensure_directories(cfg: AppConfig) -> None:
    """Create the runtime data directories if missing (all git-ignored)."""
    for directory in (
        cfg.db_path.parent,
        cfg.storage.reports_dir,
        cfg.storage.backups_dir,
        cfg.storage.logs_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def open_database(cfg: AppConfig, *, migrate: bool = True) -> DatabaseManager:
    """Open the configured WAL database, applying pending migrations on demand."""
    db = DatabaseManager(cfg.db_path)
    if migrate:
        db.apply_migrations()
    return db


def seed_from_config(db: DatabaseManager, cfg: AppConfig) -> dict[str, int]:
    """Upsert config-declared sources and companies (idempotent, returns counts)."""
    counts = {"sources": 0, "companies": 0, "markets_created": 0, "company_market_links": 0}

    source_repo = SourceRepository(db)
    for seed in cfg.sources:
        source_repo.upsert_by_name(
            Source(
                name=seed.name,
                type=seed.type,
                url=seed.url,
                lang=seed.lang,
                priority=seed.priority,
                active=seed.active,
                rate_limit_per_sec=seed.rate_limit_per_sec,
            )
        )
        counts["sources"] += 1

    if not cfg.companies:
        return counts

    market_repo = MarketRepository(db)
    company_repo = CompanyRepository(db)
    for seed in cfg.companies:
        existing = company_repo.get_by_name(seed.name)
        fields = {
            "legal_name": seed.legal_name,
            "ticker": seed.ticker,
            "isin": seed.isin,
            "country_code": seed.country_code,
            "currency": seed.currency,
            "website": seed.website,
            "summary": seed.summary,
        }
        fields = {key: value for key, value in fields.items() if value is not None}
        if existing is not None:
            if fields:
                company_repo.update(existing.id, **fields)
            company_id = existing.id
        else:
            company = company_repo.add(
                Company(
                    name=seed.name,
                    legal_name=seed.legal_name,
                    ticker=seed.ticker,
                    isin=seed.isin,
                    country_code=seed.country_code,
                    currency=seed.currency,
                    website=seed.website,
                    summary=seed.summary,
                )
            )
            company_id = company.id
            counts["companies"] += 1

        for market_seed in seed.markets:
            market = market_repo.get_by_name(market_seed.name)
            if market is None:
                market = market_repo.add(market_seed.name, market_seed.type)
                counts["markets_created"] += 1
            if market.id is not None and company_id is not None:
                company_repo.attach_market(company_id, market.id)
                counts["company_market_links"] += 1

        if seed.aliases is not None and company_id is not None:
            company_repo.set_aliases(company_id, seed.aliases)

    return counts