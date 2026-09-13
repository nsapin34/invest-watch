"""invest-watch CLI — argparse entry point (specs §13.1, architecture §8 Step 1).

Step 1 makes the entity-management commands operational:

    invest-watch company add|list|edit|remove
    invest-watch market  add|list|edit|remove
    invest-watch source  add|list|edit|remove|ping
    invest-watch db      migrate|version

``invest-watch run`` (Step 2) harvests raw candidates into the database.
The remaining workflow commands (``report``, ``status``, ``errors``,
``export``, ``search``, ``stop``) report "not implemented" until later steps.

Exit codes: 0 success · 1 operation error / partial run · 2 usage / not-implemented.
"""

from __future__ import annotations

import argparse
import sys
from argparse import Namespace
from pathlib import Path

from . import __version__
from .bootstrap import ensure_directories, open_database, seed_from_config
from .company_repository import Company, CompanyRepository
from .config_manager import AppConfig, load_config
from .exceptions import InvestWatchError
from .logger import get_logger, setup_logging
from .market_repository import _print_tree, Market, MarketRepository
from .source_repository import Source, SourceRepository

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOT_IMPLEMENTED = 2

# command -> step that implements it (see docs/architecture-eng.md §8)
_STUB_STEPS: dict[str, str] = {}

logger = get_logger("cli")


# ─────────────────────────────────────────────────────────────────────────────
# Parser construction
# ─────────────────────────────────────────────────────────────────────────────


def _add_company_parser(sub) -> None:
    company = sub.add_parser("company", help="manage tracked companies")
    actions = company.add_subparsers(dest="action", required=True, metavar="ACTION")

    add = actions.add_parser("add", help="add a new company")
    add.add_argument("--name", required=True, help="company name (unique, case-insensitive)")
    add.add_argument("--legal-name", dest="legal_name")
    add.add_argument("--ticker")
    add.add_argument("--isin")
    add.add_argument("--country", dest="country_code")
    add.add_argument("--currency")
    add.add_argument("--website")
    add.add_argument("--summary")
    add.add_argument("--primary-market", dest="primary_market", metavar="NAME_OR_ID")
    add.add_argument("--market", action="append", default=[], metavar="NAME[=WEIGHT]")
    add.add_argument("--alias", action="append", default=[], metavar="TERM")
    add.add_argument("--inactive", action="store_true", help="create as inactive")

    lst = actions.add_parser("list", help="list companies")
    lst.add_argument("--all", action="store_true", help="include inactive companies")
    lst.add_argument("--search", metavar="TEXT", help="filter by name/ticker/ISIN/website")

    edit = actions.add_parser("edit", help="edit an existing company")
    edit.add_argument("target", metavar="NAME_OR_ID")
    edit.add_argument("--rename", dest="name")
    edit.add_argument("--legal-name", dest="legal_name")
    edit.add_argument("--ticker")
    edit.add_argument("--isin")
    edit.add_argument("--country", dest="country_code")
    edit.add_argument("--currency")
    edit.add_argument("--website")
    edit.add_argument("--summary")
    edit.add_argument("--primary-market", dest="primary_market", metavar="NAME_OR_ID|none")
    edit.add_argument("--set-aliases", metavar="TERM1,TERM2,...", help="replace all aliases")
    edit.add_argument("--add-alias", action="append", default=[], metavar="TERM")
    edit.add_argument("--remove-alias", action="append", default=[], metavar="TERM")
    edit.add_argument("--attach-market", action="append", default=[], metavar="NAME[=WEIGHT]")
    edit.add_argument("--detach-market", action="append", default=[], metavar="NAME")
    group = edit.add_mutually_exclusive_group()
    group.add_argument("--active", dest="active", action="store_true", default=None)
    group.add_argument("--inactive", dest="active", action="store_false")

    remove = actions.add_parser("remove", help="remove a company (junctions cascade)")
    remove.add_argument("target", metavar="NAME_OR_ID")
    remove.add_argument("--yes", action="store_true", help="skip confirmation")


