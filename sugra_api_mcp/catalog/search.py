"""Runtime search over the bundled endpoint catalog."""

from __future__ import annotations

import re
from typing import Any

from .aliases import (
    CENTRAL_BANK_PREFIX_BOOSTS,
    SOURCE_COUNTRY_PREFIXES,
    detect_currency_pairs,
    detect_network_terms,
    detect_query_countries,
    detect_tickers,
    detect_us_macro_query,
    matching_aliases,
    matching_central_bank_prefixes,
    query_has_equity_context,
)
from .models import Catalog, Endpoint

TOKEN_RE = re.compile(r"[a-z0-9]+")

_ALL_CB_PREFIXES = frozenset(CENTRAL_BANK_PREFIX_BOOSTS.values())

# Compounds whose constituent tokens must not read as standalone domain words
# (token-normalized, lowercase). Curated and tiny on purpose.
_PROPER_NAME_COMPOUNDS: tuple[str, ...] = (
    "federal funds",   # the federal funds RATE - not the funds toolset
)

# English function words stripped from QUERY terms only (never from endpoint
# tokenization or the raw-query ticker/fx/central-bank/us-macro detectors).
# Natural-language filler ("what is the price for ...") otherwise inflates any
# endpoint whose prose parameter/description text contains those words: the
# ChatGPT App submission prompt "What is the latest price for NVDA and how has
# it moved over the past week?" tied quotes_symbol_logo_png (a 302 image
# redirect with a prose description) with quotes_symbol_price purely on the
# filler tokens "the"/"for"/"has", and the alphabetical operation_id tie-break
# then surfaced the logo PNG.
#
# LENGTH >= 3 ONLY, on purpose: 2-letter tokens collide with ISO country codes
# (in=India, it=Italy, is=Iceland, be=Belgium, at=Austria) and short tickers
# (SO, IT, IP), so stripping them could drop the one meaningful token of a
# query. The NVDA tie was created entirely by 3+ letter filler ("the"/"for"/
# "has"), so the >=3 floor fixes it without any 2-letter risk. The filter below
# also guards on len explicitly, so adding a 2-letter word here would be inert.
# 3-letter ticker collisions (HAS=Hasbro, CAN=Canaan) still route correctly
# because detect_tickers runs on the RAW query, independent of this filter.
_QUERY_STOPWORDS: frozenset[str] = frozenset({
    "the", "this", "that", "these", "those",
    "and", "but", "nor", "then", "than",
    "our", "you", "your", "him", "his", "she", "her",
    "its", "they", "them", "their", "who", "whom", "whose", "what", "which",
    "how", "when", "where", "why", "whether",
    "are", "was", "were", "been", "being",
    "does", "did", "has", "have", "had",
    "will", "would", "shall", "should", "can", "could", "might", "must",
    "from", "with", "for", "about", "into", "over", "under", "against",
})

