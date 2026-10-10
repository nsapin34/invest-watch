"""Configuration loading for invest-watch.

Loads ``conf/config.toml`` (runtime config), resolves ``${VAR}`` placeholders
from the process environment (augmented by the project-root ``.env``) and
produces an immutable :class:`AppConfig`.

Resolution priority (architecture Annex A.4 / A.7):
    environment / .env   >   config.toml   >   built-in defaults

If ``conf/config.toml`` is absent, ``conf/config.example.toml`` is used as a
degraded fallback (warning logged) — the application never stops (ENF-01).
Secrets never live in the TOML: they are referenced as ``${WATSONX_API_KEY}``
and resolved from the environment only.
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from .exceptions import ConfigError
from .translator import TranslationSection
from .weekly_config import WeeklySection
from .finnhub_symbols import normalize_finnhub_symbol, warn_if_bare_ticker

logger = logging.getLogger(__name__)

_ENV_HOME_VAR = "INVEST_WATCH_HOME"
_VAR_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_SOURCE_TYPES = ("rss", "search", "website", "api")

# Finnhub API symbols are exchange tickers, never free text:
#   AIQUY, SPCX, KSTR, 03750.HK, 300750.SZ, 601899.SS
# Anything with a space / accent / lowercase word (e.g. "Air Liquide",
# "SpaceX news") is rejected at load time — such a value used to be sent as
# `symbol=...` and produced HTTP 403 or an empty result set (run #4).
# A bare numeric listing ticker (3750, 2899, 3393) is NOT accepted as a
# symbol either: it only triggers a guiding warning suggesting the qualified
# form (see finnhub_symbols.suggest_finnhub_symbol) — `ticker` keeps the
# display value, `finnhub_symbol` carries the qualified API symbol.
_FINNHUB_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")

_DEFAULTS: dict[str, dict[str, Any]] = {
    "app": {
        "timezone": "Europe/Paris",
        "run_time": "07:00",
        "max_duration_minutes": 90,
        "language": "fr",
    },
    "storage": {
        "db_path": "data/invest.db",
        "reports_dir": "reports",
        "backups_dir": "backups",
        "logs_dir": "logs",
        "retention_days": 90,
        "backup_enabled": True,
    },
    "collect": {
        "period_tolerance_hours": 6,
        "min_content_chars": 200,
        "max_response_bytes": 2_000_000,
        "rate_limit_requests_per_sec": 0.5,
        "max_queries_per_run": 40,
        "queries_per_company": 3,
        "queries_per_market": 2,
        "fetch_user_agent": "invest-watch/1.0 (+contact@example.com)",
        "respect_robots": True,
        "use_search_engines": True,
        "finnhub_api_key": "",
        "max_brave_queries_per_run": 23,
        "blocked_domains": [],
        "max_errors_persisted": 0,
        "slow_hosts": ["eastmoney.com"],
        "slow_host_timeout_seconds": 45,
        "slow_host_retries": 1,
    },
    "dedup": {
        "title_similarity": 0.85,
        "minhash_threshold": 0.9,
        "event_window_hours": 24,
        "seen_days": 7,
        "new_event_lookback_days": 14,
    },
    "scoring": {
        "relevance_min": 0.5,
        "importance_default": "moyenne",
        "importance_boost_categories": ["regulator", "financial_results"],
        "importance_boost_source_priority": [1, 2],
        "topic_link_min": 0.45,
        "peer_min_topic_hits": 2,
    },
    "llm": {
        "enabled": True,
        "model": "ibm/granite-3-8b-instruct",
        "endpoint": "https://eu-de.ml.cloud.ibm.com",
        "project_id": "",
        "max_input_chars": 8000,
        # 3000: article-JSON stops at EOS, digests (≤25 article blocks) need 1200+.
        "max_new_tokens": 3000,
        "temperature": 0.1,
        "top_p": 0.9,
        "timeout_seconds": 120,
        "max_retries": 2,
        "circuit_breaker_threshold": 3,
        "max_calls_per_run": 150,
    },
    "logging": {"level": "INFO", "max_bytes": 5_000_000, "backup_count": 5},
    "weekly": {
        "use_llm": True,
        "max_articles_per_company": 25,
        "max_chars_per_article": 1200,
        "max_input_chars": 30_000,
        "quota_direct": 15,
        "quota_sector": 5,
        "quota_peer": 5,
    },
    "translation": {
        "enabled": False,
        "provider": "azure",
        "target": "fr",
        "languages": ["zh"],
        "max_chars_per_run": 60_000,
        "max_chars_per_article": 10_000,
        "timeout_seconds": 30,
        "max_retries": 2,
    },
}

@dataclass(frozen=True)
class AppSection:
    """Settings from the ``[app]`` TOML section."""

    timezone: str
    run_time: str
    max_duration_minutes: int
    language: str


@dataclass(frozen=True)
class StorageSection:
    """Settings from the ``[storage]`` TOML section (paths anchored to root)."""

    db_path: Path
    reports_dir: Path
    backups_dir: Path
    logs_dir: Path
    retention_days: int
    backup_enabled: bool


@dataclass(frozen=True)
class CollectSection:
    """Settings from the ``[collect]`` TOML section."""

    period_tolerance_hours: int
    min_content_chars: int
    max_response_bytes: int
    rate_limit_requests_per_sec: float
    max_queries_per_run: int
    queries_per_company: int
    queries_per_market: int
    fetch_user_agent: str
    respect_robots: bool
    use_search_engines: bool
    finnhub_api_key: str = ""
    # Brave-specific budget cap: companies are served first (pass 1),
    # markets fill the remainder (pass 2 = max(0, cap - N_companies)).
    # No manual adjustment needed when adding companies — markets auto-shrink.
    max_brave_queries_per_run: int = 23
    # Publisher blacklist: articles whose (final) URL is on one of these
    # domains - subdomains included - are skipped before storage.
    blocked_domains: tuple[str, ...] = ()
    # processing_error rows persisted per run; 0 = unlimited
    max_errors_persisted: int = 0
    # Slow publishers (subdomains included): longer page timeout + N retries on timeout
    slow_hosts: tuple[str, ...] = ("eastmoney.com",)
    slow_host_timeout_seconds: int = 45
    slow_host_retries: int = 1


@dataclass(frozen=True)
class DedupSection:
    """Settings from the ``[dedup]`` TOML section."""

    title_similarity: float
    minhash_threshold: float
    event_window_hours: int
    seen_days: int
    new_event_lookback_days: int


@dataclass(frozen=True)
class ScoringSection:
    """Settings from the ``[scoring]`` TOML section."""

    relevance_min: float
    importance_default: str
    importance_boost_categories: tuple[str, ...]
    importance_boost_source_priority: tuple[int, ...]
    # v010: minimum topic score to link an article to a company it does not name
    topic_link_min: float = 0.45
    # v010: distinct topic keywords required for a peer (competitor/partner) link
    peer_min_topic_hits: int = 2


@dataclass(frozen=True)
class LLMSection:
    """Settings from the ``[llm]`` TOML section.

    ``api_key`` is never read from the TOML: it only ever comes from the
    environment / ``.env`` (``WATSONX_API_KEY``), directly or through a
    ``${WATSONX_API_KEY}`` placeholder.
    """

    enabled: bool
    model: str
    endpoint: str
    project_id: str
    api_key: str
    max_input_chars: int
    max_new_tokens: int
    temperature: float
    top_p: float
    timeout_seconds: int
    max_retries: int
    circuit_breaker_threshold: int
    max_calls_per_run: int


@dataclass(frozen=True)
class LoggingSection:
    """Settings from the ``[logging]`` TOML section."""

    level: str
    max_bytes: int
    backup_count: int


@dataclass(frozen=True)
class SourceSeed:
    """A source declared in ``[[sources]]``, upserted into the database."""

    name: str
    type: str
    url: str
    lang: str = "*"
    priority: int = 5
    active: bool = True
    rate_limit_per_sec: float = 0.5


@dataclass(frozen=True)
class CompanyMarketSeed:
    """A market attached to a configured company (subsection ``[[companies.markets]]``)."""

    name: str
    type: str = "industry"


@dataclass(frozen=True)
class CompanySeed:
    """A company declared in ``[[companies]]``, upserted into the database.

    ``aliases`` is ``None`` when the TOML key is absent (no sync performed),
    and an (empty) tuple when present (aliases replaced by the config).
    """

    name: str
    legal_name: str | None = None
    ticker: str | None = None
    isin: str | None = None
    country_code: str | None = None
    currency: str | None = None
    website: str | None = None
    summary: str | None = None
    finnhub_symbol: str | None = None
    brave_query: str | None = None
    hkex_stock_id: str | None = None
    topic_keywords: tuple[str, ...] | None = None  # v010
    peers: tuple[str, ...] | None = None  # v010
    topic_query: str | None = None  # v011: extra Brave sector query
    # Config keys explicitly set to "" (e.g. finnhub_symbol = ""): cleared in the DB on sync.
    clear_fields: tuple[str, ...] = ()
    aliases: tuple[str, ...] | None = None
    languages: tuple[str, ...] | None = None
    markets: tuple[CompanyMarketSeed, ...] = ()


@dataclass(frozen=True)
class AppConfig:
    """Immutable, project-rooted application configuration (architecture §4)."""

    project_root: Path
    config_path: Path
    app: AppSection
    storage: StorageSection
    collect: CollectSection
    dedup: DedupSection
    scoring: ScoringSection
    llm: LLMSection
    logging: LoggingSection
    sources: tuple[SourceSeed, ...] = ()
    companies: tuple[CompanySeed, ...] = ()
    translation: TranslationSection = TranslationSection()
    weekly: WeeklySection = WeeklySection()

    @property
    def db_path(self) -> Path:
        """Convenience shortcut to the anchored database path."""
        return self.storage.db_path


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def default_project_root() -> Path:
    """Project root: ``INVEST_WATCH_HOME`` env var, else the package parent."""
    env_home = os.environ.get(_ENV_HOME_VAR)
    if env_home:
        return Path(env_home).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


def _infer_project_root(config_path: Path) -> Path:
    """Root of a project when an explicit ``--config`` path is given.

    ``<root>/conf/config.toml`` is the canonical layout (PROJECT_STRUCTURE).
    """
    if config_path.parent.name.lower() == "conf":
        return config_path.parent.parent
    return config_path.parent


def _resolve_env(value: str) -> str:
    """Resolve ``${VAR}`` placeholders from the environment (missing -> '')."""

    def _sub(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), "")

    return _VAR_PATTERN.sub(_sub, value)


def _resolve_deep(node: Any) -> Any:
    """Recursively resolve ``${VAR}`` placeholders in strings of a TOML tree."""
    if isinstance(node, str):
        return _resolve_env(node) if "${" in node else node
    if isinstance(node, list):
        return [_resolve_deep(item) for item in node]
    if isinstance(node, dict):
        return {key: _resolve_deep(value) for key, value in node.items()}
    return node


def _anchor(path_value: str, project_root: Path) -> Path:
    path = Path(path_value)
    return path if path.is_absolute() else project_root / path


def _merge_section(name: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Merge a TOML section over the defaults, validating key types."""
    merged = dict(_DEFAULTS[name])
    unknown = set(raw) - set(merged)
    if unknown:
        logger.warning("config.toml [%s]: unknown key(s) ignored: %s", name, ", ".join(sorted(unknown)))
    for key, value in raw.items():
        if key not in merged:
            continue
        expected = type(merged[key])
        # bool is an int subclass in Python: accept either for int-typed keys.
        if not isinstance(value, expected) and not (expected is int and isinstance(value, bool)):
            raise ConfigError(
                f"config.toml [{name}].{key}: expected {expected.__name__}, got {type(value).__name__}"
            )
        merged[key] = value
    return merged