def _add_market_parser(sub) -> None:
    market = sub.add_parser("market", help="manage markets / sectors / industries")
    actions = market.add_subparsers(dest="action", required=True, metavar="ACTION")

    add = actions.add_parser("add", help="add a market node")
    add.add_argument("name")
    add.add_argument("--type", required=True, choices=["market", "sector", "industry"])
    add.add_argument("--code", help="optional stable code, e.g. GICS.2510")
    add.add_argument("--parent", metavar="NAME_OR_ID", help="parent market (hierarchy)")
    add.add_argument("--inactive", action="store_true")

    lst = actions.add_parser("list", help="list markets")
    lst.add_argument("--type", choices=["market", "sector", "industry"])
    lst.add_argument("--tree", action="store_true", help="render the hierarchy as a tree")
    lst.add_argument("--all", action="store_true")

    edit = actions.add_parser("edit", help="edit a market node")
    edit.add_argument("target", metavar="NAME_OR_ID")
    edit.add_argument("--rename", dest="name")
    edit.add_argument("--type", choices=["market", "sector", "industry"])
    edit.add_argument("--code", help="empty string '' clears the code")
    edit.add_argument("--parent", metavar="NAME_OR_ID|none", help="'none' detaches from parent")
    group = edit.add_mutually_exclusive_group()
    group.add_argument("--active", dest="active", action="store_true", default=None)
    group.add_argument("--inactive", dest="active", action="store_false")

    remove = actions.add_parser("remove", help="remove a market node")
    remove.add_argument("target", metavar="NAME_OR_ID")
    remove.add_argument("--force", action="store_true", help="allow removal when children exist")


def _add_source_parser(sub) -> None:
    source = sub.add_parser("source", help="manage sources")
    actions = source.add_subparsers(dest="action", required=True, metavar="ACTION")

    add = actions.add_parser("add", help="add a source")
    add.add_argument("name")
    add.add_argument("--type", required=True, choices=["rss", "search", "website", "api"])
    add.add_argument("--url", required=True, help="feed URL or API endpoint")
    add.add_argument("--lang", default="*")
    add.add_argument("--priority", type=int, default=5)
    add.add_argument("--rate-limit", dest="rate_limit_per_sec", type=float, default=0.5)
    add.add_argument("--inactive", action="store_true")

    lst = actions.add_parser("list", help="list sources")
    lst.add_argument("--all", action="store_true")

    edit = actions.add_parser("edit", help="edit a source")
    edit.add_argument("target", metavar="NAME_OR_ID")
    edit.add_argument("--rename", dest="name")
    edit.add_argument("--type", choices=["rss", "search", "website", "api"])
    edit.add_argument("--url")
    edit.add_argument("--lang")
    edit.add_argument("--priority", type=int)
    edit.add_argument("--rate-limit", dest="rate_limit_per_sec", type=float)
    group = edit.add_mutually_exclusive_group()
    group.add_argument("--active", dest="active", action="store_true", default=None)
    group.add_argument("--inactive", dest="active", action="store_false")

    remove = actions.add_parser("remove", help="remove a source")
    remove.add_argument("target", metavar="NAME_OR_ID")
    remove.add_argument("--yes", action="store_true", help="skip confirmation")

    ping = actions.add_parser("ping", help="check a source URL reachability (httpx GET)")
    ping.add_argument("target", metavar="NAME_OR_ID")
    ping.add_argument("--timeout", type=float, default=10.0)


def _add_db_parser(sub) -> None:
    db = sub.add_parser("db", help="database schema and settings")
    actions = db.add_subparsers(dest="action", required=True, metavar="ACTION")
    actions.add_parser("migrate", help="apply pending migrations + sync config sources/companies")
    actions.add_parser("version", help="show current and latest schema version")

