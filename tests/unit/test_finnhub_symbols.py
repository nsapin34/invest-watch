"""Unit tests for finnhub_symbols (option A — bare tickers without suffix).

Covers: normalisation, HKEX zero-padding, SZ/SS inference, US symbols
left untouched, and the warn-if-bare helper wording.
"""

import logging

from invest_watch.finnhub_symbols import (
    is_bare_numeric_ticker,
    normalize_finnhub_symbol,
    suggest_finnhub_symbol,
    warn_if_bare_ticker,
)


def test_normalize_strips_uppercases_and_dedots():
    assert normalize_finnhub_symbol("  03750.hk ") == "03750.HK"
    assert normalize_finnhub_symbol("hsai") == "HSAI"
    assert normalize_finnhub_symbol("  ") is None
    assert normalize_finnhub_symbol(None) is None


def test_bare_numeric_detection():
    assert is_bare_numeric_ticker("3750")
    assert is_bare_numeric_ticker("2899")
    assert is_bare_numeric_ticker("300750")
    assert not is_bare_numeric_ticker("HSAI")
    assert not is_bare_numeric_ticker("KSTR")
    assert not is_bare_numeric_ticker("03750.HK")
    assert not is_bare_numeric_ticker(None)
    assert not is_bare_numeric_ticker("")


def test_suggest_hkex_zero_padded():
    assert suggest_finnhub_symbol("3750", country="CN", currency="HKD") == "03750.HK"
    assert suggest_finnhub_symbol("2899", country="CN", currency="HKD") == "02899.HK"
    assert suggest_finnhub_symbol("3393", country="CN", currency="HKD") == "03393.HK"
    # 5-digit HKEX code without suffix → qualifier ajouté.
    assert suggest_finnhub_symbol("03750", country="CN", currency="HKD") == "03750.HK"


def test_suggest_mainland_exchanges():
    assert suggest_finnhub_symbol("300750", country="CN") == "300750.SZ"
    assert suggest_finnhub_symbol("601899", country="CN") == "601899.SS"


def test_suggest_returns_none_for_complete_symbols():
    assert suggest_finnhub_symbol("HSAI") is None
    assert suggest_finnhub_symbol("KSTR") is None
    assert suggest_finnhub_symbol("AIQUY") is None
    assert suggest_finnhub_symbol(None) is None


def test_warn_if_bare_ticker_only_for_bare_without_symbol(caplog):
    with caplog.at_level(logging.WARNING, logger="invest_watch.finnhub_symbols"):
        warn_if_bare_ticker("CATL", "3750", None, country="CN", currency="HKD")
    assert "03750.HK" in caplog.text
    assert "CATL" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="invest_watch.finnhub_symbols"):
        # Curated symbol wins → silence.
        warn_if_bare_ticker("CATL", "3750", "03750.HK", country="CN", currency="HKD")
        # Complete US ticker → silence.
        warn_if_bare_ticker("Hesai", "HSAI", None, country="CN", currency="USD")
    assert caplog.text == ""
