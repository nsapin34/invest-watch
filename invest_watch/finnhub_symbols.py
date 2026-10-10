"""Normalisation of Finnhub API symbols (option A).

Finnhub ``/company-news?symbol=`` only accepts an **exchange symbol**, never
free text: ``symbol=AIQUY`` works, ``symbol=Air Liquide`` returns an empty
list, ``symbol=SpaceX`` returns HTTP 403 ("You don't have access to this
resource").  A bare ``3750`` / ``2899`` without an exchange suffix is the
same class of error — the API expects ``03750.HK``, ``300750.SZ``,
``02899.HK``, ``601899.SS``, ``03393.HK`` while ``HSAI`` / ``KSTR`` are
already complete US symbols.

This module centralises the helpers used at config load time:

* :func:`normalize_finnhub_symbol` — canonical form (strip, upper-case,
  remove inner whitespace);
* :func:`suggest_finnhub_symbol` — best-effort qualified symbol derived from
  a bare listing ``ticker`` + ``country``/``currency`` context, used only to
  emit a guiding ``logger.warning`` (never silently substituted: an HKEX
  company also has an OTC ADR — e.g. ``ZIJMY`` — and picking the wrong
  venue would fetch the wrong news stream).
* :func:`is_bare_numeric_ticker` / :func:`warn_if_bare_ticker` — detection
  + shared one-line guidance used by both ``config_manager`` and ``cli``.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Same shape as config_manager._FINNHUB_SYMBOL_RE is intentionally NOT
# duplicated here: validation stays in config_manager (ConfigError) so all
# config errors keep a single origin.
_BARE_NUMERIC_RE = re.compile(r"^[0-9]{1,6}$")

_HK_CURRENCIES = {"HKD"}
_HK_COUNTRIES = {"HK", "HKG", "CN", "CHN"}


def normalize_finnhub_symbol(raw: str | None) -> str | None:
    """Return the canonical form of *raw* (``None``/blank → ``None``).

    Canonicalisation is deliberately conservative: strip surrounding
    whitespace, drop inner spaces, upper-case.  ``' 03750.hk '`` →
    ``'03750.HK'``; ``'hsai'`` → ``'HSAI'``.  The result is NOT validated
    here — validation stays in ``config_manager`` (``ConfigError``) so all
    config errors keep a single origin.
    """
    if raw is None:
        return None
    cleaned = re.sub(r"\s+", "", str(raw)).strip().upper()
    return cleaned or None


def is_bare_numeric_ticker(ticker: str | None) -> bool:
    """True when *ticker* looks like an unsuffixed Asian listing code."""
    if not ticker:
        return False
    return bool(_BARE_NUMERIC_RE.match(ticker.strip()))


def suggest_finnhub_symbol(
    ticker: str | None,
    *,
    country: str | None = None,
    currency: str | None = None,
) -> str | None:
    """Suggest a qualified Finnhub symbol for a bare numeric *ticker*.

    Rules (documented, minimal, no network):

    * 4 digits + HKD/CN/HK context → zero-padded 5-digit ``.HK``
      (``3750`` → ``03750.HK``, ``2899`` → ``02899.HK``,
      ``3393`` → ``03393.HK``);
    * 6 digits starting with ``0``/``3`` → ``.SZ`` (Shenzhen,
      ``300750`` → ``300750.SZ``);
    * 6 digits starting with ``6`` → ``.SS`` (Shanghai,
      ``601899`` → ``601899.SS``);
    * anything else → ``None`` (US tickers such as ``HSAI``/``KSTR`` are
      already complete and never reach this path).
    """
    if not ticker:
        return None
    digits = ticker.strip()
    if not _BARE_NUMERIC_RE.match(digits):
        return None
    country_u = (country or "").strip().upper()
    currency_u = (currency or "").strip().upper()
    if len(digits) <= 4 and (currency_u in _HK_CURRENCIES or country_u in _HK_COUNTRIES or not country_u):
        return f"{digits.zfill(5)}.HK"
    if len(digits) == 6:
        if digits[0] in ("0", "3"):
            return f"{digits}.SZ"
        if digits[0] == "6":
            return f"{digits}.SS"
    if len(digits) == 5:
        # Already padded but suffix missing (e.g. "03750") → HKEX.
        if currency_u in _HK_CURRENCIES or country_u in _HK_COUNTRIES or not country_u:
            return f"{digits}.HK"
    return None


def warn_if_bare_ticker(
    company_name: str,
    ticker: str | None,
    finnhub_symbol: str | None,
    *,
    country: str | None = None,
    currency: str | None = None,
) -> None:
    """Emit a one-line guidance warning for a bare numeric ticker.

    No-op when ``finnhub_symbol`` is set (curated symbol wins) or when the
    ticker is not a bare number.  Pure logging helper so both
    ``config_manager`` (TOML seeds) and ``cli`` (``company add/edit``) share
    the exact same wording.
    """
    if finnhub_symbol or not is_bare_numeric_ticker(ticker):
        return
    suggestion = suggest_finnhub_symbol(ticker, country=country, currency=currency)
    hint = f" — did you mean finnhub_symbol='{suggestion}'?" if suggestion else ""
    logger.warning(
        "[[companies]] '%s': ticker '%s' has no exchange suffix and is not a "
        "valid Finnhub symbol%s Set finnhub_symbol explicitly (e.g. 03750.HK, "
        "300750.SZ, 02899.HK, 601899.SS, 03393.HK) or omit it when the "
        "listing is not covered by the Finnhub plan.",
        company_name,
        (ticker or "").strip(),
        hint,
    )
