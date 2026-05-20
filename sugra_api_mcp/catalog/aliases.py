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
    "data sources": ["list sources", "source families"],
    "news": ["latest news", "headlines"],
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
# common business and tech acronyms (CEO, SEC, IRS, AI, IT, OK).
TICKER_TOKEN_RE = re.compile(r"\b[A-Z]{2,5}(?:\.[A-Z])?\b")
_NON_TICKER_WORDS: frozenset[str] = frozenset({
    # Tech / formats
    "API", "MCP", "URL", "JSON", "HTTP", "HTTPS", "SSL", "TLS", "TCP", "UDP", "DNS",
    "AI", "ML", "OS", "IT", "PC", "TV", "GPU", "CPU", "RAM",
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
})

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

    Heuristic: 1-5 uppercase letters not in the known non-ticker word list (CPI, USD, US, etc.).
    """
    return [m for m in TICKER_TOKEN_RE.findall(query) if m not in _NON_TICKER_WORDS]


def detect_currency_pairs(query: str) -> list[tuple[str, str]]:
    """Return currency-pair tuples (base, quote) where both are recognised ISO codes."""
    pairs: list[tuple[str, str]] = []
    for m in CURRENCY_PAIR_RE.finditer(query):
        base, quote = m.group(1), m.group(2)
        if base in _KNOWN_CURRENCIES and quote in _KNOWN_CURRENCIES and base != quote:
            pairs.append((base, quote))
    return pairs


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