def _parse_source_seeds(raw_sources: list[dict[str, Any]]) -> tuple[SourceSeed, ...]:
    seeds: list[SourceSeed] = []
    for i, item in enumerate(raw_sources):
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"[[sources]] #{i + 1}: missing or invalid 'name'")
        src_type = item.get("type")
        if src_type not in _SOURCE_TYPES:
            raise ConfigError(f"[[sources]] '{name}': 'type' must be one of {_SOURCE_TYPES}")
        url = item.get("url", "")
        if not isinstance(url, str):
            raise ConfigError(f"[[sources]] '{name}': 'url' must be a string")
        seeds.append(
            SourceSeed(
                name=name.strip(),
                type=src_type,
                url=url,
                lang=str(item.get("lang", "*")),
                priority=int(item.get("priority", 5)),
                active=bool(item.get("active", True)),
                rate_limit_per_sec=float(item.get("rate_limit_per_sec", 0.5)),
            )
        )
    return tuple(seeds)


def _parse_company_seeds(raw_companies: list[dict[str, Any]]) -> tuple[CompanySeed, ...]:
    seeds: list[CompanySeed] = []
    for i, item in enumerate(raw_companies):
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"[[companies]] #{i + 1}: missing or invalid 'name'")
        alias_key = "alias" if "alias" in item else "aliases"
        aliases_raw = item.get(alias_key)
        aliases = (
            tuple(str(a).strip() for a in aliases_raw if str(a).strip()) if aliases_raw is not None else None
        )
        lang_key = next((k for k in ("langue", "lang", "languages") if k in item), None)
        languages_raw = item.get(lang_key) if lang_key else None
        languages = (
            tuple(str(l).strip() for l in languages_raw if str(l).strip()) if languages_raw is not None else None
        )
        markets_raw = item.get("markets") or []
        markets = tuple(
            CompanyMarketSeed(name=str(m.get("name", "")).strip(), type=str(m.get("type", "industry")))
            for m in markets_raw
            if str(m.get("name", "")).strip()
        )
        country = item.get("country", item.get("country_code"))
        finnhub_key = next(
            (k for k in ("finnhub_symbol", "finnhub_ticker") if k in item), None
        )
        finnhub_symbol: str | None = None
        if finnhub_key:
            raw_symbol = str(item.get(finnhub_key) or "").strip()
            if raw_symbol:
                normalised = normalize_finnhub_symbol(raw_symbol)
                if normalised is None or not _FINNHUB_SYMBOL_RE.match(normalised):
                    raise ConfigError(
                        f"[[companies]] '{name.strip()}': "
                        f"'{finnhub_key}' must be an exchange symbol "
                        f"(e.g. AIQUY, SPCX, 03750.HK) — got {raw_symbol!r}"
                    )
                finnhub_symbol = normalised
        ticker_raw = str(item["ticker"]).strip() if item.get("ticker") else None
        ticker_value = ticker_raw or None
        country_value = str(country).strip() if country else None
        currency_value = str(item["currency"]).strip() if item.get("currency") else None
        warn_if_bare_ticker(
            name.strip(),
            ticker_value,
            finnhub_symbol,
            country=country_value,
            currency=currency_value,
        )
        seeds.append(
            CompanySeed(
                name=name.strip(),
                legal_name=str(item["legal_name"]).strip() if item.get("legal_name") else None,
                ticker=ticker_value,
                isin=str(item["isin"]).strip() if item.get("isin") else None,
                country_code=str(country).strip() if country else None,
                currency=str(item["currency"]).strip() if item.get("currency") else None,
                website=str(item["website"]).strip() if item.get("website") else None,
                summary=str(item["summary"]).strip() if item.get("summary") else None,
                finnhub_symbol=finnhub_symbol,
                brave_query=str(item["brave_query"]).strip() or None if item.get("brave_query") else None,
                hkex_stock_id=str(item["hkex_stock_id"]).strip() or None if item.get("hkex_stock_id") else None,
                topic_keywords=_str_list(item.get("topic_keywords"), "topic_keywords", name),
                peers=_str_list(item.get("peers"), "peers", name),
                topic_query=str(item["topic_query"]).strip() or None if item.get("topic_query") else None,
                clear_fields=tuple(
                    k for k in ("finnhub_symbol", "brave_query", "hkex_stock_id", "topic_query")
                    if k in item and not str(item[k] or "").strip()
                ),
                aliases=aliases,
                languages=languages,
                markets=markets,
            )
        )
    return tuple(seeds)