# Boosts (additive on top of token-level score). Tuned empirically against
# tests/test_search_relevance_benchmark.py — see that file for the target queries.
ALIAS_PHRASE_BOOST = 10
TICKER_MARKETS_TOOLSET_BOOST = 12
TICKER_QUOTES_SYMBOL_BOOST = 25
# Symbol-aware relevance (board MCP-4.9): when the raw query carries a
# ticker-like token (MSFT, AAPL - detect_tickers is conservative on purpose),
# endpoints that actually TAKE a symbol input must outrank market-wide ones.
# Without this, "MSFT earnings" ranked market_calendar_earnings (params
# from/to only, market-wide) above quotes_symbol_earnings_events
# (symbol-routed). Two tiers: a {symbol}/{ticker} path segment marks a
# per-symbol resource (strongest signal); a required parameter named
# symbol/ticker is symbol-scoped too, slightly weaker. Zero effect on queries
# without a ticker-like token ("federal funds rate", "EUR USD exchange rate").
TICKER_SYMBOL_PATH_BOOST = 10
TICKER_SYMBOL_PARAM_BOOST = 6
CURRENCY_PAIR_FOREX_BOOST = 15
CENTRAL_BANK_PREFIX_BOOST = 15
CRYPTO_NAMESPACE_BOOST = 18
# Strongest boost on purpose: when query asks for US-specific macro data,
# the generic FRED proxy reaches series that no country-specific endpoint
# in our catalog covers (CPIAUCSL, GDP, UNRATE, etc.). Live ChatGPT MCP
# session 2026-05-20 saw the LLM skip the MCP call entirely for "US CPI
# inflation" because non-US endpoints out-ranked fred_series_series_id.
US_MACRO_FRED_BOOST = 30
US_MACRO_FED_BOOST = 20
# MCP-9 (audit P1-3) ranking mechanisms:
# - COVERAGE: matching MORE DISTINCT query terms must beat one token repeated
#   across prose fields ('address' x4 in a crypto endpoint outranked the
#   geocoding endpoint that matched 'geocode' + 'address').
COVERAGE_BONUS_PER_TERM = 3
# - TOOLSET INTENT: a query word naming a toolset (news, weather, geocoding)
#   is the strongest domain signal a user can give.
TOOLSET_INTENT_BOOST = 10
# - GEOGRAPHY: a national source whose country differs from the one the query
#   names is a silent substitution ('Georgia CPI' returned the UK ons_cpi).
WRONG_COUNTRY_PENALTY = 22
# - CB MISMATCH: the query named a specific central bank (the cb pattern
#   fired); every OTHER bank's namespace is the wrong answer by construction.
CENTRAL_BANK_MISMATCH_PENALTY = 10
# - DEPRECATION: a deprecated route with a live replacement in the catalog
#   must never outrank it; the penalty exceeds every token-luck margin.
DEPRECATED_REPLACED_PENALTY = 25


def _tokens(value: str) -> list[str]:
    return [token for token in TOKEN_RE.findall(value.lower()) if len(token) >= 2]


def _endpoint_text(endpoint: Endpoint) -> str:
    parts = [
        endpoint.operation_id,
        endpoint.path,
        endpoint.summary,
        endpoint.description,
        " ".join(endpoint.tags),
        endpoint.toolset,
    ]
    for parameter in endpoint.parameters:
        parts.extend(
            [
                parameter.name,
                parameter.description,
                str(parameter.example or ""),
            ]
        )
    return " ".join(parts).lower()


def _field_has(field: str, term: str) -> bool:
    return term in _tokens(field)


def _phrase_has(field: str, phrase: str) -> bool:
    normalized_field = " ".join(_tokens(field))
    normalized_phrase = " ".join(_tokens(phrase))
    return bool(normalized_phrase) and normalized_phrase in normalized_field


def _alias_matches(endpoint_text: str, expansion: str) -> bool:
    expansion_tokens = _tokens(expansion)
    if len(expansion_tokens) <= 1:
        return expansion_tokens[0] in _tokens(endpoint_text) if expansion_tokens else False
    return _phrase_has(endpoint_text, expansion)


_SYMBOL_PATH_PARAM_RE = re.compile(r"\{(?:symbol|ticker)\}", re.IGNORECASE)
_SYMBOL_PARAM_NAMES = frozenset({"symbol", "ticker"})


def _symbol_input_kind(endpoint: Endpoint) -> str | None:
    """Classify how an endpoint accepts a symbol-like input (MCP-4.9).

    Returns "path" when the path contains a {symbol}/{ticker} segment (the
    endpoint is a per-symbol resource), "param" when a required parameter is
    named symbol/ticker, and None when the endpoint takes no symbol-like
    input (market-wide endpoints such as calendar feeds).
    """
    if _SYMBOL_PATH_PARAM_RE.search(endpoint.path):
        return "path"
    for parameter in endpoint.parameters:
        if parameter.required and parameter.name.lower() in _SYMBOL_PARAM_NAMES:
            return "param"
    return None


