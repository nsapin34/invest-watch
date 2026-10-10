"""Pre-flight service pings — P1 item 8 (doctor + run-start check).

``invest-watch doctor`` (dedicated command) and the orchestrator (start of
every run) validate the three remote services **before** burning requests:

* **watsonx.ai** — exchanges the IBM Cloud API key for an IAM token
  (``https://iam.cloud.ibm.com/identity/token``).  A 200 means the key is
  valid; 400/401/403 means *clé invalide*.  This is an honest key check
  without spending model tokens.
* **Brave Search** — one minimal news search (``count=1``).  200 → key ok;
  401/403 → invalid key; 429 → key valid but rate-limited (counts as ok).
* **Finnhub** — one minimal quote call (``symbol=AAPL``).  Same mapping.

The orchestrator uses the results to **skip cleanly** instead of spraying
raw 403s across the whole run: an invalid watsonx key installs a
:class:`~invest_watch.llm_client.DisabledLLMClient` (every summary degrades
immediately with the reason ``clé invalide``), and an invalid Finnhub/Brave
key puts the engine on the ``disabled_engines`` list so the collector records
one ``invalid_key`` error per source instead of one HTTP failure per query.

Every ping is best-effort: a network failure downgrades to ``unreachable``
and never aborts the run.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

from .config_manager import AppConfig

logger = logging.getLogger(__name__)

_IAM_TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"
_BRAVE_PING_URL = "https://api.search.brave.com/res/v1/news/search?q=test&count=1"
_FINNHUB_PING_URL = "https://finnhub.io/api/v1/quote?symbol=AAPL"
#: Endpoint that actually matters for collection — used by the symbol-coverage
#: check (a quote call never reveals whether `company-news` serves a listing).
_FINNHUB_NEWS_URL = "https://finnhub.io/api/v1/company-news"

_DEFAULT_TIMEOUT = 10.0

_PLACEHOLDER_MARKERS = ("your-", "changeme", "placeholder", "xxx", "<your", "todo")

# Status vocabulary (kept stable — surfaced in logs, CLI and stats_json)
ST_OK = "ok"
ST_INVALID_KEY = "invalid_key"
ST_MISSING_KEY = "missing_key"
ST_UNREACHABLE = "unreachable"
ST_OFF = "off"
ST_SKIPPED = "skipped"
ST_PARTIAL = "partial"  # additive: key ok but some configured symbols are unserved


@dataclass(frozen=True)
class ServicePing:
    """Outcome of one pre-flight check.

    Attributes:
        service: ``watsonx`` | ``brave`` | ``finnhub``.
        ok:      True when the service can be used as configured.
        status:  One of ``ok / invalid_key / missing_key / unreachable /
                 off / skipped``.
        message: Human-readable detail (never contains the key itself).
    """

    service: str
    ok: bool
    status: str
    message: str = ""


def _is_placeholder_key(value: str) -> bool:
    lowered = (value or "").lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _http_client(client: httpx.Client | None) -> httpx.Client:
    if client is not None:
        return client
    return httpx.Client(timeout=_DEFAULT_TIMEOUT, follow_redirects=True)


def ping_watsonx(
    cfg: AppConfig,
    *,
    client: httpx.Client | None = None,
) -> ServicePing:
    """Validate the watsonx.ai API key by exchanging it for an IAM token."""
    if not cfg.llm.enabled:
        return ServicePing("watsonx", True, ST_OFF, "LLM disabled (off mode)")
    api_key = cfg.llm.api_key or ""
    if not api_key or _is_placeholder_key(api_key):
        return ServicePing(
            "watsonx", False, ST_MISSING_KEY,
            "clé manquante ou placeholder (WATSONX_API_KEY)",
        )
    own_client = client is None
    http = _http_client(client)
    try:
        response = http.post(
            _IAM_TOKEN_URL,
            data={
                "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
                "apikey": api_key,
            },
        )
        if response.status_code == 200:
            return ServicePing("watsonx", True, ST_OK, "clé valide (token IAM obtenu)")
        if response.status_code in (400, 401, 403):
            return ServicePing(
                "watsonx", False, ST_INVALID_KEY,
                f"clé invalide (IAM http_{response.status_code})",
            )
        return ServicePing(
            "watsonx", False, ST_UNREACHABLE,
            f"IAM http_{response.status_code} — réessayer plus tard",
        )
    except httpx.HTTPError as exc:
        return ServicePing("watsonx", False, ST_UNREACHABLE, f"réseau: {exc}")
    finally:
        if own_client:
            http.close()


def ping_brave(
    cfg: AppConfig,
    *,
    client: httpx.Client | None = None,
    key: str | None = None,
) -> ServicePing:
    """Validate the Brave Search API key with one minimal news search."""
    resolved = key if key is not None else os.environ.get("BRAVE_API_KEY", "")
    if not resolved or _is_placeholder_key(resolved):
        return ServicePing(
            "brave", False, ST_MISSING_KEY,
            "clé manquante ou placeholder (BRAVE_API_KEY)",
        )
    own_client = client is None
    http = _http_client(client)
    try:
        response = http.get(
            _BRAVE_PING_URL,
            headers={"Accept": "application/json", "X-Subscription-Token": resolved},
        )
        if response.status_code == 200:
            return ServicePing("brave", True, ST_OK, "clé valide")
        if response.status_code == 429:
            # Rate-limited but authenticated — the key works.
            return ServicePing("brave", True, ST_OK, "clé valide (rate-limited 429)")
        if response.status_code in (401, 403):
            return ServicePing(
                "brave", False, ST_INVALID_KEY,
                f"clé invalide (http_{response.status_code})",
            )
        return ServicePing(
            "brave", False, ST_UNREACHABLE,
            f"http_{response.status_code} — réessayer plus tard",
        )
    except httpx.HTTPError as exc:
        return ServicePing("brave", False, ST_UNREACHABLE, f"réseau: {exc}")
    finally:
        if own_client:
            http.close()


def ping_finnhub(
    cfg: AppConfig,
    *,
    client: httpx.Client | None = None,
    key: str | None = None,
) -> ServicePing:
    """Validate the Finnhub API key with one minimal quote call."""
    resolved = (
        key if key is not None
        else (cfg.collect.finnhub_api_key or os.environ.get("FINNHUB_API_KEY", ""))
    )
    if not resolved or _is_placeholder_key(resolved):
        return ServicePing(
            "finnhub", False, ST_MISSING_KEY,
            "clé manquante ou placeholder (FINNHUB_API_KEY)",
        )
    own_client = client is None
    http = _http_client(client)
    try:
        response = http.get(f"{_FINNHUB_PING_URL}&token={resolved}")
        if response.status_code == 200:
            return ServicePing("finnhub", True, ST_OK, "clé valide")
        if response.status_code == 429:
            return ServicePing("finnhub", True, ST_OK, "clé valide (rate-limited 429)")
        if response.status_code in (401, 403):
            return ServicePing(
                "finnhub", False, ST_INVALID_KEY,
                f"clé invalide (http_{response.status_code})",
            )
        return ServicePing(
            "finnhub", False, ST_UNREACHABLE,
            f"http_{response.status_code} — réessayer plus tard",
        )
    except httpx.HTTPError as exc:
        return ServicePing("finnhub", False, ST_UNREACHABLE, f"réseau: {exc}")
    finally:
        if own_client:
            http.close()


# ── Symbol coverage (Finnhub entitlement) ─────────────────────────────────────


def configured_finnhub_symbols(cfg: AppConfig, *, db=None) -> list[str]:
    """Curated Finnhub symbols to check: active DB companies, else config seeds.

    Returns a de-duplicated list preserving discovery order (DB first).  Never
    raises: a pre-v005 database (no ``finnhub_symbol`` column) falls back to the
    ``[[companies]]`` seeds of ``config.toml``.
    """
    symbols: list[str] = []

    def _push(value: str | None) -> None:
        cleaned = (value or "").strip()
        if cleaned and cleaned not in symbols:
            symbols.append(cleaned)

    if db is not None:
        try:
            rows = db.query(
                "SELECT DISTINCT finnhub_symbol FROM company"
                " WHERE active = 1 AND finnhub_symbol IS NOT NULL"
                " AND TRIM(finnhub_symbol) <> '' ORDER BY finnhub_symbol"
            )
            for row in rows:
                _push(row["finnhub_symbol"])
        except Exception as exc:  # noqa: BLE001 — config seeds still apply
            logger.debug("doctor: could not read finnhub symbols — %s", exc)

    if not symbols:
        for seed in getattr(cfg, "companies", ()) or ():
            _push(getattr(seed, "finnhub_symbol", None))

    return symbols


def ping_finnhub_symbols(
    cfg: AppConfig,
    symbols: list[str] | tuple[str, ...],
    *,
    client: httpx.Client | None = None,
    key: str | None = None,
    max_symbols: int = 25,
) -> ServicePing:
    """Check which configured symbols the current Finnhub plan actually serves.

    One ``/company-news`` call per symbol (the endpoint that actually matters),
    bounded by ``max_symbols``.  ``200`` → served; ``403``/``404`` → *not
    covered by the subscription* (``You don't have access to this resource``);
    ``429`` → stop early (rate-limited, coverage unknown).

    ``ok`` is True only when every checked symbol is served; a partial result
    uses the additive ``partial`` status, which never disables the engine
    (:func:`disabled_engines` only reacts to ``invalid_key``).
    """
    resolved = (
        key if key is not None
        else (cfg.collect.finnhub_api_key or os.environ.get("FINNHUB_API_KEY", ""))
    )
    if not resolved or _is_placeholder_key(resolved):
        return ServicePing(
            "finnhub_symbols", False, ST_MISSING_KEY,
            "clé manquante ou placeholder (FINNHUB_API_KEY)",
        )
    if not symbols:
        return ServicePing(
            "finnhub_symbols", True, ST_SKIPPED, "aucun symbole finnhub_symbol configuré"
        )

    checked = list(symbols)[:max_symbols]
    to_date = date.today().isoformat()
    from_date = (date.today() - timedelta(days=3)).isoformat()

    owned_client = client is None
    http = _http_client(client)
    served: list[str] = []
    uncovered: list[str] = []
    try:
        for symbol in checked:
            try:
                response = http.get(
                    f"{_FINNHUB_NEWS_URL}?symbol={symbol}"
                    f"&from={from_date}&to={to_date}&token={resolved}"
                )
            except httpx.HTTPError as exc:
                return ServicePing(
                    "finnhub_symbols", False, ST_UNREACHABLE,
                    f"arrêt après {len(checked)} symbole(s) — réseau: {exc}",
                )
            if response.status_code == 200:
                served.append(symbol)
                continue
            if response.status_code in (401, 403, 404):
                uncovered.append(symbol)
                continue
            return ServicePing(
                "finnhub_symbols", False, ST_UNREACHABLE,
                f"http_{response.status_code} sur '{symbol}' — couverture indéterminée",
            )
    finally:
        if owned_client:
            http.close()

    if not uncovered:
        return ServicePing(
            "finnhub_symbols", True, ST_OK,
            f"{len(served)}/{len(checked)} symboles servis",
        )
    shown = ", ".join(uncovered[:6]) + ("…" if len(uncovered) > 6 else "")
    return ServicePing(
        "finnhub_symbols", False, ST_PARTIAL,
        f"{len(served)}/{len(checked)} symboles servis — non couverts: {shown}",
    )


# ── Orchestration of the pings ────────────────────────────────────────────────


def _active_sources(cfg: AppConfig) -> list[str]:
    """Lowercased ``name + url`` of every active source declared in config.toml.

    Mirrors the source hints used by ``search_engine`` so the doctor only
    pings engines an operator actually configured.
    """
    names: list[str] = []
    for source in getattr(cfg, "sources", []) or []:
        if source.active:
            names.append(f"{source.name} {source.url or ''}".lower())
    return names


def _hinted(name_url: list[str], *hints: str) -> bool:
    return any(any(h in entry for h in hints) for entry in name_url)


def run_preflight(
    cfg: AppConfig,
    *,
    db=None,
    client: httpx.Client | None = None,
    check_symbols: bool = False,
) -> list[ServicePing]:
    """Run every relevant pre-flight ping and return the results in order.

    Relevance rules (a non-relevant service is ``skipped``, which is ``ok``):
    * watsonx — pinged when ``llm.enabled``; skipped otherwise.
    * finnhub / brave — pinged when an active ``[[sources]]`` entry hints at
      the engine, or (fallback) when ``db`` has an active matching source row.
    * finnhub_symbols — only when ``check_symbols`` is True (``invest-watch
      doctor``).  It costs one ``company-news`` call per configured symbol, so
      the orchestrator leaves it off to keep run-time API usage at zero.
    """
    name_url = _active_sources(cfg)
    db_finnhub = db_brave = False
    if db is not None:
        try:
            rows = db.query("SELECT name, url FROM source WHERE active = 1")
            db_name_url = [f"{r['name']} {r['url'] or ''}".lower() for r in rows]
            db_finnhub = _hinted(db_name_url, "finnhub", "finnhub.io")
            db_brave = _hinted(db_name_url, "brave", "api.search.brave.com")
        except Exception as exc:  # non-fatal — config hints still apply
            logger.debug("doctor: could not read source rows — %s", exc)

    pings: list[ServicePing] = [ping_watsonx(cfg, client=client)]
    finnhub_relevant = _hinted(name_url, "finnhub", "finnhub.io") or db_finnhub
    if finnhub_relevant:
        pings.append(ping_finnhub(cfg, client=client))
        if check_symbols:
            pings.append(
                ping_finnhub_symbols(
                    cfg, configured_finnhub_symbols(cfg, db=db), client=client
                )
            )
    else:
        pings.append(ServicePing("finnhub", True, ST_SKIPPED, "aucune source Finnhub active"))
    if _hinted(name_url, "brave", "api.search.brave.com") or db_brave:
        pings.append(ping_brave(cfg, client=client))
    else:
        pings.append(ServicePing("brave", True, ST_SKIPPED, "aucune source Brave active"))
    return pings


def disabled_engines(pings: list[ServicePing]) -> frozenset[str]:
    """Engines whose key is *invalid* (401/403) — skip without network calls.

    Missing keys are NOT listed: :class:`~invest_watch.search_engine.SearchEngine`
    already skips them natively, and an absent key is a known degraded state
    (see ``_warn_on_missing_api_keys``) rather than an authenticated failure.
    """
    return frozenset(
        p.service for p in pings
        if not p.ok and p.status == ST_INVALID_KEY and p.service in ("finnhub", "brave")
    )


def watsonx_invalid(pings: list[ServicePing]) -> bool:
    """True when the watsonx key is definitively invalid (IAM 401/403)."""
    return any(
        p.service == "watsonx" and p.status == ST_INVALID_KEY for p in pings
    )


def format_doctor_report(pings: list[ServicePing]) -> str:
    """Human-readable multi-line report for ``invest-watch doctor``."""
    glyph = {ST_OK: "[ok]", ST_INVALID_KEY: "[FAIL]", ST_MISSING_KEY: "[FAIL]",
             ST_UNREACHABLE: "[FAIL]", ST_OFF: "[off]", ST_SKIPPED: "[skip]",
             ST_PARTIAL: "[warn]"}
    lines = ["Pré-vol des services (doctor):"]
    for p in pings:
        lines.append(f"  {glyph.get(p.status, '[?]'):6s} {p.service:8s} {p.status:12s} {p.message}")
    return "\n".join(lines)