def _add_workflow_parsers(sub) -> None:
    report = sub.add_parser("report", help="display latest report (or for a specific date)")
    report.add_argument("--date", metavar="YYYY-MM-DD", help="report date (YYYY-MM-DD)")

    sub.add_parser("status", help="database state, last run, LLM availability, lock")

    errors = sub.add_parser("errors", help="recent processing errors")
    errors.add_argument("--limit", type=int, default=20, help="maximum errors to show (default: 20)")

    export = sub.add_parser("export", help="export a report (Markdown or JSON)")
    export.add_argument("--format", choices=["md", "json"], default="md", help="export format (default: md)")
    export.add_argument("--date", metavar="YYYY-MM-DD", help="report date (YYYY-MM-DD)")

    search = sub.add_parser("search", help="full-text search across article title, excerpt and content")
    search.add_argument("query", help="search phrase or FTS5 query terms")

    sub.add_parser("stop", help="write data/stop.flag to request clean shutdown")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="invest-watch",
        description="Automated daily press monitoring for your portfolio "
        "(CLI + SQLite + optional watsonx.ai).",
        epilog="Commands from Step 2 onwards are progressively activated "
        "(docs/architecture-eng.md §8).",
    )
    parser.add_argument("--version", action="version", version=f"invest-watch {__version__}")
    parser.add_argument("--config", type=Path, help="path to a config.toml file")
    parser.add_argument("--verbose", "-v", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    _add_company_parser(sub)
    _add_market_parser(sub)
    _add_source_parser(sub)
    _add_db_parser(sub)
    run = sub.add_parser("run", help="daily 15-step workflow (Steps 2–6)")
    run.add_argument("--date", help="run date YYYY-MM-DD (collects D-1 for that day)")
    run.add_argument("--resume", metavar="RUN_ID", type=int,
                     help="resume an interrupted run by its run_id")
    run.add_argument("--force", action="store_true",
                     help="override a live run.lock (stale process recovery)")
    _add_workflow_parsers(sub)
    return parser


# ─────────────────────────────────────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────────────────────────────────────


def _print_table(headers: list[str], rows: list[list]) -> None:
    if not rows:
        print("(none)")
        return
    widths = [len(header) for header in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    print("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row)))


def _yes(marker: str) -> bool:
    """Interactive confirmation; treat empty/EOF input as a refusal."""
    try:
        answer = input(f"{marker} — type 'yes' to confirm: ").strip().lower()
    except EOFError:
        return False
    return answer == "yes"


def _parse_name_weight(spec: str) -> tuple[str, float]:
    """Split ``Name=1.5`` into (name, weight); weight defaults to 1.0."""
    if "=" in spec:
        name, _, raw_weight = spec.rpartition("=")
        try:
            return name.strip(), float(raw_weight)
        except ValueError:
            pass
    return spec.strip(), 1.0


def _fmt_bool(value: bool) -> str:
    return "yes" if value else "no"


# ─────────────────────────────────────────────────────────────────────────────
# Command handlers — company
# ─────────────────────────────────────────────────────────────────────────────


def _cmd_company_add(args: Namespace, cfg: AppConfig, db) -> int:
    company_repo = CompanyRepository(db)
    market_repo = MarketRepository(db)

    primary_market_id = None
    if args.primary_market:
        primary_market_id = market_repo.get_by_ref(args.primary_market).id

    markets: list[tuple[int, float]] = []
    for spec in args.market:
        name, weight = _parse_name_weight(spec)
        market = market_repo.get_by_ref(name)
        if market.id is not None:
            markets.append((market.id, weight))

    company = company_repo.add(
        Company(
            name=args.name,
            legal_name=args.legal_name,
            ticker=args.ticker,
            isin=args.isin,
            country_code=args.country_code,
            currency=args.currency,
            website=args.website,
            primary_market_id=primary_market_id,
            summary=args.summary,
            active=not args.inactive,
        ),
        markets=markets,
        aliases=args.alias,
    )
    print(f"added company #{company.id}: {company.name}")
    return EXIT_OK


def _cmd_company_list(args: Namespace, cfg: AppConfig, db) -> int:
    company_repo = CompanyRepository(db)
    if args.search:
        companies = company_repo.search(args.search)
    else:
        companies = company_repo.list(include_inactive=args.all)

    rows: list[list] = []
    for company in companies:
        market_names = ", ".join(market.name for market, _ in company_repo.markets_for(company.id))
        rows.append(
            [
                company.id,
                company.name,
                company.ticker or "",
                company.isin or "",
                company.country_code or "",
                market_names,
                _fmt_bool(company.active),
            ]
        )
    _print_table(["id", "name", "ticker", "isin", "country", "markets", "active"], rows)
    return EXIT_OK


def _cmd_company_edit(args: Namespace, cfg: AppConfig, db) -> int:
    company_repo = CompanyRepository(db)
    market_repo = MarketRepository(db)
    company = company_repo.get_by_ref(args.target)

    fields: dict = {}
    for key in ("name", "legal_name", "ticker", "isin", "country_code", "currency", "website", "summary"):
        value = getattr(args, key)
        if value is not None:
            fields[key] = value
    if args.active is not None:
        fields["active"] = args.active
    if args.primary_market is not None:
        if args.primary_market.lower() == "none":
            fields["primary_market_id"] = None
        else:
            fields["primary_market_id"] = market_repo.get_by_ref(args.primary_market).id

    if fields:
        company = company_repo.update(company.id, **fields)

    for term in args.remove_alias:
        company_repo.remove_alias(company.id, term)
    for term in args.add_alias:
        company_repo.add_alias(company.id, term)
    if args.set_aliases is not None:
        terms = [part.strip() for part in args.set_aliases.split(",") if part.strip()]
        company_repo.set_aliases(company.id, terms)

    for spec in args.attach_market:
        name, weight = _parse_name_weight(spec)
        market = market_repo.get_by_ref(name)
        if market.id is not None:
            company_repo.attach_market(company.id, market.id, weight)
    for name in args.detach_market:
        market = market_repo.get_by_ref(name)
        if market.id is not None:
            company_repo.detach_market(company.id, market.id)

    aliases = ", ".join(alias.term for alias in company_repo.aliases_for(company.id))
    print(f"updated company #{company.id}: {company.name}" + (f"  [aliases: {aliases}]" if aliases else ""))
    return EXIT_OK


def _cmd_company_remove(args: Namespace, cfg: AppConfig, db) -> int:
    company_repo = CompanyRepository(db)
    company = company_repo.get_by_ref(args.target)
    if not args.yes and not _yes(f"remove company #{company.id} '{company.name}'?"):
        print("aborted")
        return EXIT_ERROR
    company_repo.delete(company.id)
    print(f"removed company #{company.id}: {company.name}")
    return EXIT_OK

# ─────────────────────────────────────────────────────────────────────────────
# Command handlers — market / source
# ─────────────────────────────────────────────────────────────────────────────


def _cmd_market_add(args: Namespace, cfg: AppConfig, db) -> int:
    market_repo = MarketRepository(db)
    parent_id = market_repo.get_by_ref(args.parent).id if args.parent else None
    market = market_repo.add(
        args.name, args.type, code=args.code, parent_id=parent_id, active=not args.inactive
    )
    parent = market_repo.get(parent_id) if parent_id else None
    print(
        f"added {args.type} #{market.id}: {market.name}"
        + (f"  (under {parent.name})" if parent else "")
    )
    return EXIT_OK


def _cmd_market_list(args: Namespace, cfg: AppConfig, db) -> int:
    market_repo = MarketRepository(db)
    if args.tree:
        tree = market_repo.tree(include_inactive=args.all)
        print(_print_tree(tree) or "(none)")
        return EXIT_OK
    rows: list[list] = []
    for market in market_repo.list(include_inactive=args.all, type=args.type):
        parent = market_repo.get(market.parent_id) if market.parent_id else None
        rows.append(
            [
                market.id,
                market.name,
                market.type,
                market.code or "",
                parent.name if parent else "",
                _fmt_bool(market.active),
            ]
        )
    _print_table(["id", "name", "type", "code", "parent", "active"], rows)
    return EXIT_OK


def _cmd_market_edit(args: Namespace, cfg: AppConfig, db) -> int:
    market_repo = MarketRepository(db)
    market = market_repo.get_by_ref(args.target)
    fields: dict = {}
    if args.name is not None:
        fields["name"] = args.name
    if args.type is not None:
        fields["type"] = args.type
    if args.code is not None:
        fields["code"] = args.code
    if args.parent is not None:
        fields["parent_id"] = None if args.parent.lower() == "none" else market_repo.get_by_ref(args.parent).id
    if args.active is not None:
        fields["active"] = args.active
    if fields:
        market = market_repo.update(market.id, **fields)
    print(f"updated {market.type} #{market.id}: {market.name}")
    return EXIT_OK


def _cmd_market_remove(args: Namespace, cfg: AppConfig, db) -> int:
    market_repo = MarketRepository(db)
    market = market_repo.get_by_ref(args.target)
    market_repo.delete(market.id, force=args.force)
    print(f"removed market #{market.id}: {market.name}")
    return EXIT_OK


def _cmd_source_add(args: Namespace, cfg: AppConfig, db) -> int:
    source_repo = SourceRepository(db)
    source = source_repo.add(
        Source(
            name=args.name,
            type=args.type,
            url=args.url,
            lang=args.lang,
            priority=args.priority,
            rate_limit_per_sec=args.rate_limit_per_sec,
            active=not args.inactive,
        )
    )
    print(f"added source #{source.id}: {source.name} ({source.type})")
    return EXIT_OK


def _cmd_source_list(args: Namespace, cfg: AppConfig, db) -> int:
    source_repo = SourceRepository(db)
    rows: list[list] = []
    for source in source_repo.list(include_inactive=args.all):
        rows.append(
            [
                source.id,
                source.name,
                source.type,
                source.url,
                source.lang,
                source.priority,
                source.error_count,
                source.last_success_at or "",
                _fmt_bool(source.active),
            ]
        )
    _print_table(
        ["id", "name", "type", "url", "lang", "prio", "errors", "last_success", "active"],
        rows,
    )
    return EXIT_OK


def _cmd_source_edit(args: Namespace, cfg: AppConfig, db) -> int:
    source_repo = SourceRepository(db)
    source = source_repo.get_by_ref(args.target)
    fields: dict = {}
    for key in ("name", "type", "url", "lang", "priority", "rate_limit_per_sec"):
        value = getattr(args, key)
        if value is not None:
            fields[key] = value
    if args.active is not None:
        fields["active"] = args.active
    if fields:
        source = source_repo.update(source.id, **fields)
    print(f"updated source #{source.id}: {source.name}")
    return EXIT_OK


def _cmd_source_remove(args: Namespace, cfg: AppConfig, db) -> int:
    source_repo = SourceRepository(db)
    source = source_repo.get_by_ref(args.target)
    if not args.yes and not _yes(f"remove source #{source.id} '{source.name}'?"):
        print("aborted")
        return EXIT_ERROR
    source_repo.delete(source.id)
    print(f"removed source #{source.id}: {source.name}")
    return EXIT_OK


def _cmd_source_ping(args: Namespace, cfg: AppConfig, db) -> int:
    source_repo = SourceRepository(db)
    source = source_repo.get_by_ref(args.target)
    print(f"pinging {source.name} ({source.url})...")
    ok = source_repo.ping(source.id, timeout=args.timeout)
    print(f"reachable: {ok}")
    return EXIT_OK if ok else EXIT_ERROR

# ─────────────────────────────────────────────────────────────────────────────
# Command handlers — database
# ─────────────────────────────────────────────────────────────────────────────


def _cmd_db_migrate(args: Namespace, cfg: AppConfig, db) -> int:
    applied = db.apply_migrations()
    if applied:
        print(f"applied migrations: {', '.join(applied)}")
    else:
        print(f"database already at schema version {db.schema_version} — nothing to apply")

    counts = seed_from_config(db, cfg)
    if any(counts.values()):
        print(
            f"synced from config: {counts['sources']} source(s), {counts['companies']} company(ies), "
            f"{counts['markets_created']} market(s) created"
        )
    else:
        print("no sources/companies declared in config to sync")
    return EXIT_OK


def _cmd_db_version(args: Namespace, cfg: AppConfig, db) -> int:
    print(f"schema version:  {db.schema_version}")
    print(f"latest available: {db.latest_migration_version()}")
    return EXIT_OK


def _cmd_run(args: Namespace, cfg: AppConfig, db) -> int:
    from .orchestrator import run as orchestrate
    from .run_manager import EXIT_COMPLETED, EXIT_PARTIAL, EXIT_FAILED

    resume_id: int | None = int(args.resume) if args.resume else None
    rc = orchestrate(
        cfg,
        db,
        run_date=args.date or None,
        resume_run_id=resume_id,
        force=bool(args.force),
    )

    # Print a one-line summary drawn from daily_run (may be absent on lock-exit).
    from .article_repository import ArticleRepository
    from .db_manager import DatabaseManager
    repo = ArticleRepository(db)
    if resume_id:
        run_row = repo.get_run(resume_id)
    else:
        # Fetch the most recently created run for today's date.
        row = db.query_one(
            "SELECT id FROM daily_run ORDER BY id DESC LIMIT 1"
        )
        run_row = repo.get_run(row["id"]) if row else None

    if run_row:
        import json as _json
        stats_d = _json.loads(run_row.stats_json) if run_row.stats_json else {}
        print(
            f"run #{run_row.id} {run_row.run_date}  status={run_row.status}\n"
            f"period {run_row.period_start} → {run_row.period_end} ({cfg.app.timezone})\n"
            f"queries={stats_d.get('queries', 0)} "
            f"harvested={stats_d.get('harvested', 0)} "
            f"fetched={stats_d.get('fetched', 0)} "
            f"stored={stats_d.get('stored', 0)} "
            f"skipped_date={stats_d.get('skipped_date', 0)} "
            f"robots={stats_d.get('skipped_robots', 0)} "
            f"fetch_errors={stats_d.get('fetch_errors', 0)}"
        )
        errors = stats_d.get("error_count", 0)
        if errors:
            print(f"errors: {errors} (see `invest-watch errors`)")

    if rc == EXIT_COMPLETED:
        return EXIT_OK
    if rc == EXIT_PARTIAL:
        return EXIT_ERROR
    if rc == EXIT_FAILED:
        return EXIT_FAILED
    return EXIT_ERROR


def _cmd_stop(args: Namespace, cfg: AppConfig, db) -> int:
    from .stop_flag import write_stop_flag
    data_dir = cfg.storage.db_path.parent
    flag_path = write_stop_flag(data_dir)
    print(f"stop flag written: {flag_path}")
    print("The running invest-watch process will cancel after its current step.")
    return EXIT_OK


def _cmd_report(args: Namespace, cfg: AppConfig, db) -> int:
    from .report_generator import ReportConfig, ReportGenerator
    report_date = args.date
    if report_date:
        run_row = db.query_one("SELECT * FROM daily_run WHERE run_date = ? ORDER BY id DESC LIMIT 1", (report_date,))
    else:
        run_row = db.query_one("SELECT * FROM daily_run WHERE status IN ('completed', 'partial') ORDER BY run_date DESC, id DESC LIMIT 1")
        if run_row is None:
            run_row = db.query_one("SELECT * FROM daily_run ORDER BY id DESC LIMIT 1")

    if run_row is None:
        if report_date:
            print(f"no run found for date {report_date}", file=sys.stderr)
        else:
            print("no runs found in database", file=sys.stderr)
        return EXIT_ERROR

    md_path = cfg.storage.reports_dir / f"{run_row['run_date']}.md"
    if md_path.is_file():
        content = md_path.read_text(encoding="utf-8")
    else:
        report_cfg = ReportConfig(
            reports_dir=cfg.storage.reports_dir,
            model_id=cfg.llm.model,
            timezone_name=cfg.app.timezone,
        )
        generator = ReportGenerator(db, report_cfg)
        md_path, _ = generator.generate(run_row["id"])
        content = md_path.read_text(encoding="utf-8")

    print(content)
    return EXIT_OK


def _cmd_status(args: Namespace, cfg: AppConfig, db) -> int:
    from .lock_manager import read_lock_info
    from .stop_flag import stop_flag_exists
    import json as _json

    data_dir = cfg.storage.db_path.parent
    lock_pid, lock_ts = read_lock_info(data_dir)
    has_stop = stop_flag_exists(data_dir)

    company_count = db.query_one("SELECT COUNT(*) AS n FROM company")["n"]
    source_count = db.query_one("SELECT COUNT(*) AS n FROM source")["n"]
    market_count = db.query_one("SELECT COUNT(*) AS n FROM market")["n"]
    article_count = db.query_one("SELECT COUNT(*) AS n FROM article")["n"]
    run_count = db.query_one("SELECT COUNT(*) AS n FROM daily_run")["n"]

    print("=== Database Status ===")
    print(f"Schema version:     {db.schema_version}")
    print(f"Database path:      {cfg.storage.db_path}")
    print(f"Tracked entities:   {company_count} companies, {market_count} markets, {source_count} sources")
    print(f"Stored articles:    {article_count} articles across {run_count} runs")
    print()

    print("=== Execution Locks ===")
    if lock_pid is not None:
        print(f"Process lock:       ACTIVE (pid={lock_pid}, acquired_at={lock_ts})")
    else:
        print("Process lock:       none")
    print(f"Stop flag:          {'PRESENT (stop requested)' if has_stop else 'none'}")
    print()

    print("=== Last Run ===")
    last_run = db.query_one("SELECT * FROM daily_run ORDER BY id DESC LIMIT 1")
    if last_run:
        stats_d = _json.loads(last_run["stats_json"]) if last_run["stats_json"] else {}
        print(f"Run ID:             #{last_run['id']} ({last_run['run_date']})")
        print(f"Status:             {last_run['status']}")
        print(f"Period:             {last_run['period_start']} → {last_run['period_end']}")
        print(f"LLM status:         {last_run['llm_status'] or 'off'}")
        print(f"Error count:        {last_run['error_count']}")
        if stats_d:
            print(f"Harvested/Stored:   {stats_d.get('harvested', 0)} harvested, {stats_d.get('stored', 0)} stored")
    else:
        print("No runs recorded yet.")
    print()

    print("=== LLM Service ===")
    print(f"LLM enabled:        {cfg.llm.enabled}")
    print(f"Model ID:           {cfg.llm.model}")
    print(f"Endpoint:           {cfg.llm.endpoint}")
    if not cfg.llm.enabled:
        print("LLM availability:   disabled (off mode)")
    else:
        if not cfg.llm.api_key:
            print("LLM availability:   unavailable (missing API key)")
        else:
            print("LLM availability:   configured")

    return EXIT_OK


def _cmd_errors(args: Namespace, cfg: AppConfig, db) -> int:
    limit = args.limit
    rows = db.query(
        "SELECT pe.*, dr.run_date, s.name AS source_name "
        " FROM processing_error pe "
        " LEFT JOIN daily_run dr ON pe.run_id = dr.id "
        " LEFT JOIN source s ON pe.source_id = s.id "
        " ORDER BY pe.id DESC LIMIT ?",
        (limit,),
    )
    if not rows:
        print("No processing errors recorded.")
        return EXIT_OK

    table_rows: list[list] = []
    for r in rows:
        date_str = r["run_date"] or ""
        msg = r["message"] or ""
        if len(msg) > 60:
            msg = msg[:57] + "..."
        table_rows.append([
            r["id"],
            f"#{r['run_id']} ({date_str})" if r["run_id"] else "",
            r["step"],
            r["source_name"] or "",
            r["retry_count"],
            r["status"],
            msg,
        ])
    _print_table(["id", "run", "step", "source", "retries", "status", "message"], table_rows)
    return EXIT_OK


def _cmd_export(args: Namespace, cfg: AppConfig, db) -> int:
    from .report_generator import ReportConfig, ReportGenerator
    import json as _json

    report_date = args.date
    if report_date:
        run_row = db.query_one("SELECT * FROM daily_run WHERE run_date = ? ORDER BY id DESC LIMIT 1", (report_date,))
    else:
        run_row = db.query_one("SELECT * FROM daily_run WHERE status IN ('completed', 'partial') ORDER BY run_date DESC, id DESC LIMIT 1")
        if run_row is None:
            run_row = db.query_one("SELECT * FROM daily_run ORDER BY id DESC LIMIT 1")

    if run_row is None:
        if report_date:
            print(f"no run found for date {report_date}", file=sys.stderr)
        else:
            print("no runs found in database", file=sys.stderr)
        return EXIT_ERROR

    fmt = args.format.lower()
    file_path = cfg.storage.reports_dir / f"{run_row['run_date']}.{fmt}"

    if file_path.is_file():
        content = file_path.read_text(encoding="utf-8")
    else:
        report_cfg = ReportConfig(
            reports_dir=cfg.storage.reports_dir,
            model_id=cfg.llm.model,
            timezone_name=cfg.app.timezone,
        )
        generator = ReportGenerator(db, report_cfg)
        md_path, json_path = generator.generate(run_row["id"])
        chosen_path = json_path if fmt == "json" else md_path
        content = chosen_path.read_text(encoding="utf-8")

    print(content)
    return EXIT_OK


def _cmd_search(args: Namespace, cfg: AppConfig, db) -> int:
    query = args.query.strip()
    if not query:
        print("empty search query", file=sys.stderr)
        return EXIT_ERROR

    # SQLite FTS5 query with fallback to quoted phrase if syntax error
    sql = (
        "SELECT a.id, a.run_id, a.title, a.published_at, a.short_code, a.status, a.url_original, "
        "       snippet(article_fts, 1, '[', ']', '...', 10) as match_title, "
        "       snippet(article_fts, 2, '[', ']', '...', 15) as match_excerpt "
        " FROM article_fts "
        " JOIN article a ON article_fts.article_id = a.id "
        " WHERE article_fts MATCH ? "
        " ORDER BY a.id DESC LIMIT 50"
    )
    try:
        rows = db.query(sql, (query,))
    except Exception:
        # Try as quoted phrase
        escaped_query = f'"{query.replace('"', '""')}"'
        try:
            rows = db.query(sql, (escaped_query,))
        except Exception as exc:
            print(f"search query error: {exc}", file=sys.stderr)
            return EXIT_ERROR

    if not rows:
        print(f"No articles matching '{query}' found.")
        return EXIT_OK

    print(f"Found {len(rows)} matching article(s):\n")
    table_rows: list[list] = []
    for r in rows:
        title = r["title"] or ""
        if len(title) > 50:
            title = title[:47] + "..."
        date_str = (r["published_at"] or "")[:10]
        code = r["short_code"] or ""
        table_rows.append([
            r["id"],
            date_str,
            code,
            r["status"],
            title,
            r["url_original"] or "",
        ])
    _print_table(["id", "date", "code", "status", "title", "url"], table_rows)
    return EXIT_OK


# ─────────────────────────────────────────────────────────────────────────────
# Dispatch and entry point
# ─────────────────────────────────────────────────────────────────────────────

_DB_HANDLERS = {
    ("company", "add"): _cmd_company_add,
    ("company", "list"): _cmd_company_list,
    ("company", "edit"): _cmd_company_edit,
    ("company", "remove"): _cmd_company_remove,
    ("market", "add"): _cmd_market_add,
    ("market", "list"): _cmd_market_list,
    ("market", "edit"): _cmd_market_edit,
    ("market", "remove"): _cmd_market_remove,
    ("source", "add"): _cmd_source_add,
    ("source", "list"): _cmd_source_list,
    ("source", "edit"): _cmd_source_edit,
    ("source", "remove"): _cmd_source_remove,
    ("source", "ping"): _cmd_source_ping,
    ("db", "migrate"): _cmd_db_migrate,
    ("db", "version"): _cmd_db_version,
    ("run", None): _cmd_run,
    ("stop", None): _cmd_stop,
    ("report", None): _cmd_report,
    ("status", None): _cmd_status,
    ("errors", None): _cmd_errors,
    ("export", None): _cmd_export,
    ("search", None): _cmd_search,
}


def _run_stub(command: str, args: Namespace) -> int:
    print(
        f"'invest-watch {command}' is not implemented yet — planned for {_STUB_STEPS[command]} "
        "(see docs/architecture-eng.md §8)."
    )
    return EXIT_NOT_IMPLEMENTED


def main(argv: list[str] | None = None) -> int:
    """CLI entry point (console script and ``python -m invest_watch``)."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command in _STUB_STEPS:
        return _run_stub(args.command, args)

    try:
        cfg = load_config(args.config)
        setup_logging(
            logs_dir=cfg.storage.logs_dir,
            level="DEBUG" if args.verbose else str(cfg.logging.level),
            max_bytes=cfg.logging.max_bytes,
            backup_count=cfg.logging.backup_count,
            run_date=None,
        )

        ensure_directories(cfg)
        # Entity commands auto-migrate quietly (idempotent); `db migrate` stays
        # explicit so it can report what it applied.
        migrate = not (args.command == "db")
        db = open_database(cfg, migrate=migrate)
        try:
            action = getattr(args, "action", None)
            handler = _DB_HANDLERS[(args.command, action)]
            return handler(args, cfg, db) or EXIT_OK
        finally:
            db.close()
    except InvestWatchError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())