def _score(
    endpoint: Endpoint,
    query_terms: list[str],
    aliases: dict[str, list[str]],
    *,
    boost_quotes_symbol: bool,
    boost_markets_toolset: bool,
    boost_symbol_input: bool,
    boost_forex: bool,
    boost_crypto: bool,
    boost_us_macro: bool,
    central_bank_prefixes: list[str],
    query_countries: set[str],
    coverage_excluded: frozenset[str] = frozenset(),
) -> tuple[int, list[str]]:
    alias_terms = [term for terms in aliases.values() for term in terms]
    all_terms = [*query_terms, *_tokens(" ".join(alias_terms))]
    why: list[str] = []
    score = 0
    endpoint_text = _endpoint_text(endpoint)

    alias_consumed: set[str] = set()
    for phrase, expansions in aliases.items():
        if any(_alias_matches(endpoint_text, expansion) for expansion in expansions):
            score += ALIAS_PHRASE_BOOST
            why.append(f"alias:{phrase}")
            # The alias boost IS the phrase's contribution - its tokens must
            # not be re-paid through the coverage bonus (double-paying
            # 'exchange rate' lifted CB converters over the forex namespace).
            alias_consumed.update(_tokens(phrase))
            break

    # Pattern-detection boosts: tilt the ranking toward the right domain when the
    # query has a distinctive shape (ticker symbol, currency pair, central bank
    # name). Token-level scoring below still runs, so weak matches don't pass.
    if boost_quotes_symbol and endpoint.operation_id.startswith("quotes_symbol_"):
        score += TICKER_QUOTES_SYMBOL_BOOST
        why.append("pattern:ticker->quotes_symbol")
    elif boost_markets_toolset and endpoint.toolset == "markets":
        score += TICKER_MARKETS_TOOLSET_BOOST
        why.append("pattern:ticker->markets")

    if boost_symbol_input:
        symbol_kind = _symbol_input_kind(endpoint)
        if symbol_kind == "path":
            score += TICKER_SYMBOL_PATH_BOOST
            why.append("pattern:ticker->symbol-path")
        elif symbol_kind == "param":
            score += TICKER_SYMBOL_PARAM_BOOST
            why.append("pattern:ticker->symbol-param")

    if boost_forex and (
        endpoint.operation_id.startswith("forex_")
        or endpoint.operation_id.startswith("frankfurter_")
        or endpoint.operation_id.startswith("exchangerate_")
    ):
        score += CURRENCY_PAIR_FOREX_BOOST
        why.append("pattern:fx->forex")

    if boost_crypto and (
        endpoint.operation_id.startswith("crypto_")
        or endpoint.operation_id.startswith("mempool_")
        or endpoint.operation_id.startswith("onchain_")
        or endpoint.toolset == "crypto"
    ):
        score += CRYPTO_NAMESPACE_BOOST
        why.append("pattern:crypto->namespace")

    for prefix in central_bank_prefixes:
        if endpoint.operation_id.startswith(prefix):
            score += CENTRAL_BANK_PREFIX_BOOST
            why.append(f"pattern:cb->{prefix}")
            break
    else:
        if central_bank_prefixes:
            # The query named a SPECIFIC bank; a different bank's namespace is
            # the wrong answer by construction (SARB outranked the Fed on the
            # word 'reserve' in 'Federal Reserve').
            for other in _ALL_CB_PREFIXES:
                if endpoint.operation_id.startswith(other):
                    score -= CENTRAL_BANK_MISMATCH_PENALTY
                    why.append(f"cb-mismatch:{other}")
                    break

    if boost_us_macro:
        # FRED is the canonical primary source for US macro time series. The
        # generic proxy at fred_series_series_id covers ~800k indicators that
        # no country-specific *_cpi / *_gdp endpoint can substitute for US
        # queries. Strongest single boost in the file by design.
        if endpoint.operation_id.startswith("fred_"):
            score += US_MACRO_FRED_BOOST
            why.append("pattern:us-macro->fred")
        elif endpoint.operation_id.startswith("fed_"):
            # Federal Reserve datasets (rates, SOMA, Z.1) cover the rate-policy
            # side of US macro. Smaller boost since fred_series is the
            # preferred catch-all entry point.
            score += US_MACRO_FED_BOOST
            why.append("pattern:us-macro->fed")

    tag_text = " ".join([*endpoint.tags, endpoint.toolset, endpoint.source_family])
    param_text = " ".join(
        f"{parameter.name} {parameter.description}" for parameter in endpoint.parameters
    )

    matched_query_terms: set[str] = set()
    for term in all_terms:
        hit = False
        if _field_has(endpoint.operation_id, term):
            score += 5
            hit = True
            why.append(f"operation_id:{term}")
        if _field_has(tag_text, term):
            score += 4
            hit = True
            why.append(f"tag_toolset:{term}")
        if _field_has(endpoint.summary, term):
            score += 3
            hit = True
            why.append(f"summary:{term}")
        if _field_has(endpoint.path, term):
            score += 2
            hit = True
            why.append(f"path:{term}")
        if _field_has(param_text, term):
            score += 2
            hit = True
            why.append(f"params:{term}")
        if _field_has(endpoint.description, term):
            score += 1
            why.append(f"description:{term}")
        # Coverage counts STRONG-field hits only (a description-only match
        # is too weak), skips sub-3-letter noise ('is' matched a parameter and
        # re-ranked the NVDA prompt), and skips tokens a pattern detector
        # already consumed (EUR/USD fired the forex boost - counting them
        # again double-paid central-bank converters over the forex namespace).
        if (hit and term in query_terms and len(term) >= 3
                and term not in coverage_excluded
                and term not in alias_consumed):
            matched_query_terms.add(term)

    # MCP-9 coverage: breadth of DISTINCT query-term matches beats depth of
    # one term repeated across prose fields.
    if len(matched_query_terms) >= 2:
        score += COVERAGE_BONUS_PER_TERM * len(matched_query_terms)
        why.append(f"coverage:{len(matched_query_terms)}")

    # MCP-9 toolset intent: a query term that IS the toolset name (or its
    # stem: 'geocode' -> 'geocoding') pins the domain. Except when the term
    # only appears inside a proper-name compound ('federal FUNDS rate' names
    # an interest rate, not the funds toolset).
    # agy review: an EMPTY toolset made startswith('') true for every term
    # and handed the intent boost to toolset-less endpoints on any query.
    toolset_lower = endpoint.toolset.lower()
    for term in query_terms if len(toolset_lower) >= 4 else ():
        if term in coverage_excluded:
            continue
        if term == toolset_lower or (
            len(term) >= 4 and (toolset_lower.startswith(term) or term.startswith(toolset_lower))
        ):
            score += TOOLSET_INTENT_BOOST
            why.append(f"toolset-intent:{term}")
            break

    # MCP-9 geography: the query names a country; a NATIONAL source of a
    # different country is a silent substitution, never a top answer.
    if query_countries:
        for prefix, country in SOURCE_COUNTRY_PREFIXES.items():
            if endpoint.operation_id.startswith(prefix):
                if country not in query_countries:
                    score -= WRONG_COUNTRY_PENALTY
                    why.append(f"geo-mismatch:{country}")
                break

    # MCP-9 deprecation: never above the live replacement.
    if endpoint.deprecated and endpoint.replaced_by:
        score -= DEPRECATED_REPLACED_PENALTY
        why.append(f"deprecated->{endpoint.replaced_by}")

    return score, list(dict.fromkeys(why))[:6]


