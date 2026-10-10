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
    ETF_TICKERS,
    country_statistic_words,
    detect_currency_pairs,
    detect_tickers,
    detect_us_macro_query,
    matching_central_bank_prefixes,
    query_names_a_period,
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
        # Equity vocabulary must match whole tokens, not
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


def test_natural_language_price_prompt_beats_logo_redirect(catalog) -> None:
    """OpenAI ChatGPT App submission case (2026-07-03): the natural-language
    price prompt tied quotes_symbol_logo_png (a 302 image redirect with a prose
    description) with quotes_symbol_price on English filler ("the"/"for"/"has"),
    and the alphabetical operation_id tie-break surfaced the logo PNG top-1 - so
    fetch_data returned a logo image instead of a price. Query-stopword stripping
    must let the real price endpoint win.
    """
    query = "What is the latest price for NVDA and how has it moved over the past week?"
    results = search_catalog(catalog, query, limit=5)
    assert results
    assert results[0]["operation_id"] == "quotes_symbol_price", (
        f"expected quotes_symbol_price top-1, got {[r['operation_id'] for r in results[:5]]}"
    )
    top_3 = [r["operation_id"] for r in results[:3]]
    assert "quotes_symbol_logo_png" not in top_3, (
        f"logo image redirect leaked into top-3 for a price prompt: {top_3}"
    )


def test_query_stopwords_conservative_and_do_not_break_routing(catalog) -> None:
    """The stopword set must stay pure grammatical filler AND stripping must not
    wipe routing for queries whose meaningful token is short. End-to-end against
    the real catalog, not just a set-intersection check.

    Scope guards: every entry is >= 3 letters (so 2-letter ISO codes / short
    tickers are never stripped), the US-macro token "us" is absent, and no
    data-semantic word leaked in.
    """
    from sugra_api_mcp.catalog.search import _QUERY_STOPWORDS

    assert all(len(word) >= 3 for word in _QUERY_STOPWORDS), (
        f"2-letter stopwords risk ISO codes / tickers: "
        f"{[w for w in _QUERY_STOPWORDS if len(w) < 3]}"
    )
    assert "us" not in _QUERY_STOPWORDS
    data_words = {
        "price", "rate", "gdp", "cpi", "news", "cap", "week", "day", "year",
        "flows", "list", "order", "inflation", "yield", "series", "may",
    }
    leaked = data_words & _QUERY_STOPWORDS
    assert not leaked, f"data-semantic words wrongly in stopword set: {leaked}"

    # End-to-end: filler-heavy and short-token queries still return results,
    # and US-macro routing still lands the US series.
    for query in ("US CPI inflation", "IT sector data", "What is the GDP of India?"):
        results = search_catalog(catalog, query, limit=5)
        assert results, f"stopword filter wiped all results for {query!r}"
    top = search_catalog(catalog, "US CPI inflation", limit=1)[0]
    assert (top["operation_id"], top["macro_keys"][0]["key"]) == ("macro_country_section", "us/cpi")


NAMESPACE_TOP_1_CASES = [
    # query, allowed top-1 operation_id prefixes (any match)
    # MSFT earnings: finnhub_* were renamed in Tier-C scrub - accept the
    # current top-1 winner `earnings` (the standalone equity-earnings endpoint),
    # `calendar_earnings`, and the ticker-routed quotes_symbol_earnings_*.
    # 2026-07-18 catalog resync: the market-wide earnings calendar moved to
    # /api/v2 as `market_calendar_earnings` - same already-allowed namespace
    # under a new operation_id, so it joins the tuple.
    ("MSFT earnings", ("earnings", "calendar_earnings", "market_calendar_earnings", "quotes_symbol_earnings", "quotes_symbol_calendar")),
    ("Apple dividends", ("quotes_symbol_dividend", "quotes_symbol_actions", "market_calendar_dividends")),
    ("Tesla market cap", ("quotes_symbol_market_cap", "quotes_symbol_summary", "quotes_symbol_info")),
    ("FED interest rate", ("fed_rates", "fed_policy")),
    ("Federal Reserve policy rate", ("fed_rates", "fed_policy")),
    ("EUR USD exchange rate", ("forex_", "frankfurter_", "exchangerate_")),
    # ENTSO-E / EU grid discovery after A44 day-ahead prices.
    # Exact top-1 where possible - startswith("energy_grid") would also match
    # energy_grid_fuel_mix, so price/load queries require exact energy_grid.
    ("ENTSO-E day-ahead electricity price", ("energy_grid",)),
    ("ENTSO-E bidding zone load", ("energy_grid",)),
    ("EU electricity grid demand", ("energy_grid",)),
    ("grid fuel mix Germany", ("energy_grid_fuel_mix",)),
]


@pytest.mark.parametrize("query,allowed_prefixes", NAMESPACE_TOP_1_CASES)
def test_search_top_1_lands_in_correct_namespace(
    catalog, query: str, allowed_prefixes: tuple[str, ...]
) -> None:
    results = search_catalog(catalog, query, limit=5)
    assert results, f"search returned no results for {query!r}"
    actual = results[0]["operation_id"]
    # energy_* ids must match exactly - startswith("energy_grid")
    # also accepts energy_grid_fuel_mix. Other cases keep prefix matching.
    if any(p.startswith("energy_") for p in allowed_prefixes):
        ok = actual in allowed_prefixes
    else:
        ok = actual.startswith(allowed_prefixes)
    assert ok, (
        f"top-1 for {query!r} was {actual!r}; expected operation_id equal to or starting with "
        f"one of {allowed_prefixes}. Full top-5: {[r['operation_id'] for r in results]}"
    )


# Entity screening-corpus metadata discoverability. The screening
# benchmark found agents could not reach /entity/sources via the gateway
# queries below are the exact agent-vocabulary
# queries that must now surface the coverage/staleness manifest top-1.
ENTITY_SOURCES_TOP_1_QUERIES = [
    "ofac_sdn max_age_hours",
    "ofac sdn staleness",
    "sanctions source attribution",
    "covered_regimes order",
    "staleness thresholds for sanctions lists",
    "sanctions screening corpus coverage",
    "source configuration for screening",
]


