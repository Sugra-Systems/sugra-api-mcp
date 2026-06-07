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
    detect_us_macro_query,
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
        # Single-letter words and common business acronyms were previously
        # mis-classified as tickers; the detector requires >=2 chars and an
        # exclusion list.
        ("I need GDP data", []),       # "I" is one letter, regex now requires >=2
        ("A CPI endpoint", []),         # same
        ("CEO announcement", []),       # CEO in non-ticker list
        ("SEC filing", []),             # SEC in non-ticker list
        ("IRS form 1040", []),          # IRS in non-ticker list
        ("USDT price", []),             # major stablecoin, in non-ticker list
        # AI and IT are real NYSE tickers (C3.ai, Gartner). Without equity
        # context they stay generic English acronyms.
        ("AI revolution", []),          # no equity context -> not a ticker
        ("IT support team", []),        # no equity context -> not a ticker
        # WITH equity context, ambiguous tickers are re-admitted.
        ("AI stock price", ["AI"]),     # equity context "stock price" -> ticker
        ("IT price", ["IT"]),           # equity context "price" -> ticker
        ("AI dividend", ["AI"]),        # equity context "dividend" -> ticker
        ("AI market cap", ["AI"]),      # equity context "market cap" -> ticker
        # Networking acronyms (field test 2026-06-07: IXP parsed as a ticker
        # and routed a network query to top-20 quotes_symbol_*).
        ("IXP peering map", []),
        ("BGP hijack history", []),
        ("ASN lookup for Cogent", []),
        ("RIPE measurement results", []),
        ("CDN and VPN detection", []),
        ("TOR exit node list", []),
        # IP (International Paper) and NAT (Nordic American Tankers) are real
        # NYSE tickers AND core networking acronyms - equity-context gated.
        ("IP geolocation lookup", []),
        ("NAT traversal test", []),
        ("IP stock price", ["IP"]),
        ("NAT dividend history", ["NAT"]),
        # Codex S3 review: equity vocabulary must match whole tokens, not
        # substrings - "Stockholm" satisfied "stock" and re-admitted IP as a
        # ticker; "stockpile" did the same for NAT.
        ("IP address geolocation Stockholm", []),
        ("NAT gateway stockpile audit", []),
        # Bare "exchange" was dropped from the equity vocabulary (collides
        # with internet exchange + exchange rate); the phrase survives.
        ("IP internet exchange map", []),
        ("IP stock exchange listing", ["IP"]),
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
        # Substring matching previously gave false positives. All three
        # queries below must return empty now that we require word boundaries
        # (without it: "CNBC" matched cnb_, "Boca Raton" matched boc_,
        # "federal debt" matched fed_).
        ("CNBC news headlines", []),
        ("Boca Raton real estate", []),
        ("federal debt ceiling", []),  # "federal" alone isn't "fed"
        ("Greenland weather", []),     # contains "RBA" inside "greenlanRBA"? No - sanity
    ],
)
def test_matching_central_bank_prefixes(query: str, expected_prefixes: list[str]) -> None:
    assert matching_central_bank_prefixes(query) == expected_prefixes


# ---- Search relevance contract (against the bundled production catalog) ----


@pytest.fixture(scope="module")
def catalog():
    return load_catalog()


# Two contract levels:
# - EXACT_TOP_1: only the most stable, single-correct-answer queries. These
#   fail if catalog renames or splits these specific endpoints.
# - NAMESPACE_TOP_1: most queries — assert top-1 lands in a namespace family
#   (prefix or toolset). Tolerates catalog growth, endpoint renames within
#   the same domain, and addition of new "better" endpoints.

EXACT_TOP_1_CASES = [
    # Foundational equity endpoint, stable since v0.4.0.
    ("AAPL price", {"quotes_symbol_price"}),
    ("Apple stock price", {"quotes_symbol_price"}),
    # Bitcoin via the primary crypto coin endpoint or the bitcoin-specific
    # onchain endpoints (mempool_price was renamed to onchain_bitcoin_price
    # in the Tier-C cleanup, see feedback_mcp_registry_sync_with_route_renames).
    ("Bitcoin price", {"crypto_coin_id_price", "mempool_price", "onchain_bitcoin_price"}),
]


@pytest.mark.parametrize("query,must_be_in_top_1", EXACT_TOP_1_CASES)
def test_search_top_1_exact_for_stable_endpoints(catalog, query: str, must_be_in_top_1: set[str]) -> None:
    results = search_catalog(catalog, query, limit=5)
    assert results, f"search returned no results for {query!r}"
    actual = results[0]["operation_id"]
    assert actual in must_be_in_top_1, (
        f"top-1 for {query!r} was {actual!r}; expected one of {sorted(must_be_in_top_1)}. "
        f"Full top-5: {[r['operation_id'] for r in results]}"
    )