def known_toolsets(catalog: Catalog) -> set[str]:
    """Every toolset value the toolset filter below can match.

    Derived from the catalog itself, so it is exactly the accept-set of the
    filter in search_catalog - a caller can validate against this and be sure a
    passing value cannot silently match zero endpoints for taxonomy reasons.
    """
    return {endpoint.toolset for endpoint in catalog.endpoints}


def known_sources(catalog: Catalog) -> set[str]:
    """Every source value the source filter below can match.

    The filter accepts a value present in an endpoint's `sources` list OR equal
    to its `source_family`, so the accept-set is the union of both - wider than
    the source_family-only list reported by the sources listing. Deriving it
    here, next to the filter, keeps the two from drifting apart.
    """
    values: set[str] = set()
    for endpoint in catalog.endpoints:
        values.update(endpoint.sources or [endpoint.source_family])
        values.add(endpoint.source_family)
    return values


def search_catalog(
    catalog: Catalog,
    query: str,
    *,
    toolset: str | None = None,
    source: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search catalog operations by free-text query."""
    terms = _tokens(query)
    if not terms:
        return []
    # Strip English filler (function words of length >= 3) from the query terms.
    # The raw-empty guard above already handled a genuinely token-less query;
    # a query that is ALL stopwords ("what is the") yields an empty term list
    # here and falls through to pattern-only matching, returning no results when
    # no ticker/fx/central-bank/us-macro pattern fires (those detectors read the
    # raw `query`, so "what is AAPL" still routes via the ticker boost). The
    # explicit len guard keeps 2-letter tokens (ISO codes, short tickers) intact
    # regardless of the stopword set.
    terms = [term for term in terms if len(term) < 3 or term not in _QUERY_STOPWORDS]
    aliases = matching_aliases(query)

    # Pattern detection runs against the raw query (preserves uppercase) so
    # tickers and currency codes can be identified before token-folding.
    tickers = detect_tickers(query)
    currency_pairs = detect_currency_pairs(query)
    central_bank_prefixes = matching_central_bank_prefixes(query)

    lowered = query.lower()
    # Crypto-domain hint: when the query references a known crypto asset or
    # blockchain concept, suppress the equity boost so "BTC market cap" / "Bitcoin
    # price" don't get pulled into quotes_symbol_* endpoints.
    crypto_phrase_terms = (
        "bitcoin", "ethereum", "solana", "cardano", "dogecoin", "ripple", "polkadot",
        "crypto", "coin", "token", "blockchain", "altcoin", "stablecoin", "defi",
        "mempool", "onchain",
    )
    crypto_symbol_pattern = re.compile(r"\b(btc|eth|sol|ada|xrp|doge|bnb|usdt|usdc|dai)\b")
    has_crypto_context = (
        any(t in lowered for t in crypto_phrase_terms)
        or bool(crypto_symbol_pattern.search(lowered))
    )

    # Network-domain dominance: when two or more distinct networking terms
    # appear (traceroute, IXP, peering, ...), a ticker-shaped token is almost
    # certainly an acronym, not an equity symbol - suppress the ticker boost
    # the same way crypto context does (field test 2026-06-07: IXP routed a
    # Net Atlas query to top-20 quotes_symbol_*). Explicit equity vocabulary
    # ("stock price", "dividend") overrides the suppression, consistent with
    # the ambiguous-ticker gate in detect_tickers.
    network_dominated = (
        len(detect_network_terms(query)) >= 2
        and not query_has_equity_context(query)
    )

    # Symbol-aware gate (MCP-4.9): a ticker-like token in the RAW query means
    # the user asks about one instrument, so endpoints that take a symbol
    # input get the symbol-input boost. Crypto context and network dominance
    # suppress it exactly like the quotes_symbol boost. The phrase-based
    # extension below ("stock price" without a literal ticker) deliberately
    # does NOT enable it: the gate needs an explicit ticker-like token.
    has_ticker_token = bool(tickers) and not has_crypto_context and not network_dominated
    boost_quotes_symbol = has_ticker_token
    # Also boost markets toolset on common stock-related phrases that don't
    # contain a literal ticker but clearly target equity ("Apple stock price",
    # "Tesla market cap"). Crypto context still suppresses.
    if (
        not boost_quotes_symbol
        and not has_crypto_context
        and any(p in lowered for p in ("stock price", "share price", "stock market cap", "market cap"))
    ):
        boost_quotes_symbol = True

    boost_markets_toolset = boost_quotes_symbol
    boost_forex = bool(currency_pairs)
    boost_crypto = has_crypto_context
    boost_us_macro = detect_us_macro_query(query)
    query_countries = detect_query_countries(query)
    # codex final: geography resolves BEFORE the US-macro heuristic - the
    # word 'American' inside 'American Samoa' read as US context and the +30
    # FRED boost out-muscled the wrong-country penalty. An explicitly named
    # non-US geography suppresses the US-macro boost outright.
    if boost_us_macro and query_countries and "US" not in query_countries:
        boost_us_macro = False
    # Tokens consumed by pattern detectors are excluded from the coverage
    # bonus - the pattern boost IS their contribution.
    consumed: set[str] = {t.lower() for t in tickers}
    for base, quote in currency_pairs:
        consumed.update((base.lower(), quote.lower()))
    # Central-bank phrases that fired the cb pattern are consumed too:
    # 'federal reserve' tokens re-counted as coverage lifted the Z.1 flow-of-
    # funds op over the policy-rate op inside the SAME namespace. Token-
    # bounded: 'fed' the phrase must not consume via the substring 'FEDeral'.
    normalized_query = f" {' '.join(_tokens(query))} "
    if central_bank_prefixes:
        for cb_phrase in CENTRAL_BANK_PREFIX_BOOSTS:
            if f" {' '.join(_tokens(cb_phrase))} " in normalized_query:
                consumed.update(_tokens(cb_phrase))
    # Proper-name compounds: the constituent token names a toolset only by
    # accident ('federal FUNDS rate' is an interest rate, not the funds
    # toolset) - consume it so neither coverage nor toolset intent fires.
    for compound in _PROPER_NAME_COMPOUNDS:
        if f" {compound} " in normalized_query:
            consumed.update(compound.split())
    # Country tokens are NOT consumed (agy review): geography grants no
    # positive boost, so consuming them would strip the CORRECT national
    # source of the coverage credit for the country the user typed.
    coverage_excluded = frozenset(consumed)

    scored: list[tuple[int, Endpoint, list[str]]] = []
    for endpoint in catalog.endpoints:
        if toolset and endpoint.toolset != toolset:
            continue
        endpoint_sources = endpoint.sources or [endpoint.source_family]
        if source and source not in endpoint_sources and endpoint.source_family != source:
            continue
        score, why = _score(
            endpoint,
            terms,
            aliases,
            boost_quotes_symbol=boost_quotes_symbol,
            boost_markets_toolset=boost_markets_toolset,
            boost_symbol_input=has_ticker_token,
            boost_forex=boost_forex,
            boost_crypto=boost_crypto,
            boost_us_macro=boost_us_macro,
            central_bank_prefixes=central_bank_prefixes,
            query_countries=query_countries,
            coverage_excluded=coverage_excluded,
        )
        if score > 0:
            scored.append((score, endpoint, why))

    # MCP-9 structural guarantee: a deprecated route never outranks its live
    # replacement, whatever the token luck (a query built from the legacy
    # summary text otherwise always wins textually). Clamp strictly below.
    by_id = {endpoint.operation_id: score for score, endpoint, _ in scored}
    for i, (score, endpoint, why) in enumerate(scored):
        if endpoint.deprecated and endpoint.replaced_by:
            # agy review: a replacement matching NOTHING (absent from scored)
            # must not leave the deprecated route standing on its legacy
            # text - default 0 clamps it out of the results entirely; the
            # why-pointer to the successor still ships on any surviving entry.
            rep_score = by_id.get(endpoint.replaced_by, 0)
            if score >= rep_score:
                scored[i] = (rep_score - 1, endpoint,
                             [*why, f"clamped-below:{endpoint.replaced_by}"])

    scored = [item for item in scored if item[0] > 0]
    scored.sort(key=lambda item: (-item[0], item[1].operation_id))
    return [
        {
            "operation_id": endpoint.operation_id,
            "method": endpoint.method,
            "path": endpoint.path,
            "summary": endpoint.summary,
            "toolset": endpoint.toolset,
            "source_family": endpoint.source_family,
            "sources": endpoint.sources or [endpoint.source_family],
            "tags": endpoint.tags,
            "required_parameters": endpoint.required_parameters,
            "score": score,
            "why": why,
        }
        for score, endpoint, why in scored[: max(0, limit)]
    ]
