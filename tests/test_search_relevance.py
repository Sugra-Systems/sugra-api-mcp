"""Search relevance tests: pattern detection + top-1 contract.

Companion to the live ChatGPT MCP feedback loop. Baseline measurement
(2026-05-20 against the bundled 1293-endpoint catalog) before the
pattern-aware boosts: 16.7% top-1, 60% miss-rate. After: 56.7% top-1,
20% miss-rate. These tests pin the regressions that drove that gain so a
future search-algorithm change cannot silently regress equity / forex /
central-bank routing without explicit failures here.
"""

from __future__ import annotations

import pytest

from sugra_api_mcp.catalog.aliases import (
    detect_currency_pairs,
    detect_tickers,
    matching_central_bank_prefixes,
)
from sugra_api_mcp.catalog.loader import load_catalog
from sugra_api_mcp.catalog.search import search_catalog

# ---- Pattern detection unit tests ----


@pytest.mark.parametrize(
    "query,expected",
    [
        ("AAPL price", ["AAPL"]),
        ("Apple stock price", []),  # 'Apple' lowercase 'a', not all-caps
        ("Compare MSFT and GOOG", ["MSFT", "GOOG"]),
        ("BRK.A holders", ["BRK.A"]),
        # Non-ticker uppercase words must be excluded.
        ("US CPI inflation", []),
        ("BTC market cap", []),
        ("USD JPY rate", []),
        ("FED interest rate", []),
        ("ETF flows", []),
    ],
)
def test_detect_tickers(query: str, expected: list[str]) -> None:
    assert detect_tickers(query) == expected


@pytest.mark.parametrize(
    "query,expected",
    [
        ("EUR USD exchange rate", [("EUR", "USD")]),
        ("EUR/USD", [("EUR", "USD")]),
        ("EURUSD", [("EUR", "USD")]),
        ("USD-JPY rate", [("USD", "JPY")]),
        # Two pairs in one query.
        ("EUR USD and GBP JPY", [("EUR", "USD"), ("GBP", "JPY")]),
        # Unknown currency code is rejected.
        ("FOO BAR exchange", []),
        # Same code twice is not a pair.
        ("USD USD spot", []),
        ("price of gold", []),
    ],
)
def test_detect_currency_pairs(query: str, expected: list[tuple[str, str]]) -> None:
    assert detect_currency_pairs(query) == expected


@pytest.mark.parametrize(
    "query,expected_prefixes",
    [
        ("FED interest rate", ["fed_"]),
        ("Federal Reserve policy rate", ["fed_"]),
        ("FOMC decision", ["fed_"]),
        ("ECB main rate", ["ecb_"]),
        ("European Central Bank deposit facility", ["ecb_"]),
        ("Bank of Japan policy rate", ["boj_"]),
        ("BOJ rate", ["boj_"]),
        ("BOE bank rate", ["boe_"]),
        ("RBA cash rate", ["rba_"]),
        # No central bank reference -> empty.
        ("Apple price", []),
        ("Crypto market data", []),
    ],
)
def test_matching_central_bank_prefixes(query: str, expected_prefixes: list[str]) -> None:
    assert matching_central_bank_prefixes(query) == expected_prefixes


# ---- Search relevance contract (against the bundled production catalog) ----


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


@pytest.mark.parametrize(
    "query,must_be_in_top_1",
    [
        # Equity tickers routed to /api/v2/quotes/* family.
        ("AAPL price", {"quotes_symbol_price"}),
        ("Apple stock price", {"quotes_symbol_price"}),
        # MSFT earnings: either quotes_symbol_earnings_* (ticker-routed) or
        # finnhub_calendar_earnings (calendar-routed) is acceptable - both lead
        # the LLM to a valid endpoint. We're testing that ticker boost lands SOMETHING
        # earnings-related top-1, not that we override finnhub.
        ("MSFT earnings", {
            "quotes_symbol_earnings_history", "quotes_symbol_earnings_events",
            "quotes_symbol_calendar", "finnhub_calendar_earnings", "finnhub_earnings",
        }),
        ("Apple dividends", {"quotes_symbol_dividend", "quotes_symbol_actions"}),
        ("Tesla market cap", {"quotes_symbol_market_cap", "quotes_symbol_summary"}),
        # Crypto: BTC token disambiguated from equity tickers via crypto-context.
        ("Bitcoin price", {"crypto_coin_id_price", "mempool_price"}),
        # Central bank symbols route to their namespace.
        ("FED interest rate", {"fed_rates_rate_type", "fed_rates"}),
        ("Federal Reserve policy rate", {"fed_rates_rate_type", "fed_rates"}),
        # Forex currency-pair detection routes to forex namespace.
        ("EUR USD exchange rate", {"forex_latest", "forex_pair", "frankfurter_latest"}),
    ],
)
def test_search_top_1_meets_contract(catalog, query: str, must_be_in_top_1: set[str]) -> None:
    results = search_catalog(catalog, query, limit=5)
    assert results, f"search returned no results for {query!r}"
    actual = results[0]["operation_id"]
    assert actual in must_be_in_top_1, (
        f"top-1 for {query!r} was {actual!r}; expected one of {sorted(must_be_in_top_1)}. "
        f"Full top-5: {[r['operation_id'] for r in results]}"
    )


def test_btc_crypto_namespace_present_in_top_3(catalog) -> None:
    """Crypto-context queries must surface crypto-namespace endpoints in top-3.

    Before the crypto-context boost, "BTC market cap" returned five equity
    quotes_symbol_* endpoints with zero crypto results visible. We accept that
    quotes_symbol_market_cap (a generic market-cap endpoint) may still rank
    high due to strong token overlap on "market" + "cap", but crypto endpoints
    must be present so the LLM can pick a sensible one.
    """
    results = search_catalog(catalog, "BTC market cap", limit=5)
    assert results
    top_3_ops = [r["operation_id"] for r in results[:3]]
    crypto_in_top_3 = sum(
        1 for op in top_3_ops
        if op.startswith(("crypto_", "mempool_", "onchain_"))
    )
    assert crypto_in_top_3 >= 1, (
        f"BTC query produced no crypto-namespace endpoint in top-3: {top_3_ops}"
    )


def test_central_bank_boost_narrows_to_correct_namespace(catalog) -> None:
    """All top-5 for ECB queries should be ecb_*, not other central banks."""
    results = search_catalog(catalog, "ECB interest rate", limit=5)
    top_5_ops = [r["operation_id"] for r in results[:5]]
    ecb_count = sum(1 for op in top_5_ops if op.startswith("ecb_"))
    assert ecb_count >= 4, (
        f"ECB query did not concentrate in ecb_* namespace: {top_5_ops}"
    )


def test_forex_boost_does_not_mask_non_forex_results(catalog) -> None:
    """Sanity: queries without a currency pair should not get forex-skewed results."""
    results = search_catalog(catalog, "Apple stock price", limit=5)
    top_5_ops = [r["operation_id"] for r in results[:5]]
    forex_count = sum(1 for op in top_5_ops if op.startswith(("forex_", "frankfurter_", "exchangerate_")))
    assert forex_count == 0, (
        f"Apple stock price query incorrectly pulled in forex endpoints: {top_5_ops}"
    )