NAMESPACE_TOP_1_CASES = [
    # query, allowed top-1 operation_id prefixes (any match)
    # MSFT earnings: finnhub_* were renamed in Tier-C scrub - accept the
    # current top-1 winner `earnings` (the standalone equity-earnings endpoint),
    # `calendar_earnings`, and the ticker-routed quotes_symbol_earnings_*.
    ("MSFT earnings", ("earnings", "calendar_earnings", "quotes_symbol_earnings", "quotes_symbol_calendar")),
    ("Apple dividends", ("quotes_symbol_dividend", "quotes_symbol_actions", "market_calendar_dividends")),
    ("Tesla market cap", ("quotes_symbol_market_cap", "quotes_symbol_summary", "quotes_symbol_info")),
    ("FED interest rate", ("fed_rates", "fed_policy")),
    ("Federal Reserve policy rate", ("fed_rates", "fed_policy")),
    ("EUR USD exchange rate", ("forex_", "frankfurter_", "exchangerate_")),
]


@pytest.mark.parametrize("query,allowed_prefixes", NAMESPACE_TOP_1_CASES)
def test_search_top_1_lands_in_correct_namespace(
    catalog, query: str, allowed_prefixes: tuple[str, ...]
) -> None:
    results = search_catalog(catalog, query, limit=5)
    assert results, f"search returned no results for {query!r}"
    actual = results[0]["operation_id"]
    assert actual.startswith(allowed_prefixes), (
        f"top-1 for {query!r} was {actual!r}; expected operation_id starting with "
        f"one of {allowed_prefixes}. Full top-5: {[r['operation_id'] for r in results]}"
    )


