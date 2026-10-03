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
    "bcra": "bcra_",
    "argentina central bank": "bcra_",
    "rbi": "rbi_",
    "reserve bank of india": "rbi_",
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
    # Index ETFs
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO",
})

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
    "commodities_agriculture_grains": "US", "commodities_energy_natural_": "US",
    "congress_amendments_": "US", "congress_committee_": "US", "congress_committees_": "US",
    "congress_communications_": "US", "congress_hearings": "US", "congress_laws": "US",
    "congress_members_": "US", "congress_nominations": "US", "congress_record": "US",
    "congress_sessions": "US", "congress_summaries": "US",
    "energy_retail_": "US", "energy_tariffs": "US", "energy_utilities_": "US",
    "environment_usgs_": "US", "equities_sp500_": "US", "etf_flows_": "US",
    "etf_sectors_": "US", "fixed_income_treasury_": "US", "macro_net_liquidity": "US",
    "macro_regime": "US", "maritime_history_": "US", "markets_equity_": "US", "multpl_": "US",
    "post_congress_": "US", "short_interest_": "US", "treasury_auctions": "US",
    "treasury_daily_": "US", "treasury_debt_": "US", "treasury_deficit": "US",
    "treasury_gold": "US", "treasury_interest_": "US", "treasury_rates": "US",
    "usaspending_agencies": "US", "usaspending_agency_": "US", "usaspending_budget_": "US",
    "usaspending_last_": "US", "usaspending_spending_": "US", "weather_nws_aviation_": "US",
    "weather_nws_forecast_": "US", "weather_nws_office_": "US", "weather_nws_point": "US",
    "weather_nws_zones": "US", "weather_us_alerts": "US", "weather_us_forecast_": "US",
    # Port and vessel sources of one country: Fintraffic Portnet covers
    # Finnish ports only, and the NOAA AIS history (the successor of
    # maritime_history_) covers United States waters only. Untagged, Portnet
    # ranked first for ship calls at Rotterdam.
    "transport_ports_port_calls": "FI", "transport_vessels_history_": "US",
}
# The dead-prefix test (tests/test_search_relevance.py) guards this map: every
# entry must match at least one bundled operation, so a source rename or
# removal fails loudly instead of silently disarming the geography penalty.

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
# Matches "EUR/USD", "EURUSD", "EUR USD".
CURRENCY_PAIR_RE = re.compile(r"\b([A-Z]{3})[ /\-]?([A-Z]{3})\b")
_KNOWN_CURRENCIES: frozenset[str] = frozenset({
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "CAD", "NZD", "CNY", "INR",
    "RUB", "ZAR", "BRL", "MXN", "SEK", "NOK", "DKK", "PLN", "TRY", "HKD",
    "SGD", "KRW", "TWD", "THB", "IDR", "MYR", "PHP", "ILS", "AED", "SAR",
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


def matching_aliases(query: str) -> dict[str, list[str]]:
    """Alias phrases the query names as whole words.

    The phrase or one of its expansions must occur as consecutive query
    tokens, plural-tolerant. A substring test fired "cot" on "cotton",
    "currency" on "cryptocurrency", "environment" on "environmental" and
    "aqi" on "Iraqi".
    """
    tokens = _WORD_TOKEN_RE.findall(query.lower())
    return {
        phrase: expansions
        for phrase, expansions in ALIASES.items()
        if any(_phrase_spans(tokens, term) for term in (phrase, *expansions))
    }


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

# ISO codes that are also English words when written in lowercase.
_LOWERCASE_CODE_WORDS: frozenset[str] = frozenset({"try", "php", "cad", "sar", "rub"})

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


def detect_fx_request(query: str) -> FxRequest | None:
    """The exchange-rate question the query asks, or None.

    A query asks one when it joins two different currencies as a conversion
    ("dollar to yen", "Turkish lira for one US dollar", "EUR/USD") or names a
    currency beside an exchange-rate cue ("euro exchange rate"). A currency
    named alone ("coffee price in dollars") asks nothing. Crypto queries are
    the caller's to exclude: "convert bitcoin to dollars" is a crypto price.
    """
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


# Everyday names of a benchmark, waterway or measure -> the operations that
# answer it. The catalog spells these names inside prose ("Crude oil prices
# (Brent & WTI)"), as a parameter example ("chokepoint1 for Suez Canal") or
# not at all (TTF is the World Bank's "Natural gas, Europe" series), so the
# token score alone never finds them. A product points to its price only
# with the word price: "crude oil" alone also opens questions about
# pipelines, tankers and stocks.
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
    "turkish straits": ("maritime_chokepoints_activity",),
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
}

# Names that point to their operations only when the query also holds one of
# these words: "trucking freight rates" asks for the trucking price index,
# "trucking employment" does not.
_NAME_CUES: dict[str, tuple[str, ...]] = {
    "trucking": ("freight",),
    "truckload": ("freight",),
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


def detect_named_operations(query: str) -> NamedRequest:
    """Benchmarks, waterways, measures and ports the query names.

    A port counts only beside a shipping cue ("ship calls at Rotterdam"),
    because most of these ports are also cities: "weather in Rotterdam" and
    "traffic congestion in Los Angeles" name no port. A shipping cue beside an
    activity word asks for port activity without a name ("port congestion",
    "how busy are ports"); "busy airports" asks nothing of a seaport.
    """
    tokens = _blank(_WORD_TOKEN_RE.findall(query.lower()), _NAME_BLOCKERS)
    not_spot = any(_phrase_spans(tokens, word) for word in _NOT_SPOT_PRICE_WORDS)
    operations: dict[str, str] = {}
    countries: set[str] = set()
    words: set[str] = set()
    for start, end, name in _claim_names(tokens, NAMED_OPERATIONS):
        cues = _NAME_CUES.get(name, ())
        if cues and not any(_phrase_spans(tokens, cue) for cue in cues):
            continue
        targets = [op for op in NAMED_OPERATIONS[name]
                   if not (not_spot and op.startswith("commodities_"))]
        if not targets:
            continue
        for op in targets:
            operations.setdefault(op, name)
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
    return NamedRequest(operations, frozenset(countries), frozenset(words))


# The operation a topic word means when the query asks nothing narrower:
# "weather in Paris" and "Paris weather" ask for the forecast. It comes first
# among the operations the query's words score equally - the word "weather"
# alone scores nine operations equally, and their operation_id order put the
# Hong Kong Observatory first.
TOPIC_DEFAULT_OPERATIONS: dict[str, str] = {"weather": "v2_weather_forecast"}

# Operations named by a compound whose last word is an everyday topic of its
# own: "space weather" is solar activity, so the word "weather" finds these
# operations only when the query also says "space".
COMPOUND_NAMED_OPERATIONS: dict[str, tuple[str, str]] = {
    "space_weather_": ("space", "weather"),
}


def topic_default_operations(query: str) -> frozenset[str]:
    """The default operations of the topic words the query names."""
    tokens = _WORD_TOKEN_RE.findall(query.lower())
    return frozenset(
        operation for word, operation in TOPIC_DEFAULT_OPERATIONS.items()
        if _phrase_spans(tokens, word)
    )


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
    if not _US_CONTEXT_PATTERN.search(query):
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
