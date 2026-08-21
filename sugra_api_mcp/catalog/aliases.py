"""Search aliases and pattern detection for common user phrases."""

from __future__ import annotations

import re

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
    "unemployment": ["labor force", "jobless"],
    "treasury yield": ["treasury rates", "bond yield"],
    "ip geolocation": ["network atlas", "ip address", "asn"],
    "available data sources": ["list sources", "source catalog"],
    # MCP-9: screening-metadata intent - the sources manifest, not a screen call.
    "corpus coverage": ["sources manifest", "screening sources", "source lists"],
    "screening coverage": ["sources manifest", "screening sources"],
    "data sources": ["list sources", "source families"],
    "news": ["latest news", "headlines"],
    # ENERGY-1.1.1.5: EU bidding-zone / day-ahead discovery after ENTSO-E A44
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
    # Intergovernmental and statistical organizations (board MCP-4.9 field
    # find: "IMF reserves" ranked quotes_symbol_* top-3 because IMF passed
    # the ticker regex - every org acronym below leaked the same way). These
    # dominate data-catalog queries; none is an active major US listing worth
    # the ambiguous-ticker gate.
    "IMF", "BIS", "OECD", "WTO", "WHO", "UN", "ILO", "FAO", "OPEC", "NATO",
    "EIA", "BLS", "BEA", "CBO", "GAO", "ONS", "EIB", "EBRD", "ADB", "IFC",
    "WB",
    # Energy / grid (ENERGY-1.1.1.5): ENTSO-E and regional grid codes are not
    # equity tickers; keep them out of quotes_symbol_* boosts.
    "ENTSO", "AEMO", "NESO", "NEM",
})

# MCP-9 (audit P1-3): the ticker gate is INVERTED. The old default-allow
# blacklist was patched three times (IXP 2026-06-07, IMF/org acronyms MCP-4.9,
# ENTSO/grid ENERGY-1.1.1.5) - a guard patched three times is the wrong guard.
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
    # grok final: no 2-letter whitelist entry may be a valid ISO2
    # country (BA=Bosnia beat Boeing into the geo guard; GS=South
    # Georgia likewise) - equity context or sole-token still admits
    # the bare quote lookups.
    "XOM", "CVX", "WMT", "KO", "PEP", "DIS", "CAT", "JNJ", "PFE",
    "UNH", "HD", "MCD", "NKE",
    # Index ETFs
    "SPY", "QQQ", "IWM", "DIA", "VTI", "VOO",
})

# National-source geography (MCP-9): operation_id prefix -> ISO2 country of
# the NATIONAL source. Used by search to demote a national source when the
# query names a DIFFERENT country - the audit's 'Georgia CPI' returned the UK
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
    "forex_cbr_": "RU",
    "nbp_": "PL", "cnb_": "CZ", "bnm_": "MY",
    "statistical_agencies_stat_estonia_": "EE",
    "fred_": "US", "fed_": "US", "worldbank_bls_": "US", "bea_": "US",
    "census_": "US",
    "ine_": "ES",
}
# The dead-prefix test (tests/test_search_relevance.py) guards this map: every
# entry must match at least one bundled operation, so a source rename or
# removal fails loudly instead of silently disarming the geography penalty.

# Query-side country vocabulary: comprehensive generated module (codex/agy
# review: a closed 30-entry list recreated silent substitution for every
# omitted country - Netherlands CPI still returned the UK ons_cpi).
from ._countries import COUNTRY_QUERY_TERMS  # noqa: E402 - documented above

# codex review: country/US-state homonyms. The sovereign reading of an
# ambiguous name is DROPPED when the query carries explicit US-state cues -
# 'Georgia census states' must not penalize the US census namespace.
_AMBIGUOUS_US_STATE_COUNTRIES: dict[str, str] = {"georgia": "GE"}
# NOTE: deliberately excludes "us"/"usa" - those tokens are the US COUNTRY
# reading itself ('US CPI inflation'); a state needs a state-shaped cue.
_US_STATE_CUES: tuple[str, ...] = (
    "state", "states", "census", "county", "counties", "acs", "atlanta",
)

# codex review: compact queries use bare ISO2 codes ('NL CPI inflation').
# Uppercase-only in the RAW query, and codes colliding with English words or
# US postal abbreviations are excluded - with US itself kept (it IS the
# country the US-macro path expects).
_ISO2_QUERY_RE = re.compile(r"\b[A-Z]{2}\b")

# US postal codes that are ALSO valid ISO2 countries (agy r3 + codex r3):
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
    # codex r3: overlapping matches resolve longest-phrase-first -
    # 'American Samoa' must be AS alone, not AS+US ('american')+WS ('samoa'),
    # or the US component defeats the wrong-country guard entirely.
    matched = _match_vocabulary(query, tuple(COUNTRY_QUERY_TERMS))
    # codex confirm: suppression is OCCURRENCE-aware - a component term dies
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
    # grok confirmation (terminal simplification): NO enumerated collision
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
# Bare "exchange" was dropped (Codex S3 review): it collides with "internet
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


def matching_aliases(query: str) -> dict[str, list[str]]:
    normalized = " ".join(query.lower().split())
    return {
        phrase: expansions
        for phrase, expansions in ALIASES.items()
        if phrase in normalized or any(expansion in normalized for expansion in expansions)
    }


def detect_tickers(query: str) -> list[str]:
    """Return likely stock ticker tokens (e.g. AAPL, MSFT, BRK.A) found in the raw query.

    MCP-9 inverted gate: a 2-5 uppercase token is a ticker ONLY when the query
    carries equity-context vocabulary (price, stock, dividend, ...) or the
    token is on the short high-liquidity whitelist (AAPL, SPY, ...). The
    _NON_TICKER_WORDS hard list (CPI, USD, IMF, ...) always wins. So "AI
    revolution" and "search FRED series" stay non-equity while "AI stock
    price" and bare "NVDA today" land on quotes_symbol_*.
    """
    matches = TICKER_TOKEN_RE.findall(query)
    if not matches:
        return []

    # MCP-9 inverted gate: equity context (or the high-liquidity whitelist)
    # ADMITS a ticker-shaped token; the hard NEVER list still wins over both.
    # The old default-allow blacklist misread FRED, AIS, RF, MMSI, IMF and
    # every future acronym as equities until someone patched the list again.
    has_equity_context = query_has_equity_context(query)
    # Sole-substantive-token rule (codex review): a bare 'PLTR' (optionally
    # with temporal filler) is a quote lookup - there is no other intent the
    # query could carry. Acronym safety is preserved: multi-token queries
    # ('search FRED series for gold') still require context or whitelist.
    # codex r3: judge sole-ness on what REMAINS after removing the ticker
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

    Token-bounded (Codex S3 review): "Stockholm" must not satisfy "stock" -
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