@pytest.mark.parametrize("query", ENTITY_SOURCES_TOP_1_QUERIES)
def test_entity_sources_is_top_1_for_screening_metadata(catalog, query: str) -> None:
    results = search_catalog(catalog, query, limit=5)
    assert results, f"search returned no results for {query!r}"
    actual = results[0]["operation_id"]
    assert actual == "entity_sources", (
        f"top-1 for {query!r} was {actual!r}; expected entity_sources. "
        f"Full top-5: {[r['operation_id'] for r in results]}"
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
    """'Stockholm' must not satisfy the 'stock' equity
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
    """An ECB query ranks no other central bank: below the ECB's policy rate,
    a curated series of the country macro operation, the top 5 are ecb_*."""
    results = search_catalog(catalog, "ECB interest rate", limit=5)
    top_5_ops = [r["operation_id"] for r in results[:5]]
    assert top_5_ops[0] == "macro_country_section", top_5_ops
    assert all(op.startswith("ecb_") for op in top_5_ops[1:]), (
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


def test_us_macro_query_lands_the_us_series_first(catalog) -> None:
    """Live ChatGPT MCP feedback (2026-05-20): the LLM skipped MCP entirely
    for "US CPI inflation" because non-US country endpoints (ons_cpi, rba_cpi)
    out-ranked fred_series_series_id. The US-macro boost made FRED dominant;
    now the curated macro key that names the series ranks first, and the
    boost stays for the US series no key names.
    """
    for query, key in (
        ("US CPI inflation", "us/cpi"),
        ("US GDP", "us/gdp"),
        ("US unemployment rate", "us/unrate"),
    ):
        results = search_catalog(catalog, query, limit=3)
        assert results, f"no results for {query!r}"
        top = results[0]
        assert (top["operation_id"], top.get("macro_keys", [{}])[0].get("key")) == (
            "macro_country_section", key,
        ), f"top-3 for {query!r}: {[r['operation_id'] for r in results]}"
    results = search_catalog(catalog, "US CPI airline fares", limit=3)
    assert results and results[0]["operation_id"].startswith("fred_"), (
        f"top-3: {[r['operation_id'] for r in results]}"
    )


# A curated series by its everyday name: the macro operation answers with the
# series' key first, and for a US series FRED's generic proxy stays among the
# five hits fetch_data selects from, for a call that sends a FRED series id.
MACRO_KEY_TOP_1 = [
    ("US nonfarm payrolls", "us/payrolls"),
    ("US housing starts", "us/housing-starts"),
    ("US real interest rate", "us/reaintratrearat10y"),
    ("US debt to GDP ratio", "us/gfdegdq188s"),
    ("US 30-year mortgage rate", "us/mortgage-30y"),
    ("US 10-year Treasury yield", "us/t10y"),
    ("Japan GDP growth", "jp/gdp"),
    ("China GDP growth", "cn/chngdpnqdsmei"),
    ("euro area HICP inflation", "eu/cpi"),
]


@pytest.mark.parametrize("query,key", MACRO_KEY_TOP_1)
def test_a_curated_series_lands_its_macro_key_first(catalog, query: str, key: str) -> None:
    results = search_catalog(catalog, query, limit=5)
    ids = [r["operation_id"] for r in results]
    top = results[0]

    assert (top["operation_id"], top.get("macro_keys", [{}])[0].get("key")) == (
        "macro_country_section", key,
    ), ids
    if key.startswith("us/"):
        assert "fred_series_series_id" in ids, ids


@pytest.mark.parametrize("query,expected", [
    ("next FOMC meeting date", "macro_cb_calendar"),
    ("when is the next Fed meeting", "macro_cb_calendar"),
    ("ECB meeting dates", "macro_cb_calendar"),
    ("next BoE meeting", "macro_cb_calendar"),
    ("central bank meeting calendar", "macro_cb_calendar_bank"),
    # A rate decision asked about by its date is the meeting's question.
    ("when is the next ECB rate decision", "macro_cb_calendar"),
    ("next Fed rate decision date", "macro_cb_calendar"),
])
def test_a_central_bank_meeting_lands_the_meeting_calendar(catalog, query: str, expected: str) -> None:
    """The bank's own operations hold its rates, not its meeting dates."""
    results = search_catalog(catalog, query, limit=3)

    assert results[0]["operation_id"] == expected, [r["operation_id"] for r in results]


@pytest.mark.parametrize("query", [
    "OPEC meeting",
    "congress committee meeting",
    "shareholder meeting AAPL",
    # The calendar does not publish the Bank of Canada's meetings.
    "BoC meeting",
])
def test_a_meeting_the_calendar_does_not_cover_names_no_calendar(query: str) -> None:
    from sugra_api_mcp.catalog.aliases import MEETING_CALENDAR_OPERATIONS, detect_named_operations

    named = detect_named_operations(query).operations
    assert not MEETING_CALENDAR_OPERATIONS & set(dict(named)), named


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


# ---- Unemployment: the rate itself, never the participation rate ----
# "unemployment rate Germany" ranked ilostat_labor_force first: the
# unemployment alias expanded to "labor force", which only the
# participation-rate operation carries, so a different measure won.


@pytest.mark.parametrize(
    "query",
    [
        "unemployment rate Germany",
        "Germany unemployment rate",
        "unemployment rate Germany France",
    ],
)
def test_unemployment_rate_by_country_lands_the_unemployment_rate(catalog, query: str) -> None:
    results = search_catalog(catalog, query, limit=3)
    top_3_ops = [r["operation_id"] for r in results]
    # ilostat_youth_unemployment scores the same on these words; the
    # operation_id tie-break puts the general measure first.
    assert top_3_ops[0] == "ilostat_unemployment", top_3_ops
    assert "ilostat_labor_force" not in top_3_ops, top_3_ops


def test_jobless_rate_lands_an_unemployment_measure(catalog) -> None:
    """The alias needs an anchor on the measure itself: without one,
    "jobless rate" fell to a central bank's prime rate."""
    results = search_catalog(catalog, "jobless rate", limit=3)
    assert "unemployment" in results[0]["operation_id"], [r["operation_id"] for r in results]


# ---- Symbol-aware relevance: ticker queries prefer symbol-routed endpoints ----
# "MSFT earnings" ranked market_calendar_earnings (market-wide,
# parameters from/to only) above quotes_symbol_earnings_events (symbol-routed).
# The symbol-input boost fires only when the raw query carries a ticker-like
# token and must leave every no-ticker query byte-identical to the pre-boost
# ranking.


def _is_symbol_routed(result: dict) -> bool:
    """True when the endpoint takes a symbol-like input: a {symbol}/{ticker}
    path segment or a required parameter named symbol/ticker."""
    path = result["path"].lower()
    if "{symbol}" in path or "{ticker}" in path:
        return True
    return any(name.lower() in ("symbol", "ticker") for name in result["required_parameters"])


def test_msft_earnings_prefers_symbol_routed_endpoint(catalog) -> None:
    """A ticker plus "earnings" must land on a SYMBOL-TAKING earnings endpoint,
    never the market-wide earnings calendar (params from/to only - it cannot
    answer a single-ticker question). The pin is semantic, not name-based:
    after the toolset-coverage change the v1 `earnings` endpoint
    (required symbol param, markets toolset) legitimately outranks
    quotes_symbol_earnings_events - both satisfy the symbol-aware goal."""
    results = search_catalog(catalog, "MSFT earnings", limit=5)
    assert results, "search returned no results for 'MSFT earnings'"
    top_1 = results[0]
    assert top_1["operation_id"] != "market_calendar_earnings", (
        f"market-wide calendar won a ticker query. "
        f"Full top-5: {[r['operation_id'] for r in results]}"
    )
    assert _is_symbol_routed(top_1), (
        f"top-1 {top_1['operation_id']!r} does not take a symbol input: {top_1['path']}"
    )
    symbol_reasons = {"pattern:ticker->quotes_symbol", "pattern:ticker->symbol-path",
                      "pattern:ticker->symbol-param"}
    assert symbol_reasons & set(top_1["why"]), (
        f"top-1 {top_1['operation_id']!r} won without a symbol-input boost reason: "
        f"{top_1['why']}"
    )


def test_aapl_dividends_top_1_is_symbol_routed(catalog) -> None:
    results = search_catalog(catalog, "AAPL dividends", limit=5)
    assert results, "search returned no results for 'AAPL dividends'"
    top_1 = results[0]
    assert _is_symbol_routed(top_1), (
        f"top-1 for 'AAPL dividends' is not symbol-routed: {top_1['operation_id']!r} "
        f"({top_1['path']}). Full top-5: {[r['operation_id'] for r in results]}"
    )


def test_no_ticker_token_leaves_ranking_unchanged(catalog) -> None:
    """NEGATIVE pin: "federal funds rate" carries no ticker-like token, so the
    symbol-input boost must not fire on any result. Top-1 captured on main
    (2026-07-19, v0.9.0 bundled catalog) BEFORE the boost landed:
    fred_series_series_id at score 18. Both the winner and the absence of any
    symbol-input boost reason are pinned.
    """
    results = search_catalog(catalog, "federal funds rate", limit=5)
    assert results, "search returned no results for 'federal funds rate'"
    # Full ordered top-5 pinned (not just top-1): a scoring change that
    # reshuffles ranks 2-5 without touching the winner must still fail here.
    assert [r["operation_id"] for r in results] == [
        "fred_series_series_id",
        "boc_prime_rate",
        "catalog_funds",
        "fed_rates_rate_type",
        "fixed_income_treasury_reference_rates_rate_history",
    ], f"non-ticker query ranking drifted: {[r['operation_id'] for r in results]}"
    assert results[0]["operation_id"] == "fred_series_series_id", (
        f"top-1 for 'federal funds rate' changed from the pre-boost main winner "
        f"fred_series_series_id to {results[0]['operation_id']!r}. "
        f"Full top-5: {[r['operation_id'] for r in results]}"
    )
    for result in results:
        symbol_reasons = [
            reason for reason in result["why"]
            if reason.startswith("pattern:ticker->symbol")
        ]
        assert not symbol_reasons, (
            f"symbol-input boost fired without a ticker token on "
            f"{result['operation_id']!r}: {symbol_reasons}"
        )


ORG_ACRONYMS = [
    "IMF", "BIS", "OECD", "WTO", "WHO", "UN", "ILO", "FAO", "OPEC", "NATO",
    "EIA", "BLS", "BEA", "CBO", "GAO", "ONS", "EIB", "EBRD", "ADB", "IFC",
    "WB",
]


@pytest.mark.parametrize("org", ORG_ACRONYMS)
def test_org_acronyms_are_not_tickers(org) -> None:
    """Intergovernmental and statistical org acronyms never pass as tickers
    (field find: "IMF reserves" ranked quotes_symbol_* top-3 before the
    blacklist covered them)."""
    from sugra_api_mcp.catalog.aliases import detect_tickers

    assert detect_tickers(f"{org} data report") == [], org


def test_imf_reserves_lands_in_the_imf_namespace(catalog) -> None:
    results = search_catalog(catalog, "IMF reserves", limit=3)
    assert results[0]["operation_id"].startswith("imf_"), (
        f"'IMF reserves' top-1 left the imf namespace: "
        f"{[r['operation_id'] for r in results]}"
    )


# ---- Versioned semantic eval set ---------------------------------------------
# The six evaluation scenarios with semantic top-1 oracles plus acronym negatives.
# PASS is stricter than "technically callable": right domain, right geography,
# right data type, never a deprecated route above its available replacement.

AUDIT_EVAL_TOP1 = [
    # (query, oracle: top-1 operation_id predicate description)
    ("current AAPL stock price", lambda op: op == "quotes_symbol_price"),
    ("weather forecast Tbilisi next 5 days",
     lambda op: op == "v2_weather_forecast"),
    ("geocode a postal address",
     lambda op: op.startswith("geocoding_")),
    ("search FRED series for gold", lambda op: op.startswith("fred_")),
    ("Apple earnings news from the last 7 days",
     lambda op: op.startswith("news_")),
]


@pytest.mark.parametrize("query,oracle", AUDIT_EVAL_TOP1,
                         ids=[q for q, _ in AUDIT_EVAL_TOP1])
def test_audit_eval_semantic_top1(catalog, query, oracle) -> None:
    results = search_catalog(catalog, query, limit=5)
    assert results, f"no results for {query!r}"
    top = results[0]["operation_id"]
    assert oracle(top), (
        f"semantic oracle failed for {query!r}: top-1 {top!r}; "
        f"top-5 {[r['operation_id'] for r in results]}"
    )


def test_audit_eval_georgia_cpi_no_silent_country_substitution(catalog) -> None:
    """'Georgia CPI' must not silently return another country's CPI top-1."""
    from sugra_api_mcp.catalog.aliases import SOURCE_COUNTRY_PREFIXES

    results = search_catalog(catalog, "Georgia CPI inflation", limit=5)
    assert results
    top = results[0]["operation_id"]
    for prefix, country in SOURCE_COUNTRY_PREFIXES.items():
        if top.startswith(prefix):
            assert country == "GE", (
                f"top-1 {top!r} belongs to {country}, silently substituted "
                f"for the Georgia query")


def test_audit_eval_deprecated_never_above_replacement(catalog) -> None:
    """Property over the whole bundle: for every deprecated endpoint whose
    replacement exists in the catalog, a query built from its summary must not
    rank the deprecated route above the replacement."""
    deprecated = [e for e in catalog.endpoints
                  if e.deprecated and e.replaced_by]
    assert deprecated, "bundle carries no deprecated endpoints - rebuild it"
    by_id = {e.operation_id: e for e in catalog.endpoints}
    for endpoint in deprecated:
        replacement = by_id.get(endpoint.replaced_by)
        if replacement is None:
            continue
        results = search_catalog(catalog, endpoint.summary or endpoint.path,
                                 limit=len(catalog.endpoints))
        ranks = {r["operation_id"]: i for i, r in enumerate(results)}
        dep_rank = ranks.get(endpoint.operation_id)
        rep_rank = ranks.get(replacement.operation_id)
        if dep_rank is not None and rep_rank is not None:
            assert rep_rank < dep_rank, (
                f"deprecated {endpoint.operation_id} (rank {dep_rank}) above "
                f"its replacement {replacement.operation_id} (rank {rep_rank})")


@pytest.mark.parametrize("query", [
    "search FRED series for gold",
    "AIS vessel density in the North Sea",
    "RF propagation for MMSI vessel tracking",
])
def test_audit_eval_acronyms_are_not_tickers(query) -> None:
    assert detect_tickers(query) == []


# ---- Regression pins: tickers, countries and route coverage -----------------

@pytest.mark.parametrize("query,expected", [
    ("PLTR", ["PLTR"]),               # sole substantive token = quote lookup
    ("PLTR today", ["PLTR"]),         # temporal filler does not change it
    ("search FRED series for gold", []),   # multi-token stays context-gated
])
def test_bare_sole_ticker_is_admitted(query, expected) -> None:
    assert detect_tickers(query) == expected


def test_bare_ticker_routes_to_quotes(catalog) -> None:
    results = search_catalog(catalog, "PLTR today", limit=3)
    assert results and results[0]["operation_id"].startswith("quotes_symbol_"), (
        f"bare ticker lost symbol routing: {[r['operation_id'] for r in results]}")


def test_unlisted_country_is_still_detected(catalog) -> None:
    """The closed vocabulary recreated silent substitution for
    every omitted country - the generated module must know them all."""
    from sugra_api_mcp.catalog.aliases import SOURCE_COUNTRY_PREFIXES, detect_query_countries

    assert detect_query_countries("Netherlands CPI inflation") == {"NL"}
    results = search_catalog(catalog, "Netherlands CPI inflation", limit=3)
    assert results
    top = results[0]["operation_id"]
    for prefix, country in SOURCE_COUNTRY_PREFIXES.items():
        if top.startswith(prefix):
            assert country == "NL", (
                f"top-1 {top!r} is a {country} national source for a NL query")


def test_source_country_prefixes_match_live_operations(catalog) -> None:
    """Dead-prefix guard (bcra_/bcrp_ mapped a namespace that
    does not exist in the bundle while central_banks_bcra_ evaded the
    penalty). Every mapped prefix must match at least one bundled op."""
    from sugra_api_mcp.catalog.aliases import SOURCE_COUNTRY_PREFIXES

    ids = [e.operation_id for e in catalog.endpoints]
    dead = [p for p in SOURCE_COUNTRY_PREFIXES
            if not any(op.startswith(p) for op in ids)]
    assert not dead, f"country prefixes matching no bundled operation: {dead}"


def test_every_deprecated_operation_resolves_or_is_allowlisted(catalog) -> None:
    from sugra_api_mcp.catalog.builder import DEPRECATED_WITHOUT_REPLACEMENT

    unresolved = [e.operation_id for e in catalog.endpoints
                  if e.deprecated and not e.replaced_by
                  and e.operation_id not in DEPRECATED_WITHOUT_REPLACEMENT]
    assert not unresolved, f"deprecated without twin or allowlist: {unresolved}"


def test_empty_toolset_gets_no_intent_boost() -> None:
    """startswith('') is True for every term - a toolset-less
    endpoint must never collect the intent boost."""
    from sugra_api_mcp.catalog.models import Endpoint
    from sugra_api_mcp.catalog.search import _score

    endpoint = Endpoint(operation_id="x_op", method="GET", path="/x",
                        summary="anything at all", toolset="")
    _score_value, why = _score(
        endpoint, ["anything"], {},
        boost_quotes_symbol=False, boost_markets_toolset=False,
        boost_symbol_input=False, boost_forex=False, boost_crypto=False,
        boost_us_macro=False, central_bank_prefixes=[], query_countries=set())
    assert not any(w.startswith("toolset-intent") for w in why), why


# ---- Regression pins: country and code disambiguation -----------------------

def test_georgia_us_state_cues_suppress_the_country_reading(catalog) -> None:
    """'Georgia census states' is a US-state query - the sovereign
    GE reading must not strip the US census namespace out of the results."""
    from sugra_api_mcp.catalog.aliases import detect_query_countries

    assert detect_query_countries("Georgia census states") == set()
    assert detect_query_countries("Georgia CPI inflation") == {"GE"}
    results = search_catalog(catalog, "Georgia census states", limit=10)
    assert any(r["operation_id"].startswith("census_") for r in results), (
        f"US census ops vanished: {[r['operation_id'] for r in results][:5]}")


def test_bare_iso2_code_is_recognized(catalog) -> None:
    """'NL CPI inflation' must trigger geography protection."""
    from sugra_api_mcp.catalog.aliases import SOURCE_COUNTRY_PREFIXES, detect_query_countries

    assert detect_query_countries("NL CPI inflation") == {"NL"}
    results = search_catalog(catalog, "NL CPI inflation", limit=3)
    assert results
    top = results[0]["operation_id"]
    for prefix, country in SOURCE_COUNTRY_PREFIXES.items():
        if top.startswith(prefix):
            assert country == "NL"


def test_ambiguous_iso2_words_are_not_countries() -> None:
    from sugra_api_mcp.catalog.aliases import detect_query_countries

    assert detect_query_countries("IT support costs") == set()
    assert detect_query_countries("IN the beginning") == set()
    assert detect_query_countries("US CPI inflation") == {"US"}


def test_unmatched_replacement_clamps_the_deprecated_route_out(catalog) -> None:
    """A deprecated route whose replacement matches nothing must not
    stand on its legacy text alone."""
    from sugra_api_mcp.catalog.search import search_catalog as sc

    results = sc(catalog, "deprecated legacy maritime vessels density grid",
                 limit=50)
    ids = [r["operation_id"] for r in results]
    for dep in ("maritime_vessels_density", "maritime_history_density"):
        if dep in ids:
            rep = next(e.replaced_by for e in catalog.endpoints
                       if e.operation_id == dep)
            assert rep in ids and ids.index(rep) < ids.index(dep)


def test_us_postal_country_collisions_resolve_by_intent() -> None:
    """Colliding codes read as a
    COUNTRY under macro vocabulary, as postal under state cues or none."""
    from sugra_api_mcp.catalog.aliases import detect_query_countries

    assert detect_query_countries("CA CPI inflation") == {"CA"}
    assert detect_query_countries("IL unemployment") == {"IL"}
    assert detect_query_countries("AZ housing") == set()
    assert detect_query_countries("Canada CPI inflation") == {"CA"}
    assert detect_query_countries("Israel CPI") == {"IL"}


def test_compound_country_phrases_resolve_longest_first() -> None:
    """'American Samoa' is AS alone - component matches (american
    -> US, samoa -> WS) must not survive and defeat the geo guard."""
    from sugra_api_mcp.catalog.aliases import detect_query_countries

    assert detect_query_countries("American Samoa CPI inflation") == {"AS"}


def test_colliding_codes_resolve_by_intent() -> None:
    from sugra_api_mcp.catalog.aliases import detect_query_countries

    assert detect_query_countries("IL CPI inflation") == {"IL"}
    assert detect_query_countries("CA central bank rate") == {"CA"}
    assert detect_query_countries("IL state census") == set()
    assert detect_query_countries("AZ housing permits") == set()


def test_bare_dotted_ticker_is_sole_token() -> None:
    assert detect_tickers("HEI.A") == ["HEI.A"]
    assert detect_tickers("HEI.A today") == ["HEI.A"]


def test_postal_iso2_countries_resolve_by_intent_everywhere() -> None:
    """EVERY postal/ISO2 collision resolves by intent - the
    blanket drop suppressed valid country queries like DE CPI (Germany)."""
    from sugra_api_mcp.catalog.aliases import detect_query_countries as d

    assert d("DE CPI inflation") == {"DE"}
    assert d("AR central bank rate") == {"AR"}
    assert d("CO inflation") == {"CO"}
    assert d("DE state census") == set()


def test_component_country_survives_outside_the_compound() -> None:
    """A component term dies only where its span lies inside
    a longer match - separate occurrences survive."""
    from sugra_api_mcp.catalog.aliases import detect_query_countries as d

    assert d("American Samoa and American government TIPS") == {"AS", "US"}
    assert d("American Samoa CPI inflation") == {"AS"}


def test_all_territory_postal_codes_pass_the_intent_gate() -> None:
    from sugra_api_mcp.catalog.aliases import detect_query_countries as d

    assert d("IN CPI inflation") == {"IN"}
    assert d("PR CPI inflation") == {"PR"}
    assert d("ME state census") == set()


def test_uniform_iso2_intent_gate() -> None:
    """grok terminal round: NO enumerated collision sets - every bare
    uppercase ISO2 code resolves through one macro-vs-state rule, so there
    is no list to leak the next collision (AS, MP, AI, TV, HR...)."""
    from sugra_api_mcp.catalog.aliases import detect_query_countries as d

    assert d("AS CPI inflation") == {"AS"}
    assert d("IT CPI inflation") == {"IT"}
    assert d("MP CPI inflation") == {"MP"}
    assert d("HR unemployment") == {"HR"}
    assert d("US CPI inflation") == {"US"}
    assert d("IS THE MARKET OPEN") == set()
    assert d("IT support costs") == set()
    assert d("IL state census") == set()

def test_american_samoa_macro_does_not_route_to_fred(catalog) -> None:
    """Cross-detector conflict - 'American' inside 'American
    Samoa' must not arm the US-macro FRED boost. End-to-end ranking pin."""
    for q in ("American Samoa CPI inflation", "American Samoa GDP"):
        results = search_catalog(catalog, q, limit=3)
        assert results, q
        top = results[0]["operation_id"]
        assert not top.startswith(("fred_", "fed_")), (
            f"{q!r} routed to the US source {top!r}: "
            f"{[r['operation_id'] for r in results]}")


def test_adjectival_compound_territories_do_not_read_as_us(catalog) -> None:
    """'American Samoan GDP' is the adjectival form - it
    must resolve AS (longest phrase) and never arm the US FRED boost."""
    from sugra_api_mcp.catalog.aliases import detect_query_countries as d

    assert d("American Samoan GDP") == {"AS"}
    results = search_catalog(catalog, "American Samoan GDP", limit=3)
    assert results
    assert not results[0]["operation_id"].startswith(("fred_", "fed_")), (
        f"adjectival AS query routed US: {[r['operation_id'] for r in results]}")


def test_ticker_whitelist_never_shadows_a_country_code() -> None:
    """An unconditional whitelist entry that is ALSO a valid ISO2 country
    defeats the geography guard ('BA CPI inflation' read as Boeing).
    Invariant: no whitelist entry is a country code; the bare quote
    lookups still work through sole-token and equity-context admission."""
    from sugra_api_mcp.catalog.aliases import (
        _ISO2_CODES_ALL,
        _TICKER_WHITELIST,
        detect_query_countries,
        detect_tickers,
    )

    overlap = {t for t in _TICKER_WHITELIST if t in _ISO2_CODES_ALL}
    assert not overlap, f"whitelist entries shadowing countries: {sorted(overlap)}"
    assert detect_tickers("BA CPI inflation") == []
    assert detect_query_countries("BA CPI inflation") == {"BA"}
    assert detect_tickers("BA") == ["BA"]          # sole token
    assert detect_tickers("GS today") == ["GS"]    # sole + filler
    assert detect_tickers("BA stock price") == ["BA"]  # equity context


def test_uk_short_form_resolves_to_gb() -> None:
    """'UK' is not an ISO2 code (ISO assigns GB), so the uppercase ISO2
    intent gate cannot admit it; the curated vocabulary short form must.
    Word-boundary matching keeps 'ukulele'/'Ukraine' unaffected."""
    from sugra_api_mcp.catalog.aliases import detect_query_countries

    assert detect_query_countries("UK CPI inflation") == {"GB"}
    assert detect_query_countries("uk unemployment rate") == {"GB"}
    assert detect_query_countries("Ukraine GDP") == {"UA"}
    assert detect_query_countries("ukulele market size") == set()


def test_untagged_us_source_no_longer_wins_german_cpi() -> None:
    """fixed_income_treasury (US TIPS) escaped the wrong-country
    penalty and ranked top-1 for 'Germany CPI inflation' (measured live
    2026-08-21). The full-bundle sweep tagged it - and every other
    single-country prefix - so no untagged national source outruns the
    geography guard again."""
    from sugra_api_mcp.catalog.loader import load_catalog
    from sugra_api_mcp.catalog.search import search_catalog

    catalog = load_catalog()
    results = search_catalog(catalog, "Germany CPI inflation", limit=3)
    ids = [r["operation_id"] for r in results]
    assert not ids[0].startswith("fixed_income_treasury_"), ids
    assert not ids[0].startswith("fred_"), ids


def test_country_tag_sweep_examples() -> None:
    """Representative pins from the verified sweep: the tagged national
    sources win their OWN country's queries."""
    from sugra_api_mcp.catalog.loader import load_catalog
    from sugra_api_mcp.catalog.search import search_catalog

    catalog = load_catalog()
    res = search_catalog(catalog, "Sweden CPI", limit=3)
    assert res[0]["operation_id"].startswith("scb_")
    res = search_catalog(catalog, "Japan corporate filings EDINET", limit=3)
    assert res[0]["operation_id"].startswith("edinet_")


def test_all_country_prefixes_cover_live_operations() -> None:
    """Both directions of drift are loud: every map entry matches at least
    one bundled operation (no phantom tags), and the measured single-country
    prefixes from the single-country sweep are all present."""
    from sugra_api_mcp.catalog.aliases import SOURCE_COUNTRY_PREFIXES
    from sugra_api_mcp.catalog.loader import load_catalog

    catalog = load_catalog()
    ids = [e.operation_id for e in catalog.endpoints]
    for prefix in SOURCE_COUNTRY_PREFIXES:
        assert any(i.startswith(prefix) for i in ids), prefix
    for prefix, country in (("fixed_income_treasury_", "US"), ("scb_", "SE"),
                            ("edinet_", "JP"), ("data_gov_", "HK"),
                            ("fca_shorts_", "GB"), ("insee_", "FR"),
                            ("statistical_agencies_ssb_", "NO")):
        assert SOURCE_COUNTRY_PREFIXES.get(prefix) == country, prefix


def test_market_parameterized_cot_stays_untagged() -> None:
    """cot_index_traders takes a free market parameter over
    CFTC futures including globally-relevant commodities - the same class
    as its six sibling clusters the sweep refuted. No cot_ prefix may
    carry a country tag."""
    from sugra_api_mcp.catalog.aliases import SOURCE_COUNTRY_PREFIXES

    assert not any(p.startswith("cot_") for p in SOURCE_COUNTRY_PREFIXES)


# ---- Regression pins: keyword-indexed everyday goods and country boost -----

_COMMODITY_KEYWORD_CANDIDATES = frozenset({
    "fred_series_series_id", "commodities_prices", "commodities_commodity_id",
    "futures_root_historical", "futures_root_curve", "futures_root_contracts",
    "futures_root_info",
})


@pytest.mark.parametrize("word", ["coffee", "cocoa", "sugar", "wheat", "gasoline"])
def test_everyday_commodity_words_find_their_series_endpoint(catalog, word: str) -> None:
    """These everyday-goods words appear nowhere in the operations' own
    path/summary/description - only in the x-sugra-keywords vendor extension.
    Before keyword indexing, every one of these queries returned nothing."""
    results = search_catalog(catalog, word, limit=5)
    assert results, f"{word!r} returned no candidates"
    top_ids = {r["operation_id"] for r in results}
    assert top_ids & _COMMODITY_KEYWORD_CANDIDATES, (
        f"{word!r} top-5 {sorted(top_ids)} missed every known commodity endpoint")
    top = results[0]
    assert any(reason == f"keyword:{word}" for reason in top["why"]), (
        f"{word!r} top-1 {top['operation_id']!r} why={top['why']} has no keyword hit")


def test_country_named_query_boosts_country_parameterized_endpoints(catalog) -> None:
    """'Portugal' alone matched almost nothing before this boost: the only
    prior hits were a handful of ILOSTAT operations whose parameter
    description happens to name Portugal as an example. A generic
    country-scoped endpoint whose OWN text never mentions Portugal at all -
    worldbank_country_overview takes a bare ISO country code and its spec
    text uses only 'US, CN, KZ, AZ, GE, UZ' as examples - must now surface
    too, and only the pattern:country->param boost can put it there."""
    from sugra_api_mcp.catalog.aliases import detect_query_countries

    assert detect_query_countries("Portugal") == {"PT"}
    endpoint = catalog.get("worldbank_country_overview")
    haystack = " ".join(
        [endpoint.summary, endpoint.description]
        + [f"{p.description} {p.example or ''}" for p in endpoint.parameters]
    ).lower()
    assert "portugal" not in haystack, "fixture assumption broke: text now names Portugal"

    results = search_catalog(catalog, "Portugal", limit=200)
    ids = {r["operation_id"] for r in results}
    assert "worldbank_country_overview" in ids, (
        f"a country-parameterized endpoint with no textual Portugal mention "
        f"did not surface for a bare country-name query: {sorted(ids)[:10]}")
    hit = next(r for r in results if r["operation_id"] == "worldbank_country_overview")
    assert hit["why"] == ["pattern:country->param"], hit["why"]


def test_country_param_boost_fires_only_when_query_names_a_country() -> None:
    """The boost is additive and gated on query_countries alone - it must
    not fire for a country-parameterized endpoint when the query names no
    country, and its magnitude must be exactly COUNTRY_PARAM_BOOST."""
    from sugra_api_mcp.catalog.models import Endpoint, EndpointParameter
    from sugra_api_mcp.catalog.search import COUNTRY_PARAM_BOOST, _score

    endpoint = Endpoint(
        operation_id="generic_country_overview",
        method="GET",
        path="/x",
        summary="Country overview",
        toolset="macro",
        parameters=[EndpointParameter(name="country", location="query", required=True)],
    )
    kwargs = dict(
        boost_quotes_symbol=False, boost_markets_toolset=False,
        boost_symbol_input=False, boost_forex=False, boost_crypto=False,
        boost_us_macro=False, central_bank_prefixes=[],
    )
    score_without, why_without = _score(
        endpoint, ["overview"], {}, query_countries=set(), **kwargs)
    score_with, why_with = _score(
        endpoint, ["overview"], {}, query_countries={"PT"}, **kwargs)
    assert "pattern:country->param" not in why_without
    assert "pattern:country->param" in why_with
    assert score_with - score_without == COUNTRY_PARAM_BOOST


def test_country_param_boost_needs_the_topic_when_the_query_has_one() -> None:
    """'Portugal weather' asks about weather: a country-scoped endpoint that
    matches nothing but the country must not earn the boost, or every such
    endpoint becomes a candidate for every country query. One that matches
    the topic still does."""
    from sugra_api_mcp.catalog.models import Endpoint, EndpointParameter
    from sugra_api_mcp.catalog.search import COUNTRY_PARAM_BOOST, _score

    def endpoint(summary: str, description: str = "") -> Endpoint:
        return Endpoint(
            operation_id="generic_country_op", method="GET", path="/x",
            summary=summary, description=description, toolset="macro",
            parameters=[EndpointParameter(name="country", location="query", required=True)],
        )

    kwargs = dict(
        boost_quotes_symbol=False, boost_markets_toolset=False,
        boost_symbol_input=False, boost_forex=False, boost_crypto=False,
        boost_us_macro=False, central_bank_prefixes=[], query_countries={"PT"},
        country_terms=frozenset({"portugal"}),
    )
    off_topic_score, off_topic_why = _score(endpoint("Trade balance"), ["portugal", "weather"], {}, **kwargs)
    assert "pattern:country->param" not in off_topic_why
    assert off_topic_score == 0

    on_topic_score, on_topic_why = _score(endpoint("Weather by country"), ["portugal", "weather"], {}, **kwargs)
    assert "pattern:country->param" in on_topic_why
    assert on_topic_score == 3 + COUNTRY_PARAM_BOOST

    only_country_score, only_country_why = _score(endpoint("Trade balance"), ["portugal"], {}, **kwargs)
    assert only_country_why == ["pattern:country->param"]
    assert only_country_score == COUNTRY_PARAM_BOOST

    # Naming the country is not the topic, whatever field names it.
    _, names_country_why = _score(endpoint("Portugal trade balance"), ["portugal", "weather"], {}, **kwargs)
    assert "pattern:country->param" not in names_country_why

    # A topic match in the description alone is still a topic match.
    described_score, described_why = _score(
        endpoint("Trade balance", "Includes weather effects"), ["portugal", "weather"], {}, **kwargs)
    assert "pattern:country->param" in described_why
    assert described_score == 1 + COUNTRY_PARAM_BOOST


@pytest.mark.parametrize("query", [
    "Portugal weather", "Portugal GDP", "Sweden CPI", "Portuguese wine exports",
    "United Kingdom unemployment", "PT inflation",
])
def test_a_country_plus_topic_query_surfaces_nothing_on_the_country_alone(catalog, query: str) -> None:
    """Before the topic gate, each of these queries surfaced 80 to 100
    country-scoped endpoints whose only reason was the country boost. Every
    boosted result must now carry a reason for a word that is not the country
    (the reasons before the boost in `why` are never cut by its cap)."""
    from sugra_api_mcp.catalog.aliases import detect_query_countries
    from sugra_api_mcp.catalog.search import _country_terms, _tokens

    fields = ("operation_id", "tag_toolset", "summary", "path", "params", "keyword", "description")
    country_words = _country_terms(query, _tokens(query), detect_query_countries(query))
    assert country_words
    results = search_catalog(catalog, query, limit=500)
    assert results
    for result in results:
        if "pattern:country->param" not in result["why"]:
            continue
        topic_reasons = []
        for reason in result["why"][:result["why"].index("pattern:country->param")]:
            kind, _, term = reason.partition(":")
            if kind == "alias" or (kind in fields and term not in country_words):
                topic_reasons.append(reason)
        assert topic_reasons, f"{query!r}: {result['operation_id']} boosted on the country alone: {result['why']}"


def test_country_terms_are_the_whole_name_of_a_named_country() -> None:
    from sugra_api_mcp.catalog.aliases import detect_query_countries
    from sugra_api_mcp.catalog.search import _country_terms

    query = "United Kingdom unemployment"
    terms = ["united", "kingdom", "unemployment"]
    assert _country_terms(query, terms, detect_query_countries(query)) == {"united", "kingdom"}
    assert _country_terms("unemployment", ["unemployment"], set()) == frozenset()


def test_endpoint_without_keywords_gets_no_keyword_boost() -> None:
    """An operation the API never annotated with x-sugra-keywords must score
    exactly as it did before keyword indexing existed - no keyword: reason
    may appear in its why list, and none of the query words this test uses
    are commodity words that could tempt some OTHER field into a spurious
    match."""
    from sugra_api_mcp.catalog.models import Endpoint
    from sugra_api_mcp.catalog.search import _score

    endpoint = Endpoint(
        operation_id="legacy_op", method="GET", path="/legacy",
        summary="Legacy widget summary", toolset="core",
    )
    assert endpoint.keywords == []
    score, why = _score(
        endpoint, ["widget"], {},
        boost_quotes_symbol=False, boost_markets_toolset=False,
        boost_symbol_input=False, boost_forex=False, boost_crypto=False,
        boost_us_macro=False, central_bank_prefixes=[], query_countries=set())
    assert not any(w.startswith("keyword:") for w in why), why
    # The only possible hit is the summary field (weight 3); no coverage
    # bonus applies for a single matched term.
    assert score == 3


# ---- Everyday names: currencies, benchmarks, waterways, ports ---------------
# People ask for "dollar to yen" or "ships through Suez", not for an
# operation_id. Before these names counted, each query below ranked an
# unrelated operation first on a shared word ("to", "in", "sea", "rates").

_SCORE_FLAGS = dict(
    boost_quotes_symbol=False, boost_markets_toolset=False,
    boost_symbol_input=False, boost_forex=False, boost_crypto=False,
    boost_us_macro=False, central_bank_prefixes=[], query_countries=set(),
)

EVERYDAY_NAME_TOP_1 = [
    ("dollar to yen exchange rate", "forex_convert"),
    ("How many Turkish lira for one US dollar", "forex_convert"),
    ("EUR/USD", "forex_convert"),
    ("usd to jpy", "forex_convert"),
    ("How much is 100 euros in dollars", "forex_convert"),
    ("euro exchange rate", "forex_rates"),
    ("Indian rupee rate", "forex_rates"),
    ("dollar to yen history", "forex_history"),
    ("EUR/USD history", "forex_history"),
    ("oil price today", "commodities_energy_petroleum"),
    ("oil price", "commodities_energy_petroleum"),
    ("Brent crude oil price", "commodities_energy_petroleum"),
    ("WTI crude oil price", "commodities_energy_petroleum"),
    ("Henry Hub natural gas", "commodities_energy_natural_gas"),
    ("European natural gas price TTF this winter", "commodities_commodity_id"),
    ("US crude oil inventory", "commodities_energy_petroleum_stocks"),
    ("are trucking freight rates going up in the US", "fred_series_series_id"),
    ("how many ships are going through the Suez Canal and Red Sea now",
     "maritime_chokepoints_activity"),
    ("Strait of Hormuz transits", "maritime_chokepoints_hormuz_transits"),
    ("Panama Canal transits", "maritime_chokepoints_panama_transits"),
    ("Malacca strait throughput", "maritime_chokepoints_malacca_throughput"),
    ("port congestion", "transport_ports_congestion"),
    ("how busy are ports", "transport_ports_congestion"),
    ("ship calls at Rotterdam", "transport_ports_congestion"),
    ("Rotterdam port", "transport_ports_congestion"),
    ("Finnish port calls", "transport_ports_port_calls"),
]


@pytest.mark.parametrize("query,expected", EVERYDAY_NAME_TOP_1)
def test_everyday_names_land_their_operation_top_1(catalog, query: str, expected: str) -> None:
    results = search_catalog(catalog, query, limit=5)
    assert results and results[0]["operation_id"] == expected, (
        f"{query!r}: top-5 {[(r['operation_id'], r['score']) for r in results]}")


# An exchange rate asked of no currency asks for every currency: "exchange
# rate" ranked Peru's sol against one currency first.
@pytest.mark.parametrize("query,expected", [
    ("exchange rate", "forex_rates"),
    ("exchange rates", "forex_rates"),
    ("currency exchange rate", "forex_rates"),
    ("foreign exchange rates", "forex_rates"),
    ("what is the exchange rate today", "forex_rates"),
    ("what's the exchange rate today", "forex_rates"),
    ("exchange rate for today", "forex_rates"),
    ("exchange rate history", "forex_history"),
    ("exchange rates since 2020", "forex_history"),
    ("exchange rate 2020", "forex_history"),
    ("exchange rates this week", "forex_history"),
    ("what were exchange rates in 2020", "forex_history"),
    ("what was the exchange rate in 2020", "forex_history"),
    ("world's exchange rates", "forex_rates"),
])
def test_an_exchange_rate_of_no_currency_ranks_every_currency_first(
    catalog, query: str, expected: str,
) -> None:
    results = search_catalog(catalog, query, limit=5)
    assert results and results[0]["operation_id"] == expected, (
        f"{query!r}: top-5 {[(r['operation_id'], r['score']) for r in results]}")
    assert "name:exchange rates" in results[0]["why"], results[0]


@pytest.mark.parametrize("query,expected", [
    ("real effective exchange rate", "bis_fx_effective"),
    ("Peru exchange rate", "central_banks_bcrp_fx_currency"),
    ("dollar to yen exchange rate", "forex_convert"),
    ("euro exchange rate", "forex_rates"),
])
def test_an_exchange_rate_that_names_more_keeps_its_answer(
    catalog, query: str, expected: str,
) -> None:
    results = search_catalog(catalog, query, limit=5)
    assert results and results[0]["operation_id"] == expected, (
        f"{query!r}: top-5 {[(r['operation_id'], r['score']) for r in results]}")
    assert not any(note == "name:exchange rates" for r in results for note in r["why"]), results


@pytest.mark.parametrize("query", [
    # What an exchange rate is, not what the rates are.
    "what is a currency exchange rate",
    "what is the exchange rate",
    "what was the exchange rate",
    "what's the exchange rate",
    # A number that is not a year from 1900 to 2099, a date included.
    "exchange rate in 1899",
    "exchange rate in 99",
    "exchange rate 2020-01-01",
    # A currency code in capitals names one currency.
    "ALL exchange rate",
])
def test_an_exchange_rate_question_that_asks_no_rates_names_no_panel(catalog, query: str) -> None:
    results = search_catalog(catalog, query, limit=5)
    assert not any(note == "name:exchange rates" for r in results for note in r["why"]), results


# A policy rate asked of no country asks for every central bank's: "policy
# rate" ranked eight operations of six national banks first.
@pytest.mark.parametrize("query", [
    "policy rate",
    "policy rates",
    "central bank rates",
    "central bank policy rates",
    "central bank interest rates",
    "the policy rate",
])
def test_a_policy_rate_of_no_country_ranks_every_central_bank_first(catalog, query: str) -> None:
    results = search_catalog(catalog, query, limit=5)
    assert results and results[0]["operation_id"] == "bis_cb_rates", (
        f"{query!r}: top-5 {[(r['operation_id'], r['score']) for r in results]}")
    assert "name:central bank policy rates" in results[0]["why"], results[0]
    # The rates by country follow, the answer once a country is named.
    assert results[1]["operation_id"] == "bis_cb_rates_country", results


@pytest.mark.parametrize("query", [
    # A bank, a country or any other word keeps its own answer.
    "Fed policy rate",
    "Canada policy rate",
    "policy rate history",
    "current policy rate",
    "interest rate",
    "interest rates",
    "mortgage interest rate",
])
def test_a_policy_rate_that_names_more_names_no_every_bank_panel(catalog, query: str) -> None:
    results = search_catalog(catalog, query, limit=5)
    assert not any(note == "name:central bank policy rates" for r in results for note in r["why"]), results


@pytest.mark.parametrize("query,expected_prefix", [
    # Futures have operations of their own, which carry the benchmark names.
    ("WTI crude oil futures", "futures_root_"),
    # "euro rates" also names the euro interest rates: no exchange-rate question.
    ("euro rates", "riksbank_euro_rates_"),
    # Crypto context keeps a conversion into dollars a crypto price.
    ("convert bitcoin to dollars", "onchain_bitcoin_price"),
])
def test_everyday_names_leave_narrower_questions_alone(
    catalog, query: str, expected_prefix: str,
) -> None:
    top = search_catalog(catalog, query, limit=1)[0]["operation_id"]
    assert top.startswith(expected_prefix), f"{query!r}: top-1 {top}"


def test_an_exchange_rate_question_ranks_no_central_bank_of_another_country(catalog) -> None:
    """Before currencies counted, "dollar to yen exchange rate" ranked the
    central banks of Peru, South Africa, Argentina and Malaysia first."""
    top_5 = [r["operation_id"] for r in search_catalog(catalog, "dollar to yen exchange rate", limit=5)]
    assert all(op.startswith("forex_") for op in top_5), top_5


def test_container_shipping_question_ranks_no_filler_match(catalog) -> None:
    """No operation answers container freight rates, and the short-sale
    fails-to-deliver operation ranked first on the word "to" alone."""
    results = search_catalog(
        catalog, "how much does it cost to ship a container from China to Europe", limit=5)
    assert "short_side_fails_to_deliver" not in [r["operation_id"] for r in results]


def test_lowercase_two_letter_words_are_filler_and_capitals_stay(catalog) -> None:
    """"to", "up" and "in" ranked operations that matched nothing else. In
    capitals the same word is a country code or a ticker and still counts,
    and "us" names the United States as often as the pronoun."""
    from sugra_api_mcp.catalog.search import _TWO_LETTER_FILLER

    assert all(len(word) == 2 and word.islower() for word in _TWO_LETTER_FILLER)
    assert "us" not in _TWO_LETTER_FILLER

    def reason_words(query: str) -> set[str]:
        return {reason.rsplit(":", 1)[-1]
                for result in search_catalog(catalog, query, limit=50)
                for reason in result["why"]}

    trucking = reason_words("are trucking freight rates going up in the US")
    assert not trucking & {"up", "in"}, trucking
    assert "us" in trucking
    assert "to" not in reason_words("dollar to yen")
    assert "in" in reason_words("IN GDP")


@pytest.mark.parametrize("query,absent", [
    ("cotton price", "cot"),
    ("cryptocurrency exchange", "exchange rate"),
    ("environmental data", "air quality"),
    ("Iraqi oil exports", "air quality"),
])
def test_alias_phrases_match_whole_words_only(query: str, absent: str) -> None:
    from sugra_api_mcp.catalog.aliases import matching_aliases

    assert absent not in matching_aliases(query)


@pytest.mark.parametrize("query,present", [
    ("exchange rates", "exchange rate"),
    ("currency list", "exchange rate"),
    ("COT report", "cot"),
    ("air quality Beijing", "air quality"),
])
def test_alias_phrases_still_match_their_own_words(query: str, present: str) -> None:
    from sugra_api_mcp.catalog.aliases import matching_aliases

    assert present in matching_aliases(query)


@pytest.mark.parametrize("query", [
    "TTF gas price", "European natural gas price TTF this winter", "WTI crude oil price",
])
def test_commodity_benchmarks_are_not_tickers(query: str) -> None:
    assert detect_tickers(query) == []


@pytest.mark.parametrize("query,operation,name", [
    ("oil price today", "commodities_energy_petroleum", "oil price"),
    ("Brent crude", "commodities_energy_petroleum", "brent"),
    ("TTF gas price", "commodities_commodity_id", "ttf"),
    ("Henry Hub natural gas", "commodities_energy_natural_gas", "henry hub"),
    ("ships through Suez", "maritime_chokepoints_activity", "suez"),
    ("trucking freight rates", "fred_series_series_id", "trucking price"),
    ("truckload rates", "fred_series_series_id", "trucking price"),
    ("how much does trucking cost", "fred_series_series_id", "trucking price"),
    ("truck freight prices", "fred_series_series_id", "trucking price"),
    ("port congestion", "transport_ports_congestion", "port"),
])
def test_everyday_names_point_to_their_operation(query: str, operation: str, name: str) -> None:
    from sugra_api_mcp.catalog.aliases import detect_named_operations

    assert detect_named_operations(query).operations.get(operation) == name


# Crude oil and the trucking price index are named only as the subject of a
# price word (PRICED_SUBJECTS): the price word follows the subject, or comes
# before it directly or through "of", "for" or "per", past qualifiers and, but
# before "oil" alone, places and grades; the subject's phrase then ends at a
# word such as a month, a place or a currency code. A subject joined to another
# word by "and", "or" or "vs" or by a list mark keeps the names it had before
# the rule, and so does crude oil whose phrase goes on past its price. The near
# misses, in both word orders, put trucking before another noun, another oil
# inside the subject, a word that claims the price before it or a traded
# instrument after it.
_OIL = "commodities_energy_petroleum"
_TRUCKING = "fred_series_series_id"

PRICED_SUBJECT_NAMED = [
    ("crude oil price", _OIL),
    ("WTI", _OIL),
    ("crude price", _OIL),
    ("price of crude", _OIL),
    ("price of crude oil", _OIL),
    ("crude oil spot price", _OIL),
    ("oil barrel price", _OIL),
    ("price of oil per barrel", _OIL),
    ("oil prices of the 1970s", _OIL),
    ("what's the price of oil", _OIL),
    ("why is the price of oil going up", _OIL),
    ("price of oil and gas", _OIL),
    ("palm oil vs crude oil price", _OIL),
    ("price of crude vs palm oil", _OIL),
    ("Russian oil price", _OIL),
    ("US price of crude", _OIL),
    ("oil price Germany", _OIL),
    ("price of oil Germany", _OIL),
    ("light sweet crude oil price", _OIL),
    ("price of Russian crude", _OIL),
    ("price of light sweet crude oil", _OIL),
    ("price of spot crude", _OIL),
    ("spot crude price", _OIL),
    ("price of a barrel of oil", _OIL),
    ("price of a barrel of oil, today", _OIL),
    ("price per barrel of crude", _OIL),
    ("oil price per barrel", _OIL),
    ("oil's price", _OIL),
    ("price of Venezuelan crude", _OIL),
    ("Venezuelan oil price", _OIL),
    # A demonym that names a country keeps naming an origin of the oil.
    ("Iraqi oil price", _OIL),
    ("Kuwaiti oil price", _OIL),
    ("Angolan oil price", _OIL),
    ("price of Libyan crude", _OIL),
    ("Iranian crude oil price", _OIL),
    ("annual price of oil", _OIL),
    ("price of United States crude oil", _OIL),
    ("price of U.S. crude", _OIL),
    ("U.S. crude oil price", _OIL),
    ("price of North Sea crude", _OIL),
    ("price of West Texas Intermediate crude", _OIL),
    ("West Texas Intermediate crude price", _OIL),
    ("gold and oil prices", _OIL),
    ("palm oil and crude oil prices", _OIL),
    ("OPEC price of oil", _OIL),
    ("OPEC oil price", _OIL),
    ("what affects oil prices", _OIL),
    ("what drives oil prices", _OIL),
    ("price of Saudi Arabian crude oil", _OIL),
    ("Saudi Arabian oil price", _OIL),
    ("price of Moroccan crude oil", _OIL),
    ("price of the world's oil", _OIL),
    ("synthetic crude price", _OIL),
    ("live oil price", _OIL),
    ("yesterday's oil price", _OIL),
    ("price of oil yesterday", _OIL),
    ("price of oil March 2020", _OIL),
    ("price of crude Texas", _OIL),
    ("price of oil EIA", _OIL),
    ("USD price of oil", _OIL),
    ("price of crude oil USD per barrel", _OIL),
    ("European oil prices", _OIL),
    ("price of European crude", _OIL),
    ("Middle East oil prices", _OIL),
    ("price of Middle East crude", _OIL),
    ("EIA oil price", _OIL),
    ("war oil prices", _OIL),
    ("truckload cost", _TRUCKING),
    ("cost per truckload", _TRUCKING),
    ("price per truckload", _TRUCKING),
    ("rates for truckload freight", _TRUCKING),
    ("freight rates for trucking", _TRUCKING),
    ("truckload spot rates", _TRUCKING),
    ("trucking PPI", _TRUCKING),
    ("producer price index for trucking", _TRUCKING),
    ("trucking producer price index", _TRUCKING),
    ("price of long distance trucking", _TRUCKING),
    ("is the price of trucking going up", _TRUCKING),
    ("average cost of trucking", _TRUCKING),
    ("cost of trucking goods from Chicago to Dallas", _TRUCKING),
    ("cost of trucking March 2020", _TRUCKING),
    ("truck freight rate per mile", _TRUCKING),
    ("trucking rates in Texas", _TRUCKING),
    ("less than truckload rates", _TRUCKING),
    ("long-haul trucking rates", _TRUCKING),
    ("rates for spot trucking", _TRUCKING),
    ("spot trucking rates", _TRUCKING),
    ("price of refrigerated trucking", _TRUCKING),
    ("refrigerated trucking price", _TRUCKING),
    ("rates for flatbed trucking", _TRUCKING),
    ("rates for flatbed trucking, Texas", _TRUCKING),
    ("flatbed trucking rates", _TRUCKING),
    ("trucking and rail freight rates", _TRUCKING),
    ("price index of trucking", _TRUCKING),
    ("PPI trucking", _TRUCKING),
    # The words of the name score for the named operation alone: "history"
    # and "predict" matched the prediction-market operations' own names, and
    # a repeated "price" the operations named after it.
    ("price history of oil", _OIL),
    ("oil price history", _OIL),
    ("predict the price of oil", _OIL),
    ("price of oil vs price of gold", _OIL),
]

# Named in either word order, though another operation ranks first: natural
# gas is named beside oil, and inflation has operations of its own.
PRICED_SUBJECT_NAMED_BESIDE_OTHERS = [
    ("price of oil and natural gas", _OIL),
    ("how does the price of oil affect inflation", _OIL),
]

PRICED_SUBJECT_NOT_NAMED = [
    # Trucking beside another subject, with or without a price word.
    ("trucking stocks", _TRUCKING),
    ("trucking accidents", _TRUCKING),
    ("trucking jobs", _TRUCKING),
    ("trucking companies", _TRUCKING),
    ("trucking stock price", _TRUCKING),
    ("stock price of trucking companies", _TRUCKING),
    ("price of trucking stocks", _TRUCKING),
    ("trucking index fund", _TRUCKING),
    ("cost of trucking accidents", _TRUCKING),
    ("cost of trucking school", _TRUCKING),
    ("price of trucking permits", _TRUCKING),
    ("insurance cost of trucking", _TRUCKING),
    ("environmental cost of trucking", _TRUCKING),
    ("accident rate of trucking", _TRUCKING),
    ("injury rate for trucking", _TRUCKING),
    ("rate of trucking accidents", _TRUCKING),
    ("trucking rate of growth", _TRUCKING),
    ("truckload price of corn", _TRUCKING),
    ("price per truckload of apples", _TRUCKING),
    ("a truckload of apples", _TRUCKING),
    # Freight alone also goes by sea, air and rail; a truck alone is a vehicle.
    ("freight cost per mile", _TRUCKING),
    ("container freight rates", _TRUCKING),
    ("truck prices", _TRUCKING),
    # Another oil in any word order, crude another commodity.
    ("palm oil crude price", _OIL),
    ("crude palm oil futures", _OIL),
    ("crude palm oil price", _OIL),
    ("price of crude palm oil", _OIL),
    ("palm crude price", _OIL),
    ("price of oil palm", _OIL),
    ("heating oil price", _OIL),
    ("oil price ETF", _OIL),
    ("crude steel price", _OIL),
    ("crude prices for palm oil", _OIL),
    ("palm oil price of crude", _OIL),
    ("palm oil price crude", _OIL),
    ("price of shipping oil", _OIL),
    # A word between the price and "oil" that is no grade, place or qualifier
    # may name another oil.
    ("price of car oil", _OIL),
    ("price of beard oil", _OIL),
    ("price of lemon oil", _OIL),
    ("price of synthetic oil", _OIL),
    ("price of base oil", _OIL),
    ("price of furnace oil", _OIL),
    ("price of cutting oil", _OIL),
    ("price of gas oil", _OIL),
    # A place before "oil" alone may name another oil: Moroccan oil is argan
    # oil, Italian oil olive oil.
    ("price of Moroccan oil", _OIL),
    ("price of Italian oil", _OIL),
    # A company, a traded instrument or another subject the price belongs to,
    # also beside a subject joined to another word.
    ("oil and gas company prices", _OIL),
    ("oil and the price of gold", _OIL),
    ("oil price per share", _OIL),
    ("price of oil per share", _OIL),
    ("price of oil index fund", _OIL),
    ("oil price index fund", _OIL),
    ("price of oil ETF", _OIL),
    ("oil and gas ETF prices", _OIL),
    ("oil price options", _OIL),
    ("price of oil options", _OIL),
    ("oil and gas stock prices", _OIL),
    ("price of oil and gas stocks", _OIL),
    ("trucking and logistics stock prices", _TRUCKING),
    ("trucking rate per share", _TRUCKING),
    ("price of trucking per share", _TRUCKING),
    ("price of trucking index fund", _TRUCKING),
    ("trucking price index fund", _TRUCKING),
    ("truckload price of the corn", _TRUCKING),
    ("accident rate for trucking", _TRUCKING),
    ("trucking accident rate", _TRUCKING),
    ("trucking insurance cost", _TRUCKING),
]


@pytest.mark.parametrize("query,operation", PRICED_SUBJECT_NAMED)
def test_a_priced_subject_names_its_operation_first(
    catalog, query: str, operation: str,
) -> None:
    from sugra_api_mcp.catalog.aliases import detect_named_operations

    assert operation in detect_named_operations(query).operations
    top = search_catalog(catalog, query, limit=1)[0]["operation_id"]
    assert top == operation, f"{query!r}: top-1 {top}"


@pytest.mark.parametrize("query,operation", PRICED_SUBJECT_NAMED_BESIDE_OTHERS)
def test_a_priced_subject_is_named_beside_other_words(query: str, operation: str) -> None:
    from sugra_api_mcp.catalog.aliases import detect_named_operations

    assert operation in detect_named_operations(query).operations


def test_natural_gas_named_beside_oil_ranks_first(catalog) -> None:
    """Its own words still score for the gas operation: only the words of
    the oil's name score for the oil operation alone."""
    top = [r["operation_id"] for r in search_catalog(catalog, "price of oil and natural gas", limit=2)]
    assert top == ["commodities_energy_natural_gas", _OIL], top


def test_the_words_of_a_name_score_for_the_named_operation_alone() -> None:
    from sugra_api_mcp.catalog.models import Endpoint
    from sugra_api_mcp.catalog.search import _score

    endpoint = Endpoint(operation_id="widget_price_history", method="GET", path="/x",
                        summary="Price history", toolset="markets")
    terms = ["oil", "price", "history"]
    _, why = _score(endpoint, terms, {}, named_words=frozenset({"oil", "price"}), **_SCORE_FLAGS)
    assert "summary:price" not in why and "summary:history" in why, why
    _, why = _score(endpoint, terms, {}, named_words=frozenset({"oil", "price"}),
                    own_named_words=frozenset({"oil", "price"}),
                    named_operations={"widget_price_history": "oil price"}, **_SCORE_FLAGS)
    assert "summary:price" in why, why


@pytest.mark.parametrize("query,operation", PRICED_SUBJECT_NOT_NAMED)
def test_a_near_miss_names_no_priced_subject(catalog, query: str, operation: str) -> None:
    """FRED holds the producer price index of general freight trucking and the
    petroleum operation crude oil spot prices; neither answers jobs,
    accidents, companies, stocks, funds, options, another oil or another crude
    commodity."""
    from sugra_api_mcp.catalog.aliases import detect_named_operations

    assert operation not in detect_named_operations(query).operations
    results = search_catalog(catalog, query, limit=1)
    assert not results or results[0]["operation_id"] != operation, results


@pytest.mark.parametrize("query,operation,name", [
    ("gold price and oil price", _OIL, "oil price"),
    ("gas price or oil price", _OIL, "oil price"),
    ("coffee price and crude price", _OIL, "crude price"),
    ("house and oil price", _OIL, "oil price"),
    ("price of oil and house", _OIL, "price of oil"),
    ("price of house and oil price", _OIL, "oil price"),
    ("price of oil and house price", _OIL, "price of oil"),
    ("price of oil and the dollar", _OIL, "price of oil"),
    ("rail rates and trucking rates", _TRUCKING, "trucking rate"),
    ("rail rates and trucking cost", _TRUCKING, "trucking cost"),
    ("cost of trucking and rail", _TRUCKING, "cost of trucking"),
    ("trucking and rail freight rates", _TRUCKING, "trucking"),
    ("oil and gas company prices", _OIL, None),
    ("oil and the price of gold", _OIL, None),
    ("trucking and logistics stock prices", _TRUCKING, None),
    ("trucking & rail freight rates", _TRUCKING, "trucking"),
    ("price of gold, oil", _OIL, None),
    ("gold, crude price", _OIL, "crude price"),
    ("rail rates, trucking cost", _TRUCKING, "trucking cost"),
    ("rates of rail freight, trucking", _TRUCKING, "trucking"),
    ("gold and crude outlook; price of a barrel of oil", _OIL, None),
    ("oil and vinegar; price of oil paintings", _OIL, "price of oil"),
])
def test_a_joined_priced_subject_keeps_its_names(
    query: str, operation: str, name: str | None,
) -> None:
    """A subject joined to another word by "and", "or" or "vs" or by a list
    mark, on either side, is named only by its names in NAMED_OPERATIONS, as
    before the price rule, and so is every other run of it in the query."""
    from sugra_api_mcp.catalog.aliases import detect_named_operations

    assert detect_named_operations(query).operations.get(operation) == name


@pytest.mark.parametrize("query,name", [
    ("price of oil California", "price of oil"),
    ("price of oil Bloomberg", "price of oil"),
    ("price of oil Gulf Coast", "price of oil"),
    ("price of crude Texas refineries", "price of crude"),
    ("oil price of California", "oil price"),
    ("price of oil change", "price of oil"),
    ("price of oil paintings", "price of oil"),
    ("price of crude steel", "price of crude"),
    ("price of a barrel of oil California", None),
    ("price of oil ETF", None),
])
def test_crude_oil_past_its_phrase_keeps_its_names(query: str, name: str | None) -> None:
    """Crude oil whose phrase goes on past the words its price reaches, after
    the subject or after the price's "of", is named only by its names in
    NAMED_OPERATIONS, as before the price rule; a traded instrument there
    still claims the price."""
    from sugra_api_mcp.catalog.aliases import detect_named_operations

    assert detect_named_operations(query).operations.get(_OIL) == name


@pytest.mark.parametrize("query", [
    "price of oil California", "price of oil Bloomberg", "price of oil Gulf Coast",
    "price of crude Texas refineries", "oil price of California",
])
def test_crude_oil_past_its_phrase_ranks_first(catalog, query: str) -> None:
    """Crude oil has no catalog keywords of its own: only its name ranks the
    petroleum operation first."""
    top = search_catalog(catalog, query, limit=1)[0]["operation_id"]
    assert top == _OIL, f"{query!r}: top-1 {top}"


@pytest.mark.parametrize("query,operation", [
    ("price of oil, gold and silver", _OIL),
    ("price of oil/gas", _OIL),
    ("price of gold, price of oil", _OIL),
    ("price of a barrel of oil, today", _OIL),
    ("cost of trucking, rail and air freight", _TRUCKING),
])
def test_a_list_mark_after_a_priced_subject_ends_its_phrase(query: str, operation: str) -> None:
    """A list mark after a subject whose price precedes it ends the subject's
    phrase as the end of the query does, so its price names it."""
    from sugra_api_mcp.catalog.aliases import PRICED_SUBJECTS, detect_named_operations

    label = next(subject.label for subject in PRICED_SUBJECTS if subject.operation == operation)
    assert detect_named_operations(query).operations.get(operation) == label


def test_priced_subject_words_keep_apart() -> None:
    """A word that ends a phrase among the words of other subjects would read
    "price of oil rose" as another oil; "of" ending a phrase would read "a
    truckload of apples" as trucking; a conjunction that ended no phrase would
    leave "gold and price of oil" unnamed; a grade among the words of other
    subjects would both keep and break the subject; a modified head that is no
    head would let no place or grade precede the subject."""
    from sugra_api_mcp.catalog.aliases import (
        _CONJUNCTIONS,
        _PHRASE_ENDS,
        _PRICE_LEADS,
        PRICED_SUBJECTS,
    )

    assert "of" not in _PRICE_LEADS
    assert _CONJUNCTIONS <= _PHRASE_ENDS
    for subject in PRICED_SUBJECTS:
        assert not subject.words & subject.others, subject.label
        assert not subject.others & _PRICE_LEADS, subject.label
        assert not subject.modifiers & subject.others, subject.label
        assert all(set(head.split()) <= subject.words for head in subject.heads), subject.label
        assert set(subject.modified_heads) <= set(subject.heads), subject.label


@pytest.mark.parametrize("query", [
    "crude oil", "crude oil pipelines",
    "Brent inventories", "WTI crude oil futures", "TTF futures", "European gas storage",
    "US crude oil inventory",
])
def test_no_price_name_without_a_spot_price_question(query: str) -> None:
    """"crude oil" alone also asks about pipelines and stocks; futures, stocks
    and output have operations of their own."""
    from sugra_api_mcp.catalog.aliases import detect_named_operations

    operations = detect_named_operations(query).operations
    assert not [op for op in operations if op.startswith("commodities_")], operations


def test_a_named_port_asks_for_port_activity_in_its_country() -> None:
    from sugra_api_mcp.catalog.aliases import detect_named_operations

    named = detect_named_operations("ship calls at Rotterdam")
    assert named.operations == {"transport_ports_congestion": "rotterdam"}
    assert named.countries == {"NL"}
    assert named.words == {"rotterdam"}


@pytest.mark.parametrize("query", [
    "weather in Rotterdam", "traffic congestion in Los Angeles", "busy airports",
    "hotels in Dubai",
])
def test_a_city_without_a_shipping_cue_names_no_port(query: str) -> None:
    from sugra_api_mcp.catalog.aliases import detect_named_operations

    assert detect_named_operations(query).operations == {}


def test_a_named_port_steps_the_single_country_port_source_down(catalog) -> None:
    """Fintraffic Portnet covers Finnish ports only; untagged, it ranked
    first for ship calls at Rotterdam."""
    from sugra_api_mcp.catalog.search import WRONG_COUNTRY_PENALTY, _score

    endpoint = catalog.get("transport_ports_port_calls")
    plain, _ = _score(endpoint, ["port", "calls"], {}, **_SCORE_FLAGS)
    stepped, _ = _score(endpoint, ["port", "calls"], {}, penalty_countries={"NL"}, **_SCORE_FLAGS)
    assert plain - stepped == WRONG_COUNTRY_PENALTY
    # `why` keeps six reasons; with no word matched the penalty is the only one.
    _, why = _score(endpoint, [], {}, penalty_countries={"NL"}, **_SCORE_FLAGS)
    assert why == ["geo-mismatch:FI"]


@pytest.mark.parametrize("query,currencies,pair,over_time", [
    ("dollar to yen", ("USD", "JPY"), True, False),
    ("How many Turkish lira for one US dollar", ("TRY", "USD"), True, False),
    ("EUR/USD", ("EUR", "USD"), True, False),
    ("usd to jpy", ("USD", "JPY"), True, False),
    ("How much is 100 euros in dollars", ("EUR", "USD"), True, False),
    ("euro exchange rate", ("EUR",), False, False),
    ("Indian rupee rate", ("INR",), False, False),
    ("dollar to yen history", ("USD", "JPY"), True, True),
    ("Japanese yen exchange rate history", ("JPY",), False, True),
])
def test_detect_fx_request(
    query: str, currencies: tuple[str, ...], pair: bool, over_time: bool,
) -> None:
    from sugra_api_mcp.catalog.aliases import detect_fx_request

    fx = detect_fx_request(query)
    assert fx is not None
    assert (fx.currencies, fx.pair, fx.over_time) == (currencies, pair, over_time)


@pytest.mark.parametrize("query", [
    "price of coffee per pound in dollars",
    "how much is a pound of beef in euros",
    "coffee price in dollars",
    "dollar and euro",
    "euro rates",
    "try again later",
])
def test_detect_fx_request_asks_nothing_without_a_rate_question(query: str) -> None:
    from sugra_api_mcp.catalog.aliases import detect_fx_request

    assert detect_fx_request(query) is None


def test_fx_request_names_its_words_and_issuers() -> None:
    from sugra_api_mcp.catalog.aliases import detect_fx_request

    fx = detect_fx_request("dollar to yen")
    assert fx is not None
    assert fx.words == {"dollar", "yen"}
    assert fx.issuer_countries == {"US", "JP"}


def test_every_detectable_currency_has_an_issuer() -> None:
    from sugra_api_mcp.catalog.aliases import _CURRENCY_ISSUERS, _KNOWN_CURRENCIES, CURRENCY_NAMES

    codes = set(CURRENCY_NAMES.values()) | _KNOWN_CURRENCIES
    assert codes <= set(_CURRENCY_ISSUERS), sorted(codes - set(_CURRENCY_ISSUERS))


def test_every_named_target_is_a_bundled_operation(catalog) -> None:
    """A renamed or removed operation would silently disarm the name that
    points to it; fail loudly instead, like the country-prefix map."""
    import re

    from sugra_api_mcp.catalog.aliases import (
        CENTRAL_BANK_POLICY_RATES,
        COMPOUND_NAMED_OPERATIONS,
        FX_CONVERT_OPERATION,
        NAMED_OPERATIONS,
        NAMED_PLACES,
        OPERATION_INPUT_WORDS,
        PORT_OPERATIONS,
        TOPIC_DEFAULT_OPERATIONS,
        US_WEATHER_OPERATION,
        WEATHER_FORECAST_OPERATION,
        WEATHER_HISTORY_OPERATION,
    )

    ids = {endpoint.operation_id for endpoint in catalog.endpoints}
    targets = {op for ops in NAMED_OPERATIONS.values() for op in ops}
    targets |= {*PORT_OPERATIONS, FX_CONVERT_OPERATION, *TOPIC_DEFAULT_OPERATIONS.values()}
    targets |= {WEATHER_FORECAST_OPERATION, WEATHER_HISTORY_OPERATION, US_WEATHER_OPERATION}
    targets |= {*CENTRAL_BANK_POLICY_RATES.values(), *OPERATION_INPUT_WORDS}
    assert targets <= ids, f"names pointing to no bundled operation: {sorted(targets - ids)}"
    dead = [prefix for prefix in COMPOUND_NAMED_OPERATIONS
            if not any(op.startswith(prefix) for op in ids)]
    assert not dead, f"compound prefixes matching no bundled operation: {dead}"
    for heads, tails in COMPOUND_NAMED_OPERATIONS.values():
        assert heads and tails and all(
            re.fullmatch(r"[a-z0-9]+", word) for word in (*heads, *tails)), (heads, tails)
    assert set(NAMED_PLACES) <= set(NAMED_OPERATIONS)


# ---- Weather: the topic default and the space-weather compound ---------------

WEATHER_TOP_1 = [
    ("Paris weather", "v2_weather_forecast"),
    ("weather in Paris", "v2_weather_forecast"),
    ("weather in Rotterdam", "v2_weather_forecast"),
    ("weather today", "v2_weather_forecast"),
    ("weather forecast Paris", "v2_weather_forecast"),
    ("weather history in London", "v2_weather_history"),
    ("weather alerts in Texas", "weather_us_alerts"),
    ("Hong Kong weather", "data_gov_hk_hko_current_weather"),
    ("space weather alerts", "space_weather_alerts"),
    ("Kp index", "space_weather_kp_index"),
    # Everyday words: the Hong Kong Observatory ("current"), NOAA water
    # temperature, a climate projection, the forecast's past_days parameter and
    # a deprecated operation's "New York" example answered these first.
    ("current weather in Berlin", "v2_weather_forecast"),
    ("current weather in berlin", "v2_weather_forecast"),
    ("temperature in Dubai", "v2_weather_forecast"),
    ("will it rain in Rome tomorrow", "v2_weather_forecast"),
    ("is it raining in London", "v2_weather_forecast"),
    ("weather in New York", "v2_weather_forecast"),
    ("past weather in London", "v2_weather_history"),
    ("weather in USA", "weather_us_forecast"),
    ("temperature in US", "weather_us_forecast"),
    ("sea level forecast", "weather_marine_sea_level"),
    ("Weather Station Observations", "weather_nws_station_station_id_observations"),
]


@pytest.mark.parametrize("query,expected", WEATHER_TOP_1)
def test_weather_questions_land_their_operation_top_1(catalog, query: str, expected: str) -> None:
    results = search_catalog(catalog, query, limit=5)
    assert results and results[0]["operation_id"] == expected, (
        f"{query!r}: top-5 {[(r['operation_id'], r['score']) for r in results]}")


def test_space_weather_still_answers_space_weather(catalog) -> None:
    top_3 = [r["operation_id"] for r in search_catalog(catalog, "space weather", limit=3)]
    assert all(op.startswith("space_weather_") for op in top_3), top_3


def test_equal_scores_put_the_topic_default_operation_first() -> None:
    """The word "weather" alone scores nine operations equally, and the
    operation_id order put the Hong Kong Observatory first for Paris. "Paris
    weather" now names the forecast outright; a question that names nothing
    ("weather data") still ties."""
    from sugra_api_mcp.catalog.aliases import topic_default_operations
    from sugra_api_mcp.catalog.models import Catalog, Endpoint

    def endpoint(operation_id: str) -> Endpoint:
        return Endpoint(operation_id=operation_id, method="GET", path="/x",
                        summary="Weather report", toolset="environment")

    two = Catalog(source="test", endpoints=[endpoint("a_weather"), endpoint("v2_weather_forecast")])
    ranked = [(r["operation_id"], r["score"]) for r in search_catalog(two, "weather data", limit=5)]
    assert [op for op, _ in ranked] == ["v2_weather_forecast", "a_weather"]
    assert ranked[0][1] == ranked[1][1]
    # Without the topic word the tie stays in operation_id order.
    ranked = [r["operation_id"] for r in search_catalog(two, "report", limit=5)]
    assert ranked == ["a_weather", "v2_weather_forecast"]

    assert topic_default_operations("Weather in Paris") == {"v2_weather_forecast"}
    assert topic_default_operations("whether it rains") == frozenset()


def test_a_compound_named_operation_answers_its_last_word_only_beside_its_first() -> None:
    """"space weather" is solar activity: an operation named for it must not
    answer the weather in Paris, and must still answer space weather."""
    from sugra_api_mcp.catalog.models import Endpoint
    from sugra_api_mcp.catalog.search import _score

    def endpoint(operation_id: str) -> Endpoint:
        return Endpoint(operation_id=operation_id, method="GET", path="/x",
                        summary="Space weather scales", toolset="environment")

    space = endpoint("space_weather_widget")
    assert _score(space, ["paris", "weather"], {}, **_SCORE_FLAGS) == (0, [])
    _, why = _score(space, ["space", "weather"], {}, **_SCORE_FLAGS)
    assert {"summary:space", "summary:weather"} <= set(why), why
    # Only the compound's operations go quiet on the word.
    _, why = _score(endpoint("v2_weather_widget"), ["paris", "weather"], {}, **_SCORE_FLAGS)
    assert "summary:weather" in why


# ---- Weather questions in everyday words ---------------------------------------
# The terms below are what search_catalog passes: the query's words with its
# filler ("will", "it", "in", "for") already dropped.

def test_an_everyday_weather_question_names_the_forecast_or_the_history() -> None:
    from sugra_api_mcp.catalog.aliases import (
        WEATHER_FORECAST_OPERATION,
        WEATHER_HISTORY_OPERATION,
        detect_weather_request,
    )

    rome = detect_weather_request("will it rain in Rome tomorrow", ["rain", "rome", "tomorrow"])
    assert rome is not None
    assert rome.operation == WEATHER_FORECAST_OPERATION
    # The weather and time words are consumed; the place stays a search term.
    assert rome.words == {"rain", "tomorrow"}

    past = detect_weather_request("past weather in London", ["past", "weather", "london"])
    assert past is not None
    assert (past.operation, past.name) == (WEATHER_HISTORY_OPERATION, "past weather")

    # "weather" asks by itself; a word after "in" names a place in lowercase too.
    for query, terms in [
        ("weather", ["weather"]),
        ("current weather in berlin", ["current", "weather", "berlin"]),
        ("temperature in Dubai", ["temperature", "dubai"]),
        ("weather forecast Paris", ["weather", "forecast", "paris"]),
        ("South America weather", ["south", "america", "weather"]),
    ]:
        request = detect_weather_request(query, terms)
        assert request is not None and request.operation == WEATHER_FORECAST_OPERATION, query


def test_a_us_weather_question_names_the_national_weather_service() -> None:
    from sugra_api_mcp.catalog.aliases import (
        US_WEATHER_OPERATION,
        WEATHER_FORECAST_OPERATION,
        WEATHER_HISTORY_OPERATION,
        detect_weather_request,
    )

    for query, terms in [
        ("weather in USA", ["weather", "usa"]),
        ("US weather", ["us", "weather"]),
        ("temperature in US", ["temperature", "us"]),
        ("weather in the United States", ["weather", "united", "states"]),
    ]:
        request = detect_weather_request(query, terms)
        assert request is not None and request.operation == US_WEATHER_OPERATION, query
    # Beside another place the worldwide forecast answers; the past is the
    # worldwide history's; Hong Kong's own words find the Observatory.
    miami = detect_weather_request("weather in Miami USA", ["weather", "miami", "usa"])
    assert miami is not None and miami.operation == WEATHER_FORECAST_OPERATION
    past = detect_weather_request("past weather in USA", ["past", "weather", "usa"])
    assert past is not None and past.operation == WEATHER_HISTORY_OPERATION
    assert detect_weather_request("Hong Kong weather", ["hong", "kong", "weather"]) is None


@pytest.mark.parametrize("query,terms", [
    ("marine weather", ["marine", "weather"]),
    ("weather alerts in Texas", ["weather", "alerts", "texas"]),
    ("temperature anomaly 2023", ["temperature", "anomaly", "2023"]),
    ("water temperature in Miami", ["water", "temperature", "miami"]),
    ("weather station observations", ["weather", "station", "observations"]),
    # Capitals mark nothing in a query written all in title case.
    ("Weather Station Observations", ["weather", "station", "observations"]),
    # Another weather word needs a place or a time beside it.
    ("temperature", ["temperature"]),
    # "forecast" and "wind" also name an economic forecast and wind power.
    ("forecast for Germany", ["forecast", "germany"]),
    ("wind in Germany", ["wind", "germany"]),
    # A two-letter word after "in" is no town.
    ("weather in NY", ["weather", "ny"]),
])
def test_a_question_that_asks_something_else_is_no_weather_request(query: str, terms: list[str]) -> None:
    from sugra_api_mcp.catalog.aliases import detect_weather_request

    assert detect_weather_request(query, terms) is None


def test_weather_questions_that_ask_something_else_keep_their_operations(catalog) -> None:
    for query, prefix in [("marine weather", "weather_marine_"), ("US weather alerts", "weather_us_alerts")]:
        top = search_catalog(catalog, query, limit=1)[0]["operation_id"]
        assert top.startswith(prefix), (query, top)
    top = search_catalog(catalog, "wind in Germany", limit=1)[0]["operation_id"]
    assert top != "v2_weather_forecast"


def test_the_national_weather_countries_are_those_of_the_weather_sources(catalog) -> None:
    """A country whose own weather service joins the catalog joins the list,
    or its questions go to the worldwide forecast."""
    from sugra_api_mcp.catalog.aliases import NATIONAL_WEATHER_COUNTRIES, SOURCE_COUNTRY_PREFIXES

    ids = [endpoint.operation_id for endpoint in catalog.endpoints]
    countries = {
        country for prefix, country in SOURCE_COUNTRY_PREFIXES.items()
        if any(op.startswith(prefix) and "weather" in op for op in ids)
    }
    assert countries == NATIONAL_WEATHER_COUNTRIES


# ---- Central banks' policy rates ---------------------------------------------------
# A rate word beside a bank's name, with nothing else asked, names the
# operation that holds the bank's policy rate. Before, "Bank of Canada rate
# decision" ranked the Bank Rate and the prime rate above it, and no ECB rate
# question reached the ECB's policy rate, a curated series.

POLICY_RATE_TOP_1 = [
    ("BoC rate decision", "boc_policy_rate", None),
    ("Bank of Canada rate decision", "boc_policy_rate", None),
    # The meeting calendar does not publish the Bank of Canada's meetings.
    ("when is the next Bank of Canada rate decision", "boc_policy_rate", None),
    ("ECB rate decision", "macro_country_section", "eu/ecbdfr"),
    ("ECB interest rate", "macro_country_section", "eu/ecbdfr"),
    ("ECB interest rates", "macro_country_section", "eu/ecbdfr"),
    ("ECB deposit rate", "macro_country_section", "eu/ecbdfr"),
    ("ECB deposit facility rate", "macro_country_section", "eu/ecbdfr"),
    ("BoJ rate decision", "boj_rates", None),
    ("SNB rate decision", "snb_policy_rate", None),
    ("Riksbank rate decision", "riksbank_policy_rate", None),
    ("Norges Bank rate decision", "norges_bank_policy_rate", None),
    ("RBA rate decision", "rba_cash_rate", None),
    ("Bank of England rate decision", "boe_rate", None),
    ("Fed rate decision", "fed_rates_rate_type", None),
    ("Fed funds rate", "fed_rates_rate_type", None),
    ("Bank Negara Malaysia interest rate", "bnm_opr", None),
    ("BCB interest rate", "bcb_selic", None),
]


@pytest.mark.parametrize("query,operation,key", POLICY_RATE_TOP_1)
def test_a_central_bank_rate_question_lands_its_policy_rate_first(
    catalog, query: str, operation: str, key: str | None,
) -> None:
    results = search_catalog(catalog, query, limit=3)
    top = results[0]
    assert top["operation_id"] == operation, [r["operation_id"] for r in results]
    if key is not None:
        assert top["macro_keys"][0]["key"] == key, top["macro_keys"]


# A question that asks more than the policy rate asks for another series of
# the bank, and keeps it first.
POLICY_RATE_QUALIFIED_TOP_1 = [
    ("Norges Bank interest rate swaps", "norges_bank_irs"),
    ("Norges Bank policy rate announcements", "norges_bank_policy_rate_announcements"),
    ("SARB prime interest rate", "central_banks_sarb_prime_rate"),
    ("CNB policy rate history", "cnb_policy_rate_history"),
    ("RBA housing interest rates", "rba_housing_rates"),
    ("BoE household interest rates", "boe_household_rates"),
    ("SNB sight deposit rate", "snb_sight_deposit_rate"),
    ("Riksbank policy rates all", "riksbank_policy_rates_all"),
    ("BNM interbank interest rate", "bnm_interest_rate"),
]


@pytest.mark.parametrize("query,operation", POLICY_RATE_QUALIFIED_TOP_1)
def test_a_qualified_rate_question_keeps_its_series_first(catalog, query: str, operation: str) -> None:
    top = [r["operation_id"] for r in search_catalog(catalog, query, limit=3)]
    assert top[0] == operation, top


# The terms below are what search_catalog passes: the query's words with its
# filler dropped.
@pytest.mark.parametrize("query,terms,operations,keys,words", [
    ("Bank of Canada rate decision", ["bank", "canada", "rate", "decision"],
     {"boc_policy_rate": "rate decision"}, set(), {"rate", "decision"}),
    ("ECB deposit rate", ["ecb", "deposit", "rate"], {}, {"eu/ecbdfr"}, {"deposit", "rate"}),
    ("Fed and ECB interest rates", ["fed", "ecb", "interest", "rates"],
     {"fed_rates_rate_type": "interest rate"}, {"eu/ecbdfr"}, {"interest", "rates"}),
    ("when is the next ECB rate decision", ["next", "ecb", "rate", "decision"],
     {"macro_cb_calendar": "rate decision", "macro_cb_calendar_bank": "rate decision"},
     set(), {"rate", "decision"}),
])
def test_detect_policy_rate_request(
    query: str, terms: list[str], operations: dict[str, str], keys: set[str], words: set[str],
) -> None:
    from sugra_api_mcp.catalog.aliases import (
        detect_policy_rate_request,
        matching_central_bank_prefixes,
    )

    request = detect_policy_rate_request(query, terms, matching_central_bank_prefixes(query))
    assert (request.operations, request.keys, request.words) == (operations, keys, words)


@pytest.mark.parametrize("query,terms", [
    # Another word asks for another series of the bank.
    ("Norges Bank interest rate swaps", ["norges", "bank", "interest", "rate", "swaps"]),
    ("CNB policy rate history", ["cnb", "policy", "rate", "history"]),
    # No rate word.
    ("ECB yield curve", ["ecb", "yield", "curve"]),
    ("BoC meeting", ["boc", "meeting"]),
    # No bank named.
    ("Canada interest rate", ["canada", "interest", "rate"]),
])
def test_a_question_that_asks_more_names_no_policy_rate(query: str, terms: list[str]) -> None:
    from sugra_api_mcp.catalog.aliases import (
        detect_policy_rate_request,
        matching_central_bank_prefixes,
    )

    request = detect_policy_rate_request(query, terms, matching_central_bank_prefixes(query))
    assert (request.operations, request.keys, request.words) == ({}, frozenset(), frozenset())


def test_every_policy_rate_belongs_to_its_bank(catalog) -> None:
    """A bank prefix that left the boost map, an operation of another bank or
    a curated key no operation carries would silently disarm a policy rate."""
    from sugra_api_mcp.catalog.aliases import (
        CENTRAL_BANK_POLICY_RATE_KEYS,
        CENTRAL_BANK_POLICY_RATES,
        CENTRAL_BANK_PREFIX_BOOSTS,
    )

    banks = set(CENTRAL_BANK_POLICY_RATES) | set(CENTRAL_BANK_POLICY_RATE_KEYS)
    assert banks <= set(CENTRAL_BANK_PREFIX_BOOSTS.values())
    assert not set(CENTRAL_BANK_POLICY_RATES) & set(CENTRAL_BANK_POLICY_RATE_KEYS)
    for prefix, operation in CENTRAL_BANK_POLICY_RATES.items():
        assert operation.startswith(prefix), (prefix, operation)
    carried = {key.key for endpoint in catalog.endpoints for key in endpoint.macro_keys or ()}
    assert set(CENTRAL_BANK_POLICY_RATE_KEYS.values()) <= carried


# ---- "real": adjusted for inflation, or real estate -------------------------------
# "real" alone means adjusted for inflation, as in real wages or real GDP, and
# names the real-estate operations only beside a word for property. The word
# alone ranked them first for "real wages UK".

@pytest.mark.parametrize("query", [
    "real wages UK", "real interest rate", "real disposable income",
    "US real interest rate", "real GDP Germany",
])
def test_real_alone_ranks_no_real_estate(catalog, query: str) -> None:
    top_5 = [r["operation_id"] for r in search_catalog(catalog, query, limit=5)]
    assert not [op for op in top_5 if op.startswith("real_estate_")], top_5


@pytest.mark.parametrize("query,expected", [
    ("real estate prices", "real_estate_"),
    ("realty prices", "real_estate_"),
    ("real home prices", "real_estate_home_values_geo_type"),
])
def test_real_beside_a_property_word_ranks_real_estate_first(catalog, query: str, expected: str) -> None:
    top = search_catalog(catalog, query, limit=1)[0]["operation_id"]
    assert top.startswith(expected), (query, top)


def test_real_house_prices_rank_a_house_price_series_first(catalog) -> None:
    top = search_catalog(catalog, "real house prices", limit=1)[0]["operation_id"]
    assert top.endswith("_house_prices"), top


def test_real_answers_the_real_estate_operations_only_beside_a_property_word() -> None:
    from sugra_api_mcp.catalog.models import Endpoint
    from sugra_api_mcp.catalog.search import _score

    estate = Endpoint(operation_id="real_estate_widget", method="GET", path="/x",
                      summary="Real home values", toolset="real_estate")
    _, why = _score(estate, ["real", "wages"], {}, **_SCORE_FLAGS)
    assert not [reason for reason in why if reason.endswith(":real")], why
    for word in ("estate", "realty", "home", "houses", "housing"):
        _, why = _score(estate, ["real", word], {}, **_SCORE_FLAGS)
        assert "summary:real" in why, (word, why)
    # Only the real-estate operations go quiet on the word.
    wages = Endpoint(operation_id="earnings_widget", method="GET", path="/x",
                     summary="Real wages", toolset="macro")
    _, why = _score(wages, ["real", "wages"], {}, **_SCORE_FLAGS)
    assert "summary:real" in why, why


# ---- A country without curated keys, and the demonyms that name it ----------------

@pytest.mark.parametrize("demonym,country", [
    ("Afghan", "AF"), ("Algerian", "DZ"), ("Angolan", "AO"), ("Bahraini", "BH"),
    ("Bangladeshi", "BD"), ("Ghanaian", "GH"), ("Iranian", "IR"), ("Iraqi", "IQ"),
    ("Jordanian", "JO"), ("Kuwaiti", "KW"), ("Lebanese", "LB"), ("Libyan", "LY"),
    ("Omani", "OM"), ("Qatari", "QA"), ("Syrian", "SY"), ("Tunisian", "TN"),
    ("Venezuelan", "VE"), ("Yemeni", "YE"),
])
def test_a_demonym_names_its_country(demonym: str, country: str) -> None:
    from sugra_api_mcp.catalog.aliases import detect_query_countries

    assert detect_query_countries(f"{demonym} inflation") == {country}


@pytest.mark.parametrize("query,expected", [
    ("Netherlands CPI inflation", {"bis_cpi"}),
    # The two operations that answer for the country tie.
    ("Dutch inflation", {"macro_country_profile", "worldbank_country_overview"}),
    ("Iranian inflation rate", {"macro_country_profile"}),
    ("Venezuelan GDP", {"research_pwt_country_iso3"}),
])
def test_a_country_without_curated_keys_lands_an_operation_that_answers_for_it(
    catalog, query: str, expected: set[str],
) -> None:
    """A telecom-demand heuristic, a US series and the GDP of Spain ranked
    first; each expected operation takes the country as a parameter."""
    top = [r["operation_id"] for r in search_catalog(catalog, query, limit=3)]
    assert top[0] in expected, top


def test_a_national_source_of_another_country_ranks_below_the_answers(catalog) -> None:
    results = search_catalog(catalog, "Venezuelan GDP", limit=200)
    ranks = {r["operation_id"]: i for i, r in enumerate(results)}
    answer = ranks["research_pwt_country_iso3"]
    for operation in ("ine_gdp", "ons_gdp", "stat_finland_gdp"):
        assert ranks.get(operation, len(ranks)) > answer, (operation, ranks.get(operation))


def test_an_operation_computed_from_inflation_is_no_inflation_series(catalog) -> None:
    """The telecom-demand heuristic is computed from growth, income and
    inflation; it ranked first for "Dutch inflation" and still answers for
    telecom demand."""
    top_5 = [r["operation_id"] for r in search_catalog(catalog, "Dutch inflation", limit=5)]
    assert "imf_signals_telecom_demand_country" not in top_5, top_5
    top = search_catalog(catalog, "telecom demand Netherlands", limit=1)[0]["operation_id"]
    assert top == "imf_signals_telecom_demand_country", top


# ---- A statistic every country reports, asked for no country -----------------------

@pytest.mark.parametrize("query", [
    "inflation", "inflation rate", "current inflation rate", "what drives inflation",
    "how do oil prices affect inflation", "how does the price of oil affect inflation",
    "oil prices and inflation", "inflation and oil prices", "oil price inflation",
    "how does inflation affect gold", "inflation in emerging markets",
    "unemployment", "unemployment rate", "GDP", "real GDP growth", "CPI",
])
def test_a_statistic_asked_for_no_country_finds_an_operation_that_takes_the_country(
    catalog, query: str,
) -> None:
    """The inflation of Argentina, the unemployment of Finland and the GDP of
    Spain ranked first for a question that names no country."""
    from sugra_api_mcp.catalog.aliases import SOURCE_COUNTRY_PREFIXES

    top = search_catalog(catalog, query, limit=1)[0]["operation_id"]
    endpoint = next(e for e in catalog.endpoints if e.operation_id == top)
    assert not top.startswith(tuple(SOURCE_COUNTRY_PREFIXES)), top
    assert any(parameter.name.lower() == "country" for parameter in endpoint.parameters), top


def test_a_national_source_stepped_down_keeps_its_rank_over_unrelated_operations(catalog) -> None:
    """Pushed below the weakest operation that takes the country, FRED fell
    below price histories of prediction-market tokens, which match only
    "history" and a word of the CPI alias."""
    results = search_catalog(catalog, "inflation history since 1970", limit=200)
    ranks = {r["operation_id"]: i for i, r in enumerate(results)}
    fred = ranks["fred_series_series_id"]
    for operation in ("predictions_price_history_token_id", "onchain_bitcoin_price_history"):
        assert ranks.get(operation, len(ranks)) > fred, (operation, ranks.get(operation), fred)


@pytest.mark.parametrize("query,expected", [
    ("Argentina inflation", "central_banks_bcra_inflation"),
    ("Australia inflation", "rba_cpi"),
    # No operation that takes the country holds the coffee price; FRED does.
    ("coffee prices and inflation", "fred_series_series_id"),
])
def test_a_national_source_keeps_first_place_where_the_question_asks_for_it(
    catalog, query: str, expected: str,
) -> None:
    top = search_catalog(catalog, query, limit=1)[0]["operation_id"]
    assert top == expected, top


@pytest.mark.parametrize("query", [
    "coffee CPI",
    "gasoline CPI",
    "coffee consumer prices",
    "gasoline consumer prices",
])
def test_a_product_named_before_a_price_statistic_is_the_subject(catalog, query: str) -> None:
    """The CPI of every country ranked above FRED, which holds the coffee and
    the gasoline price index: no operation that takes the country answers
    the product."""
    top = search_catalog(catalog, query, limit=1)[0]
    assert top["operation_id"] == "fred_series_series_id", top
    assert any(note.startswith("lifted-above:") for note in top["why"]), top["why"]


@pytest.mark.parametrize("query", [
    # Coffee says which countries.
    "CPI for countries producing coffee",
    # Two things asked, not the coffee CPI.
    "coffee, CPI",
    "coffee; CPI",
])
def test_a_product_named_elsewhere_in_the_question_is_no_subject(catalog, query: str) -> None:
    """The question asks for the CPI, so FRED keeps its own rank below the
    CPI of every country."""
    results = search_catalog(catalog, query, limit=2)
    assert [r["operation_id"] for r in results] == ["bis_cpi", "fred_series_series_id"], results
    assert not any(note.startswith(("lifted-above:", "clamped-below:"))
                   for note in results[1]["why"]), results[1]["why"]


def test_a_product_before_one_of_two_statistics_is_no_subject() -> None:
    """In "coffee GDP; inflation" coffee comes before the GDP, so a national
    source keyed on coffee that answers the inflation is not the subject of
    the question, and both operations keep their own ranks."""
    from sugra_api_mcp.catalog.models import Catalog, Endpoint, EndpointParameter

    country = EndpointParameter(name="country", location="query")
    takes = Endpoint(operation_id="world_widget", method="GET", path="/inflation",
                     summary="Inflation", description="Inflation and GDP", parameters=[country])
    keyed = Endpoint(operation_id="ine_widget", method="GET", path="/y",
                     summary="Prices", description="Inflation", keywords=["coffee"])
    results = search_catalog(Catalog(source="test", endpoints=[takes, keyed]),
                             "coffee GDP; inflation", limit=5)
    assert [r["operation_id"] for r in results] == ["world_widget", "ine_widget"], results
    for result in results:
        assert not any(note.startswith(("lifted-above:", "clamped-below:"))
                       for note in result["why"]), result["why"]


@pytest.mark.parametrize("query,expected", [
    ("CPI", "bis_cpi"),
    ("consumer prices", "bis_cpi"),
    ("inflation", "macro_country_profile"),
    ("food inflation", "macro_country_profile"),
    ("core inflation", "macro_country_profile"),
    ("wage growth and inflation", "macro_country_profile"),
    # Words that only a summary, a path or a parameter of a national source
    # names are no subject of the question.
    ("inflation forecast", "macro_country_profile"),
    ("inflation history since 1970", "macro_country_profile"),
])
def test_a_price_statistic_without_a_product_keeps_its_any_country_answer(
    catalog, query: str, expected: str,
) -> None:
    top = search_catalog(catalog, query, limit=1)[0]["operation_id"]
    assert top == expected, top


def test_only_a_keyword_makes_a_national_source_the_subject() -> None:
    """A word the national source names in its keywords lifts it above the
    operation that takes the country; the same word in its summary keeps
    its rank only."""
    from sugra_api_mcp.catalog.models import Catalog, Endpoint, EndpointParameter

    country = EndpointParameter(name="country", location="query")
    takes = Endpoint(operation_id="world_widget", method="GET", path="/inflation",
                     summary="Inflation", description="Inflation", parameters=[country])
    keyed = Endpoint(operation_id="ine_widget", method="GET", path="/y",
                     summary="Prices", description="Inflation", keywords=["coffee"])
    worded = Endpoint(operation_id="ons_widget", method="GET", path="/z",
                      summary="Prices coffee", description="Inflation")
    results = search_catalog(Catalog(source="test", endpoints=[takes, keyed, worded]),
                             "coffee inflation", limit=5)
    assert [r["operation_id"] for r in results] == ["ine_widget", "world_widget", "ons_widget"], results
    assert "lifted-above:any-country-answers" in results[0]["why"], results[0]["why"]
    assert "clamped-below:subject-answers" in results[1]["why"], results[1]["why"]
    # The summary-only operation scores and explains itself as it does alone.
    alone = search_catalog(Catalog(source="test", endpoints=[worded]), "coffee inflation", limit=1)
    assert (results[2]["score"], results[2]["why"]) == (alone[0]["score"], alone[0]["why"]), (
        results[2], alone)


def test_a_subject_names_only_what_it_moved_past() -> None:
    """A subject that overtakes only a national source says so, not that it
    overtook an operation that takes the country, which it ranked above
    already; the national source it overtook names the operation that takes
    the country, which overtook it too."""
    from sugra_api_mcp.catalog.models import Catalog, Endpoint, EndpointParameter

    country = EndpointParameter(name="country", location="query")
    # Before the trade: the national source 32, the subject 30, the operation
    # that takes the country 18.
    national = Endpoint(operation_id="ine_inflation", method="GET", path="/inflation",
                        summary="Inflation", description="Inflation")
    keyed = Endpoint(operation_id="ons_widget", method="GET", path="/y",
                     summary="Prices", description="Inflation", keywords=["coffee", "inflation"])
    takes = Endpoint(operation_id="world_widget", method="GET", path="/x",
                     summary="Prices", description="Inflation", parameters=[country])
    results = search_catalog(Catalog(source="test", endpoints=[national, keyed, takes]),
                             "coffee inflation", limit=5)
    assert [r["operation_id"] for r in results] == ["ons_widget", "world_widget", "ine_inflation"], results
    assert "lifted-above:national-sources" in results[0]["why"], results[0]["why"]
    assert "lifted-above:national-sources" in results[1]["why"], results[1]["why"]
    assert "clamped-below:any-country-answers" in results[2]["why"], results[2]["why"]


@pytest.mark.parametrize("query,words", [
    ("GDP", {"gdp"}),
    ("CPI inflation", {"cpi", "inflation"}),
    ("the exchange rate and unemployment", {"unemployment"}),
    ("interest rate", set()),
    ("bond yield and the trade balance", set()),
])
def test_only_a_one_word_statistic_asks_for_any_country(query: str, words: set[str]) -> None:
    """One query word matched in an operation's fields says the operation
    answers the statistic, so the cues of two words stay out."""
    assert country_statistic_words(query) == words


_ANY_COUNTRY_NOTES = {"pattern:any-country->param", "lifted-above:national-sources",
                      "clamped-below:any-country-answers"}


@pytest.mark.parametrize("query", [
    "Henry Hub and inflation",  # a benchmark's market
    "Fed interest rate and inflation",  # a central bank
    "ECB inflation",
    "AAPL inflation",  # a listing
    "us inflation",  # the United States, in any spelling
    "U.S. inflation",
    # A statistic in other words, beside a place.
    "Spain consumer prices",
    "ECB consumer prices",
    "Fed jobless claims",
    "TLT consumer prices",
])
def test_a_question_that_names_a_place_asks_for_no_other_country(catalog, query: str) -> None:
    for row in search_catalog(catalog, query, limit=2000):
        assert not _ANY_COUNTRY_NOTES & set(row["why"]), (row["operation_id"], row["why"])


@pytest.mark.parametrize(("query", "place"), [
    ("ECB inflation", "EU"),  # the euro area: no national source answers it
    ("Fed inflation", "US"),
    ("Bank of England inflation", "GB"),
    ("Fed unemployment", "US"),
    ("us inflation", "US"),  # the United States, in any spelling
    ("U.S. inflation", "US"),
])
def test_a_named_central_bank_or_the_united_states_names_its_place(catalog, query: str, place: str) -> None:
    """A national source of another country is never among the first answers
    about a named central bank or the United States: the inflation of Argentina
    ranked first for "Fed inflation" and third for "U.S. inflation"."""
    from sugra_api_mcp.catalog.search import _source_country

    for row in search_catalog(catalog, query, limit=3):
        country = _source_country(catalog.get(row["operation_id"]))
        assert country in (None, place), (query, row["operation_id"], country)


def test_a_named_place_still_wins_over_the_word_us(catalog) -> None:
    """"show us Japan inflation" asks about Japan: the word "us" names the
    United States only when no other place is named."""
    from sugra_api_mcp.catalog.search import _source_country

    for row in search_catalog(catalog, "show us Japan inflation", limit=3):
        assert _source_country(catalog.get(row["operation_id"])) != "US", row["operation_id"]


def test_every_central_bank_names_its_place() -> None:
    from sugra_api_mcp.catalog.aliases import (
        CENTRAL_BANK_PLACES,
        CENTRAL_BANK_PREFIX_BOOSTS,
        SOURCE_COUNTRY_PREFIXES,
    )

    assert set(CENTRAL_BANK_PLACES) == set(CENTRAL_BANK_PREFIX_BOOSTS.values())
    for prefix, place in CENTRAL_BANK_PLACES.items():
        assert SOURCE_COUNTRY_PREFIXES.get(prefix, place) == place, prefix


@pytest.mark.parametrize("query", [
    "current account",
    "current account balance",
    "current account deficit",
    "current account surplus",
])
def test_a_current_account_of_no_country_ranks_the_country_profile_first(catalog, query: str) -> None:
    """The country profile holds the current account to GDP of any country:
    "current account" ranked the Swiss National Bank's first and the profile
    38th."""
    results = search_catalog(catalog, query, limit=200)
    assert results[0]["operation_id"] == "macro_country_profile", [r["operation_id"] for r in results[:5]]
    assert "pattern:any-country->param" in results[0]["why"], results[0]["why"]
    ranks = {r["operation_id"]: i for i, r in enumerate(results)}
    assert "clamped-below:any-country-answers" in results[ranks["snb_current_account"]]["why"]


@pytest.mark.parametrize(("query", "first"), [
    ("Swiss current account", "snb_current_account"),
    ("euro area current account", "ecb_balance_of_payments"),
])
def test_a_current_account_of_a_named_place_keeps_its_source(catalog, query: str, first: str) -> None:
    results = search_catalog(catalog, query, limit=5)
    assert results[0]["operation_id"] == first, [r["operation_id"] for r in results]
    assert not _ANY_COUNTRY_NOTES & set(results[0]["why"]), results[0]["why"]


@pytest.mark.parametrize("query", [
    "bond yields",
    "government bond yield",
    "government bond yields",
    "sovereign bond yields",
    "bond yield",
    "10 year bond yield",
])
def test_a_bond_yield_of_no_country_ranks_the_country_profile_first(catalog, query: str) -> None:
    """The country profile holds the 10-year yield of any country: "government
    bond yields" ranked the Reserve Bank of Australia's first and the profile
    198th."""
    results = search_catalog(catalog, query, limit=200)
    assert results[0]["operation_id"] == "macro_country_profile", [r["operation_id"] for r in results[:5]]
    assert "pattern:any-country->param" in results[0]["why"], results[0]["why"]
    ranks = {r["operation_id"]: i for i, r in enumerate(results)}
    assert "clamped-below:any-country-answers" in results[ranks["rba_bond_yields"]]["why"]


@pytest.mark.parametrize("query", ["10-year yield", "10 year yield", "10y yield"])
def test_a_10_year_yield_of_no_country_ranks_the_country_profile_first(catalog, query: str) -> None:
    """A 10-year yield names no bond, so no national bond source holds a strong
    word of it to clamp; the profile, which names its "10Y yield", answers it."""
    results = search_catalog(catalog, query, limit=5)
    assert results[0]["operation_id"] == "macro_country_profile", [r["operation_id"] for r in results]
    assert "pattern:any-country->param" in results[0]["why"], results[0]["why"]


def test_a_bond_yield_of_no_country_clamps_the_euro_area_curve(catalog) -> None:
    """The ECB answers for the euro area alone, one place the question never
    named: "bond yield" ranked the euro area yield curve first and the
    profile second."""
    results = search_catalog(catalog, "bond yield", limit=5)
    ids = [r["operation_id"] for r in results]
    assert ids[:2] == ["macro_country_profile", "ecb_yield_curve"], ids
    assert "clamped-below:any-country-answers" in results[1]["why"], results[1]["why"]


@pytest.mark.parametrize(("query", "first"), [
    ("Australia bond yields", "rba_bond_yields"),
    ("euro area yield curve", "ecb_yield_curve"),
    ("euro area bond yields", "ecb_yield_curve"),
    ("ECB bond yields", "ecb_yield_curve"),
])
def test_a_bond_yield_of_a_named_place_keeps_its_source(catalog, query: str, first: str) -> None:
    results = search_catalog(catalog, query, limit=5)
    assert results[0]["operation_id"] == first, [r["operation_id"] for r in results]
    assert not _ANY_COUNTRY_NOTES & set(results[0]["why"]), results[0]["why"]


@pytest.mark.parametrize("query", [
    "Germany current account",
    "India current account",
    "UK current account",
])
def test_a_current_account_of_a_country_without_its_own_source_ranks_the_profile_first(
    catalog, query: str,
) -> None:
    """The words of "current account" score only where the statistic is
    spelled: "Germany current account" ranked the current air quality first,
    on the word "current", and the country profile fourth."""
    results = search_catalog(catalog, query, limit=5)
    ids = [r["operation_id"] for r in results]
    assert ids[0] == "macro_country_profile", ids
    assert "air_quality_current" not in ids, ids


@pytest.mark.parametrize("query", [
    "trade balance",
    "trade balance by country",
    "goods trade balance",
])
def test_a_trade_balance_of_no_country_ranks_the_imf_direction_of_trade_first(
    catalog, query: str,
) -> None:
    """The IMF Direction of Trade serves the trade balance of any reporter
    country: "trade balance" ranked the US Census source first."""
    results = search_catalog(catalog, query, limit=5)
    ids = [r["operation_id"] for r in results]
    assert ids[0] == "imf_direction_of_trade", ids
    assert ids.index("census_trade_balance") > 0, ids


def test_a_us_trade_balance_keeps_the_census_source_above_the_imf(catalog) -> None:
    """A named place turns the any-country reading off: the US source stays
    above the source of every country."""
    ids = [r["operation_id"] for r in search_catalog(catalog, "US trade balance", limit=5)]
    assert ids.index("census_trade_balance") < ids.index("imf_direction_of_trade"), ids


def test_an_operation_that_answers_through_the_statistics_alias_takes_the_country(catalog) -> None:
    """The country profile answers "unemployment" with its jobless rate, a word
    of the statistic's alias. Counted by its own words alone, it would lose the
    boost, and the ILO gender gap would take first place."""
    results = search_catalog(catalog, "unemployment", limit=10)
    assert results[0]["operation_id"] == "ilostat_unemployment", [r["operation_id"] for r in results]
    profile = next(r for r in results if r["operation_id"] == "macro_country_profile")
    assert "pattern:any-country->param" in profile["why"], profile["why"]


def test_an_operation_in_neither_group_keeps_its_rank(catalog) -> None:
    """The Penn World Table takes the country as an ISO3 code, so it neither
    earns the boost nor is a national source: it keeps its place above the
    national GDP sources."""
    results = search_catalog(catalog, "GDP", limit=200)
    ranks = {r["operation_id"]: i for i, r in enumerate(results)}
    pwt = ranks["research_pwt_country_iso3"]
    for operation in ("ine_gdp", "ons_gdp", "stat_finland_gdp"):
        assert ranks.get(operation, len(ranks)) > pwt, (operation, ranks.get(operation), pwt)


def test_a_two_letter_word_keeps_no_national_source_in_place(catalog) -> None:
    """Denmark's EU-harmonised price index names the EU in its summary; two
    letters are too short to be a word that only it answers."""
    rows = search_catalog(catalog, "EU inflation", limit=100)
    ranks = {row["operation_id"]: i for i, row in enumerate(rows)}
    answers = [ranks[row["operation_id"]] for row in rows
               if "pattern:any-country->param" in row["why"]]
    assert answers
    assert ranks["statistical_agencies_statbank_dk_hicp"] > max(answers), ranks


def test_an_equal_score_never_ranks_a_national_source_first() -> None:
    """Equal scores fell back on operation_id order, so a national source
    could still rank above an operation that takes the country. An operation
    in neither group keeps its slot between them."""
    from sugra_api_mcp.catalog.models import Catalog, Endpoint, EndpointParameter

    country = EndpointParameter(name="country", location="query")
    # The country boost stands in for the path and the description: a tie.
    takes = Endpoint(operation_id="world_widget", method="GET", path="/x",
                     summary="Inflation", parameters=[country])
    national = Endpoint(operation_id="ine_widget", method="GET", path="/inflation",
                        summary="Inflation", description="Inflation")
    neither = Endpoint(operation_id="jj_widget", method="GET", path="/inflation",
                       summary="Inflation", description="Inflation")
    tied = Catalog(source="test", endpoints=[national, neither, takes])
    results = search_catalog(tied, "inflation", limit=5)
    assert len({r["score"] for r in results}) == 1, results
    assert [r["operation_id"] for r in results] == ["world_widget", "jj_widget", "ine_widget"]
    assert "lifted-above:national-sources" in results[0]["why"], results[0]["why"]
    assert "clamped-below:any-country-answers" in results[2]["why"], results[2]["why"]


@pytest.mark.parametrize(("texts", "bases"), [
    (["Credit-to-GDP gaps"], {"gdp"}),
    (["Government debt to GDP"], {"gdp"}),
    (["credit_to_gdp"], {"gdp"}),
    (["Debt as percent of GDP"], {"gdp"}),
    (["Debt as per cent of GDP"], {"gdp"}),
    (["Debt (% of GDP)"], {"gdp"}),
    (["Exports as a share of GDP"], {"gdp"}),
    (["GDP and debt to GDP"], set()),  # named on its own as well
    (["Debt to GDP", "Quarterly GDP"], set()),  # in another field
    (["Credit to", "GDP"], set()),  # a ratio never spans two fields
    (["Converted into GDP terms"], set()),  # "into" is no ratio
    (["Number of years of CPI"], set()),  # "of" alone is no ratio
    (["Price to earnings"], set()),  # no country statistic
])
def test_a_statistic_named_only_as_the_base_of_a_ratio(texts: list[str], bases: set[str]) -> None:
    from sugra_api_mcp.catalog.search import _ratio_base_statistics

    assert _ratio_base_statistics(texts) == bases


@pytest.mark.parametrize("query", ["GDP", "real GDP", "GDP forecast", "gdp per capita"])
def test_a_ratio_to_gdp_answers_no_question_about_gdp(catalog, query: str) -> None:
    """The credit-to-GDP gap ranked first for "GDP": it measures credit
    against GDP and names GDP only as the base of the ratio."""
    results = search_catalog(catalog, query, limit=200)
    assert "bis_credit_gap" not in [r["operation_id"] for r in results[:5]], results[:5]
    gap = next(r for r in results if r["operation_id"] == "bis_credit_gap")
    assert "pattern:any-country->param" not in gap["why"], gap["why"]


@pytest.mark.parametrize("query", ["credit to GDP gap", "credit-to-GDP gap"])
def test_a_question_that_names_the_ratio_still_finds_it(catalog, query: str) -> None:
    """A question that names GDP only as the base of a ratio asks for the
    ratio, and the operation that names it so answers it."""
    top = search_catalog(catalog, query, limit=1)[0]["operation_id"]
    assert top == "bis_credit_gap", top


@pytest.mark.parametrize(("text", "numerators"), [
    ("government debt to GDP", {"gdp": {"government", "debt"}}),
    ("debt-to-GDP", {"gdp": {"debt"}}),
    ("debt % of GDP", {"gdp": {"debt"}}),
    ("debt as a percent of GDP", {"gdp": {"debt"}}),
    ("budget deficit as a share of GDP", {"gdp": {"budget", "deficit"}}),
    ("debt ratio to GDP", {"gdp": {"debt"}}),
    ("credit to GDP gap", {"gdp": {"credit"}}),
    ("credit to GDP and debt to GDP", {"gdp": {"credit", "debt"}}),
    # Each base reads only the words back to the ratio before it.
    ("credit to inflation and government debt to GDP",
     {"inflation": {"credit"}, "gdp": {"government", "debt"}}),
    ("debt to GDP and ratio to GDP", {"gdp": {"debt"}}),
    ("ratio to GDP", {}),  # nothing before the ratio that measures
    ("share of GDP", {}),
    # Function words, request verbs, numbers and the words that say when,
    # where or how much measure nothing.
    ("what is the ratio to GDP", {}),
    ("show me the share of GDP", {}),
    ("what's the share of GDP", {}),
    ("whats the ratio to GDP", {}),
    ("get the ratio to GDP", {}),
    ("2024 ratio to GDP", {}),
    ("annual ratio to GDP", {}),
    ("quarterly share of GDP", {}),
    ("which country has the highest ratio to GDP", {}),
    ("which country has the highest debt to GDP", {"gdp": {"debt"}}),
    # Every phrasing of the numerator reads it.
    ("government debt 80% of GDP", {"gdp": {"government", "debt"}}),
    ("government debt is 80% of GDP", {"gdp": {"government", "debt"}}),
    ("debt 2024 ratio to GDP", {"gdp": {"debt"}}),
    ("debt and deficit to GDP", {"gdp": {"debt", "deficit"}}),
    ("debt and budget deficit to GDP", {"gdp": {"debt", "budget", "deficit"}}),
    ("exports or imports as a share of GDP", {"gdp": {"exports", "imports"}}),
    ("debt, deficit and spending to GDP", {"gdp": {"debt", "deficit", "spending"}}),
    ("in 2020, debt to GDP", {"gdp": {"debt"}}),
    ("GDP and debt to GDP", {"gdp": {"debt"}}),  # a country statistic is none
    ("M2 to GDP", {"gdp": {"m2"}}),
    ("inflation to GDP", {}),  # a country statistic is no numerator
    ("price to earnings", {}),  # no country statistic as the base
    ("GDP", {}),
])
def test_the_numerator_of_a_ratio(text: str, numerators: dict[str, set[str]]) -> None:
    from sugra_api_mcp.catalog.search import _ratio_numerators

    assert _ratio_numerators(text) == numerators


@pytest.mark.parametrize(("numerator", "token"), [
    ("taxes", "tax"), ("tax", "taxes"), ("liabilities", "liability"), ("liability", "liabilities"),
    ("debts", "debt"), ("debt", "debts"), ("expenses", "expense"), ("expense", "expenses"),
    ("gases", "gas"), ("gas", "gases"), ("assets", "asset"), ("loss", "loss"),
])
def test_a_numerator_matches_its_singular_and_its_plural(numerator: str, token: str) -> None:
    from sugra_api_mcp.catalog.search import _names_any

    assert _names_any(frozenset({numerator}), frozenset({token}))


@pytest.mark.parametrize(("numerator", "token"), [
    ("rates", "rat"), ("loss", "los"), ("gas", "ga"), ("debt", "credit"),
])
def test_a_numerator_matches_no_other_word(numerator: str, token: str) -> None:
    from sugra_api_mcp.catalog.search import _names_any

    assert not _names_any(frozenset({numerator}), frozenset({token}))


@pytest.mark.parametrize("query", [
    "government debt to GDP",
    "debt % of GDP",
    "debt as a percent of GDP",
    "debt-to-GDP",
    "debts to GDP",
    "household debt to GDP",
    "deficit to GDP",
    "budget deficit as a percent of GDP",
    "exports to GDP",
    "tax revenue as a percent of GDP",
    "debt to gross domestic product",
    "government debt 80% of GDP",
    "government debt is 80% of GDP",
    "debt and deficit to GDP",
    "which country has the highest debt to GDP",
])
def test_a_ratio_with_another_numerator_answers_no_ratio_question(catalog, query: str) -> None:
    """The credit-to-GDP gap ranked first for "government debt to GDP": it
    names GDP as the base of a ratio, but of credit, never of debt."""
    results = search_catalog(catalog, query, limit=200)
    assert results[0]["operation_id"] != "bis_credit_gap", results[:3]
    gap = next(r for r in results if r["operation_id"] == "bis_credit_gap")
    assert "pattern:any-country->param" not in gap["why"], gap["why"]


@pytest.mark.parametrize("query", [
    "credit-to-GDP",
    "private credit to GDP",
    "credit to GDP ratio",
    "bank credit as a percent of GDP",
    "ratio to GDP",  # names no numerator
    "what is the ratio to GDP",
    "2024 ratio to GDP",
])
def test_a_ratio_question_with_its_own_numerator_finds_the_ratio(catalog, query: str) -> None:
    top = search_catalog(catalog, query, limit=1)[0]["operation_id"]
    assert top == "bis_credit_gap", top


@pytest.mark.parametrize(("summary", "query", "answers"), [
    ("Government debt to GDP", "government debt to GDP", True),
    ("Government debts to GDP", "government debt to GDP", True),
    ("Credit-to-GDP gaps", "government debt to GDP", False),
    # The plural of either side: "taxes" and "tax", "liabilities" and "liability".
    ("Tax revenue to GDP", "taxes to GDP", True),
    ("Taxes to GDP", "tax to GDP", True),
    ("External liabilities to GDP", "external liability to GDP", True),
    ("Government expense to GDP", "government expenses to GDP", True),
    # A number between the numerator and its base hides neither.
    ("Credit-to-GDP gaps", "government debt 80% of GDP", False),
    ("Government debt to GDP", "government debt 80% of GDP", True),
    # Either of two numerators joined by "and" answers.
    ("Government debt to GDP", "debt and deficit to GDP", True),
    ("Credit-to-GDP gaps", "debt and deficit to GDP", False),
    ("Government debt to GDP", "debt and budget deficit to GDP", True),
    ("Government debt to GDP", "debt or private credit to GDP", True),
    ("Credit-to-GDP gaps", "government debt is 80% of GDP", False),
    ("Government debt to GDP", "government debt is 80% of GDP", True),
    # A numerator answers only its own base: credit is measured against
    # inflation here, and GDP against debt.
    ("Credit-to-GDP gaps", "credit to inflation and government debt to GDP", False),
    ("Government debt to GDP", "credit to inflation and government debt to GDP", True),
])
def test_an_operation_that_names_the_numerator_answers_the_ratio(
    summary: str, query: str, answers: bool,
) -> None:
    from sugra_api_mcp.catalog.models import Catalog, Endpoint, EndpointParameter

    country = EndpointParameter(name="country", location="query")
    ratio = Endpoint(operation_id="world_ratio", method="GET", path="/ratio",
                     summary=summary, parameters=[country])
    results = search_catalog(Catalog(source="test", endpoints=[ratio]), query, limit=1)
    assert ("pattern:any-country->param" in results[0]["why"]) is answers, results[0]["why"]


@pytest.mark.parametrize("query,places,words", [
    ("yen inflation", {"JP"}, {"yen"}),
    ("JPY inflation", {"JP"}, {"jpy"}),
    ("dollars inflation", {"US"}, {"dollars"}),
    ("Mexican peso inflation", {"MX"}, {"mexican", "peso"}),
    ("GBP CPI", {"GB"}, {"gbp"}),
    ("yuan GDP", {"CN"}, {"yuan"}),
    # The statistic in other words.
    ("yen consumer prices", {"JP"}, {"yen"}),
    ("dollar gross domestic product", {"US"}, {"dollar"}),
    ("pound jobless rate", {"GB"}, {"pound"}),
    ("yen consumer confidence", set(), set()),
    # A unit, a market, a weight or a word of several currencies: no place.
    ("GDP in dollars", set(), set()),
    ("coffee price in dollars and inflation", set(), set()),
    ("yen and inflation", set(), set()),
    ("dollar exchange rate and inflation", set(), set()),
    ("pound of coffee inflation", set(), set()),
    ("peso inflation", set(), set()),
    ("euro area inflation", set(), set()),
])
def test_a_currency_right_before_a_country_statistic_names_its_issuer(
    query: str, places: set[str], words: set[str],
) -> None:
    from sugra_api_mcp.catalog.aliases import currency_statistic_places

    assert currency_statistic_places(query) == (frozenset(places), frozenset(words))


@pytest.mark.parametrize("query,key", [
    ("yen inflation", "jp/cpi"),
    ("JPY inflation", "jp/cpi"),
    ("euro inflation", "eu/cpi"),
    ("dollars inflation", "us/cpi"),
    ("pound inflation", "gb/cpi"),
    ("GBP CPI", "gb/cpi"),
    ("euro unemployment", "eu/unrate"),
    ("yen consumer prices", "jp/cpi"),
    ("euro jobless rate", "eu/unrate"),
    ("dollar gross domestic product", "us/gdp"),
])
def test_a_currency_before_a_statistic_asks_about_its_country(catalog, query: str, key: str) -> None:
    """"yen inflation" ranked the composite country profile first, for any
    country, and not the inflation of Japan."""
    top = search_catalog(catalog, query, limit=1)[0]
    assert key in [series["key"] for series in top.get("macro_keys") or []], top


@pytest.mark.parametrize("query", ["euro unemployment", "euro GDP"])
def test_the_word_that_names_the_currency_is_no_topic(catalog, query: str) -> None:
    """The euro area's balance of payments names the euro and answers
    neither statistic: the word names the place, as "Germany" does in
    "Germany unemployment", and takes no boost for the country."""
    row = next(r for r in search_catalog(catalog, query, limit=2000)
               if r["operation_id"] == "ecb_balance_of_payments")
    assert "pattern:country->param" not in row["why"], row["why"]


@pytest.mark.parametrize(("query", "place"), [
    ("yen inflation", "JP"),
    ("franc inflation", "CH"),
    ("pound inflation", "GB"),
    ("dollar inflation", "US"),
])
def test_a_currency_before_a_statistic_lists_no_other_country(catalog, query: str, place: str) -> None:
    from sugra_api_mcp.catalog.search import _source_country

    for row in search_catalog(catalog, query, limit=3):
        country = _source_country(catalog.get(row["operation_id"]))
        assert country in (None, place), (query, row["operation_id"], country)


@pytest.mark.parametrize(("query", "key"), [
    ("EU inflation", "eu/cpi"),
    ("euro area inflation", "eu/cpi"),
    ("eurozone inflation", "eu/cpi"),
    ("European Union inflation", "eu/cpi"),
    ("EU unemployment", "eu/unrate"),
])
def test_the_euro_area_lists_no_other_country(catalog, query: str, key: str) -> None:
    """"EU inflation" listed the inflation of Argentina and US TIPS among its
    first answers: the euro area is the place of no national source."""
    from sugra_api_mcp.catalog.search import _source_country

    rows = search_catalog(catalog, query, limit=10)
    assert key in [series["key"] for series in rows[0].get("macro_keys") or []], rows[0]
    if key == "eu/cpi":
        for row in rows:
            country = _source_country(catalog.get(row["operation_id"]))
            assert country is None, (query, row["operation_id"], country)


@pytest.mark.parametrize(("query", "named"), [
    ("EU inflation", True), ("euro zone GDP", True), ("Germany vs EU inflation", True),
    ("euro inflation", False), ("European stocks", False), ("Europe inflation", False),
])
def test_the_euro_area_is_named_in_its_own_words(query: str, named: bool) -> None:
    from sugra_api_mcp.catalog.macro_keys import query_names_euro_area

    assert query_names_euro_area(query) is named


@pytest.mark.parametrize("query", ["EU inflation", "eurozone inflation"])
def test_the_euro_area_keeps_the_operations_that_take_a_country(catalog, query: str) -> None:
    """The euro area is no country those operations boost for, so they keep
    the any-country reading right below the euro area's own series."""
    rows = search_catalog(catalog, query, limit=3)
    assert {row["operation_id"] for row in rows[1:]} == {
        "macro_country_profile", "worldbank_country_overview"}, rows
    assert all("pattern:any-country->param" in row["why"] for row in rows[1:])


def test_a_country_beside_the_euro_area_keeps_its_own_sources(catalog) -> None:
    rows = search_catalog(catalog, "Germany vs EU inflation", limit=10)
    german = [row for row in rows if row["operation_id"].startswith("destatis_")]
    assert german, [row["operation_id"] for row in rows]
    assert not any("clamped-below:country-answers" in row["why"] for row in german)


@pytest.mark.parametrize("query", [
    "GDP in dollars",  # a unit
    "inflation in dollars",
    "coffee price in dollars and inflation",
    "yen and inflation",  # a market beside the statistic
])
def test_a_currency_elsewhere_in_the_question_names_no_place(catalog, query: str) -> None:
    rows = search_catalog(catalog, query, limit=2000)
    assert not [row["operation_id"] for row in rows if "pattern:country->param" in row["why"]]
    assert any("pattern:any-country->param" in row["why"] for row in rows)


def test_a_named_country_or_a_listing_wins_over_a_currency(catalog) -> None:
    """A currency names its country only where nothing else names a place:
    Turkey's inflation in euros, the chip maker's price."""
    for row in search_catalog(catalog, "euro inflation in Turkey", limit=5):
        assert not [series for series in row.get("macro_keys") or []
                    if series["key"].startswith("eu/")], row
    for row in search_catalog(catalog, "AMD inflation", limit=2000):
        assert "pattern:country->param" not in row["why"], row


@pytest.mark.parametrize("query,ticker", [
    ("TLT inflation", "TLT"),
    ("TLT since 2020", "TLT"),
    ("IEF yield", "IEF"),
    ("XLK flows", "XLK"),
])
def test_a_bond_or_sector_fund_is_a_listing_without_an_equity_word(catalog, query: str, ticker: str) -> None:
    """"TLT inflation" asks about the bond fund: it took the any-country
    reading and ranked the composite country profile first."""
    assert detect_tickers(query) == [ticker]
    top = search_catalog(catalog, query, limit=1)[0]
    assert {"pattern:ticker->quotes_symbol", "pattern:ticker->etf_symbol"} & set(top["why"]), top


@pytest.mark.parametrize("query,first", [
    ("SPY flows", "etf_symbol_flows"),
    ("XLK flows", "etf_symbol_flows"),
    ("TLT flows", "etf_symbol_flows"),
    ("SPY snapshot", "etf_symbol_snapshot"),
    ("QQQ quote", "etf_symbol_quote_cboe"),
    ("XLF sector weightings", "etf_symbol_sector_weightings_history"),
    ("SPY top holdings changes", "etf_symbol_top_holdings_changes"),
    ("how did SPY's top holdings change", "etf_symbol_top_holdings_changes"),
])
def test_an_etf_ticker_reaches_the_etf_operations(catalog, query: str, first: str) -> None:
    """"SPY flows" ranked a company cash flow statement first and the ETF's
    own flows below the top 60: the ticker boosted only the quotes."""
    top = search_catalog(catalog, query, limit=1)[0]
    assert top["operation_id"] == first, top
    assert "pattern:ticker->etf_symbol" in top["why"], top


@pytest.mark.parametrize("query", [
    "SPY holdings", "SPY top holdings", "Show me VOO's NAV, AUM and top holdings",
])
def test_holdings_are_no_change_in_them(catalog, query: str) -> None:
    """The changes to an ETF's top holdings answer "top" and "holdings" only
    beside a word for change: VOO's top holdings ranked the churn between two
    dates first."""
    rows = search_catalog(catalog, query, limit=2000)
    assert rows[0]["operation_id"] == "etf_symbol_holdings_sec", rows[0]
    changes = next(row for row in rows if row["operation_id"] == "etf_symbol_top_holdings_changes")
    assert "pattern:ticker->etf_symbol" not in changes["why"], changes


@pytest.mark.parametrize("query", ["SPY changes", "SPY churn", "what changed in SPY"])
def test_a_change_is_no_holdings_change(catalog, query: str) -> None:
    """And a word for change answers it only beside "top" or "holdings": "SPY
    changes" says nothing about holdings."""
    rows = search_catalog(catalog, query, limit=2000)
    assert rows[0]["operation_id"] != "etf_symbol_top_holdings_changes", rows[0]
    changes = next(row for row in rows if row["operation_id"] == "etf_symbol_top_holdings_changes")
    assert "pattern:ticker->etf_symbol" not in changes["why"], changes


@pytest.mark.parametrize("query,first", [
    # The topic word picks the quotes.
    ("SPY price", "quotes_symbol_price"),
    ("SPY cash flow", "quotes_symbol_periodicity_cash_flow"),
    ("SPY dividends", "quotes_symbol_actions"),
    ("AAPL cash flow", "quotes_symbol_periodicity_cash_flow"),
])
def test_an_etf_ticker_keeps_the_quotes_its_topic_names(catalog, query: str, first: str) -> None:
    assert search_catalog(catalog, query, limit=1)[0]["operation_id"] == first


@pytest.mark.parametrize("query", ["TLT", "SPY today", "AAPL flows", "AAPL holdings", "NVDA snapshot"])
def test_an_etf_boost_needs_an_etf_ticker_and_a_topic_word(catalog, query: str) -> None:
    """A stock ticker never lifts the ETF operations, and an ETF ticker
    alone, or beside a word only an ETF operation's description holds
    ("today"), lifts none of them and keeps the quotes first."""
    rows = search_catalog(catalog, query, limit=2000)
    assert rows[0]["operation_id"].startswith("quotes_symbol_"), rows[0]
    assert not [row for row in rows if "pattern:ticker->etf_symbol" in row["why"]]


@pytest.mark.parametrize("query", ["SPY flows crypto", "SPY peering traceroute flows"])
def test_crypto_or_network_context_suppresses_the_etf_boost(catalog, query: str) -> None:
    """The ETF boost takes the ticker gate the quotes take, which crypto
    context and network dominance switch off."""
    assert ETF_TICKERS.intersection(detect_tickers(query))
    rows = search_catalog(catalog, query, limit=2000)
    assert not [row for row in rows if any(
        reason in row["why"] for reason in ("pattern:ticker->etf_symbol", "pattern:ticker->quotes_symbol"))]


@pytest.mark.parametrize("query", ["PCE inflation", "HICP inflation", "TIPS inflation", "CPI inflation"])
def test_a_statistic_acronym_is_no_listing(query: str) -> None:
    assert detect_tickers(query) == []


@pytest.mark.parametrize("query", [
    "TLT since 2020", "SPY since 2020", "AAPL over the last five years",
    "How has QQQ done over the past decade?", "AAPL in 2008",
    # Level with a description-only word ("aapl", "month"): the tie default.
    "AAPL over the last month",
])
def test_a_listing_and_a_period_ask_for_the_price_history(catalog, query: str) -> None:
    """"TLT since 2020" ranked the dividends and splits first: the ticker
    scores the listing operations equally and their names broke the tie."""
    top = search_catalog(catalog, query, limit=1)[0]
    assert top["operation_id"] == "quotes_symbol_historical", top
    assert "pattern:period->history" in top["why"], top


@pytest.mark.parametrize("query,named", [
    ("TLT since 2020", True), ("AAPL past week", True), ("SPY a month ago", True),
    ("AAPL over the last month", True), ("TLT over the decades", True),
    ("IWM Russell 2000", False), ("AAPL 2020", False), ("AAPL since open", False),
    ("AAPL 15 minutes ago", False), ("AAPL last close", False),
])
def test_a_period_is_days_or_longer(query: str, named: bool) -> None:
    assert query_names_a_period(query) is named


@pytest.mark.parametrize("query,first", [
    ("AAPL dividends since 2020", "quotes_symbol_actions"),
    ("NVDA earnings since 2020", "earnings"),
])
def test_a_period_leaves_the_operation_a_topic_word_names(catalog, query: str, first: str) -> None:
    assert search_catalog(catalog, query, limit=1)[0]["operation_id"] == first


@pytest.mark.parametrize("query", ["TLT", "XLE oil", "XLF banks", "PLTR today"])
def test_a_listing_no_word_narrows_asks_for_its_price(catalog, query: str) -> None:
    rows = search_catalog(catalog, query, limit=2)
    assert rows[0]["operation_id"] == "quotes_symbol_price", rows[0]
    assert rows[0]["score"] == rows[1]["score"], rows
    assert "pattern:period->history" not in rows[0]["why"], rows[0]


@pytest.mark.parametrize("query", [
    "inflation since 2020", "GDP of France since 2010",
    # No period of days or longer, or a listing named like a period word.
    "AAPL since open", "AAPL 15 minutes ago", "AAPL versus AGO", "AAPL dividend history",
    # A number in a name, and several listings with a period.
    "IWM Russell 2000", "TLT SPY since 2020",
])
def test_no_listing_period_lifts_no_price_history(catalog, query: str) -> None:
    rows = search_catalog(catalog, query, limit=2000)
    assert not any("pattern:period->history" in row["why"] for row in rows)


@pytest.mark.parametrize("query", ["TLT SPY", "TLT SPY since 2020"])
def test_several_listings_ask_for_their_prices(catalog, query: str) -> None:
    """No description names TLT or SPY: the tie order alone puts the
    several-listings operation first."""
    assert len(detect_tickers(query)) == 2
    rows = search_catalog(catalog, query, limit=2)
    assert rows[0]["operation_id"] == "quotes_symbol_multiple", rows[0]
    assert rows[0]["score"] == rows[1]["score"], rows


def test_several_listings_one_described_ask_for_their_prices(catalog) -> None:
    assert len(detect_tickers("AAPL vs MSFT")) == 2
    top = search_catalog(catalog, "AAPL vs MSFT", limit=1)[0]
    assert top["operation_id"] == "quotes_symbol_multiple", top


# ---- A statistic named in other words ------------------------------------------------

@pytest.mark.parametrize("query,spelled", [
    ("consumer prices", {"cpi": {"consumer", "prices"}}),
    ("consumer-price index", {"cpi": {"consumer", "price"}}),
    ("gross domestic products", {"gdp": {"gross", "domestic", "products"}}),
    ("jobless rate", {"unemployment": {"jobless"}}),
    ("consumer prices and the jobless rate",
     {"cpi": {"consumer", "prices"}, "unemployment": {"jobless"}}),
    # Named in its own word as well: the other words still name it.
    ("unemployment and jobless claims", {"unemployment": {"jobless"}}),
    ("current accounts", {"current account": {"current", "accounts"}}),
    ("government bond yields", {"bond yield": {"bond", "yields"}}),
    ("10Y yield", {"bond yield": {"10y", "yield"}}),
    ("10-year yield", {"bond yield": {"10", "year", "yield"}}),
    ("trade balances", {"trade balance": {"trade", "balances"}}),
    # Other statistics, part of a spelling, another word: none.
    ("consumer confidence", {}),
    ("producer prices", {}),
    ("domestic product", {}),
    ("joblessness", {}),
    ("account", {}),
    ("current price", {}),
    ("bond", {}),
    ("dividend yield", {}),
    ("CPI", {}),
])
def test_a_statistic_named_in_other_words(query: str, spelled: dict[str, set[str]]) -> None:
    from sugra_api_mcp.catalog.aliases import spelled_country_statistics

    assert spelled_country_statistics(query) == {
        statistic: frozenset(words) for statistic, words in spelled.items()
    }


@pytest.mark.parametrize("query,rewritten", [
    ("debt to gross domestic product", "debt to gdp"),
    ("Debt as percent of Gross-Domestic-Products", "debt as percent of gdp"),
    ("consumer price index", "cpi index"),
    ("jobless claims", "unemployment claims"),
    ("domestic product", "domestic product"),
    ("consumer pricing", "consumer pricing"),
    ("superjobless rate", "superjobless rate"),
])
def test_a_statistic_in_other_words_is_written_as_its_word(query: str, rewritten: str) -> None:
    from sugra_api_mcp.catalog.aliases import with_statistic_words

    assert with_statistic_words(query) == rewritten


@pytest.mark.parametrize("query,word", [
    ("consumer prices", "CPI"),
    ("consumer price index", "CPI"),
    ("gross domestic product", "GDP"),
    ("jobless rate", "unemployment rate"),
    ("jobless claims", "unemployment claims"),
    ("jobless rate and inflation", "unemployment rate and inflation"),
    # The base of a ratio, in either spelling.
    ("debt to gross domestic product", "debt to GDP"),
    ("government debt as percent of gross domestic product", "government debt as percent of GDP"),
    ("credit to gross domestic product gap", "credit to GDP gap"),
    # Both spellings in one question.
    ("gross domestic product and GDP growth", "GDP growth"),
])
def test_a_statistic_in_other_words_ranks_as_its_own_word(catalog, query: str, word: str) -> None:
    """"consumer prices" ranked Spain's price index first and "jobless rate"
    Finland's unemployment: the question names no place, as "CPI" and
    "unemployment rate" name none."""
    spelled = search_catalog(catalog, query, limit=1)[0]["operation_id"]
    literal = search_catalog(catalog, word, limit=1)[0]["operation_id"]
    assert spelled == literal, (spelled, literal)


@pytest.mark.parametrize("query", [
    "consumer prices", "consumer price index", "gross domestic product", "jobless rate",
    "jobless claims", "GDP and gross domestic product per capita",
])
def test_a_statistic_in_other_words_asks_for_any_country(catalog, query: str) -> None:
    """The question names no place: the operations that take the country
    answer it, and no national source ranks among the first three."""
    from sugra_api_mcp.catalog.search import _source_country

    rows = search_catalog(catalog, query, limit=2000)
    assert any("pattern:any-country->param" in row["why"] for row in rows), query
    for row in rows[:3]:
        assert _source_country(catalog.get(row["operation_id"])) is None, (query, row["operation_id"])


def test_a_national_source_that_names_the_statistic_in_other_words_answers_it() -> None:
    """A national source that writes the CPI as consumer prices answers
    "consumer prices" as one that writes CPI does, so it gives its slot to
    the operation that takes the country."""
    from sugra_api_mcp.catalog.models import Catalog, Endpoint, EndpointParameter

    country = EndpointParameter(name="country", location="query")
    takes = Endpoint(operation_id="world_widget", method="GET", path="/x",
                     summary="CPI", parameters=[country])
    national = Endpoint(operation_id="ine_widget", method="GET", path="/consumer-prices",
                        summary="Consumer prices", description="Consumer prices")
    tiny = Catalog(source="test", endpoints=[national, takes])
    results = search_catalog(tiny, "consumer prices", limit=5)
    assert [r["operation_id"] for r in results] == ["world_widget", "ine_widget"], results
    assert "clamped-below:any-country-answers" in results[1]["why"], results[1]["why"]


@pytest.mark.parametrize("query", [
    "Spain CPI", "Spain consumer prices", "Spain consumer price index",
    "Spain GDP", "Spain gross domestic product",
])
def test_a_named_country_keeps_its_own_source_first_in_other_words(catalog, query: str) -> None:
    """A question that names a place is read in its own words: Spain's own
    source answers "Spain consumer prices" first, as it answers "Spain CPI"."""
    from sugra_api_mcp.catalog.search import _source_country

    top = search_catalog(catalog, query, limit=1)[0]["operation_id"]
    assert _source_country(catalog.get(top)) == "ES", (query, top)


@pytest.mark.parametrize("query", [
    "consumer confidence", "consumer spending", "consumer credit", "producer prices",
    "house price index", "domestic product", "national accounts",
])
def test_the_other_words_of_a_spelling_ask_for_no_country(catalog, query: str) -> None:
    for row in search_catalog(catalog, query, limit=2000):
        assert not _ANY_COUNTRY_NOTES & set(row["why"]), (row["operation_id"], row["why"])


# ---- Currency words, a country prefix, a singular strait ---------------------------

def test_every_currency_named_in_words_is_known_by_its_code() -> None:
    from sugra_api_mcp.catalog.aliases import _KNOWN_CURRENCIES, CURRENCY_NAMES

    named = set(CURRENCY_NAMES.values())
    assert named <= _KNOWN_CURRENCIES, sorted(named - _KNOWN_CURRENCIES)


def test_a_lowercase_code_word_is_a_known_currency_code() -> None:
    from sugra_api_mcp.catalog.aliases import _KNOWN_CURRENCIES, _LOWERCASE_CODE_WORDS

    assert {word.upper() for word in _LOWERCASE_CODE_WORDS} <= _KNOWN_CURRENCIES
    assert {"pen", "cop", "gel"} <= _LOWERCASE_CODE_WORDS


@pytest.mark.parametrize("query", [
    "price of a pen in dollars", "cop salary in dollars", "hair gel price in euros",
])
def test_a_lowercase_english_word_names_no_currency(query: str) -> None:
    from sugra_api_mcp.catalog.aliases import detect_fx_request

    assert detect_fx_request(query) is None


@pytest.mark.parametrize("query,currencies", [
    ("PEN to USD", ("PEN", "USD")),
    ("COP to USD", ("COP", "USD")),
    ("GEL to USD", ("GEL", "USD")),
])
def test_a_capital_code_still_names_its_currency(catalog, query: str, currencies: tuple[str, ...]) -> None:
    from sugra_api_mcp.catalog.aliases import detect_fx_request

    fx = detect_fx_request(query)
    assert fx is not None and (fx.currencies, fx.pair) == (currencies, True)
    assert search_catalog(catalog, query, limit=1)[0]["operation_id"] == "forex_convert"


@pytest.mark.parametrize("query,ticker", [("AMD stock price", "AMD"), ("COP stock price", "COP")])
def test_a_currency_code_that_is_also_a_listing_stays_a_ticker(query: str, ticker: str) -> None:
    assert detect_tickers(query) == [ticker]


def test_no_country_prefix_misses_the_operation_it_is_named_for(catalog) -> None:
    """A trailing underscore misses the operation the prefix names itself:
    "weather_us_forecast_" never stepped weather_us_forecast down."""
    from sugra_api_mcp.catalog.aliases import SOURCE_COUNTRY_PREFIXES

    ids = {endpoint.operation_id for endpoint in catalog.endpoints}
    missed = [prefix for prefix in SOURCE_COUNTRY_PREFIXES if prefix.endswith("_") and prefix[:-1] in ids]
    assert not missed, missed


def test_a_question_about_another_country_steps_the_us_forecast_down(catalog) -> None:
    from sugra_api_mcp.catalog.search import WRONG_COUNTRY_PENALTY, _score

    endpoint = catalog.get("weather_us_forecast")
    plain, _ = _score(endpoint, ["weather", "forecast"], {}, **_SCORE_FLAGS)
    stepped, _ = _score(endpoint, ["weather", "forecast"], {}, penalty_countries={"FR"}, **_SCORE_FLAGS)
    assert plain - stepped == WRONG_COUNTRY_PENALTY


@pytest.mark.parametrize("query", ["Turkish strait", "Turkish straits", "ships through the Turkish strait"])
def test_the_turkish_strait_is_named_in_the_singular_and_the_plural(query: str) -> None:
    from sugra_api_mcp.catalog.aliases import detect_named_operations

    assert detect_named_operations(query).operations == {"maritime_chokepoints_activity": "turkish strait"}


# ---- The US territories, and two names in one question ------------------------------
# The National Weather Service warns, forecasts and observes for Puerto Rico,
# Guam, the US Virgin Islands, American Samoa and the Northern Mariana Islands.
# Marked a national source of the United States alone, it was stepped down as
# another country's source there: a worldwide forecast, a network outage list
# and an air-quality forecast ranked first.

_NWS_ALERTS = {"weather_nws_alerts_active", "weather_us_alerts"}


@pytest.mark.parametrize("query,expected", [
    ("weather alerts puerto rico", _NWS_ALERTS),
    ("active weather alerts in puerto rico", _NWS_ALERTS),
    ("guam weather alerts", _NWS_ALERTS),
    ("nws alerts guam", _NWS_ALERTS),
    ("weather alerts us virgin islands", _NWS_ALERTS),
    ("american samoa weather alerts", _NWS_ALERTS),
    ("northern mariana islands weather alerts", _NWS_ALERTS),
    ("nws forecast puerto rico", {"weather_nws_forecast", "weather_nws_forecast_hourly"}),
    ("weather stations in puerto rico", {"weather_nws_stations"}),
    ("radar guam", {"weather_nws_radar_stations"}),
])
def test_the_weather_service_answers_for_the_us_territories(catalog, query: str, expected: set[str]) -> None:
    top = search_catalog(catalog, query, limit=1)[0]["operation_id"]
    assert top in expected, (query, top)


@pytest.mark.parametrize("query", ["weather alerts in mexico", "weather alerts venezuela"])
def test_the_weather_service_still_steps_down_for_another_country(catalog, query: str) -> None:
    top_3 = [r["operation_id"] for r in search_catalog(catalog, query, limit=3)]
    assert not any(op.startswith(("weather_nws_", "weather_us_")) for op in top_3), top_3


@pytest.mark.parametrize("query", ["Puerto Rico GDP", "Guam GDP", "American Samoa GDP", "American Samoan GDP"])
def test_a_territory_gdp_question_finds_no_weather_text_product(catalog, query: str) -> None:
    """The word "product" of gross domestic product matches the weather
    service's text product listing, which therefore serves no territory."""
    top = search_catalog(catalog, query, limit=1)[0]["operation_id"]
    assert not top.startswith("weather_"), top


def test_a_source_that_also_serves_a_place_is_no_foreign_source_there(catalog) -> None:
    from sugra_api_mcp.catalog.search import _is_foreign_source

    alerts = catalog.get("weather_nws_alerts_active")
    assert not _is_foreign_source(alerts, {"PR"})
    assert not _is_foreign_source(alerts, {"GU", "MX"})
    assert _is_foreign_source(alerts, {"MX"})
    assert _is_foreign_source(catalog.get("weather_nws_product_type_id_location_id_latest"), {"PR"})


def test_every_place_a_source_also_serves_belongs_to_a_live_national_source(catalog) -> None:
    """A prefix that matches no operation serves nothing, and an operation
    that is no national source has no country to add places to."""
    from sugra_api_mcp.catalog.aliases import SOURCE_ALSO_SERVES, SOURCE_COUNTRY_PREFIXES

    ids = [endpoint.operation_id for endpoint in catalog.endpoints]
    for prefix, places in SOURCE_ALSO_SERVES.items():
        matched = [op for op in ids if op.startswith(prefix)]
        assert matched, prefix
        for op in matched:
            countries = {c for p, c in SOURCE_COUNTRY_PREFIXES.items() if op.startswith(p)}
            assert countries and countries.isdisjoint(places), (prefix, op, countries)


def test_a_word_of_another_operations_name_is_silenced() -> None:
    from sugra_api_mcp.catalog.models import Endpoint
    from sugra_api_mcp.catalog.search import _score

    endpoint = Endpoint(operation_id="gas_hub_price", method="GET", path="/x",
                        summary="Hub gas price", toolset="markets")
    terms = ["oil", "price", "henry", "hub"]
    _, why = _score(endpoint, terms, {}, named_words=frozenset(terms),
                    own_named_words=frozenset({"henry", "hub"}),
                    named_operations={"gas_hub_price": "henry hub", "oil_spot": "oil price"},
                    **_SCORE_FLAGS)
    assert "summary:hub" in why and "summary:price" not in why, why


def _scored_words(why: list[str]) -> set[str]:
    return {reason.split(":", 1)[1] for reason in why if not reason.startswith("name:")}


@pytest.mark.parametrize("query,operation,other_name", [
    ("oil price and henry hub", "commodities_energy_natural_gas", {"oil", "price"}),
    ("oil price and henry hub", _OIL, {"henry", "hub"}),
    ("ttf and brent", "commodities_commodity_id", {"brent"}),
    ("trucking freight rates and oil price", _TRUCKING, {"oil"}),
])
def test_in_a_question_with_two_names_each_operation_scores_its_own(
    catalog, query: str, operation: str, other_name: set[str],
) -> None:
    """The commodity operation's description lists Brent, and "ttf and
    brent" ranked it first on the oil's name as well as its own."""
    results = {r["operation_id"]: r for r in search_catalog(catalog, query, limit=3)}
    assert operation in results, list(results)
    assert not _scored_words(results[operation]["why"]) & other_name, results[operation]["why"]