def _str_list(value: Any, key: str, company: str) -> tuple[str, ...] | None:
    """``[[companies]]`` list of strings (topic_keywords / peers); None when absent."""
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"config.toml [[companies]] {company!r}: {key} must be a list of strings")
    out: list[str] = []
    for v in value:
        v = v.strip()
        if v and v.casefold() not in {o.casefold() for o in out}:
            out.append(v)
    return tuple(out)


def _translation_section(raw: dict[str, Any]) -> TranslationSection:
    """Build ``[translation]`` (secrets come from the environment, never TOML)."""
    merged = _merge_section("translation", raw or {})
    langs = merged.get("languages") or []
    if isinstance(langs, str):
        langs = [langs]
    merged["languages"] = tuple(str(l).strip().lower() for l in langs if str(l).strip())
    if merged["max_chars_per_run"] < 0 or merged["max_chars_per_article"] < 0:
        raise ConfigError("config.toml [translation]: character limits must be >= 0")
    return TranslationSection(**merged)


def normalize_blocked_domains(values: object) -> tuple[str, ...]:
    """Normalise ``[collect] blocked_domains`` entries to bare lower-case hosts.

    Accepts ``"fool.com"``, ``"www.fool.com"`` or ``"https://www.fool.com/x"``.
    """
    if not values:
        return ()
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        raise ConfigError("[collect] blocked_domains must be a list of domains")
    out: list[str] = []
    for raw in values:
        host = str(raw or "").strip().lower()
        if "://" in host:
            host = host.split("://", 1)[1]
        host = host.split("/", 1)[0].split(":", 1)[0].strip(".")
        if host.startswith("www."):
            host = host[4:]
        if host and host not in out:
            out.append(host)
    return tuple(out)