def test_crypto_context_query_surfaces_crypto_namespace(catalog) -> None:
    """Crypto-context queries must surface crypto-namespace endpoints in top-3.

    Before the crypto-context boost, "BTC market cap" returned five equity
    quotes_symbol_* endpoints with zero crypto results visible.
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


def test_crypto_context_suppresses_ticker_boost_for_real_ticker_shape(catalog) -> None:
    """The strongest anti-boost proof: a token that LOOKS like a ticker but is
    a crypto symbol must NOT trigger ticker -> quotes_symbol boost.

    BTC is already in _NON_TICKER_WORDS, so detect_tickers() returns [] before
    the crypto suppression even matters. USDT / USDC also start as 4-letter
    uppercase tokens that detect_tickers() would happily return as tickers
    if they weren't in the exclusion list. Combined with the crypto-context
    check, "USDT price" must concentrate the top-3 in crypto-namespace
    endpoints rather than landing on quotes_symbol_price.
    """
    # USDT is a 4-letter uppercase token that without _NON_TICKER_WORDS
    # exclusion would be detected as a ticker by detect_tickers().
    results = search_catalog(catalog, "USDT price", limit=5)
    assert results
    top_3_ops = [r["operation_id"] for r in results[:3]]
    quotes_symbol_count = sum(1 for op in top_3_ops if op.startswith("quotes_symbol_"))
    assert quotes_symbol_count == 0, (
        f"USDT (crypto stablecoin) query leaked to equity endpoints: {top_3_ops}"
    )


def test_network_field_log_query_routes_to_network_endpoints(catalog) -> None:
    """Field test 2026-06-07 (Claude Desktop + hosted MCP, RIPE Labs use case):
    this exact query returned top-20 all quotes_symbol_* because IXP passed
    the ticker regex (score 25 = pure ticker boost, zero token relevance).
    After the fix the ticker boost must not fire and Sugra Net Atlas
    endpoints must dominate.
    """
    query = "network country internet exchange IXP traceroute ping measurement create"
    results = search_catalog(catalog, query, limit=20)
    assert results
    ops = [r["operation_id"] for r in results]
    quotes_leaks = [op for op in ops if op.startswith("quotes_symbol_")]
    assert not quotes_leaks, (
        f"network field-log query still leaks equity endpoints: {quotes_leaks}"
    )
    top_5 = ops[:5]
    network_in_top_5 = sum(1 for op in top_5 if op.startswith("network_"))
    assert network_in_top_5 >= 3, (
        f"expected network_* to dominate top-5, got {top_5}"
    )


def test_network_dominance_suppresses_unknown_ticker_shaped_token(catalog) -> None:
    """A token that LOOKS like a ticker (unknown 4-letter uppercase) must not
    trigger the quotes_symbol_* boost when the query is dominated by
    network-domain vocabulary - mirrors the crypto-context suppression.
    """
    results = search_catalog(catalog, "ZZXQ traceroute peering probe", limit=5)
    top_ops = [r["operation_id"] for r in results[:5]]
    quotes_count = sum(1 for op in top_ops if op.startswith("quotes_symbol_"))
    assert quotes_count == 0, (
        f"network-dominated query leaked to equity endpoints: {top_ops}"
    )


def test_network_dominance_survives_equity_substring_false_positives(catalog) -> None:
    """Codex S3 review: 'Stockholm' must not satisfy the 'stock' equity
    override (substring match) and 'internet exchange' must not satisfy
    'exchange' - either would re-enable the ticker boost on a clearly
    network-domain query carrying a ticker-shaped token.
    """
    for query in (
        "ZZXQ traceroute peering Stockholm",
        "ZZXQ internet exchange peering map",
    ):
        results = search_catalog(catalog, query, limit=5)
        top_ops = [r["operation_id"] for r in results[:5]]
        quotes_count = sum(1 for op in top_ops if op.startswith("quotes_symbol_"))
        assert quotes_count == 0, (
            f"equity-substring false positive re-enabled ticker boost for "
            f"{query!r}: {top_ops}"
        )


def test_single_generic_network_token_does_not_suppress_equity_boost(catalog) -> None:
    """Dominance needs >=2 distinct network terms: one generic word like
    'network' alongside a real ticker must keep the equity routing intact.
    """
    results = search_catalog(catalog, "AAPL price network", limit=5)
    assert results
    assert results[0]["operation_id"].startswith("quotes_symbol_"), (
        f"single 'network' token wrongly suppressed the ticker boost: "
        f"{[r['operation_id'] for r in results[:5]]}"
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


# ---- US-macro detection + FRED boost (live ChatGPT MCP feedback 2026-05-20) ----


@pytest.mark.parametrize(
    "query,expected",
    [
        # Positive: US context + macro keyword.
        ("US CPI inflation", True),
        ("US GDP", True),
        ("US unemployment rate", True),
        ("USA Treasury yield curve", True),
        ("United States consumer price index", True),
        ("American M2 money supply", True),
        # US context alone, no macro keyword -> no boost.
        ("US news", False),
        ("US stocks", False),
        ("US weather", False),
        # Macro keyword alone, no US context -> no boost.
        ("UK CPI", False),
        ("Germany GDP", False),
        ("Australia unemployment", False),
        ("Eurozone inflation", False),
        # "USD" / "USDT" must NOT match _US_CONTEXT_PATTERN (word boundary).
        ("USD JPY rate", False),
        ("USDT price", False),
        # Substring "us" inside another word must not match.
        ("Russia GDP", False),
        ("Aussie CPI", False),
    ],
)
def test_detect_us_macro_query(query: str, expected: bool) -> None:
    assert detect_us_macro_query(query) is expected


def test_us_macro_query_lands_fred_series_top_1(catalog) -> None:
    """Live ChatGPT MCP feedback (2026-05-20): the LLM skipped MCP entirely
    for "US CPI inflation" because non-US country endpoints (ons_cpi, rba_cpi)
    out-ranked fred_series_series_id. The US-macro boost makes FRED dominant.
    """
    for query in ("US CPI inflation", "US GDP", "US unemployment rate"):
        results = search_catalog(catalog, query, limit=3)
        assert results, f"no results for {query!r}"
        assert results[0]["operation_id"].startswith("fred_"), (
            f"expected fred_* top-1 for {query!r}, got {results[0]['operation_id']}. "
            f"Top-3: {[r['operation_id'] for r in results]}"
        )


def test_non_us_macro_query_does_not_boost_fred(catalog) -> None:
    """Anti-regression: UK / Germany / Australia macro queries must keep
    landing on their country-specific endpoints, not get pulled into FRED.
    """
    for query in ("UK CPI", "Germany GDP", "Australia unemployment"):
        results = search_catalog(catalog, query, limit=3)
        if not results:
            continue
        # FRED should not be top-1; ideally not in top-3 either.
        assert not results[0]["operation_id"].startswith("fred_"), (
            f"FRED incorrectly boosted for non-US query {query!r}: "
            f"top-3 {[r['operation_id'] for r in results]}"
        )


def test_us_context_without_macro_keyword_does_not_boost_fred(catalog) -> None:
    """'US news' has US context but no macro keyword - boost must not fire."""
    results = search_catalog(catalog, "US news", limit=3)
    assert results
    assert not results[0]["operation_id"].startswith("fred_"), (
        f"FRED incorrectly boosted for US-but-non-macro query: "
        f"top-3 {[r['operation_id'] for r in results[:3]]}"
    )
