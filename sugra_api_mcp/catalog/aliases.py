"""Search aliases and pattern detection for common user phrases."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from itertools import pairwise

ALIASES: dict[str, list[str]] = {
    "nasdaq futures": ["cot", "financial futures", "index futures", "nasdaq"],
    "stock futures": ["cot", "financial futures", "equity index futures", "stock index"],
    "earnings": ["earnings calendar", "company earnings", "quarterly results"],
    "13f": ["sec 13f", "institutional holdings", "fund holdings"],
    "cot": ["commitments of traders", "traders in financial futures", "positioning"],
    "central bank rates": ["policy rates", "interest rates", "monetary authorities"],
    "air quality": ["aqi", "pollution", "particulate", "environment"],
    "stock price": ["quotes symbol price", "current quote", "real time price"],
    "share price": ["quotes symbol price", "current quote"],
    "market cap": ["quotes symbol", "market capitalization"],
    "dividends": ["quotes symbol dividend", "quotes symbol actions", "corporate actions"],
    "exchange rate": ["forex", "currency", "fx"],
    "cpi": ["consumer price index", "inflation"],
    "gdp": ["gross domestic product", "national accounts"],
    # No "labor force": it named only the participation-rate operation, a
    # different measure, and lifted it over the unemployment rate itself.
    # "unemployment rate" anchors the family on the measure, so "jobless
    # rate" lands on unemployment and not on a central bank's prime rate.
    "unemployment": ["jobless", "unemployment rate"],
    "realty": ["real estate"],
    "treasury yield": ["treasury rates", "bond yield"],
    "ip geolocation": ["network atlas", "ip address", "asn"],
    "available data sources": ["list sources", "source catalog"],
    # Screening-metadata intent - the sources manifest, not a screen call.
    "corpus coverage": ["sources manifest", "screening sources", "source lists"],
    "screening coverage": ["sources manifest", "screening sources"],
    "data sources": ["list sources", "source families"],
    "news": ["latest news", "headlines"],
    # EU bidding-zone / day-ahead discovery after ENTSO-E A44
    # live prices (2026-07-21). Without these, agent queries for European
    # electricity prices ranked commodities/OWID energy bulk over energy_grid.
    "day ahead electricity price": [
        "energy grid",
        "electricity price",
        "day-ahead price",
        "entso-e",
        "eu bidding zone",
    ],
    "day-ahead price": ["energy grid", "electricity price", "entso-e"],
    "entso-e": [
        "energy grid",
        "eu bidding zone",
        "european electricity",
        "day-ahead price",
        "grid operating data",
    ],
    "eu electricity grid": [
        "energy grid",
        "entso-e",
        "bidding zone",
        "european grid demand",
    ],
    "bidding zone": ["energy grid", "entso-e", "eu electricity"],
    "grid fuel mix": ["energy grid fuel mix", "generation by fuel", "entso-e"],
    "electricity grid demand": ["energy grid", "grid operating data", "entso-e"],
}

# Central bank symbols -> operation_id prefix to boost.
# Used when query contains the symbol (case-insensitive standalone token).
CENTRAL_BANK_PREFIX_BOOSTS: dict[str, str] = {
    "fed": "fed_",
    "fomc": "fed_",
    "federal reserve": "fed_",
    "ecb": "ecb_",
    "european central bank": "ecb_",
    "boj": "boj_",
    "bank of japan": "boj_",
    "boe": "boe_",
    "bank of england": "boe_",
    "boc": "boc_",
    "bank of canada": "boc_",
    "rba": "rba_",
    "reserve bank of australia": "rba_",
    "rbnz": "rbnz_",
    "reserve bank of new zealand": "rbnz_",
    "snb": "snb_",
    "swiss national bank": "snb_",
    "riksbank": "riksbank_",
    "sarb": "central_banks_sarb_",
    "south african reserve bank": "central_banks_sarb_",
    "bnm": "bnm_",
    "bank negara malaysia": "bnm_",
    "norges bank": "norges_bank_",
    "norway central bank": "norges_bank_",
    "cnb": "cnb_",
    "czech national bank": "cnb_",
    "bcb": "bcb_",
    "banco central brasil": "bcb_",
    "bcrp": "central_banks_bcrp_",
    "peru central bank": "central_banks_bcrp_",
    "bcra": "central_banks_bcra_",
    "argentina central bank": "central_banks_bcra_",
    "rbi": "rbi_",
    "reserve bank of india": "rbi_",
}

# The place each central bank above answers for, by its prefix: a national
# source of another country is never the answer to a question about the bank
# ("Fed inflation" found the inflation of Argentina first). The ECB's place is
# the euro area, "EU" as for the euro: no source's country, so every national
# source steps down.
CENTRAL_BANK_PLACES: dict[str, str] = {
    "fed_": "US", "ecb_": "EU", "boj_": "JP", "boe_": "GB", "boc_": "CA",
    "rba_": "AU", "rbnz_": "NZ", "snb_": "CH", "riksbank_": "SE",
    "central_banks_sarb_": "ZA", "bnm_": "MY", "norges_bank_": "NO",
    "cnb_": "CZ", "bcb_": "BR", "central_banks_bcrp_": "PE",
    "central_banks_bcra_": "AR",
    "rbi_": "IN",
}

# Likely stock ticker: 2-5 uppercase letters, optional dot (BRK.A).
# Single-letter tokens are excluded because plain English sentences like
# "I need GDP data" or "A CPI endpoint" would otherwise count "I" / "A" as
# tickers and trigger the equity boost.
# Excluded reserved words that match this shape but are not tickers - includes
# common business and tech acronyms (CEO, SEC, IRS, etc.).
TICKER_TOKEN_RE = re.compile(r"\b[A-Z]{2,5}(?:\.[A-Z])?\b")
_NON_TICKER_WORDS: frozenset[str] = frozenset({
    # Tech / formats
    "API", "MCP", "URL", "JSON", "HTTP", "HTTPS", "SSL", "TLS", "TCP", "UDP", "DNS",
    "ML", "OS", "PC", "TV", "GPU", "CPU", "RAM",
    # Macro / finance indicators (not tickers)
    "CPI", "GDP", "PPI", "PMI", "ETF", "REIT", "IPO", "M2", "M1", "PE", "EPS",
    # Roles / institutions
    "CEO", "CFO", "CTO", "COO", "CMO", "VP", "SEC", "IRS", "FBI", "CIA",
    "DOJ", "FAA", "FDA", "EPA", "OK", "PR", "HR", "QA", "RFC",
    # Fiat currencies
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "CNY", "INR",
    "RUB", "ZAR", "BRL", "MXN", "SEK", "NOK", "DKK", "PLN", "TRY", "HKD",
    "SGD", "KRW", "TWD", "THB", "IDR", "MYR", "PHP", "ILS", "AED", "SAR",
    # Major crypto symbols
    "BTC", "ETH", "BNB", "XRP", "SOL", "ADA", "DOGE", "USDT", "USDC", "DAI",
    # Central bank symbols
    "FED", "FOMC", "ECB", "BOJ", "BOE", "BOC", "RBA", "RBNZ", "SNB",
    "RBI", "PBOC", "CBR", "SARB", "BCB", "BNM", "CNB", "BCRP", "BCRA",
    # Country / geographic codes
    "US", "UK", "EU", "EEA", "EEC", "USA", "USSR", "NYC", "LA", "SF",
    "DC", "UAE", "DRC",
    # Networking / internet infrastructure (field test 2026-06-07: "IXP"
    # passed the ticker regex and routed a Sugra Net Atlas query to top-20
    # quotes_symbol_*). IP and NAT are handled in _AMBIGUOUS_TICKERS below
    # because they are also real NYSE listings.
    "IXP", "BGP", "ASN", "CDN", "VPN", "ISP", "CIDR", "RIPE", "RDNS",
    "PTR", "NTP", "ICMP", "SNMP", "TOR", "WHOIS", "RPKI", "ROA", "IANA",
    "ICANN", "IETF", "APNIC", "ARIN", "LIR", "RIR", "DDOS", "LAN", "WAN",
    "MTU", "TTL",
    # Intergovernmental and statistical organizations (field
    # find: "IMF reserves" ranked quotes_symbol_* top-3 because IMF passed
    # the ticker regex - every org acronym below leaked the same way). These
    # dominate data-catalog queries; none is an active major US listing worth
    # the ambiguous-ticker gate.
    "IMF", "BIS", "OECD", "WTO", "WHO", "UN", "ILO", "FAO", "OPEC", "NATO",
    "EIA", "BLS", "BEA", "CBO", "GAO", "ONS", "EIB", "EBRD", "ADB", "IFC",
    "WB",
    # Energy / grid: ENTSO-E and regional grid codes are not
    # equity tickers; keep them out of quotes_symbol_* boosts.
    "ENTSO", "AEMO", "NESO", "NEM",
    # Commodity benchmarks: "TTF gas price" asked for the Dutch gas hub and
    # was read as a ticker through the word "price".
    "TTF", "WTI",
})

# The ticker gate is INVERTED. The old default-allow
# blacklist was patched three times (IXP 2026-06-07, IMF/org acronyms,
# ENTSO/grid) - a guard patched three times is the wrong guard.
# Now a ticker-shaped token counts as a ticker ONLY when the query carries
# equity-context vocabulary, OR the token is on this short high-liquidity
# whitelist where a bare mention almost always means the instrument. The
# _NON_TICKER_WORDS list above remains a hard NEVER list (currencies, org
# acronyms) that wins even over equity context.
# The whitelisted ETFs: search lets their ticker reach the per-ETF operations
# too, so "SPY flows" finds the ETF's flows, not a company cash flow statement.
ETF_TICKERS: frozenset[str] = frozenset({
    # Index ETFs
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO",
    # Bond and sector ETFs: "TLT inflation" asks about the ETF.
    "TLT", "IEF", "SHY", "AGG", "BND", "LQD", "HYG",
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB", "XLC", "XLRE",
})
_TICKER_WHITELIST: frozenset[str] = frozenset({
    # Mega-cap equities
    "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "NVDA", "TSLA", "META", "NFLX",
    "AMD", "INTC", "ORCL", "IBM", "CRM", "AVGO", "QCOM", "ADBE", "CSCO",
    "JPM", "BAC", "WFC", "BRK.A", "BRK.B",
    # No entry here may be a valid ISO2 country code (BA is Bosnia,
    # GS is South Georgia): an unconditional entry would defeat the
    # geography guard. Equity context or sole-token admission still
    # covers the bare quote lookups for such symbols.
    "XOM", "CVX", "WMT", "KO", "PEP", "DIS", "CAT", "JNJ", "PFE",
    "UNH", "HD", "MCD", "NKE",
}) | ETF_TICKERS

# National-source geography: operation_id prefix -> ISO2 country of
# the NATIONAL source. Used by search to demote a national source when the
# query names a DIFFERENT country - a 'Georgia CPI' query returned the UK
# ons_cpi top-1. Global/multi-country sources are deliberately absent (never
# demoted). Curated, like the central-bank prefix map above.
SOURCE_COUNTRY_PREFIXES: dict[str, str] = {
    "ons_": "GB", "boe_": "GB",
    "rba_": "AU", "abs_": "AU",
    "boc_": "CA", "statcan_": "CA",
    "boj_": "JP", "estat_": "JP",
    "snb_": "CH",
    "riksbank_": "SE",
    "norges_bank_": "NO",
    "stat_finland_": "FI",
    "statistical_agencies_statbank_dk_": "DK",
    "destatis_": "DE",
    "statistical_agencies_ibge_": "BR", "bcb_": "BR",
    "central_banks_bcra_": "AR",
    "central_banks_bcrp_": "PE",
    "central_banks_sarb_": "ZA",
    # No trailing underscore: "forex_cbr_" missed forex_cbr itself, the
    # latest Bank of Russia rates.
    "forex_cbr": "RU",
    "nbp_": "PL", "cnb_": "CZ", "bnm_": "MY",
    "statistical_agencies_stat_estonia_": "EE",
    "fred_": "US", "fed_": "US", "worldbank_bls_": "US", "bea_": "US",
    "census_": "US",
    "ine_": "ES",
    # Single-country prefixes swept from the FULL bundle (600
    # untagged clusters judged + adversarially verified per proposal;
    # 28 refutations kept global/parameterized sources untagged).
    "insee_": "FR", "transport_road_": "FR",
    "fca_shorts_": "GB",
    "data_gov_": "HK",
    "edinet_": "JP",
    "post_statistical_": "NO", "statistical_agencies_ssb_": "NO",
    "scb_": "SE",
    # No trailing underscore where the stem is an operation of its own, as for
    # forex_cbr: "weather_us_forecast_" missed weather_us_forecast itself.
    "commodities_agriculture_grains": "US", "commodities_energy_natural_": "US",
    "congress_amendments": "US", "congress_committee_": "US", "congress_committees": "US",
    "congress_communications_": "US", "congress_hearings": "US", "congress_laws": "US",
    "congress_members": "US", "congress_nominations": "US", "congress_record": "US",
    "congress_sessions": "US", "congress_summaries": "US",
    "energy_retail_": "US", "energy_tariffs": "US", "energy_utilities": "US",
    "environment_usgs_": "US", "equities_sp500_": "US", "etf_flows_": "US",
    "etf_sectors_": "US", "fixed_income_treasury_": "US", "macro_net_liquidity": "US",
    "macro_regime": "US", "maritime_history_": "US", "markets_equity_": "US", "multpl_": "US",
    "post_congress_": "US", "short_interest_": "US", "treasury_auctions": "US",
    "treasury_daily_": "US", "treasury_debt": "US", "treasury_deficit": "US",
    "treasury_gold": "US", "treasury_interest_": "US", "treasury_rates": "US",
    "usaspending_agencies": "US", "usaspending_agency_": "US", "usaspending_budget_": "US",
    "usaspending_last_": "US", "usaspending_spending": "US",
    # The National Weather Service serves the United States and its
    # territories alone (SOURCE_ALSO_SERVES): its product listing ranked first
    # for "Venezuelan GDP".
    "weather_nws_": "US", "weather_us_alerts": "US", "weather_us_forecast": "US",
    # Port and vessel sources of one country: Fintraffic Portnet covers
    # Finnish ports only, and the NOAA AIS history (the successor of
    # maritime_history_) covers United States waters only. Untagged, Portnet
    # ranked first for ship calls at Rotterdam.
    "transport_ports_port_calls": "FI", "transport_vessels_history_": "US",
}
# The dead-prefix test (tests/test_search_relevance.py) guards this map: every
# entry must match at least one bundled operation, so a source rename or
# removal fails loudly instead of silently disarming the geography penalty.

# The places a national source answers for besides its own country, by
# operation_id prefix: the National Weather Service warns, forecasts and
# observes for the US territories as well as the states, so "weather alerts in
# Puerto Rico" is its question, not a foreign source's. Its glossary answers
# for no place, and its text product listing stays out: its word "product"
# matches the gross domestic product of a GDP question ("American Samoa GDP"
# found it first).
US_TERRITORIES: frozenset[str] = frozenset({"AS", "GU", "MP", "PR", "VI"})
SOURCE_ALSO_SERVES: dict[str, frozenset[str]] = {
    prefix: US_TERRITORIES
    for prefix in (
        "weather_nws_alert", "weather_nws_aviation_", "weather_nws_forecast",
        "weather_nws_office_", "weather_nws_point", "weather_nws_radar_",
        "weather_nws_station", "weather_nws_zones", "weather_us_alerts",
        "weather_us_forecast",
    )
}

# Query-side country vocabulary: a comprehensive generated module, because
# a closed 30-entry list recreated silent substitution for every omitted
# country - Netherlands CPI still returned the UK ons_cpi.
from ._countries import COUNTRY_QUERY_TERMS  # noqa: E402 - documented above

# Country/US-state homonyms. The sovereign reading of an
# ambiguous name is DROPPED when the query carries explicit US-state cues -
# 'Georgia census states' must not penalize the US census namespace.
_AMBIGUOUS_US_STATE_COUNTRIES: dict[str, str] = {"georgia": "GE"}
# NOTE: deliberately excludes "us"/"usa" - those tokens are the US COUNTRY
# reading itself ('US CPI inflation'); a state needs a state-shaped cue.
_US_STATE_CUES: tuple[str, ...] = (
    "state", "states", "census", "county", "counties", "acs", "atlanta",
)

# Compact queries use bare ISO2 codes ('NL CPI inflation').
# Uppercase-only in the RAW query, and codes colliding with English words or
# US postal abbreviations are excluded - with US itself kept (it IS the
# country the US-macro path expects).
_ISO2_QUERY_RE = re.compile(r"\b[A-Z]{2}\b")

# US postal codes that are ALSO valid ISO2 countries:
# resolved by surrounding intent in detect_query_countries - macro vocabulary
# keeps the country reading, a US-state cue (or no cue) keeps the postal one.

# Macro vocabulary that marks a bare colliding code as a COUNTRY.
_COUNTRY_MACRO_CUES: tuple[str, ...] = (
    "cpi", "inflation", "gdp", "unemployment", "central bank",
    "interest rate", "policy rate", "exchange rate", "trade balance",
    "current account", "bond yield",
)
_ISO2_CODES_ALL: frozenset[str] = frozenset(COUNTRY_QUERY_TERMS.values())
_COUNTRY_STATISTIC_CUES: tuple[str, ...] = tuple(
    cue for cue in _COUNTRY_MACRO_CUES if " " not in cue
)
COUNTRY_STATISTIC_WORDS: frozenset[str] = frozenset(_COUNTRY_STATISTIC_CUES)


def country_statistic_words(query: str) -> frozenset[str]:
    """The one-word statistics among the country macro cues that the query names.

    Every country reports them (CPI, inflation, GDP, unemployment), so a
    question that asks for one and names no place asks for it for whichever
    country the user means. One word each, so that one query word matched in
    an operation's fields says the operation answers it.
    """
    return frozenset(_match_vocabulary(query, _COUNTRY_STATISTIC_CUES))


# The one-word statistics in other words: "jobless rate" asks for
# unemployment, "consumer prices" and "consumer price index" for the CPI,
# "gross domestic product" for GDP. Each word matches a whole query word,
# plural-tolerant. A statistic every country reports that has no one word
# is its own spelling: "current account" ranked the Swiss National Bank's
# current account first and the country profile, which holds the current
# account to GDP of any country, 38th. So is a bond yield, which the profile
# names as its "10Y yield": "government bond yields" ranked the Reserve Bank
# of Australia's first and the profile 198th. So is a trade balance, which
# the IMF Direction of Trade serves for any reporter country: "trade balance"
# ranked the US Census source first.
COUNTRY_STATISTIC_SPELLINGS: dict[str, tuple[str, ...]] = {
    "cpi": ("consumer price",),
    "gdp": ("gross domestic product",),
    "unemployment": ("jobless",),
    "current account": ("current account",),
    "bond yield": ("bond yield", "10y yield", "10 year yield"),
    "trade balance": ("trade balance",),
}


# The words right after a statistic that say which side of it the question
# asks for: "current account balance" asks for the current account, and the
# Bank of Japan's balance sheet answered it on the word "balance".
_STATISTIC_QUALIFIERS: frozenset[str] = frozenset({
    "balance", "balances", "deficit", "deficits", "surplus", "surpluses",
})


def spelled_country_statistics(query: str) -> dict[str, frozenset[str]]:
    """The one-word statistics the query names in other words, each with
    the query words that name it.

    "jobless rate" names unemployment in the word "jobless", and so does
    "unemployment and jobless claims", which names it in its own word too.
    A qualifier right after the spelling names it too: "deficit" in
    "current account deficit".
    """
    tokens = _WORD_TOKEN_RE.findall(query.lower())
    spelled: dict[str, frozenset[str]] = {}
    for statistic, spellings in COUNTRY_STATISTIC_SPELLINGS.items():
        words = frozenset(
            tokens[index]
            for spelling in spellings
            for start, end in _phrase_spans(tokens, spelling)
            for index in range(start, end + (
                end < len(tokens) and tokens[end] in _STATISTIC_QUALIFIERS))
        )
        if words:
            spelled[statistic] = words
    return spelled


# Each statistic's spellings as whole words, plural-tolerant, joined by any
# separator, as ``_phrase_spans`` matches them in the query's words.
_STATISTIC_SPELLING_RES: dict[str, re.Pattern[str]] = {
    statistic: re.compile(
        r"(?<![a-z0-9])(?:"
        + "|".join(
            r"[^a-z0-9]+".join(
                rf"{re.escape(word)}(?:s|es)?" for word in spelling.split()
            )
            for spelling in spellings
        )
        + r")(?![a-z0-9])"
    )
    for statistic, spellings in COUNTRY_STATISTIC_SPELLINGS.items()
}


def with_statistic_words(query: str) -> str:
    """The lowercased query with each statistic it names in other words
    written as its own word.

    "debt to gross domestic product" reads "debt to gdp", so a statistic
    named as the base of a ratio reads the same in either spelling.
    """
    text = query.lower()
    for statistic, pattern in _STATISTIC_SPELLING_RES.items():
        text = pattern.sub(statistic, text)
    return text


def detect_query_countries(query: str) -> set[str]:
    """ISO2 countries the query explicitly names.

    Token/phrase-bounded names and demonyms resolve directly. Bare
    UPPERCASE ISO2 codes resolve only under the uniform intent gate: macro
    vocabulary present and no US-state cue - one rule for every collision
    class (English words, US postal codes, acronyms). Sovereign readings of
    country/US-state homonym NAMES are likewise dropped under state cues.
    """
    # Overlapping matches resolve longest-phrase-first -
    # 'American Samoa' must be AS alone, not AS+US ('american')+WS ('samoa'),
    # or the US component defeats the wrong-country guard entirely.
    matched = _match_vocabulary(query, tuple(COUNTRY_QUERY_TERMS))
    # Suppression is OCCURRENCE-aware - a component term dies
    # only where every one of its spans lies inside a longer match ('American
    # Samoa and American government' keeps the separate US reading).
    tokens = _WORD_TOKEN_RE.findall(query.lower())
    normalized = " " + " ".join(tokens) + " "
    def _spans(term):
        needle = f" {term} "
        out, i = [], 0
        while True:
            j = normalized.find(needle, i)
            if j < 0:
                return out
            out.append((j, j + len(needle)))
            i = j + 1
    kept = []
    for term in matched:
        longer = [o for o in matched if o != term and f" {term} " in f" {o} "]
        if not longer:
            kept.append(term)
            continue
        covered_spans = [sp for o in longer for sp in _spans(o)]
        term_alive = any(
            not any(cs[0] <= ts[0] and ts[1] <= cs[1] for cs in covered_spans)
            for ts in _spans(term)
        )
        if term_alive:
            kept.append(term)
    found = {COUNTRY_QUERY_TERMS[term] for term in kept}
    # NO enumerated collision
    # sets - every bare uppercase ISO2 code resolves through the SAME rule:
    # macro vocabulary present and no US-state cue. Hand-enumerated gated
    # sets leaked a new collision every round (AS, MP, AI, TV, HR...);
    # a uniform gate has nothing to leak. Full country names and demonyms
    # (above) still resolve without a cue.
    macro_cue = bool(_match_vocabulary(query, _COUNTRY_MACRO_CUES))
    state_cue = bool(_match_vocabulary(query, _US_STATE_CUES))
    if macro_cue and not state_cue:
        for code in _ISO2_QUERY_RE.findall(query):
            if code in _ISO2_CODES_ALL:
                found.add(code)
    if found & set(_AMBIGUOUS_US_STATE_COUNTRIES.values()):
        cues = set(_match_vocabulary(query, _US_STATE_CUES))
        if cues:
            found -= set(_AMBIGUOUS_US_STATE_COUNTRIES.values())
    return found

# Strong-signal tokens that indicate an equity query when present near an
# otherwise-ambiguous ticker. Kept narrow on purpose; expanding too far would
# re-introduce the false positives that motivated the exclusion list.
# Bare "exchange" was dropped: it collides with "internet
# exchange" and "exchange rate" - the phrase form below keeps the equity case.
# Temporal/filler words that do not change a bare-ticker quote lookup.
_BARE_TICKER_FILLER: frozenset[str] = frozenset({
    "today", "now", "currently", "please", "latest",
})

_EQUITY_CONTEXT_TERMS: tuple[str, ...] = (
    "price", "stock", "ticker", "shares", "share price",
    "market cap", "dividend", "earnings", "p/e",
    "quote", "trading", "stock exchange",
)

# Network / internet-infrastructure vocabulary. Used by search to detect when
# a query is dominated by the networking domain so the equity ticker boost
# does not hijack it (field test 2026-06-07: "network ... internet exchange
# IXP traceroute ping measurement create" returned top-20 quotes_symbol_*).
# Single words match as whole tokens (so "shipping" does not hit "ping");
# multi-word phrases match as token-bounded substrings of the normalized query.
_NETWORK_CONTEXT_TERMS: tuple[str, ...] = (
    "traceroute", "ping", "ixp", "internet exchange", "bgp", "asn",
    "anycast", "peering", "rdns", "reverse dns", "geolocation", "ip address",
    "subnet", "prefix", "probe", "measurement", "ripe", "atlas", "whois",
    "latency", "dns", "tor", "exit node", "network",
)

_WORD_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _match_vocabulary(query: str, terms: tuple[str, ...]) -> list[str]:
    """Return distinct vocabulary terms present in the query, token-bounded.

    Single-word terms match whole tokens only ("Stockholm" does not satisfy
    "stock", "shipping" does not satisfy "ping"); multi-word terms match as
    token-bounded substrings of the normalized query ("p/e" matches its
    tokenized form "p e").
    """
    tokens = _WORD_TOKEN_RE.findall(query.lower())
    if not tokens:
        return []
    token_set = set(tokens)
    normalized = f" {' '.join(tokens)} "
    hits: list[str] = []
    for term in terms:
        term_tokens = _WORD_TOKEN_RE.findall(term)
        if not term_tokens:
            continue
        if len(term_tokens) == 1:
            if term_tokens[0] in token_set:
                hits.append(term)
        elif f" {' '.join(term_tokens)} " in normalized:
            hits.append(term)
    return hits


def detect_network_terms(query: str) -> list[str]:
    """Return distinct network-domain vocabulary terms found in the query.

    Search uses the count of distinct hits as a domain-dominance signal: two
    or more terms mean the query belongs to the networking domain and the
    equity ticker boost should not fire on ticker-shaped tokens like IXP.
    """
    return _match_vocabulary(query, _NETWORK_CONTEXT_TERMS)


# Currency pair detection: 3 letters + optional separator + 3 letters.
# Matches "EUR/USD", "EURUSD", "EUR USD". Every currency named in words is
# known by its code as well: "PEN to USD" asks what "Peruvian sol to dollar"
# asks.
CURRENCY_PAIR_RE = re.compile(r"\b([A-Z]{3})[ /\-]?([A-Z]{3})\b")
_KNOWN_CURRENCIES: frozenset[str] = frozenset({
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "CNY", "INR",
    "RUB", "ZAR", "BRL", "MXN", "SEK", "NOK", "DKK", "PLN", "TRY", "HKD",
    "SGD", "KRW", "TWD", "THB", "IDR", "MYR", "PHP", "ILS", "AED", "SAR",
    "ARS", "CLP", "COP", "PEN", "HUF", "CZK", "UAH", "KZT", "NGN", "GEL", "AMD",
})


@lru_cache(maxsize=4096)
def _phrase_forms(phrase: str) -> tuple[tuple[str, str, str], ...]:
    """Each word of a vocabulary phrase with the plurals a query token may take.

    Cached: every search reads the same fixed vocabularies, and building these
    forms anew for every query token doubled the time of a 64-term search.
    """
    return tuple(
        (word, f"{word}s", f"{word}es") for word in _WORD_TOKEN_RE.findall(phrase.lower())
    )


def _phrase_spans(tokens: list[str], phrase: str) -> list[tuple[int, int]]:
    """Token-index spans where the phrase occurs as consecutive query tokens.

    Each phrase word matches a whole token, plural-tolerant: "exchange rates"
    holds "exchange rate" and "ports" holds "port".
    """
    forms = _phrase_forms(phrase)
    if not forms:
        return []
    width = len(forms)
    first = forms[0]
    return [
        (start, start + width)
        for start in range(len(tokens) - width + 1)
        if tokens[start] in first
        and all(tokens[start + k] in forms[k] for k in range(1, width))
    ]


def _blank(tokens: list[str], phrases: Iterable[str]) -> list[str]:
    """The tokens with every occurrence of the phrases blanked out."""
    out = list(tokens)
    for phrase in phrases:
        for start, end in _phrase_spans(tokens, phrase):
            out[start:end] = [""] * (end - start)
    return out


def _claim_names(tokens: list[str], names: Iterable[str]) -> list[tuple[int, int, str]]:
    """Non-overlapping name spans in query order, the longest name claiming first.

    "turkish lira" claims both of its tokens before "lira" can, so a qualified
    name never also reads as the bare one.
    """
    claimed = [False] * len(tokens)
    found: list[tuple[int, int, str]] = []
    for name in sorted(names, key=lambda name: (-len(_phrase_forms(name)), name)):
        for start, end in _phrase_spans(tokens, name):
            if any(claimed[start:end]):
                continue
            claimed[start:end] = [True] * (end - start)
            found.append((start, end, name))
    return sorted(found)


# Aliases that stand for one country's own source, by its country.
NATIONAL_ALIASES: dict[str, str] = {"treasury yield": "US"}


def matching_aliases(query: str, countries: Iterable[str] = ()) -> dict[str, list[str]]:
    """Alias phrases the query names as whole words.

    The phrase or one of its expansions must occur as consecutive query
    tokens, plural-tolerant. A substring test fired "cot" on "cotton",
    "currency" on "cryptocurrency", "environment" on "environmental" and
    "aqi" on "Iraqi".

    A national alias the query reaches only through an expansion keeps, for
    a question that names ``countries`` without its own, only the
    expansions the query says: "bond yield" reaches the US Treasury's
    rates, and "Germany bond yields" found the US Treasury first.
    """
    tokens = _WORD_TOKEN_RE.findall(query.lower())
    places = set(countries)
    found: dict[str, list[str]] = {}
    for phrase, expansions in ALIASES.items():
        if not any(_phrase_spans(tokens, term) for term in (phrase, *expansions)):
            continue
        country = NATIONAL_ALIASES.get(phrase)
        if country and places and country not in places and not _phrase_spans(tokens, phrase):
            expansions = [term for term in expansions if _phrase_spans(tokens, term)]
        found[phrase] = expansions
    return found


# Everyday currency names -> ISO 4217 code. A bare word that is also common
# English or names several currencies ("real", "won", "peso", "krone", "sol")
# counts only with its qualifier.
CURRENCY_NAMES: dict[str, str] = {
    "dollar": "USD", "us dollar": "USD", "u s dollar": "USD", "american dollar": "USD",
    "greenback": "USD",
    "canadian dollar": "CAD", "australian dollar": "AUD", "aussie dollar": "AUD",
    "new zealand dollar": "NZD", "hong kong dollar": "HKD", "singapore dollar": "SGD",
    "taiwan dollar": "TWD", "new taiwan dollar": "TWD",
    "euro": "EUR",
    "yen": "JPY", "japanese yen": "JPY",
    "pound": "GBP", "pound sterling": "GBP", "sterling": "GBP", "british pound": "GBP",
    "franc": "CHF", "swiss franc": "CHF",
    "yuan": "CNY", "chinese yuan": "CNY", "renminbi": "CNY", "rmb": "CNY",
    "rupee": "INR", "indian rupee": "INR",
    "ruble": "RUB", "rouble": "RUB", "russian ruble": "RUB", "russian rouble": "RUB",
    "rand": "ZAR", "south african rand": "ZAR",
    "brazilian real": "BRL", "brazilian reais": "BRL",
    "mexican peso": "MXN", "argentine peso": "ARS", "argentinian peso": "ARS",
    "chilean peso": "CLP", "colombian peso": "COP", "philippine peso": "PHP",
    "krona": "SEK", "kronor": "SEK", "swedish krona": "SEK", "swedish kronor": "SEK",
    "norwegian krone": "NOK", "norwegian kroner": "NOK",
    "danish krone": "DKK", "danish kroner": "DKK",
    "zloty": "PLN", "polish zloty": "PLN",
    "forint": "HUF", "hungarian forint": "HUF",
    "koruna": "CZK", "czech koruna": "CZK",
    "lira": "TRY", "turkish lira": "TRY",
    "korean won": "KRW", "south korean won": "KRW",
    "baht": "THB", "thai baht": "THB",
    "rupiah": "IDR", "indonesian rupiah": "IDR",
    "ringgit": "MYR", "malaysian ringgit": "MYR",
    "shekel": "ILS", "israeli shekel": "ILS",
    "dirham": "AED", "uae dirham": "AED",
    "riyal": "SAR", "saudi riyal": "SAR",
    "hryvnia": "UAH", "tenge": "KZT", "naira": "NGN", "lari": "GEL", "dram": "AMD",
    "peruvian sol": "PEN",
}

# The country a currency belongs to: a national source of another country is
# never the answer to its exchange-rate question. "EU" is no source's
# country, so for the euro every national source steps down.
_CURRENCY_ISSUERS: dict[str, str] = {
    "USD": "US", "EUR": "EU", "JPY": "JP", "GBP": "GB", "CHF": "CH", "CNY": "CN",
    "INR": "IN", "RUB": "RU", "ZAR": "ZA", "BRL": "BR", "MXN": "MX", "ARS": "AR",
    "CLP": "CL", "COP": "CO", "PHP": "PH", "PEN": "PE", "SEK": "SE", "NOK": "NO",
    "DKK": "DK", "PLN": "PL", "HUF": "HU", "CZK": "CZ", "TRY": "TR", "KRW": "KR",
    "THB": "TH", "IDR": "ID", "MYR": "MY", "ILS": "IL", "AED": "AE", "SAR": "SA",
    "UAH": "UA", "KZT": "KZ", "NGN": "NG", "GEL": "GE", "AMD": "AM",
    "CAD": "CA", "AUD": "AU", "NZD": "NZ", "HKD": "HK", "SGD": "SG", "TWD": "TW",
}

# ISO codes that are also English words when written in lowercase: "price of
# a pen in dollars" names no Peruvian sol.
_LOWERCASE_CODE_WORDS: frozenset[str] = frozenset({
    "try", "php", "cad", "sar", "rub", "pen", "cop", "gel",
})

# Weight, not sterling: "a pound of coffee", "price per pound".
_CURRENCY_NAME_BLOCKERS: tuple[str, ...] = ("pound of", "per pound")

# Words that make a named currency an exchange-rate question. So does "rate"
# right after the currency ("Indian rupee rate"), in the singular only:
# "euro rates" also names the euro interest rates.
_FX_CUES: tuple[str, ...] = (
    "exchange rate", "convert", "converting", "conversion", "converter",
    "forex", "fx", "currency", "currencies",
)

# Words that may stand between two currencies joined as a conversion:
# "dollar to yen", "lira for one US dollar", "yen is a dollar". Not "and":
# "dollar and euro" lists two currencies without converting one to the other.
_PAIR_CONNECTIVES: frozenset[str] = frozenset({
    "to", "for", "in", "into", "per", "vs", "versus", "against",
    "one", "a", "an", "the", "is", "are", "worth",
})

# The conversion operation, and the wording that asks for a rate over time,
# which the history operation answers instead.
FX_CONVERT_OPERATION = "forex_convert"
_FX_HISTORY_WORDS: tuple[str, ...] = (
    "history", "historical", "trend", "chart", "past", "since", "ago",
    "over time", "last", "year", "month", "week",
)


@dataclass(frozen=True)
class FxRequest:
    """An exchange-rate question asked in everyday words."""

    currencies: tuple[str, ...]  # ISO codes, query order, each once
    pair: bool                   # two currencies joined as a conversion
    over_time: bool              # asks for the rate over a period
    words: frozenset[str]        # the query tokens that named a currency

    @property
    def issuer_countries(self) -> frozenset[str]:
        return frozenset(
            _CURRENCY_ISSUERS[code] for code in self.currencies if code in _CURRENCY_ISSUERS
        )


def _currency_mentions(query: str) -> tuple[list[str], list[tuple[int, int, str]]]:
    """The query's lowercase tokens, weight phrases blanked, and the currencies
    it names in words or by code, as (start, end, ISO code) in query order."""
    raw_tokens = re.findall(r"[A-Za-z0-9]+", query)
    tokens = _blank([token.lower() for token in raw_tokens], _CURRENCY_NAME_BLOCKERS)
    mentions = [
        (start, end, CURRENCY_NAMES[name])
        for start, end, name in _claim_names(tokens, CURRENCY_NAMES)
    ]
    named = {index for start, end, _ in mentions for index in range(start, end)}
    for index, raw in enumerate(raw_tokens):
        code = raw.upper()
        if (index not in named and tokens[index] and code in _KNOWN_CURRENCIES
                and (raw.isupper() or tokens[index] not in _LOWERCASE_CODE_WORDS)):
            mentions.append((index, index + 1, code))
    mentions.sort()
    return tokens, mentions


def detect_fx_request(query: str) -> FxRequest | None:
    """The exchange-rate question the query asks, or None.

    A query asks one when it joins two different currencies as a conversion
    ("dollar to yen", "Turkish lira for one US dollar", "EUR/USD") or names a
    currency beside an exchange-rate cue ("euro exchange rate"). A currency
    named alone ("coffee price in dollars") asks nothing. Crypto queries are
    the caller's to exclude: "convert bitcoin to dollars" is a crypto price.
    """
    tokens, mentions = _currency_mentions(query)
    iso_pairs = detect_currency_pairs(query)

    pair = bool(iso_pairs)
    for (_, end, first), (start, _, second) in pairwise(mentions):
        if first != second and all(
            token in _PAIR_CONNECTIVES or token.isdigit() for token in tokens[end:start]
        ):
            pair = True
    currencies = tuple(dict.fromkeys(
        [code for _, _, code in mentions] + [code for found in iso_pairs for code in found]
    ))
    cue = any(_phrase_spans(tokens, word) for word in _FX_CUES) or any(
        tokens[end:end + 1] == ["rate"] for _, end, _ in mentions
    )
    if not (pair or (currencies and cue)):
        return None
    return FxRequest(
        currencies=currencies,
        pair=pair,
        over_time=any(_phrase_spans(tokens, word) for word in _FX_HISTORY_WORDS),
        words=frozenset(
            [tokens[index] for start, end, _ in mentions for index in range(start, end)]
            + [code.lower() for found in iso_pairs for code in found]
        ),
    )


# An exchange-rate question that names no currency and asks nothing else
# asks for the rates of every currency: the currency reference panel answers
# it, or the reference rates over time when it asks for a period or a year.
# "exchange rate" ranked Peru's sol against one currency first. A word beyond
# these keeps its own answer ("real effective exchange rate", "Peru exchange
# rate", a currency code in capitals). A question word asks for the rates
# only beside a word of time: "what is a currency exchange rate" asks what
# one is, "what's the exchange rate today" asks for the rates. The "s" of
# "what's" or "world's" asks nothing by itself: "what" carries the question.
# A number is read only as a year from 1900 to 2099 or as a part of a
# period: a date ("on 2020-01-01", "March 2020"), a count of days, weeks,
# months or years before "ago" or after "last" or "past" ("20 years ago",
# "the last 5 years"), or "yesterday". A question with any other number
# ("in 99", "5 years") is not read.
FX_PANEL_OPERATION = "forex_rates"
FX_HISTORY_OPERATION = "forex_history"
_EVERY_CURRENCY_WORDS: frozenset[str] = frozenset({
    "all", "chart", "currencies", "currency", "daily", "for", "foreign", "get",
    "global", "history", "historical", "in", "last", "major", "me", "month",
    "months", "of", "over", "past", "s", "show", "since", "the", "this", "time",
    "trend", "week", "weeks", "world", "year", "years",
})
_EVERY_CURRENCY_NOW_WORDS: frozenset[str] = frozenset({"current", "latest", "now", "today"})
_EVERY_CURRENCY_QUESTION_WORDS: frozenset[str] = frozenset({"are", "is", "was", "were", "what"})
# The words that name the request; the period words keep scoring as words.
_EVERY_CURRENCY_NAME_WORDS: frozenset[str] = frozenset({
    "currencies", "currency", "exchange", "foreign", "rate", "rates",
})
_YEAR_RE = re.compile(r"(?:19|20)\d\d")
_DAY_OR_MONTH_RE = re.compile(r"\d{1,2}")
_MONTH_NAMES: frozenset[str] = frozenset({
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
})
_PERIOD_UNITS: frozenset[str] = frozenset({
    "day", "days", "week", "weeks", "month", "months", "year", "years",
})


def _period_words(tokens: list[str]) -> set[int]:
    """The indices of the tokens that name a period other than a bare year:
    a date and the "on" right before it, a count of days, weeks, months or
    years before "ago" or after "last" or "past", and "yesterday"."""
    found: set[int] = set()
    for index, token in enumerate(tokens):
        if token == "yesterday":
            found.add(index)
        elif _YEAR_RE.fullmatch(token) or token in _MONTH_NAMES:
            # A date reads as a run of day or month numbers and month names
            # around a year, or a month name beside one: "2020-01-01",
            # "1 March 2020", "March 2020".
            start = end = index
            while start > 0 and (_DAY_OR_MONTH_RE.fullmatch(tokens[start - 1])
                                 or tokens[start - 1] in _MONTH_NAMES):
                start -= 1
            while end + 1 < len(tokens) and (_DAY_OR_MONTH_RE.fullmatch(tokens[end + 1])
                                             or tokens[end + 1] in _MONTH_NAMES):
                end += 1
            run = range(start, end + 1)
            if token in _MONTH_NAMES:
                years = [i for i in (start - 1, end + 1)
                         if 0 <= i < len(tokens) and _YEAR_RE.fullmatch(tokens[i])]
                if not years:
                    continue
                run = range(min(start, *years), max(end, *years) + 1)
            elif start == end:
                continue
            found.update(run)
            if run.start > 0 and tokens[run.start - 1] == "on":
                found.add(run.start - 1)
        elif (token.isdigit() and index + 1 < len(tokens) and tokens[index + 1] in _PERIOD_UNITS
              and ((index + 2 < len(tokens) and tokens[index + 2] == "ago")
                   or (index > 0 and tokens[index - 1] in ("last", "past")))):
            found.update((index, index + 1))
            if index + 2 < len(tokens) and tokens[index + 2] == "ago":
                found.add(index + 2)
    return found


# A policy rate asked of no country asks for every central bank's: the BIS
# policy rates of all central banks answer it. "policy rate" ranked eight
# operations of six national banks first and the BIS rates 37th. Only these
# phrases, with "the" at most, are read; any other word keeps its own answer.
CB_RATES_OPERATION = "bis_cb_rates"
CB_RATES_OPERATIONS: tuple[str, ...] = (CB_RATES_OPERATION, "bis_cb_rates_country")
_CB_RATE_PHRASES = (
    "policy rate", "central bank rate", "central bank policy rate",
    "central bank interest rate",
)
_CB_RATE_FILLER: frozenset[str] = frozenset({"the"})
_CB_RATE_NAME_WORDS: frozenset[str] = frozenset({
    "bank", "central", "interest", "policy", "rate", "rates",
})


def detect_central_bank_rate_request(query: str) -> frozenset[str] | None:
    """The query words that name every central bank's policy rate, or None
    when the query says anything beyond a policy-rate phrase."""
    tokens = re.findall(r"[a-z0-9]+", query.lower())
    covered = {index for phrase in _CB_RATE_PHRASES
               for start, end in _phrase_spans(tokens, phrase)
               for index in range(start, end)}
    if not covered or any(token not in _CB_RATE_FILLER
                          for index, token in enumerate(tokens) if index not in covered):
        return None
    return frozenset(token for token in tokens if token in _CB_RATE_NAME_WORDS)


def detect_every_currency_request(query: str) -> tuple[str, frozenset[str]] | None:
    """The operation that answers an exchange-rate question naming no
    currency and nothing else, and the query words that name it, or None."""
    raw = re.findall(r"[A-Za-z0-9]+", query)
    tokens = [token.lower() for token in raw]
    phrase = {index for start, end in _phrase_spans(tokens, "exchange rate")
              for index in range(start, end)}
    period = _period_words(tokens)
    rest = [(raw[index], token) for index, token in enumerate(tokens)
            if index not in phrase and index not in period]
    if not phrase or not all(
        (token in _EVERY_CURRENCY_WORDS and not (len(word) == 3 and word.isupper()))
        or token in _EVERY_CURRENCY_NOW_WORDS or token in _EVERY_CURRENCY_QUESTION_WORDS
        or _YEAR_RE.fullmatch(token)
        for word, token in rest
    ):
        return None
    years = any(_YEAR_RE.fullmatch(token) for _, token in rest)
    over_time = years or bool(period) or any(
        _phrase_spans(tokens, word) for word in _FX_HISTORY_WORDS)
    asks = any(token in _EVERY_CURRENCY_QUESTION_WORDS for _, token in rest)
    if asks and not (over_time or any(token in _EVERY_CURRENCY_NOW_WORDS for _, token in rest)):
        return None
    return (
        FX_HISTORY_OPERATION if over_time else FX_PANEL_OPERATION,
        frozenset(token for token in tokens if token in _EVERY_CURRENCY_NAME_WORDS),
    )


# The one-word country statistics in their own words and in other words.
_COUNTRY_STATISTIC_NAMES: tuple[str, ...] = (
    *_COUNTRY_STATISTIC_CUES,
    *(spelling for spellings in COUNTRY_STATISTIC_SPELLINGS.values() for spelling in spellings),
)


def currency_statistic_places(query: str) -> tuple[frozenset[str], frozenset[str]]:
    """The countries whose currency the query names right before one of the
    one-word country statistics, and the words that name those currencies.

    "yen inflation" asks for the inflation of Japan, as "Japan inflation"
    does, and "yen consumer prices" for its CPI. A currency named anywhere
    else is a unit or a market, not a place: "GDP in dollars", "coffee price
    in dollars and inflation".
    """
    tokens, mentions = _currency_mentions(query)
    places: set[str] = set()
    words: set[str] = set()
    for start, end, code in mentions:
        if code in _CURRENCY_ISSUERS and any(
            _phrase_spans(tokens[end:end + len(name.split())], name)
            for name in _COUNTRY_STATISTIC_NAMES
        ):
            places.add(_CURRENCY_ISSUERS[code])
            words.update(tokens[start:end])
    return frozenset(places), frozenset(words)


# Everyday names of a benchmark, waterway or measure -> the operations that
# answer it. The catalog spells these names inside prose ("Crude oil prices
# (Brent & WTI)"), as a parameter example ("chokepoint1 for Suez Canal") or
# not at all (TTF is the World Bank's "Natural gas, Europe" series), so the
# token score alone never finds them. A product points to its price only
# with the word price: "crude oil" alone also opens questions about
# pipelines, tankers and stocks. The names below that hold "crude", "oil",
# "trucking" or "truckload" count only where a conjunction or a list mark
# joins the subject to another word; elsewhere the subject is named by its
# price (PRICED_SUBJECTS). "brent" and "wti" name crude oil everywhere.
NAMED_OPERATIONS: dict[str, tuple[str, ...]] = {
    "oil price": ("commodities_energy_petroleum",),
    "price of oil": ("commodities_energy_petroleum",),
    "crude price": ("commodities_energy_petroleum",),
    "price of crude": ("commodities_energy_petroleum",),
    "brent": ("commodities_energy_petroleum",),
    "wti": ("commodities_energy_petroleum",),
    "henry hub": ("commodities_energy_natural_gas",),
    "ttf": ("commodities_commodity_id",),
    "title transfer facility": ("commodities_commodity_id",),
    "dutch gas": ("commodities_commodity_id",),
    "european gas": ("commodities_commodity_id",),
    "european natural gas": ("commodities_commodity_id",),
    "eu gas": ("commodities_commodity_id",),
    "europe gas": ("commodities_commodity_id",),
    "natural gas europe": ("commodities_commodity_id",),
    "gas price europe": ("commodities_commodity_id",),
    "gas price in europe": ("commodities_commodity_id",),
    "suez": ("maritime_chokepoints_activity",),
    "red sea": ("maritime_chokepoints_activity", "maritime_chokepoints_jmic_advisory"),
    "bab el mandeb": ("maritime_chokepoints_activity", "maritime_chokepoints_jmic_advisory"),
    "bab al mandab": ("maritime_chokepoints_activity", "maritime_chokepoints_jmic_advisory"),
    "mandeb": ("maritime_chokepoints_activity", "maritime_chokepoints_jmic_advisory"),
    "hormuz": (
        "maritime_chokepoints_activity", "maritime_chokepoints_hormuz_transits",
        "maritime_chokepoints_jmic_advisory",
    ),
    "panama canal": ("maritime_chokepoints_activity", "maritime_chokepoints_panama_transits"),
    "malacca": ("maritime_chokepoints_activity", "maritime_chokepoints_malacca_throughput"),
    "bosphorus": ("maritime_chokepoints_activity",),
    "bosporus": ("maritime_chokepoints_activity",),
    # Singular: a query's plural matches a singular phrase, never the reverse.
    "turkish strait": ("maritime_chokepoints_activity",),
    "dardanelles": ("maritime_chokepoints_activity",),
    # FRED holds these under series codes only (PCU484121484121, the
    # producer price index of general freight trucking), so they name it only
    # as a price of trucking: "trucking" and "truckload" alone count beside
    # "freight" only (_NAME_CUES), because "trucking accidents" and "trucking
    # jobs" ask about something else.
    "trucking": ("fred_series_series_id",),
    "truckload": ("fred_series_series_id",),
    "truck freight": ("fred_series_series_id",),
    "trucking rate": ("fred_series_series_id",),
    "trucking price": ("fred_series_series_id",),
    "trucking cost": ("fred_series_series_id",),
    "truckload rate": ("fred_series_series_id",),
    "truckload price": ("fred_series_series_id",),
    "cost of trucking": ("fred_series_series_id",),
    "price of trucking": ("fred_series_series_id",),
    # A central bank's meetings are in the meeting calendar, not among the
    # bank's own operations ("next FOMC meeting date").
    "meeting": ("macro_cb_calendar", "macro_cb_calendar_bank"),
}

# The central bank meeting calendar, and the banks whose meetings it
# publishes by their central-bank prefix.
MEETING_CALENDAR_OPERATIONS: frozenset[str] = frozenset(NAMED_OPERATIONS["meeting"])
_MEETING_CALENDAR_PREFIXES: frozenset[str] = frozenset({
    "fed_", "ecb_", "boe_", "boj_", "snb_", "riksbank_",
})

# Names that point to their operations only when the query also holds one of
# these words: "trucking freight rates" asks for the trucking price index,
# "trucking employment" does not, and a meeting is the calendar's only when
# a bank it covers holds it.
_NAME_CUES: dict[str, tuple[str, ...]] = {
    "trucking": ("freight",),
    "truckload": ("freight",),
    "meeting": (
        "central bank",
        *(name for name, prefix in CENTRAL_BANK_PREFIX_BOOSTS.items()
          if prefix in _MEETING_CALENDAR_PREFIXES),
    ),
}

# Other oils: "palm oil price" is not a crude oil question, and neither is
# "price of crude palm oil", a grade of palm oil.
_OTHER_OILS: tuple[str, ...] = (
    "palm oil", "palm kernel oil", "olive oil", "soybean oil", "soy oil", "sunflower oil",
    "rapeseed oil", "canola oil", "coconut oil", "vegetable oil", "cooking oil",
    "fish oil", "linseed oil", "cottonseed oil", "groundnut oil", "peanut oil",
    "corn oil", "heating oil",
)
_NAME_BLOCKERS: tuple[str, ...] = _OTHER_OILS + tuple(f"crude {oil}" for oil in _OTHER_OILS)


def _forms(*words: str) -> frozenset[str]:
    """The words with the plurals a query token may take."""
    return frozenset(form for word in words for form in (word, f"{word}s", f"{word}es"))


@dataclass(frozen=True)
class PricedSubject:
    """A subject a query names by its price, and the operation that answers it.

    A run of the subject's words holding one of its heads names the operation
    only as the subject of one of its price words, in either word order, and
    only when no other word claims that price.

    The price word follows the run, past an "'s" and words that qualify a
    price ("trucking rates", "crude oil spot price", "oil's price"), or it
    precedes the run, through "of", "for" or "per", an article, a measure
    ("price of a barrel of oil") and words that qualify the run: price
    qualifiers ("price of spot crude") and, before a run that holds one of the
    subject's modified heads, places and the subject's own modifiers ("price
    of Russian crude", "price of light sweet crude oil"; "price of Moroccan
    oil" asks for argan oil). Series words may stand between a preceding price
    word and its link ("price history of oil"). The word before that price
    word is a word of asking, a phrase end, a qualifier, a place, a grade or
    origin, a number, a currency code or a word of the subject ("freight rates
    for trucking"), and the run's phrase ends where such a word stands after
    it, past places and grades or origins ("price of oil March 2020", "price of
    crude Texas").

    Any other word claims the price: another commodity inside the run ("palm
    oil", "crude palm oil", "palm oil crude"), a word the run modifies ("price
    of trucking stocks"), another word between a preceding price and the run
    ("price of shipping oil") or before that price ("insurance cost of
    trucking"), the price's own "of" ("truckload price of corn"), another
    subject after the price's link ("crude prices for palm oil"), and a traded
    instrument after the price or the run, also as the unit after "per" ("oil
    price ETF", "oil price per share", "price of oil index fund"). Otherwise
    the word before a run whose price follows it stays open, because a verb
    stands there as often as a modifier ("what affects oil prices"); there the
    other subjects' words claim the price ("palm oil price").

    A run joined to another word by "and", "or" or "versus" or by a list
    mark, on either side ("gold price and oil price", "price of oil and gas
    companies", "price of gold, oil"), is left to the subject's names in
    NAMED_OPERATIONS, and with it every run of the subject in the query. A
    list mark after a run whose price precedes it only ends the run's phrase
    ("price of a barrel of oil, today"). A subject with open phrases leaves a
    run to those names in the same way where its phrase goes on past a word
    the run modifies or the price's own "of" would claim the price ("price of
    oil California", "price of oil change", "oil price of California"); a
    traded instrument or another subject there still claims it.
    """

    operation: str
    label: str                  # the name search reports
    heads: tuple[str, ...]      # a run names the subject only holding one of these
    words: frozenset[str]       # the words a run of the subject holds
    prices: tuple[str, ...]     # the subject's price words, longest first
    others: frozenset[str] = frozenset()     # words that make a run another subject
    modifiers: frozenset[str] = frozenset()  # grades and origins that keep the subject
    measures: frozenset[str] = frozenset()   # units a preceding price is given per
    modified_heads: tuple[str, ...] = ()     # heads places and modifiers may precede; () all
    open_phrases: bool = False               # a phrase going on past the price keeps the names


PRICED_SUBJECTS: tuple[PricedSubject, ...] = (
    PricedSubject(
        operation="commodities_energy_petroleum",
        label="oil price",
        heads=("crude", "oil"),
        words=_forms("crude", "oil", "barrel"),
        prices=("price",),
        # Other oils, which are no crude oil in any order ("palm oil price",
        # "price of crude palm oil", "palm oil crude price").
        others=_forms(
            "palm", "kernel", "olive", "soybean", "soy", "sunflower", "rapeseed", "canola",
            "coconut", "vegetable", "cooking", "fish", "linseed", "cottonseed", "groundnut",
            "peanut", "corn", "heating",
        ),
        # Grades and origins of crude oil that no place name covers, and the
        # agencies and exchanges that quote its price.
        modifiers=frozenset({
            "light", "sweet", "heavy", "sour", "medium", "shale", "tight", "imported",
            "opec", "urals", "texas", "alaska", "alaskan", "slope", "arab", "arabian",
            "gulf", "north", "sea", "west", "western", "intermediate", "venezuelan",
            "iranian", "iraqi", "kuwaiti", "libyan", "omani", "qatari", "angolan",
            "algerian", "dubai", "basrah", "bonny", "bakken", "permian", "alberta", "wcs",
            "murban", "espo", "kurdistan", "kurdish", "eia", "iea", "nymex",
        }),
        measures=_forms("barrel", "bbl"),
        # A place or grade before "oil" alone may name another oil ("Moroccan
        # oil" is argan oil, "Italian oil" olive oil); before "crude" it may not.
        modified_heads=("crude",),
        # A place, a source or another noun after crude oil ("price of oil
        # California", "price of oil change") leaves it to its names.
        open_phrases=True,
    ),
    # FRED holds the producer price index of general freight trucking under
    # its series code only (PCU484121484121). "truck" alone is a vehicle
    # ("truck prices"), and freight alone also goes by sea, air and rail.
    PricedSubject(
        operation="fred_series_series_id",
        label="trucking price",
        heads=("trucking", "truckload", "truck freight"),
        words=_forms(
            "trucking", "truckload", "truck", "freight", "shipping", "transport",
            "transportation", "haul", "hauling", "haulage", "goods", "cargo", "load",
            "shipment", "container", "pallet", "service", "general", "long", "distance",
            "local", "regional",
        ),
        prices=(
            "producer price index", "price index", "price", "cost", "rate", "index",
            "indices", "ppi",
        ),
        modifiers=frozenset({
            "flatbed", "reefer", "refrigerated", "dry", "van", "ltl", "ftl", "intermodal",
            "drayage",
        }),
    ),
)

# The names in NAMED_OPERATIONS that hold a priced subject's head, and the
# subject's operation: they name it only in a query that leaves the subject to
# them (_joined, _goes_on).
_SUBJECT_NAMES: dict[str, str] = {
    name: subject.operation
    for subject in PRICED_SUBJECTS
    for name, operations in NAMED_OPERATIONS.items()
    if subject.operation in operations
    and any(_phrase_spans(_WORD_TOKEN_RE.findall(name), head) for head in subject.heads)
}

# Words that end a noun phrase. A subject followed by one of them, or by any
# other word that may stand before a price (_PRICE_LEADS), is the whole subject
# ("price of trucking in the US", "price of oil today", "price of oil chart",
# "price of oil March 2020", "price of oil affects inflation"); a subject
# followed by any other word modifies that word, which claims the price
# ("price of trucking stocks") or, for a subject with open phrases, leaves the
# subject to its names ("price of oil paintings"). Never "of": "a truckload of
# apples" asks about apples.
_FUNCTION_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "this", "that", "these", "those", "my", "our", "your", "their",
    "its", "it", "s", "some", "any", "each", "every", "all",
    "what", "which", "who", "why", "how", "when", "where",
    "is", "are", "was", "were", "be", "been", "being", "has", "have", "had", "do", "does",
    "did", "will", "would", "can", "could", "should", "may", "might",
    "and", "or", "but", "so", "if", "as", "than", "versus", "vs",
    "in", "on", "at", "for", "from", "to", "per", "by", "with", "without", "about",
    "across", "between", "within", "over", "since", "during", "into", "through", "around",
    "after", "before", "until", "under", "above", "below", "near", "via", "against", "like",
    "among",
})
_TREND_WORDS: frozenset[str] = frozenset({
    "going", "gone", "went", "rising", "rose", "risen", "falling", "fell", "fallen",
    "increasing", "increased", "decreasing", "decreased", "dropping", "dropped",
    "climbing", "climbed", "soaring", "surging", "spiking", "jumping", "changing",
    "changed", "trending", "moving", "compared", "relative", "up", "down", "higher",
    "lower", "now", "today", "tonight", "yesterday", "tomorrow", "currently", "recently",
    "lately", "still", "already", "ever", "again", "right", "very", "too", "much", "more",
    "less", "time", "year", "month", "week", "day", "daily", "weekly", "monthly",
    "quarterly", "yearly", "annually", "historically", "last", "next", "past", "ytd",
    "january", "february", "march", "april", "june", "july", "august", "september",
    "october", "november", "december", "jan", "feb", "mar", "apr", "jun", "jul", "aug",
    "sep", "sept", "oct", "nov", "dec",
})
_SERIES_WORDS: frozenset[str] = _forms(
    "chart", "graph", "history", "trend", "data", "forecast", "outlook", "prediction",
    "news", "level", "volatility", "statistic", "stats", "series", "index",
) | {"indices"}
_PHRASE_ENDS: frozenset[str] = _FUNCTION_WORDS | _TREND_WORDS | _SERIES_WORDS

# Words that qualify a price itself: they may stand between a subject and its
# price word ("truckload spot rates"), between a price's link and its subject
# ("price of spot crude") or before a price word that reaches its subject
# ("average cost of trucking"). Before such a price word may also stand a
# phrase end, a word of asking ("show me the price of oil"), a verb that acts
# on a price ("what drives the price of oil"), an event ("war price of oil"),
# a place ("US price of crude"), a currency code ("USD price of oil"), a grade
# or origin of the subject or an agency that quotes it ("OPEC price of oil") or
# a word of the subject itself ("freight rates for trucking"); any other word
# claims the price ("insurance cost of trucking", "accident rate for trucking").
_PRICE_QUALIFIERS: frozenset[str] = frozenset({
    "current", "latest", "average", "avg", "mean", "median", "typical", "total", "overall",
    "real", "true", "actual", "nominal", "adjusted", "spot", "contract", "market",
    "global", "world", "international", "national", "domestic", "historical", "historic",
    "recent", "high", "highest", "low", "lowest", "record", "peak", "new", "expected",
    "projected", "predicted", "forecasted", "estimated", "future", "breakeven",
    "benchmark", "reference", "official", "live", "realtime", "closing", "opening",
    "wholesale", "retail", "unit", "producer", "linehaul", "cheap", "cheaper", "cheapest",
    "expensive", "volatile", "fair", "annual", "annualized", "posted", "selling",
    "buying", "purchase", "fob", "cif",
})
# Places beside the country names and demonyms: world regions ("European oil
# prices", "Middle East oil price"), the codes a query writes in lowercase
# ("us crude") and the names of more than one word ("United States crude",
# "U.S. crude").
_REGIONS: tuple[str, ...] = (
    "europe", "european", "asia", "asian", "africa", "african", "eurasia", "eurasian",
    "caspian", "nordic", "scandinavian", "baltic", "mediterranean", "arctic",
    "middle east", "middle eastern", "latin america", "latin american", "south america",
    "south american", "north america", "central asia", "central asian", "east asia",
    "east asian", "southeast asia", "southeast asian", "asia pacific", "west africa",
    "west african",
)
_PLACE_CODES: frozenset[str] = frozenset({"us", "eu"})
_PLACE_WORDS: frozenset[str] = _PLACE_CODES | frozenset(
    name for name in (*COUNTRY_QUERY_TERMS, *_REGIONS) if " " not in name
)
_PLACE_PHRASES: tuple[str, ...] = (
    *(name for name in (*COUNTRY_QUERY_TERMS, *_REGIONS)
      if " " in name and " ".join(_WORD_TOKEN_RE.findall(name)) == name),
    "u s",
)
_PLACE_FIRSTS: frozenset[str] = frozenset(
    form for name in _PLACE_PHRASES for form in _phrase_forms(name)[0]
)
_PLACE_LASTS: frozenset[str] = frozenset(
    form for name in _PLACE_PHRASES for form in _phrase_forms(name)[-1]
)
# Verbs that act on a price, which may stand before it ("OPEC cuts price of
# oil").
_PRICE_VERBS: frozenset[str] = frozenset({
    "affect", "affects", "affected", "affecting", "impact", "impacts", "impacted",
    "impacting", "influence", "influences", "influenced", "influencing", "drive",
    "drives", "drove", "driven", "driving", "determine", "determines", "determined",
    "determining", "set", "sets", "control", "controls", "controlled", "controlling",
    "cut", "cuts", "raise", "raises", "raised", "raising", "lowers", "lowered",
    "lowering", "push", "pushes", "pushed", "pushing", "boost", "boosts", "boosted",
    "lift", "lifts", "lifted", "hit", "hits", "hitting", "cause", "causes", "caused",
    "causing", "reduce", "reduces", "reduced", "reducing", "manipulate", "manipulates",
    "manipulated", "manipulating", "keep", "keeps", "kept", "keeping", "support",
    "supports", "supported", "supporting", "hurt", "hurts", "hurting", "help", "helps",
    "helped", "helping", "move", "moves", "moved", "increase", "increases", "decrease",
    "decreases",
})
# Events a keyword query names before a price ("war price of oil").
_PRICE_EVENTS: frozenset[str] = frozenset({
    "war", "wars", "crisis", "embargo", "sanctions", "pandemic", "covid", "recession",
    "invasion", "election", "elections",
})
_PRICE_LEADS: frozenset[str] = (
    _PHRASE_ENDS | _PRICE_QUALIFIERS | _PRICE_VERBS | _PRICE_EVENTS | frozenset({
        "show", "get", "find", "check", "track", "tell", "me", "give", "list", "plot", "see",
        "compare", "know", "need", "want", "fetch", "predict", "estimate", "monitor",
        "analyze", "analyse", "download", "pull", "retrieve", "display", "explain",
        "calculate", "lookup", "search", "query",
    })
)
_PRICE_LINKS: frozenset[str] = frozenset({"of", "for", "per"})
_DETERMINERS: frozenset[str] = frozenset({"a", "an", "the"})

# Traded instruments: a price word in a noun phrase that names one of them is
# the instrument's price ("oil price ETF", "trucking price index fund", "oil
# price per share"). Not "future", which is time.
_INSTRUMENTS: frozenset[str] = frozenset({
    "stock", "stocks", "share", "shares", "equity", "equities", "fund", "funds", "etf",
    "etfs", "etn", "etns", "futures", "option", "options", "swap", "swaps",
    "derivative", "derivatives", "cfd", "cfds", "bond", "bonds", "warrant", "warrants",
})

# Conjunctions that join a run to another word ("gold and oil prices", "price
# of oil and gas companies"), which leaves the run to its subject's names, and
# the marks that join list items as they do ("price of gold, oil", "oil/gas
# prices"). The word tokens drop the marks, so a mark is found by the number
# of tokens before it (_list_marks).
_CONJUNCTIONS: frozenset[str] = frozenset({"and", "or", "versus", "vs"})
_LIST_MARK_RE = re.compile(r"[,;/&+]")


def _list_marks(query: str) -> frozenset[int]:
    """The token indexes the query's list marks stand before: "oil, gold"
    holds one before "gold", and a mark after the last token stands before
    the number of tokens."""
    return frozenset(
        len(_WORD_TOKEN_RE.findall(query[:mark.start()].lower()))
        for mark in _LIST_MARK_RE.finditer(query)
    )


def _ends_phrase(
    tokens: list[str], index: int, subject: PricedSubject, marks: frozenset[int] = frozenset(),
) -> bool:
    """Whether the subject's noun phrase before the token index ends there: at
    the end of the query, a list mark, a number, a currency code or a word that
    may stand before a price ("price of oil March 2020", "price of oil affects
    inflation"), also past a place or a grade or origin of the subject that
    stands there ("price of oil Germany", "price of crude Texas")."""
    if index >= len(tokens) or index in marks:
        return True
    token = tokens[index]
    if token[:1].isdigit() or token in _PRICE_LEADS or token.upper() in _KNOWN_CURRENCIES:
        return True
    place = _place_end(tokens, index)
    if place is None and (token in _PLACE_WORDS or token in subject.modifiers):
        place = index + 1
    return place is not None and _ends_phrase(tokens, place, subject, marks)


def _names_instrument(tokens: list[str], index: int, marks: frozenset[int] = frozenset()) -> bool:
    """Whether the noun phrase going on at the token index names a traded
    instrument, also as the unit after "per" ("oil price per share"); a list
    mark ends the phrase."""
    while index < len(tokens):
        if index in marks:
            return False
        if tokens[index] in _INSTRUMENTS:
            return True
        if tokens[index] == "per":
            index += 1
            while index < len(tokens) and tokens[index] in _DETERMINERS:
                index += 1
            continue
        if tokens[index] in _FUNCTION_WORDS:
            return False
        index += 1
    return False


def _qualifies_run(token: str, subject: PricedSubject) -> bool:
    """Whether a word may stand between a price's link and its subject's run."""
    return (
        token in _DETERMINERS or token in _PRICE_QUALIFIERS or token in subject.modifiers
        or token in _PLACE_WORDS or token == "s"
    )


def _takes_modifiers(run: list[str], subject: PricedSubject) -> bool:
    """Whether places and the subject's modifiers may precede the run."""
    return not subject.modified_heads or any(
        _phrase_spans(run, head) for head in subject.modified_heads
    )


def _place_start(tokens: list[str], end: int) -> int | None:
    """Where a place name of more than one word, ending at the token index, starts."""
    if end <= 0 or tokens[end - 1] not in _PLACE_LASTS:
        return None
    return _phrase_start(tokens, end, _PLACE_PHRASES)


def _place_end(tokens: list[str], start: int) -> int | None:
    """Where a place name of more than one word, starting at the token index, ends."""
    if start >= len(tokens) or tokens[start] not in _PLACE_FIRSTS:
        return None
    return _phrase_end(tokens, start, _PLACE_PHRASES)


def _qualifier_start(tokens: list[str], end: int, subject: PricedSubject) -> int:
    """Where the words that may qualify a run, ending at the token index, start."""
    while end > 0:
        place = _place_start(tokens, end)
        if place is not None:
            end = place
        elif _qualifies_run(tokens[end - 1], subject):
            end -= 1
        else:
            return end
    return end


def _qualifier_end(tokens: list[str], start: int, subject: PricedSubject) -> int:
    """Where the words that may qualify a run, starting at the token index, end."""
    while start < len(tokens):
        place = _place_end(tokens, start)
        if place is not None:
            start = place
        elif _qualifies_run(tokens[start], subject):
            start += 1
        else:
            return start
    return start


def _phrase_end(tokens: list[str], start: int, phrases: Iterable[str]) -> int | None:
    """Where the longest of the phrases starting at the token index ends."""
    for phrase in sorted(phrases, key=lambda phrase: -len(_phrase_forms(phrase))):
        forms = _phrase_forms(phrase)
        end = start + len(forms)
        if start >= 0 and end <= len(tokens) and all(
            tokens[start + k] in forms[k] for k in range(len(forms))
        ):
            return end
    return None


def _phrase_start(tokens: list[str], end: int, phrases: Iterable[str]) -> int | None:
    """Where the longest of the phrases ending at the token index starts."""
    for phrase in sorted(phrases, key=lambda phrase: -len(_phrase_forms(phrase))):
        start = end - len(_phrase_forms(phrase))
        if _phrase_end(tokens, start, (phrase,)) == end:
            return start
    return None


def _subject_runs(tokens: list[str], subject: PricedSubject) -> list[tuple[int, int]]:
    """Runs of the subject's words that hold one of its heads and no other subject."""
    runs: list[tuple[int, int]] = []
    index = 0
    while index < len(tokens):
        start = index
        while index < len(tokens) and (tokens[index] in subject.words
                                       or tokens[index] in subject.others):
            index += 1
        if index == start:
            index += 1
            continue
        run = tokens[start:index]
        if not any(token in subject.others for token in run) and any(
            _phrase_spans(run, head) for head in subject.heads
        ):
            runs.append((start, index))
    return runs


def _leads_price(
    tokens: list[str], index: int, subject: PricedSubject, marks: frozenset[int] = frozenset(),
) -> bool:
    """Whether the word at the token index may stand before a price word that
    reaches its subject: none, a list mark between them, a number, a currency
    code, a word of asking, a verb that acts on a price, an event, a phrase
    end, a qualifier, a place, a grade or origin of the subject, or a word of
    the subject in a run that holds no other subject ("freight rates for
    trucking", not "palm oil price crude")."""
    if index < 0 or index + 1 in marks:
        return True
    token = tokens[index]
    if (token[:1].isdigit() or token in _PRICE_LEADS or token.upper() in _KNOWN_CURRENCIES
            or token in _PLACE_WORDS or token in subject.modifiers
            or _place_start(tokens, index + 1) is not None):
        return True
    while index >= 0 and (tokens[index] in subject.words or tokens[index] in subject.others):
        if tokens[index] in subject.others:
            return False
        index -= 1
    return token in subject.words


def _claims_price(
    tokens: list[str], index: int, subject: PricedSubject, ended: bool = False,
) -> bool:
    """Whether the words after a price word that follows its run claim the
    price: the price's own "of" ("truckload price of corn") or another subject
    after its link ("crude prices for palm oil"). With ended, the phrase after
    the "of" counts as ended."""
    if index >= len(tokens) or tokens[index] not in _PRICE_LINKS:
        return False
    start = end = _qualifier_end(tokens, index + 1, subject)
    while end < len(tokens) and (tokens[end] in subject.words or tokens[end] in subject.others):
        end += 1
    if any(token in subject.others for token in tokens[start:end]):
        return True
    return tokens[index] == "of" and not ended and not _ends_phrase(tokens, end, subject)


def _price_after(
    tokens: list[str], start: int, end: int, subject: PricedSubject,
    marks: frozenset[int] = frozenset(), ended: bool = False,
) -> list[tuple[int, int]] | None:
    """The span naming a run whose price word follows it ("trucking rates",
    "truckload spot rates", "oil's price")."""
    index = end + 1 if tokens[end:end + 1] == ["s"] else end
    while index < len(tokens) and tokens[index] in _PRICE_QUALIFIERS:
        index += 1
    price_end = _phrase_end(tokens, index, subject.prices)
    if (price_end is None or _names_instrument(tokens, price_end, marks)
            or _claims_price(tokens, price_end, subject, ended)):
        return None
    return [(start, price_end)]


def _price_before(
    tokens: list[str], start: int, end: int, subject: PricedSubject,
    marks: frozenset[int] = frozenset(), ended: bool = False,
) -> list[tuple[int, int]] | None:
    """The spans naming a run whose price word precedes it ("price of oil",
    "rates for spot trucking", "price of a barrel of Russian crude", "price
    history of oil", "PPI trucking"). With ended, the run's phrase counts as
    ended after it."""
    ends = ended or _ends_phrase(tokens, end, subject, marks)
    if not ends or _names_instrument(tokens, end, marks):
        return None
    index = phrase_start = _qualifier_start(tokens, start, subject)
    if not _takes_modifiers(tokens[start:end], subject) and any(
        token not in _DETERMINERS and token not in _PRICE_QUALIFIERS and token != "s"
        for token in tokens[phrase_start:start]
    ):
        return None
    if index > 1 and tokens[index - 1] == "of" and tokens[index - 2] in subject.measures:
        index -= 2
        if index > 0 and tokens[index - 1] in _DETERMINERS:
            index -= 1
    if index > 0 and tokens[index - 1] in _PRICE_LINKS:
        index -= 1
    linked = index
    price_start = _phrase_start(tokens, index, subject.prices)
    while price_start is None and index > 0 and tokens[index - 1] in _SERIES_WORDS:
        index -= 1
        price_start = _phrase_start(tokens, index, subject.prices)
    if price_start is None or not _leads_price(tokens, price_start - 1, subject, marks):
        return None
    return [(price_start, index), (linked, phrase_start), (start, end)]


def _priced_spans(
    tokens: list[str], subject: PricedSubject, marks: frozenset[int] = frozenset(),
) -> list[tuple[int, int]]:
    """Token spans where the query names the subject as the subject of a price.

    The spans hold the run, its price word and the link between them. The
    words that qualify the run before it and the series words after a
    preceding price ("price history of oil") stay outside, so they keep their
    own score and a place still credits its own national source.
    """
    spans: list[tuple[int, int]] = []
    for start, end in _subject_runs(tokens, subject):
        pieces = (_price_after(tokens, start, end, subject, marks)
                  or _price_before(tokens, start, end, subject, marks) or [])
        spans += [(first, last) for first, last in pieces if first < last]
    return spans


def _joined(
    tokens: list[str], start: int, end: int, subject: PricedSubject,
    marks: frozenset[int] = frozenset(),
) -> bool:
    """Whether a run is joined to another word by "and", "or", "versus" or a
    list mark: a conjunction stands before the words that qualify the run, or
    after the run, past an "'s" and words that qualify a price; a list mark
    stands anywhere from the first of those words to the last. A list mark
    after a run whose price precedes it only ends the run's phrase ("price of
    oil, today")."""
    before = _qualifier_start(tokens, start, subject)
    after = end + 1 if tokens[end:end + 1] == ["s"] else end
    while after < len(tokens) and tokens[after] in _PRICE_QUALIFIERS:
        after += 1
    if before > 0 and tokens[before - 1] in _CONJUNCTIONS:
        return True
    if after < len(tokens) and tokens[after] in _CONJUNCTIONS:
        return True
    if any(0 < mark < end for mark in marks if mark >= before):
        return True
    return (any(end <= mark <= after for mark in marks if mark < len(tokens))
            and _price_before(tokens, start, end, subject, marks) is None)


def _goes_on(
    tokens: list[str], start: int, end: int, subject: PricedSubject,
    marks: frozenset[int] = frozenset(),
) -> bool:
    """Whether a price reaches the run only where the phrase going on past it
    counts as ended: after a run whose price precedes it ("price of oil
    California") or after the "of" of a price that follows it ("oil price of
    California"). A traded instrument or another subject there still claims
    the price."""
    def reaches(ended: bool) -> bool:
        return bool(_price_after(tokens, start, end, subject, marks, ended)
                    or _price_before(tokens, start, end, subject, marks, ended))

    return not reaches(False) and reaches(True)


# Wording that asks for something besides the spot price, where a name that
# points to a commodities_ price operation stays silent. Futures ("WTI
# futures", "Brent futures curve") answer from the futures operations, which
# carry the benchmark names as keywords; stocks, output and trade ("US crude
# oil inventory", "European gas storage") answer from their own operations.
_NOT_SPOT_PRICE_WORDS: tuple[str, ...] = (
    "futures", "contract", "front month", "curve", "expiry",
    "inventory", "inventories", "stock", "stockpile", "storage", "reserve",
    "production", "output", "export", "import", "consumption", "demand", "supply",
    "rig count",
)

# The market a benchmark is priced in. A national source of another country is
# never its answer; "EU" is no source's country, so for a European benchmark
# every national source steps down.
NAMED_PLACES: dict[str, str] = {
    "ttf": "NL", "title transfer facility": "NL", "dutch gas": "NL",
    "european gas": "EU", "european natural gas": "EU", "eu gas": "EU",
    "europe gas": "EU", "natural gas europe": "EU", "gas price europe": "EU",
    "gas price in europe": "EU",
    "henry hub": "US",
}

# Major container ports -> ISO2 country. A port named beside a shipping cue
# asks for port activity, and the single-country port source of another
# country is never its answer.
PORT_COUNTRIES: dict[str, str] = {
    "rotterdam": "NL", "antwerp": "BE", "hamburg": "DE", "bremerhaven": "DE",
    "felixstowe": "GB", "le havre": "FR", "valencia": "ES", "algeciras": "ES",
    "barcelona": "ES", "piraeus": "GR", "genoa": "IT", "gdansk": "PL",
    "tanger med": "MA", "tangier": "MA", "jebel ali": "AE", "dubai": "AE",
    "jeddah": "SA", "colombo": "LK", "mumbai": "IN", "nhava sheva": "IN",
    "durban": "ZA", "mombasa": "KE",
    "shanghai": "CN", "ningbo": "CN", "shenzhen": "CN", "qingdao": "CN",
    "tianjin": "CN", "guangzhou": "CN", "xiamen": "CN", "dalian": "CN",
    "hong kong": "HK", "singapore": "SG", "busan": "KR", "kaohsiung": "TW",
    "port klang": "MY", "klang": "MY", "tanjung pelepas": "MY", "laem chabang": "TH",
    "yokohama": "JP", "tokyo": "JP",
    "los angeles": "US", "long beach": "US", "savannah": "US", "houston": "US",
    "new york": "US", "santos": "BR", "vancouver": "CA", "callao": "PE",
}
PORT_OPERATIONS: tuple[str, ...] = ("transport_ports_congestion",)
_PORT_CUES: tuple[str, ...] = (
    "port", "seaport", "harbor", "harbour", "ship", "shipping", "shipment",
    "vessel", "container", "cargo", "berth", "teu", "maritime", "tanker",
)
_PORT_ACTIVITY_WORDS: tuple[str, ...] = (
    "busy", "busiest", "congested", "congestion", "throughput", "backlog", "queue",
    "waiting",
)


@dataclass(frozen=True)
class NamedRequest:
    """The operations a query names in everyday words."""

    operations: dict[str, str]   # operation_id -> the name that points to it
    countries: frozenset[str]    # where the named benchmark or port is
    words: frozenset[str]        # the query tokens of those names
    # operation_id -> the query tokens of the names that point to it
    operation_words: dict[str, frozenset[str]]


def detect_named_operations(query: str) -> NamedRequest:
    """Benchmarks, waterways, measures and ports the query names.

    A port counts only beside a shipping cue ("ship calls at Rotterdam"),
    because most of these ports are also cities: "weather in Rotterdam" and
    "traffic congestion in Los Angeles" name no port. A shipping cue beside an
    activity word asks for port activity without a name ("port congestion",
    "how busy are ports"); "busy airports" asks nothing of a seaport.
    """
    tokens = _WORD_TOKEN_RE.findall(query.lower())
    not_spot = any(_phrase_spans(tokens, word) for word in _NOT_SPOT_PRICE_WORDS)
    marks = _list_marks(query)
    joined = {
        subject.operation
        for subject in PRICED_SUBJECTS
        if any(_joined(tokens, start, end, subject, marks)
               or (subject.open_phrases and _goes_on(tokens, start, end, subject, marks))
               for start, end in _subject_runs(tokens, subject))
    }
    operations: dict[str, str] = {}
    countries: set[str] = set()
    words: set[str] = set()
    operation_words: dict[str, set[str]] = {}
    names = [
        (start, end, name, NAMED_OPERATIONS[name])
        for start, end, name in _claim_names(_blank(tokens, _NAME_BLOCKERS), NAMED_OPERATIONS)
        if name not in _SUBJECT_NAMES or _SUBJECT_NAMES[name] in joined
    ] + [
        (start, end, subject.label, (subject.operation,))
        for subject in PRICED_SUBJECTS
        if subject.operation not in joined
        for start, end in _priced_spans(tokens, subject, marks)
    ]
    for start, end, name, named in sorted(names):
        cues = _NAME_CUES.get(name, ())
        if cues and not any(_phrase_spans(tokens, cue) for cue in cues):
            continue
        targets = [op for op in named if not (not_spot and op.startswith("commodities_"))]
        if not targets:
            continue
        for op in targets:
            operations.setdefault(op, name)
            operation_words.setdefault(op, set()).update(tokens[start:end])
        if name in NAMED_PLACES:
            countries.add(NAMED_PLACES[name])
        words.update(tokens[start:end])

    ports = _claim_names(tokens, PORT_COUNTRIES)
    cue = any(_phrase_spans(tokens, word) for word in _PORT_CUES)
    activity = any(_phrase_spans(tokens, word) for word in _PORT_ACTIVITY_WORDS)
    if cue and (ports or activity):
        for op in PORT_OPERATIONS:
            operations.setdefault(op, ports[0][2] if ports else "port")
        for start, end, name in ports:
            countries.add(PORT_COUNTRIES[name])
            words.update(tokens[start:end])
            for op in PORT_OPERATIONS:
                operation_words.setdefault(op, set()).update(tokens[start:end])
    return NamedRequest(
        operations, frozenset(countries), frozenset(words),
        {op: frozenset(found) for op, found in operation_words.items()},
    )


# The operation a topic word means when the query asks nothing narrower:
# "weather in Paris" and "Paris weather" ask for the forecast. It comes first
# among the operations the query's words score equally - the word "weather"
# alone scores nine operations equally, and their operation_id order put the
# Hong Kong Observatory first.
TOPIC_DEFAULT_OPERATIONS: dict[str, str] = {"weather": "v2_weather_forecast"}

# Operations named by a compound some of whose words are everyday words of their
# own, as (the words that make it the compound, those words): "space weather"
# is solar activity, so the word "weather" finds these operations only when the
# query also says "space"; and "real" in "real wages" or "real GDP" means
# adjusted for inflation, so it finds the real-estate operations only beside a
# word for property, as in "real estate" or "real home prices". The changes to
# an ETF's top holdings answer "top" and "holdings" only beside a word for
# change, and a word for change only beside "top" or "holdings": "VOO's top
# holdings" asks for the holdings, not how they moved between two dates, and
# "SPY changes" asks nothing about holdings.
COMPOUND_NAMED_OPERATIONS: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "space_weather_": (("space",), ("weather",)),
    "real_estate_": (
        ("estate", "realty", "property", "properties", "home", "homes",
         "house", "houses", "housing"),
        ("real",),
    ),
    "etf_symbol_top_holdings_changes": (
        ("change", "changes", "changed", "churn"),
        ("top", "holdings"),
    ),
    "etf_symbol_top_holdings_": (
        ("top", "holdings"),
        ("change", "changes", "changed", "churn"),
    ),
}

# Words an operation's summary names as the measures it is computed from,
# never as its subject: the telecom-demand heuristic is computed from growth,
# income and inflation, and "Dutch inflation" found it above every Dutch
# inflation series.
OPERATION_INPUT_WORDS: dict[str, frozenset[str]] = {
    "imf_signals_telecom_demand_country": frozenset({"growth", "income", "inflation"}),
}


def topic_default_operations(query: str) -> frozenset[str]:
    """The default operations of the topic words the query names."""
    tokens = _WORD_TOKEN_RE.findall(query.lower())
    return frozenset(
        operation for word, operation in TOPIC_DEFAULT_OPERATIONS.items()
        if _phrase_spans(tokens, word)
    )


# A question about a listing that no other word narrows asks for its price,
# for its price history when it names a period of days or longer ("TLT since
# 2020", "AAPL over the last five years"), and for the prices of several
# listings when it names several ("TLT SPY"). The ticker scores some fifty
# listing operations equally, and their operation_id order put the dividends
# and splits first. "since" and "ago" alone name no such period ("AAPL since
# open", "15 minutes ago"), and a year counts only after a word that dates
# ("since 2020", "in 2008"), so a name holding a number ("IWM Russell 2000")
# names none.
LISTING_DEFAULT_OPERATION = "quotes_symbol_price"
LISTING_HISTORY_OPERATION = "quotes_symbol_historical"
LISTINGS_DEFAULT_OPERATION = "quotes_symbol_multiple"
_PERIOD_WORDS: tuple[str, ...] = (
    "days", "weeks", "months", "years", "decade", "over time",
    "last week", "past week", "week ago", "last month", "past month",
    "month ago", "last year", "past year", "year ago",
)
_YEAR_RE = re.compile(r"(?:19|20)\d\d")
_YEAR_PREPOSITIONS = frozenset({
    "since", "from", "in", "after", "before", "until", "between", "through",
    "during",
})


def query_names_a_period(query: str) -> bool:
    """Whether the query names a period of days or longer: a year after a
    word that dates, or a period word."""
    tokens = _WORD_TOKEN_RE.findall(query.lower())
    return any(
        _YEAR_RE.fullmatch(token) and index > 0
        and tokens[index - 1] in _YEAR_PREPOSITIONS
        for index, token in enumerate(tokens)
    ) or any(_phrase_spans(tokens, word) for word in _PERIOD_WORDS)


# An everyday weather question names the worldwide forecast, or the history
# when it asks about the past: "temperature in Dubai", "will it rain in Rome
# tomorrow", "past weather in London". Its words alone found NOAA water
# temperature, a climate projection, the Hong Kong Observatory ("current
# weather") and the forecast's past_days parameter first. A query asks one
# only when it asks nothing else: each of its words is a weather, time or
# question word or names a place, so "marine weather", "weather alerts in
# Texas" and "temperature anomaly" keep their own operations. "weather" asks
# by itself; the other weather words ask only beside a place or a time.
# "forecast" and "wind" ask nothing by themselves: they also name an economic
# forecast ("forecast for Germany") and wind power ("wind in Germany").
WEATHER_FORECAST_OPERATION = "v2_weather_forecast"
WEATHER_HISTORY_OPERATION = "v2_weather_history"
_WEATHER_WORDS: tuple[str, ...] = (
    "weather", "temperature", "rain", "raining", "rainy", "rainfall",
    "snow", "snowing", "snowy", "snowfall", "windy", "wind speed",
    "sunny", "cloudy", "foggy", "humid", "humidity", "precipitation",
    "hot", "cold", "warm", "freezing",
)
_WEATHER_TIME_WORDS: tuple[str, ...] = (
    "now", "right now", "today", "tonight", "tomorrow", "current", "currently",
    "morning", "afternoon", "evening", "night", "day", "week", "weekend",
    "next", "coming", "few days", "hourly", "daily",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)
_WEATHER_PAST_WORDS: tuple[str, ...] = (
    "past", "yesterday", "history", "historical", "ago",
    "last night", "last week", "last weekend", "last month", "last year",
)
_WEATHER_QUESTION_WORDS: tuple[str, ...] = (
    "forecast", "like", "conditions", "outlook", "report", "going", "chance", "expected",
)
# A word of three letters or more right after one of these names a place,
# capitalized or not ("in berlin"); a shorter one is more often a state or a
# code ("in NY") than a town.
_PLACE_PREPOSITIONS: frozenset[str] = frozenset({"in", "at", "for"})
# Countries with a weather service of their own among the catalog's sources,
# the Hong Kong Observatory and the US National Weather Service: a question
# that names one of them is that service's to answer. The Observatory's own
# words find it ("Hong Kong weather"); the Weather Service's do not ("weather
# in USA" found port congestion first), so a US question names its forecast.
# "us" names the United States here, as in the search filler rule. The past
# is the worldwide history's in every country.
NATIONAL_WEATHER_COUNTRIES: frozenset[str] = frozenset({"HK", "US"})
US_WEATHER_OPERATION = "weather_us_forecast"


@dataclass(frozen=True)
class WeatherRequest:
    """A weather question asked in everyday words."""

    operation: str               # the forecast, the history, or the US forecast
    name: str                    # what the question asks, for the search reason
    words: frozenset[str]        # the query tokens of its weather and time words


def detect_weather_request(query: str, terms: Iterable[str]) -> WeatherRequest | None:
    """The everyday weather question the query asks, or None.

    ``terms`` are the query's search terms, its filler words already dropped;
    each must be a weather, time or question word or name a place. A place is
    a country, a port city, a word of three letters or more right after "in",
    "at" or "for", or a capitalized word. A capital marks a place only beside
    lowercase words: in a query written all in capitals or all in title case
    ("Weather Station Observations") it marks nothing.
    """
    raw_tokens = re.findall(r"[A-Za-z0-9]+", query)
    tokens = [token.lower() for token in raw_tokens]
    term_set = set(terms)

    def covered(phrases: Iterable[str]) -> set[int]:
        return {
            index
            for phrase in phrases
            for start, end in _phrase_spans(tokens, phrase)
            for index in range(start, end)
        }

    weather = covered(_WEATHER_WORDS)
    times = covered(_WEATHER_TIME_WORDS)
    past = covered(_WEATHER_PAST_WORDS)
    known = weather | times | past | covered(_WEATHER_QUESTION_WORDS)
    names = _claim_names(tokens, (*COUNTRY_QUERY_TERMS, *PORT_COUNTRIES))
    places = {index for start, end, _ in names for index in range(start, end)}
    us_places = {
        index
        for start, end, name in names if COUNTRY_QUERY_TERMS.get(name) == "US"
        for index in range(start, end)
    }
    us_places.update(index for index, token in enumerate(tokens) if token == "us")
    places |= us_places
    places.update(
        index for index in range(1, len(tokens))
        if tokens[index - 1] in _PLACE_PREPOSITIONS
        and tokens[index].isalpha() and len(tokens[index]) >= 3
        and tokens[index] in term_set
    )
    if any(raw.islower() and len(raw) >= 3 for raw in raw_tokens):
        places.update(
            index for index, raw in enumerate(raw_tokens)
            if raw[0].isupper() and raw[1:].islower() and tokens[index] in term_set
        )
    places -= known
    if not weather or not term_set <= {tokens[index] for index in known | places}:
        return None
    if not (places or times or past or _phrase_spans(tokens, "weather")):
        return None
    words = frozenset(tokens[index] for index in known)
    if past:
        return WeatherRequest(WEATHER_HISTORY_OPERATION, "past weather", words)
    national = detect_query_countries(query) & NATIONAL_WEATHER_COUNTRIES
    if us_places:
        national |= {"US"}
    if "HK" in national:
        return None
    # The United States alone; beside another place ("South America weather",
    # "weather in Miami USA") the worldwide forecast answers.
    if national and places <= us_places:
        return WeatherRequest(US_WEATHER_OPERATION, "US weather", words)
    return WeatherRequest(WEATHER_FORECAST_OPERATION, "weather", words)


def detect_tickers(query: str) -> list[str]:
    """Return likely stock ticker tokens (e.g. AAPL, MSFT, BRK.A) found in the raw query.

    Inverted gate: a 2-5 uppercase token is a ticker ONLY when the query
    carries equity-context vocabulary (price, stock, dividend, ...) or the
    token is on the short high-liquidity whitelist (AAPL, SPY, ...). The
    _NON_TICKER_WORDS hard list (CPI, USD, IMF, ...) always wins. So "AI
    revolution" and "search FRED series" stay non-equity while "AI stock
    price" and bare "NVDA today" land on quotes_symbol_*.
    """
    matches = TICKER_TOKEN_RE.findall(query)
    if not matches:
        return []

    # Inverted gate: equity context (or the high-liquidity whitelist)
    # ADMITS a ticker-shaped token; the hard NEVER list still wins over both.
    # The old default-allow blacklist misread FRED, AIS, RF, MMSI, IMF and
    # every future acronym as equities until someone patched the list again.
    has_equity_context = query_has_equity_context(query)
    # Sole-substantive-token rule: a bare 'PLTR' (optionally
    # with temporal filler) is a quote lookup - there is no other intent the
    # query could carry. Acronym safety is preserved: multi-token queries
    # ('search FRED series for gold') still require context or whitelist.
    # Judge sole-ness on what REMAINS after removing the ticker
    # match itself - the word tokenizer splits dotted class shares (HEI.A)
    # into two tokens and wrongly disqualified them.
    remainder = query
    if len(matches) == 1:
        remainder = remainder.replace(matches[0], " ", 1)
    leftover = [
        t for t in _WORD_TOKEN_RE.findall(remainder.lower())
        if t not in _BARE_TICKER_FILLER
    ]
    sole = len(matches) == 1 and not leftover
    result: list[str] = []
    for token in matches:
        if token in _NON_TICKER_WORDS:
            continue
        if token in _TICKER_WHITELIST or has_equity_context or sole:
            result.append(token)
    return result


def query_has_equity_context(query: str) -> bool:
    """True when the query carries explicit equity vocabulary (stock, price, ...).

    Token-bounded: "Stockholm" must not satisfy "stock" -
    a substring match here re-admitted IP/NAT as tickers and disabled the
    network-domain suppression for clearly network queries. Public because
    search uses it as an override: explicit equity wording keeps the ticker
    boost alive even when network-domain terms dominate.
    """
    return bool(_match_vocabulary(query, _EQUITY_CONTEXT_TERMS))


def detect_currency_pairs(query: str) -> list[tuple[str, str]]:
    """Return currency-pair tuples (base, quote) where both are recognised ISO codes."""
    pairs: list[tuple[str, str]] = []
    for m in CURRENCY_PAIR_RE.finditer(query):
        base, quote = m.group(1), m.group(2)
        if base in _KNOWN_CURRENCIES and quote in _KNOWN_CURRENCIES and base != quote:
            pairs.append((base, quote))
    return pairs


# US-context tokens: standalone (not as part of words like "USD" or "USA1234").
_US_CONTEXT_PATTERN = re.compile(
    r"(?<![a-zA-Z0-9])(?:US|USA|U\.S\.|U\.S\.A\.|United States|American)(?![a-zA-Z0-9])",
    re.IGNORECASE,
)


def query_names_united_states(query: str) -> bool:
    """True when the query names the United States as a standalone word.

    "US nonfarm payrolls" names it while the country vocabulary does not:
    a bare uppercase code counts as a country there only beside a macro cue.
    """
    return bool(_US_CONTEXT_PATTERN.search(query))


# Macro-data keywords that, combined with US context, indicate the user wants
# US-specific macroeconomic data from a primary source (FRED). Kept narrow on
# purpose - we don't want generic words like "data" or "rate" alone triggering
# the boost.
_US_MACRO_KEYWORDS: tuple[str, ...] = (
    "cpi", "inflation", "ppi", "deflator",
    "gdp", "gross domestic product",
    "unemployment", "jobless", "labor force",
    "treasury yield", "treasury rate", "yield curve",
    "fed funds", "federal funds", "money supply", "m1", "m2",
    "consumer price", "producer price",
    "industrial production", "retail sales",
    "personal income", "personal consumption", "pce",
)


def detect_us_macro_query(query: str) -> bool:
    """Return True when the query asks for US-specific macroeconomic data.

    Both signals must be present: (1) US-context token (US, USA, United States,
    American) as a standalone word, and (2) at least one US-macro keyword (CPI,
    GDP, unemployment, Treasury, federal funds, money supply, etc.). This
    keeps generic queries like "US news" or "GDP forecast Germany" from
    triggering the FRED boost.
    """
    if not query_names_united_states(query):
        return False
    lowered = query.lower()
    return any(keyword in lowered for keyword in _US_MACRO_KEYWORDS)


def matching_central_bank_prefixes(query: str) -> list[str]:
    """Return operation_id prefixes to boost when query references a specific central bank.

    Each symbol must appear as a whole-token match (word boundaries) to avoid
    false positives such as "CNBC news" matching the cnb_ prefix, "Boca Raton"
    matching boc_, or "federal debt" matching fed_. Multi-word symbols like
    "federal reserve" are matched as token-bounded phrases.
    """
    normalized = " ".join(query.lower().split())
    upper_tokens = set(re.findall(r"\b[A-Z]{2,}\b", query))
    prefixes: list[str] = []
    seen: set[str] = set()
    for symbol, prefix in CENTRAL_BANK_PREFIX_BOOSTS.items():
        if prefix in seen:
            continue
        # Whole-token boundary match in the lowercase form.
        symbol_lower = symbol.lower()
        token_pattern = rf"(?<![a-z0-9]){re.escape(symbol_lower)}(?![a-z0-9])"
        if re.search(token_pattern, normalized) or symbol.upper() in upper_tokens:
            prefixes.append(prefix)
            seen.add(prefix)
    return prefixes


# Words that ask for a central bank's policy rate. Beside the bank's name, with
# nothing else asked, they name the operation that holds it: "Bank of Canada
# rate decision" found the prime rate, which the commercial banks set, and the
# Bank Rate, a quarter point above the policy rate. A query that asks more
# ("SARB prime interest rate", "Norges Bank interest rate swaps", "CNB policy
# rate history") asks for another series of the bank and is ranked as before.
POLICY_RATE_PHRASES: tuple[str, ...] = ("rate decision", "policy rate", "interest rate")
# The operation that holds each bank's policy rate, by the bank's prefix.
CENTRAL_BANK_POLICY_RATES: dict[str, str] = {
    "fed_": "fed_rates_rate_type",
    "boj_": "boj_rates",
    "boe_": "boe_rate",
    "boc_": "boc_policy_rate",
    "rba_": "rba_cash_rate",
    "snb_": "snb_policy_rate",
    "riksbank_": "riksbank_policy_rate",
    "central_banks_sarb_": "central_banks_sarb_policy_rate",
    "bnm_": "bnm_opr",
    "norges_bank_": "norges_bank_policy_rate",
    "cnb_": "cnb_policy_rate",
    "bcb_": "bcb_selic",
    "central_banks_bcrp_": "central_banks_bcrp_policy_rate",
    "central_banks_bcra_": "central_banks_bcra_policy_rate",
}
# The ECB has no policy-rate operation: its policy rate, the deposit facility
# rate, is a curated series of the country macro operation, and the name of
# that rate asks for it as well.
CENTRAL_BANK_POLICY_RATE_KEYS: dict[str, str] = {"ecb_": "eu/ecbdfr"}
_BANK_POLICY_RATE_PHRASES: dict[str, tuple[str, ...]] = {
    "ecb_": ("deposit rate", "deposit facility rate", "deposit facility"),
}
# A policy rate asked about by its date is a meeting's question ("when is the
# next Fed rate decision"), and the meeting calendar answers it for the banks
# it covers.
_DATE_WORDS: tuple[str, ...] = ("when", "next", "upcoming", "date", "schedule", "calendar")


@dataclass(frozen=True)
class PolicyRateRequest:
    """The policy rates a query asks of the central banks it names."""

    operations: dict[str, str]   # operation_id -> the words that ask for it
    keys: frozenset[str]         # the curated macro keys that hold them
    words: frozenset[str]        # the query tokens of those words


def detect_policy_rate_request(
    query: str, terms: Iterable[str], prefixes: Iterable[str],
) -> PolicyRateRequest:
    """The policy rates the query asks of the banks it names by these prefixes.

    ``terms`` are the query's search terms, its filler words already dropped,
    and ``prefixes`` the central-bank prefixes matching_central_bank_prefixes
    found in the query. The query asks for policy rates only when each term is
    a word of a bank's name, of a policy-rate phrase or a date word.
    """
    tokens = _WORD_TOKEN_RE.findall(query.lower())
    prefixes = list(prefixes)

    def covered(phrases: Iterable[str]) -> set[str]:
        return {
            tokens[index]
            for phrase in phrases
            for start, end in _phrase_spans(tokens, phrase)
            for index in range(start, end)
        }

    names = covered(name for name, prefix in CENTRAL_BANK_PREFIX_BOOSTS.items()
                    if prefix in prefixes)
    dates = covered(_DATE_WORDS)
    operations: dict[str, str] = {}
    keys: set[str] = set()
    words: set[str] = set()
    for prefix in prefixes:
        asked = [
            phrase
            for phrase in (*POLICY_RATE_PHRASES, *_BANK_POLICY_RATE_PHRASES.get(prefix, ()))
            if _phrase_spans(tokens, phrase)
        ]
        if not asked:
            continue
        if dates and prefix in _MEETING_CALENDAR_PREFIXES:
            for operation in sorted(MEETING_CALENDAR_OPERATIONS):
                operations.setdefault(operation, asked[0])
        elif prefix in CENTRAL_BANK_POLICY_RATES:
            operations.setdefault(CENTRAL_BANK_POLICY_RATES[prefix], asked[0])
        elif prefix in CENTRAL_BANK_POLICY_RATE_KEYS:
            keys.add(CENTRAL_BANK_POLICY_RATE_KEYS[prefix])
        else:
            continue
        words |= covered(asked)
    if set(terms) - names - words - dates:
        return PolicyRateRequest({}, frozenset(), frozenset())
    return PolicyRateRequest(operations, frozenset(keys), frozenset(words))