def load_config(
    config_path: str | Path | None = None,
    *,
    project_root: str | Path | None = None,
) -> AppConfig:
    """Load and validate the application configuration (see module docstring)."""
    # Determine the project root BEFORE touching .env so an explicit
    # ``--config /tmp/.../conf/config.toml`` never loads the developer's real
    # repository ``.env`` (test isolation, specs §15 offline tests).
    if config_path is not None:
        candidate = Path(config_path).expanduser().resolve()
        root = Path(project_root).resolve() if project_root else _infer_project_root(candidate)
    else:
        root = Path(project_root).resolve() if project_root else default_project_root()

    # Load the project's .env FIRST so its variables feed ${VAR} resolution
    # and env-overrides below. Real environment variables always win
    # (override=False). Guarded so python-dotenv never searches upward from
    # the CWD and picks up an unrelated .env.
    dotenv_path = root / ".env"
    if dotenv_path.is_file():
        load_dotenv(dotenv_path, override=False)

    if config_path is not None:
        path = Path(config_path).expanduser().resolve()
        if not path.is_file():
            raise ConfigError(f"config file not found: {path}")
        root = _infer_project_root(path)
        fallback = False
    else:
        path = root / "conf" / "config.toml"
        fallback = not path.is_file()
        if fallback:
            path = root / "conf" / "config.example.toml"
        if not path.is_file():
            raise ConfigError(
                f"no configuration found: create {root / 'conf' / 'config.toml'} "
                f"(template: conf/config.example.toml)"
            )

    if fallback:
        logger.warning("conf/config.toml missing — loading example config as degraded fallback: %s", path)

    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    raw = _resolve_deep(raw)

    # Sections merged over defaults.
    app_raw = _merge_section("app", raw.get("app", {}))
    storage_raw = _merge_section("storage", raw.get("storage", {}))
    collect_raw = _merge_section("collect", raw.get("collect", {}))
    dedup_raw = _merge_section("dedup", raw.get("dedup", {}))
    scoring_raw = _merge_section("scoring", raw.get("scoring", {}))
    llm_raw = _merge_section("llm", raw.get("llm", {}))
    logging_raw = _merge_section("logging", raw.get("logging", {}))

    logging_level = str(logging_raw["level"]).upper()
    if logging_level not in _VALID_LOG_LEVELS:
        raise ConfigError(f"[logging].level must be one of {sorted(_VALID_LOG_LEVELS)}")

    try:
        ZoneInfo(str(app_raw["timezone"]))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"[app].timezone '{app_raw['timezone']}' is not a valid IANA timezone") from exc

    llm = LLMSection(
        enabled=bool(llm_raw["enabled"]),
        model=str(os.environ.get("WATSONX_MODEL_ID") or llm_raw["model"]),
        endpoint=str(os.environ.get("WATSONX_URL") or llm_raw["endpoint"]),
        project_id=str(os.environ.get("WATSONX_PROJECT_ID") or llm_raw["project_id"]),
        api_key=str(os.environ.get("WATSONX_API_KEY", "")),
        max_input_chars=int(llm_raw["max_input_chars"]),
        max_new_tokens=int(llm_raw["max_new_tokens"]),
        temperature=float(llm_raw["temperature"]),
        top_p=float(llm_raw["top_p"]),
        timeout_seconds=int(llm_raw["timeout_seconds"]),
        max_retries=int(llm_raw["max_retries"]),
        circuit_breaker_threshold=int(llm_raw["circuit_breaker_threshold"]),
        max_calls_per_run=int(llm_raw["max_calls_per_run"]),
    )

    storage = StorageSection(
        db_path=_anchor(str(storage_raw["db_path"]), root),
        reports_dir=_anchor(str(storage_raw["reports_dir"]), root),
        backups_dir=_anchor(str(storage_raw["backups_dir"]), root),
        logs_dir=_anchor(str(storage_raw["logs_dir"]), root),
        retention_days=int(storage_raw["retention_days"]),
        backup_enabled=bool(storage_raw["backup_enabled"]),
    )

    sources = _parse_source_seeds(raw.get("sources", []))
    companies = _parse_company_seeds(raw.get("companies", []))

    # FINNHUB_API_KEY: environment wins over TOML value (same pattern as WATSONX_API_KEY).
    collect_raw["finnhub_api_key"] = str(
        os.environ.get("FINNHUB_API_KEY") or collect_raw.get("finnhub_api_key", "")
    )

    return AppConfig(
        project_root=root,
        config_path=path,
        app=AppSection(**app_raw),
        storage=storage,
        collect=CollectSection(
            **{
                **collect_raw,
                "blocked_domains": normalize_blocked_domains(collect_raw.get("blocked_domains")),
            }
        ),
        dedup=DedupSection(**dedup_raw),
        scoring=ScoringSection(**scoring_raw),
        llm=llm,
        logging=LoggingSection(**logging_raw),
        sources=sources,
        companies=companies,
        translation=_translation_section(raw.get("translation", {})),
        weekly=WeeklySection(**_merge_section("weekly", raw.get("weekly", {}) or {})),
    )