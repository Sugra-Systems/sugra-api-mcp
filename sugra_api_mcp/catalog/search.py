"""Runtime search over the bundled endpoint catalog."""

from __future__ import annotations

import re
from itertools import pairwise
from typing import Any

from ._countries import COUNTRY_QUERY_TERMS
from .aliases import (
    CB_RATES_OPERATION,
    CB_RATES_OPERATIONS,
    CENTRAL_BANK_PLACES,
    CENTRAL_BANK_POLICY_RATE_KEYS,
    CENTRAL_BANK_POLICY_RATES,
    CENTRAL_BANK_PREFIX_BOOSTS,
    COMPOUND_NAMED_OPERATIONS,
    COUNTRY_STATISTIC_SPELLINGS,
    COUNTRY_STATISTIC_WORDS,
    ETF_TICKERS,
    FX_CONVERT_OPERATION,
    FX_PANEL_OPERATION,
    LISTING_DEFAULT_OPERATION,
    LISTING_HISTORY_OPERATION,
    LISTINGS_DEFAULT_OPERATION,
    MEETING_CALENDAR_OPERATIONS,
    OPERATION_INPUT_WORDS,
    SOURCE_ALSO_SERVES,
    SOURCE_COUNTRY_PREFIXES,
    country_statistic_words,
    currency_statistic_places,
    detect_central_bank_rate_request,
    detect_currency_pairs,
    detect_every_currency_request,
    detect_fx_request,
    detect_named_operations,
    detect_network_terms,
    detect_policy_rate_request,
    detect_query_countries,
    detect_tickers,
    detect_us_macro_query,
    detect_weather_request,
    matching_aliases,
    matching_central_bank_prefixes,
    query_has_equity_context,
    query_names_a_period,
    query_names_united_states,
    spelled_country_statistics,
    topic_default_operations,
    with_statistic_words,
)
from .macro_keys import match_macro_keys, match_unplaced_macro_keys, query_names_euro_area
from .models import Catalog, Endpoint, MacroKey

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
# LENGTH >= 3 ONLY in this set, on purpose: 2-letter tokens collide with ISO
# country codes (in=India, it=Italy, is=Iceland, be=Belgium, at=Austria) and
# short tickers (SO, IT, IP), so stripping them blindly could drop the one
# meaningful token of a query; they go through the case-aware
# _TWO_LETTER_FILLER below instead. The filter also guards on len explicitly,
# so adding a 2-letter word here would be inert.
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

# Two-letter function words go only when the raw query writes them in
# lowercase: "dollar to yen" and "rates going up in the US" ranked operations
# that matched nothing but "to", "up" and "in". In capitals ("IN GDP", "IT
# sector") the word is a country code or a ticker and stays. "us" is never
# filler: in lowercase it names the United States as often as the pronoun.
_TWO_LETTER_FILLER: frozenset[str] = frozenset({
    "to", "in", "is", "so", "of", "on", "at", "by", "an", "or", "as", "be",
    "do", "if", "it", "me", "my", "no", "up", "we", "he", "am", "go",
})
_UPPERCASE_TWO_LETTER_RE = re.compile(r"\b[A-Z]{2}\b")

# Boosts (additive on top of token-level score). Tuned empirically against
# tests/test_search_relevance.py - see that file for the target queries.
ALIAS_PHRASE_BOOST = 10
TICKER_MARKETS_TOOLSET_BOOST = 12
TICKER_QUOTES_SYMBOL_BOOST = 25
# Symbol-aware relevance: when the raw query carries a
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
# A listing question that names a period lifts the price history above the
# listing operations no other word picks: level with a word only a description
# holds (1), where the history wins as the tie default, and below every strong
# field, so a word of the question in a strong field still picks its operation
# ("AAPL dividends since 2020" is the dividends).
LISTING_PERIOD_BOOST = 1
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
# FRED's generic proxy: beside a macro key's hit it keeps the US-macro boost,
# clamped below that hit (see search_catalog).
US_MACRO_PROXY_OPERATION = "fred_series_series_id"
# Ranking mechanisms:
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
# x-sugra-keywords carries synonyms a query might use that the operation's
# own path/summary/description never spell out ("coffee" for
# fred_series_series_id). Weighted like tag_toolset (4) - a keyword IS a tag
# the API author chose to attach to the operation, not prose.
KEYWORD_FIELD_WEIGHT = 4
# The query names a country (detect_query_countries) and the endpoint takes a
# `country` parameter - a generic signal that the endpoint can answer for
# that country, independent of the SOURCE_COUNTRY_PREFIXES national-source
# penalty above. Without this, a bare country name query ("Portugal") matched
# nothing: no field in the catalog spells country names out, only the
# parameter NAME "country" appears, and nothing in the query text touched it.
# When the query has a topic besides the country ("Portugal weather"), only
# an endpoint that matches the topic earns it - otherwise every
# country-scoped endpoint would surface for every country query.
COUNTRY_PARAM_BOOST = 6
# - NAMED OPERATION: the query names a benchmark, waterway, port or measure
#   whose operation the catalog text never spells out (Brent, TTF, Suez,
#   Rotterdam, trucking - aliases.detect_named_operations), joins two
#   currencies as a conversion, or asks an everyday weather question
#   (aliases.detect_weather_request). The name is the user's whole intent, so
#   it outweighs any one field match.
NAMED_OPERATION_BOOST = 15
# - MACRO KEY: the query names one of the curated series a country/section
#   operation serves ("US nonfarm payrolls", "Japan GDP growth" -
#   macro_keys.match_macro_keys), whose own text says only "country" and
#   "section". The series is the user's whole intent, and the hit names its key.
MACRO_KEY_BOOST = 50


def _tokens(value: str) -> list[str]:
    return [token for token in TOKEN_RE.findall(value.lower()) if len(token) >= 2]


def _country_terms(query: str, terms: list[str], query_countries: set[str]) -> frozenset[str]:
    """Query terms that only name one of the countries the query names.

    A name counts when the whole name phrase is in the query ('united kingdom'
    for GB), so a word that merely belongs to some long country name stays
    part of the topic. The rest of the query is its topic.
    """
    if not query_countries:
        return frozenset()
    normalized = f" {' '.join(_tokens(query))} "
    naming = {code.lower() for code in query_countries}
    for phrase, code in COUNTRY_QUERY_TERMS.items():
        phrase_tokens = _tokens(phrase)
        if code in query_countries and f" {' '.join(phrase_tokens)} " in normalized:
            naming.update(phrase_tokens)
    return frozenset(term for term in terms if term in naming)


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
    """Classify how an endpoint accepts a symbol-like input.

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


# A word named as the base of a ratio: "credit-to-GDP", "debt to GDP",
# "percent of GDP", "% of GDP", "share of GDP".
_RATIO_BASE_RE = re.compile(
    r"(?:(?<![a-z0-9])to[\s_-]+|(?:(?<![a-z0-9])(?:percent|per\s+cent|share)|%)\s+of\s+)"
    r"([a-z0-9]+)"
)


def _ratio_base_statistics(texts: list[str]) -> frozenset[str]:
    """The country statistics the texts name only as the base of a ratio.

    "Credit-to-GDP gaps" and "debt as percent of GDP" measure credit and
    debt against GDP, so they answer no question about GDP itself. A
    statistic the texts also name on its own ("GDP and debt to GDP") counts
    as named.
    """
    bases: set[str] = set()
    named: set[str] = set()
    for text in texts:
        lowered = text.lower()
        in_ratio = {match.start(1) for match in _RATIO_BASE_RE.finditer(lowered)}
        for match in TOKEN_RE.finditer(lowered):
            word = match.group()
            if word in COUNTRY_STATISTIC_WORDS:
                (bases if match.start() in in_ratio else named).add(word)
    return frozenset(bases - named)


# The words of a question that measure nothing: function words, request
# verbs, the words that say when or how, and the words that name where or
# the data itself ("which country has the highest annual ratio to GDP").
_RATIO_NON_MEASURES = _QUERY_STOPWORDS | _TWO_LETTER_FILLER | frozenset({
    "calculate", "chart", "compare", "compute", "fetch", "find", "get", "give",
    "list", "look", "need", "please", "plot", "see", "show", "tell", "want", "whats",
    "annual", "average", "current", "daily", "historical", "latest", "monthly",
    "quarterly", "recent", "today", "weekly", "year", "yearly",
    "high", "higher", "highest", "low", "lower", "lowest", "much", "rank", "top",
    "countries", "country", "global", "nation", "nations", "world",
    "data", "figure", "figures", "indicator", "indicators", "level", "levels",
    "number", "numbers", "ratio", "ratios", "series", "statistics", "value", "values",
})


def _measures(word: str) -> bool:
    """Whether a word before a ratio can be what the ratio measures."""
    return (len(word) > 1 and not word.isdigit() and word not in _RATIO_NON_MEASURES
            and word not in COUNTRY_STATISTIC_WORDS)


def _ratio_numerators(text: str) -> dict[str, frozenset[str]]:
    """The words a text measures against each country statistic.

    Every word before the ratio, back to the ratio before it or the start of
    the text, that can measure something: "debt" in "debt-to-GDP", "debt as
    a percent of GDP" and "government debt is 80% of GDP" ("government" too),
    "debt" and "deficit" in "debt and budget deficit to GDP". No phrasing is
    parsed, so an operation that names any of those words answers the ratio,
    and a question whose words before the ratio all measure nothing ("what is
    the ratio to GDP", "2024 share of GDP") names no numerator.
    """
    lowered = text.lower()
    numerators: dict[str, set[str]] = {}
    start = 0
    for match in _RATIO_BASE_RE.finditer(lowered):
        window = TOKEN_RE.findall(lowered[start:match.start()])
        start = match.end()
        base = match.group(1)
        if base not in COUNTRY_STATISTIC_WORDS:
            continue
        words = {word for word in window if _measures(word)}
        if words:
            numerators.setdefault(base, set()).update(words)
    return {base: frozenset(words) for base, words in numerators.items()}


def _singulars(word: str) -> frozenset[str]:
    """The word and every singular its plural ending may stand for: "taxes"
    for "tax", "expenses" for "expense", "liabilities" for "liability"."""
    forms = {word}
    if len(word) > 4 and word.endswith("ies"):
        forms.add(f"{word[:-3]}y")
    if len(word) > 3 and word.endswith("es") and word[:-2].endswith(("s", "x", "z", "ch", "sh")):
        forms.add(word[:-2])
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        forms.add(word[:-1])
    return frozenset(forms)


_TITLE_CLAUSE_RE = re.compile(r"[\[(:;,]|\b(?:for|in|of)\b")


def _head_word(title: str) -> str:
    """The noun a series title is named for: the last word before its first
    clause, "claims" in "Initial Claims for Unemployment Insurance"."""
    words = _tokens(_TITLE_CLAUSE_RE.split(title.lower(), maxsplit=1)[0])
    return words[-1] if words else ""


def _names_any(words: frozenset[str], tokens: frozenset[str]) -> bool:
    """Whether the tokens hold one of the words, in the singular or the plural."""
    wanted = frozenset().union(*(_singulars(word) for word in words))
    return any(not wanted.isdisjoint(_singulars(token)) for token in tokens)


class _EndpointProfile:
    """The token sets of one endpoint's searchable fields, built once per endpoint.

    Scoring asks, for every query term and every endpoint, whether the term is a
    token of a field. Re-tokenizing the field text for each question made a
    three-word query cost about 400 ms and let one long query hold the event
    loop for minutes. A set answers the same question:
    ``term in _tokens(field)`` is exactly ``term in set(_tokens(field))``, and
    the normalized text is exactly what ``_phrase_has`` builds.
    """

    __slots__ = (
        "description", "keywords", "operation_id", "params", "path",
        "ratio_bases", "summary", "tags", "takes_country_param", "text",
        "text_normalized",
    )

    def __init__(self, endpoint: Endpoint) -> None:
        tag_text = " ".join([*endpoint.tags, endpoint.toolset, endpoint.source_family])
        param_text = " ".join(
            f"{parameter.name} {parameter.description}" for parameter in endpoint.parameters
        )
        keyword_text = " ".join(endpoint.keywords)
        text_tokens = _tokens(_endpoint_text(endpoint))
        self.operation_id = frozenset(_tokens(endpoint.operation_id))
        self.tags = frozenset(_tokens(tag_text))
        self.summary = frozenset(_tokens(endpoint.summary))
        self.path = frozenset(_tokens(endpoint.path))
        self.params = frozenset(_tokens(param_text))
        self.description = frozenset(_tokens(endpoint.description))
        self.keywords = frozenset(_tokens(keyword_text))
        self.takes_country_param = any(
            parameter.name.lower() == "country" for parameter in endpoint.parameters
        )
        # Each field and parameter apart, so that no ratio spans two of them.
        self.ratio_bases = _ratio_base_statistics([
            endpoint.operation_id, endpoint.path, endpoint.summary,
            endpoint.description, *endpoint.tags, endpoint.toolset,
            endpoint.source_family, *endpoint.keywords,
            *(text for parameter in endpoint.parameters
              for text in (parameter.name, parameter.description)),
        ])
        self.text = frozenset(text_tokens)
        self.text_normalized = " ".join(text_tokens)


# Keyed by object identity and holding the endpoint itself, so an id is never
# reused while its entry lives. The bundled catalog needs about 1,600 entries;
# the bound only matters to callers that keep building new Endpoint objects.
_PROFILE_CACHE_LIMIT = 8192
_profiles: dict[int, tuple[Endpoint, _EndpointProfile]] = {}


def _profile(endpoint: Endpoint) -> _EndpointProfile:
    cached = _profiles.get(id(endpoint))
    if cached is not None and cached[0] is endpoint:
        return cached[1]
    profile = _EndpointProfile(endpoint)
    if len(_profiles) >= _PROFILE_CACHE_LIMIT:
        _profiles.clear()
    _profiles[id(endpoint)] = (endpoint, profile)
    return profile


def _alias_matches_profile(profile: _EndpointProfile, expansion: str) -> bool:
    """``_alias_matches`` answered from the endpoint's cached profile."""
    expansion_tokens = _tokens(expansion)
    if len(expansion_tokens) <= 1:
        return expansion_tokens[0] in profile.text if expansion_tokens else False
    normalized_phrase = " ".join(expansion_tokens)
    return bool(normalized_phrase) and normalized_phrase in profile.text_normalized


def _source_country(endpoint: Endpoint) -> str | None:
    """The country of the national source the endpoint belongs to, if any."""
    for prefix, country in SOURCE_COUNTRY_PREFIXES.items():
        if endpoint.operation_id.startswith(prefix):
            return country
    return None


def _is_foreign_source(endpoint: Endpoint, countries: set[str]) -> bool:
    """Whether the endpoint is a national source of none of the countries.

    A source that also answers for a place the query names is no foreign
    source there: the National Weather Service warns for Puerto Rico.
    """
    country = _source_country(endpoint) if countries else None
    if country is None or country in countries:
        return False
    return not any(
        endpoint.operation_id.startswith(prefix) and not places.isdisjoint(countries)
        for prefix, places in SOURCE_ALSO_SERVES.items()
    )


def _matches_only_place_words(why: list[str], place_words: frozenset[str]) -> bool:
    """Whether every reason an operation scored is a field matching one of
    the place's words, or the coverage those words give it."""
    return bool(why) and all(
        reason.partition(":")[2] in place_words or reason.startswith("coverage:")
        for reason in why
    )


def _is_foreign_euro_area(
    endpoint: Endpoint, countries: set[str], penalized: set[str], central_bank_prefixes: list[str],
) -> bool:
    """Whether the endpoint is the ECB's and the query names countries but
    neither the euro area nor the ECB.

    The ECB answers for the whole euro area, never for one country: its
    yield curve came first for "Germany bond yields" and "France 10 year
    yield", which the country profile answers.
    """
    return (bool(countries) and "EU" not in penalized
            and endpoint.operation_id.startswith("ecb_")
            and "ecb_" not in central_bank_prefixes)


def _score(
    endpoint: Endpoint,
    query_terms: list[str],
    aliases: dict[str, list[str]],
    *,
    boost_quotes_symbol: bool,
    boost_listing_history: bool = False,
    boost_markets_toolset: bool,
    boost_symbol_input: bool,
    boost_forex: bool,
    boost_etf_symbol: bool = False,
    boost_crypto: bool,
    boost_us_macro: bool,
    central_bank_prefixes: list[str],
    query_countries: set[str],
    coverage_excluded: frozenset[str] = frozenset(),
    country_terms: frozenset[str] = frozenset(),
    penalty_countries: set[str] | None = None,
    named_operations: dict[str, str] | None = None,
    named_words: frozenset[str] = frozenset(),
    own_named_words: frozenset[str] = frozenset(),
    macro_key_operations: dict[str, str] | None = None,
    country_answers: set[str] | None = None,
    any_country_cues: frozenset[str] = frozenset(),
    ratio_cues: frozenset[str] = frozenset(),
    ratio_numerators: dict[str, frozenset[str]] | None = None,
    spelled_statistics: frozenset[str] = frozenset(),
    place_answers: set[str] | None = None,
    term_hits: dict[str, tuple[frozenset[str], frozenset[str], frozenset[str]]] | None = None,
    query_ratio_bases: frozenset[str] = frozenset(),
) -> tuple[int, list[str]]:
    """Score one endpoint for the query.

    ``named_words`` are the query words of every name the query holds, and
    ``own_named_words`` those of the names that point to this endpoint.

    ``country_answers``, when given, collects the endpoint's operation_id if
    it answers for the country the query names: the country-parameter boost
    reached it and it is no national source of another country.

    ``any_country_cues`` holds the statistic words of a question that names
    no place, which asks for that statistic for whichever country the user
    means: an operation that takes the country as a parameter, is no national
    source and matches one of those words in any field answers it, so it
    earns the country-parameter boost, and ``place_answers``, when given,
    collects its operation_id. A word the operation names only as the base
    of a ratio answers only a question that names it so as well, the words
    of ``ratio_cues``: the credit-to-GDP gap answers "credit to GDP gap",
    never "GDP". When the question names what its ratio of that word
    measures, the words ``ratio_numerators`` holds for it, the operation must
    name one of them too: the credit-to-GDP gap answers no "government debt
    to GDP". Of those words, ``spelled_statistics`` are the ones the
    question names in other words ("jobless rate"): an operation answers one
    when it names it in its own word or in one of those. ``term_hits``,
    when given, records the query words the endpoint matches in a field other
    than its description, those it matches in any field, and those it matches
    in its keywords. ``query_ratio_bases`` are the statistics the question
    names as the base of a ratio: a word the operation names only so matches
    its topic for a question that names it so too, and for no other, so the
    credit-to-GDP gap earns no country-parameter boost for "Japan GDP".
    """
    alias_terms = [term for terms in aliases.values() for term in terms]
    # A word scores once, however often the question repeats it.
    all_terms = [*dict.fromkeys(query_terms), *_tokens(" ".join(alias_terms))]
    why: list[str] = []
    score = 0
    profile = _profile(endpoint)
    # Whether the endpoint matched the query's topic - anything the query
    # asks besides the country names it (see COUNTRY_PARAM_BOOST).
    topic_hit = False

    named = (named_operations or {}).get(endpoint.operation_id)
    # A compound-named operation answers one word of the compound only beside
    # the other: "space weather" is solar activity, not the weather in Paris,
    # and "real wages" are no real estate.
    silenced = {
        tail for prefix, (heads, tails) in COMPOUND_NAMED_OPERATIONS.items()
        for tail in tails
        if endpoint.operation_id.startswith(prefix)
        and not any(head in query_terms for head in heads)
    }
    silenced |= OPERATION_INPUT_WORDS.get(endpoint.operation_id, frozenset())
    # The words of a name score for the operations it names alone: in "oil
    # price history" the words "oil" and "price" name the oil price, and
    # scoring them for every operation that says "price" put a prediction
    # market's price history above it. In "oil price and Henry Hub" the gas
    # benchmark scores "henry" and "hub", never "oil" or "price".
    silenced |= named_words - own_named_words

    alias_consumed: set[str] = set()
    # The statistic words the alias that matched stands for: "consumer price
    # index" answers "inflation" as the word itself does.
    alias_statistics: frozenset[str] = frozenset()
    for phrase, expansions in aliases.items():
        if any(
            _alias_matches_profile(profile, expansion)
            for expansion in expansions
            if not set(_tokens(expansion)) <= silenced
        ):
            score += ALIAS_PHRASE_BOOST
            topic_hit = True
            why.append(f"alias:{phrase}")
            # The alias boost IS the phrase's contribution - its tokens must
            # not be re-paid through the coverage bonus (double-paying
            # 'exchange rate' lifted CB converters over the forex namespace).
            alias_consumed.update(_tokens(phrase))
            alias_statistics = any_country_cues & {phrase, *expansions}
            break

    if named is not None:
        score += NAMED_OPERATION_BOOST
        topic_hit = True
        why.append(f"name:{named}")

    macro_key = (macro_key_operations or {}).get(endpoint.operation_id)
    if macro_key is not None:
        score += MACRO_KEY_BOOST
        topic_hit = True
        why.append(f"macro-key:{macro_key}")

    # Pattern-detection boosts: tilt the ranking toward the right domain when the
    # query has a distinctive shape (ticker symbol, currency pair, central bank
    # name). Token-level scoring below still runs, so weak matches don't pass.
    if boost_quotes_symbol and endpoint.operation_id.startswith("quotes_symbol_"):
        score += TICKER_QUOTES_SYMBOL_BOOST
        why.append("pattern:ticker->quotes_symbol")
        if boost_listing_history and endpoint.operation_id == LISTING_HISTORY_OPERATION:
            score += LISTING_PERIOD_BOOST
            why.append("pattern:period->history")
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

    matched_query_terms: set[str] = set()
    strong_words: set[str] = set()
    keyword_words: set[str] = set()
    matched_words: set[str] = set(alias_statistics)
    for term in all_terms:
        if term in silenced:
            continue
        hit = False
        if term in profile.operation_id:
            score += 5
            hit = True
            why.append(f"operation_id:{term}")
        if term in profile.tags:
            score += 4
            hit = True
            why.append(f"tag_toolset:{term}")
        if term in profile.summary:
            score += 3
            hit = True
            why.append(f"summary:{term}")
        if term in profile.path:
            score += 2
            hit = True
            why.append(f"path:{term}")
        if term in profile.params:
            score += 2
            hit = True
            why.append(f"params:{term}")
        if term in profile.keywords:
            score += KEYWORD_FIELD_WEIGHT
            hit = True
            why.append(f"keyword:{term}")
        # A word the operation names only as the base of a ratio is no topic
        # of it unless the question names it so as well: the credit-to-GDP
        # gap answers "credit to GDP gap for Japan", never "Japan GDP".
        ratio_base_only = term in profile.ratio_bases and term not in query_ratio_bases
        if term in profile.description:
            score += 1
            why.append(f"description:{term}")
            if term not in country_terms and not ratio_base_only:
                topic_hit = True
        if hit and term not in country_terms and not ratio_base_only:
            topic_hit = True
        # The credit-to-GDP gap ranked first for "GDP", and for "government
        # debt to GDP".
        if (any_country_cues and term in query_terms and len(term) >= 3
                and (term not in profile.ratio_bases
                     or (term in ratio_cues
                         and (not (ratio_numerators or {}).get(term)
                              or _names_any(ratio_numerators[term], profile.text))))):
            if hit:
                strong_words.add(term)
            if term in profile.keywords:
                keyword_words.add(term)
            if hit or term in profile.description:
                matched_words.add(term)
        # Coverage counts STRONG-field hits only (a description-only match
        # is too weak), skips sub-3-letter noise ('is' matched a parameter and
        # re-ranked the NVDA prompt), and skips tokens a pattern detector
        # already consumed (EUR/USD fired the forex boost - counting them
        # again double-paid central-bank converters over the forex namespace).
        if (hit and term in query_terms and len(term) >= 3
                and term not in coverage_excluded
                and term not in alias_consumed):
            matched_query_terms.add(term)
    # "consumer prices" names the CPI as "CPI" does.
    for statistic in spelled_statistics:
        if any(_alias_matches_profile(profile, spelling)
               for spelling in COUNTRY_STATISTIC_SPELLINGS[statistic]):
            matched_words.add(statistic)

    # Coverage: breadth of DISTINCT query-term matches beats depth of
    # one term repeated across prose fields.
    if len(matched_query_terms) >= 2:
        score += COVERAGE_BONUS_PER_TERM * len(matched_query_terms)
        why.append(f"coverage:{len(matched_query_terms)}")

    # A whitelisted ETF's ticker reaches the per-ETF operations as it reaches
    # the quotes, but only those whose name, summary, path, parameters or
    # keywords hold a word of the question: the topic word picks between them
    # ("SPY flows" is the ETF's flows, "SPY price" its price), and a ticker
    # alone or a word in a description keeps the quotes first.
    if boost_etf_symbol and endpoint.operation_id.startswith("etf_symbol_") and matched_query_terms:
        score += TICKER_QUOTES_SYMBOL_BOOST
        # First, where the quotes carry theirs: the reasons are cut to six.
        why.insert(0, "pattern:ticker->etf_symbol")

    # Toolset intent: a query term that IS the toolset name (or its
    # stem: 'geocode' -> 'geocoding') pins the domain. Except when the term
    # only appears inside a proper-name compound ('federal FUNDS rate' names
    # an interest rate, not the funds toolset).
    # An EMPTY toolset made startswith('') true for every term
    # and handed the intent boost to toolset-less endpoints on any query.
    # A name of several words pins it only when the query says two of them:
    # "central" alone handed the central_banks toolset the boost, and "real"
    # of "real home prices" the real_estate one.
    toolset_lower = endpoint.toolset.lower()
    names = [term for term in query_terms
             if term not in coverage_excluded and term not in silenced]

    def says(word: str) -> str | None:
        return next((term for term in names if term == word or (
            len(term) >= 4 and (word.startswith(term) or term.startswith(word)))), None)

    toolset_words = toolset_lower.split("_") if len(toolset_lower) >= 4 else []
    said = [term for term in map(says, toolset_words) if term]
    if said and len(said) >= min(len(toolset_words), 2):
        score += TOOLSET_INTENT_BOOST
        why.append(f"toolset-intent:{' '.join(said)}")

    # Geography: the query names a country; a NATIONAL source of a
    # different country is a silent substitution, never a top answer. The
    # places a query names without naming a country - a currency's issuer, a
    # benchmark's market, a port's country - count here too.
    penalized = query_countries if penalty_countries is None else penalty_countries
    mismatched = _is_foreign_source(endpoint, penalized)
    if mismatched:
        score -= WRONG_COUNTRY_PENALTY
        why.append(f"geo-mismatch:{_source_country(endpoint)}")
    elif _is_foreign_euro_area(endpoint, query_countries, penalized, central_bank_prefixes):
        mismatched = True
        score -= WRONG_COUNTRY_PENALTY
        why.append("geo-mismatch:EU")

    if query_countries:
        # A generic country-parameterized endpoint can answer for WHATEVER
        # country the query names (it is not fixed to one nation the way a
        # national source is), so it earns a positive boost rather than the
        # mismatch penalty above. A query that is only a country boosts every
        # such endpoint; a query with a topic as well boosts only those that
        # match the topic.
        country_only = not aliases and all(term in country_terms for term in query_terms)
        if profile.takes_country_param and (country_only or topic_hit):
            score += COUNTRY_PARAM_BOOST
            why.append("pattern:country->param")
            if country_answers is not None and not mismatched:
                country_answers.add(endpoint.operation_id)
    elif (any_country_cues & matched_words and profile.takes_country_param
          and _source_country(endpoint) is None):
        score += COUNTRY_PARAM_BOOST
        why.append("pattern:any-country->param")
        if place_answers is not None:
            place_answers.add(endpoint.operation_id)

    if term_hits is not None:
        term_hits[endpoint.operation_id] = (
            frozenset(strong_words), frozenset(matched_words), frozenset(keyword_words))

    # Deprecation: never above the live replacement.
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


def _in_scope(endpoint: Endpoint, toolset: str | None, source: str | None) -> bool:
    """Whether the endpoint passes the toolset and source filters of search_catalog."""
    if toolset and endpoint.toolset != toolset:
        return False
    endpoint_sources = endpoint.sources or [endpoint.source_family]
    return not source or source in endpoint_sources or endpoint.source_family == source


# The bounds a query must fit before any work is done on it. Agent
# queries name an instrument, series, place or task in a few words; even the
# long NVDA question in the stopword note above is 16 tokens. A query past
# either bound is refused, never truncated, so no result is ever computed from
# words the caller did not know were dropped.
MAX_QUERY_CHARS = 1000
MAX_QUERY_TERMS = 64


def query_limit_error(query: str) -> dict[str, Any] | None:
    """The structured query_too_long error for a query past the bounds, else None.

    Characters are counted before tokenizing, so an oversized string costs one
    len() call and no regex pass. Terms are counted the way scoring counts them:
    every token of two or more characters, repeats included, because every
    repeat is scored again.
    """
    chars = len(query)
    terms = None if chars > MAX_QUERY_CHARS else len(_tokens(query))
    if terms is not None and terms <= MAX_QUERY_TERMS:
        return None
    return {
        "error": "query_too_long",
        "max_chars": MAX_QUERY_CHARS,
        "max_terms": MAX_QUERY_TERMS,
        "chars": chars,
        **({"terms": terms} if terms is not None else {}),
        "elapsed_ms": 0,
        "hint": (
            f"Shorten the query to at most {MAX_QUERY_TERMS} terms (runs of two or more "
            f"ASCII letters or digits, repeats counted) and {MAX_QUERY_CHARS} characters: "
            "name the instrument, series, place or task."
        ),
    }


def search_catalog(
    catalog: Catalog,
    query: str,
    *,
    toolset: str | None = None,
    source: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search catalog operations by free-text query.

    Raises ValueError for a query past the bounds; the tools and the CLI refuse
    such a query with query_limit_error before they call this.
    """
    if query_limit_error(query) is not None:
        raise ValueError("query_too_long: the query is past the search bounds")
    terms = _tokens(query)
    if not terms:
        return []
    # Strip English filler (function words of length >= 3) from the query terms.
    # The raw-empty guard above already handled a genuinely token-less query;
    # a query that is ALL stopwords ("what is the") yields an empty term list
    # here and falls through to pattern-only matching, returning no results when
    # no ticker/fx/central-bank/us-macro pattern fires (those detectors read the
    # raw `query`, so "what is AAPL" still routes via the ticker boost). A
    # 2-letter token goes only as lowercase _TWO_LETTER_FILLER, so ISO codes and
    # short tickers written in capitals stay.
    capitalized = {word.lower() for word in _UPPERCASE_TWO_LETTER_RE.findall(query)}
    terms = [
        term for term in terms
        if (len(term) < 3 or term not in _QUERY_STOPWORDS)
        and (term not in _TWO_LETTER_FILLER or term in capitalized)
    ]

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
        "crypto", "coin", "token", "blockchain", "altcoin", "stablecoin",
        "mempool", "onchain",
    )
    # "defi" begins "deficit": a whole word only.
    crypto_symbol_pattern = re.compile(r"\b(btc|eth|sol|ada|xrp|doge|bnb|usdt|usdc|dai|defi)\b")
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

    # Symbol-aware gate: a ticker-like token in the RAW query means
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
    # A question about one listing that names a period asks for its price
    # history; several listings with a period still ask for their prices.
    listing_period = (
        boost_quotes_symbol and len(tickers) <= 1 and query_names_a_period(query)
    )
    # Everyday names: a currency named in words, and the benchmarks, waterways,
    # ports and measures of detect_named_operations. Crypto context keeps
    # "convert bitcoin to dollars" a crypto price. A pair asks for a
    # conversion unless the query asks for the rate over time. A weather
    # question asked in weather, time and place words alone names the
    # forecast (the National Weather Service's for the United States), or the
    # history when it asks about the past.
    fx = None if has_crypto_context else detect_fx_request(query)
    named = detect_named_operations(query)
    named_operations = dict(named.operations)
    if fx is not None and fx.pair and not fx.over_time:
        named_operations.setdefault(FX_CONVERT_OPERATION, "currency pair")
    # A currency named alone asks what its rate is: the rate panel answers.
    if fx is not None and fx.alone:
        named_operations.setdefault(FX_PANEL_OPERATION, "currency")
    weather = detect_weather_request(query, terms)
    if weather is not None:
        named_operations.setdefault(weather.operation, weather.name)
    # A policy-rate word beside a central bank's name, with nothing else asked,
    # names the operation that holds the bank's policy rate, or the meeting
    # calendar when it asks when. Its words then score for those operations
    # alone, as a name's words do: "Bank Negara Malaysia interest rate" found
    # the bank's interbank rate.
    policy_rate = detect_policy_rate_request(query, terms, central_bank_prefixes)
    # A country's interest rate, with nothing else asked, is its central
    # bank's policy rate: "Canada interest rate" found the Bank of Canada's
    # prime rate first and its policy rate fourth.
    if not central_bank_prefixes:
        places = detect_query_countries(query)
        banks = [prefix for prefix, place in CENTRAL_BANK_PLACES.items()
                 if place in places and (prefix in CENTRAL_BANK_POLICY_RATES
                                         or prefix in CENTRAL_BANK_POLICY_RATE_KEYS)]
        if len(places) == 1 and len(banks) == 1:
            place_words = _country_terms(query, terms, places)
            by_place = detect_policy_rate_request(
                query, [term for term in terms if term not in place_words], banks)
            # The country stands in for the bank's name only beside a rate
            # word: "Canada decision" asks for no rate.
            if (by_place.operations or by_place.keys) and by_place.words & {"rate", "rates"}:
                policy_rate = by_place
    for operation, words in policy_rate.operations.items():
        named_operations.setdefault(operation, words)
    # Each name's words belong to the operations it points to.
    named_words = named.words | policy_rate.words
    own_named_words = dict(named.operation_words)
    for operation, words in policy_rate.operation_words.items():
        own_named_words[operation] = own_named_words.get(operation, frozenset()) | words
    # A meeting of the bank the query names is the calendar's question: the
    # calendar joins the central-bank prefixes, so the prefix boost the bank's
    # name gives its own operations lifts the calendar too ("when is the next
    # Fed meeting"). Its words were recorded where it was named.
    if central_bank_prefixes and MEETING_CALENDAR_OPERATIONS & named_operations.keys():
        central_bank_prefixes = [*central_bank_prefixes, *sorted(MEETING_CALENDAR_OPERATIONS)]
    boost_forex = bool(currency_pairs) or fx is not None
    boost_crypto = has_crypto_context
    boost_us_macro = detect_us_macro_query(query)
    query_countries = detect_query_countries(query)
    aliases = matching_aliases(query, query_countries)
    # Geography resolves BEFORE the US-macro heuristic - the
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
    # Currency and other names are consumed too: their boost IS their
    # contribution, and their places join the geography penalty below.
    consumed.update(named.words)
    consumed.update(policy_rate.words)
    if weather is not None:
        consumed.update(weather.words)
    penalty_countries = set(query_countries) | named.countries
    if fx is not None:
        consumed.update(fx.words)
        penalty_countries |= fx.issuer_countries
    # A named central bank names the place it answers for, and "US" in any
    # spelling names the United States when no other place is named, as the
    # macro keys read it ("show us Japan GDP" asks about Japan): "Fed
    # inflation" and "U.S. inflation" ranked the inflation of Argentina among
    # the first answers.
    penalty_countries.update(
        CENTRAL_BANK_PLACES[prefix] for prefix in central_bank_prefixes
        if prefix in CENTRAL_BANK_PLACES
    )
    if not penalty_countries - {"US"} and query_names_united_states(query):
        penalty_countries.add("US")
    # A currency named right before a statistic every country reports names
    # its issuer's country, as the country's name would, where nothing else
    # names a place or a listing (a named central bank and "U.S." stand in
    # penalty_countries by now): "yen inflation" ranked the composite country
    # profile first, for any country, and not the inflation of Japan.
    currency_words: frozenset[str] = frozenset()
    if not (penalty_countries or has_ticker_token):
        currency_countries, currency_words = currency_statistic_places(query)
        query_countries |= currency_countries
        penalty_countries |= currency_countries
    # An exchange rate asked of no currency, place or bank names the rates of
    # every currency, as a pair names the conversion; its words score for that
    # operation alone, as a name's words do.
    if fx is None and not (penalty_countries or has_ticker_token or has_crypto_context
                           or central_bank_prefixes or named_operations):
        every_currency = detect_every_currency_request(query)
        if every_currency is not None:
            operation, words = every_currency
            named_operations[operation] = "exchange rates"
            named_words |= words
            own_named_words[operation] = own_named_words.get(operation, frozenset()) | words
    if not (penalty_countries or has_ticker_token or has_crypto_context
            or central_bank_prefixes or named_operations):
        # The words score for the BIS rates alone, the rates by country among
        # them, as a benchmark name's words do.
        cb_rate_words = detect_central_bank_rate_request(query)
        if cb_rate_words is not None:
            named_operations[CB_RATES_OPERATION] = "central bank policy rates"
            named_words |= cb_rate_words
            for operation in CB_RATES_OPERATIONS:
                own_named_words[operation] = (
                    own_named_words.get(operation, frozenset()) | cb_rate_words)
    # A statistic named in two words ("current account") scores its words
    # only for the operations that spell it, as a name's words do: "Germany
    # current account" ranked the current air quality first, on the word
    # "current", and the country profile fourth.
    spelling_operations: set[str] = set()
    for statistic, words in spelled_country_statistics(query).items():
        if " " not in statistic:
            continue
        named_words |= words
        for endpoint in catalog.endpoints:
            if any(_alias_matches_profile(_profile(endpoint), spelling)
                   for spelling in COUNTRY_STATISTIC_SPELLINGS[statistic]):
                spelling_operations.add(endpoint.operation_id)
                own_named_words[endpoint.operation_id] = (
                    own_named_words.get(endpoint.operation_id, frozenset()) | words)
    # Country tokens are NOT consumed: consuming them would strip the CORRECT
    # national source of the coverage credit for the country the user typed.
    # The country-param boost reads them separately, to tell a query that is
    # only a country from one with a topic as well.
    coverage_excluded = frozenset(consumed)
    country_terms = _country_terms(query, terms, query_countries) | frozenset(
        term for term in terms if term in currency_words
    )
    # The curated series of a country/section operation, read off their
    # titles. A ticker, a currency, crypto, a named central bank, benchmark or
    # measure, or a weather question already says what the query asks for.
    macro_matches: dict[str, list[MacroKey]] = {}
    if not (has_ticker_token or boost_forex or has_crypto_context
            or central_bank_prefixes or named_operations):
        filler = _QUERY_STOPWORDS | _TWO_LETTER_FILLER | currency_words
        for endpoint in catalog.endpoints:
            if endpoint.macro_keys and _in_scope(endpoint, toolset, source):
                found = match_macro_keys(query, endpoint.macro_keys,
                                         query_countries=query_countries, ignore=filler)
                if found:
                    macro_matches[endpoint.operation_id] = found
    # A question that names no place may still name a series only one place
    # holds: "jobless claims" is the weekly US claims, which no other
    # country's keys hold. Whether its key answers the question is decided
    # after scoring (the unplaced key below). A topic the keys of two places
    # answer ("unemployment rate") names no one series.
    unplaced: dict[str, list[MacroKey]] = {}
    if not (macro_matches or penalty_countries or has_ticker_token or boost_forex
            or has_crypto_context or central_bank_prefixes or named_operations
            or query_names_united_states(query)):
        filler = _QUERY_STOPWORDS | _TWO_LETTER_FILLER
        for endpoint in catalog.endpoints:
            if endpoint.macro_keys and _in_scope(endpoint, toolset, source):
                found = match_unplaced_macro_keys(query, endpoint.macro_keys, ignore=filler,
                                                  limit=len(endpoint.macro_keys))
                if found:
                    unplaced[endpoint.operation_id] = found
        if len({key.key.partition("/")[0] for found in unplaced.values() for key in found}) != 1:
            unplaced = {}
    # The policy rate of a bank with no operation of its own is a curated key.
    if policy_rate.keys:
        for endpoint in catalog.endpoints:
            if endpoint.macro_keys and _in_scope(endpoint, toolset, source):
                found = [key for key in endpoint.macro_keys if key.key in policy_rate.keys]
                if found:
                    macro_matches[endpoint.operation_id] = found
    macro_key_operations = {op: found[0].key for op, found in macro_matches.items()}
    # The key names the series the US-macro boost reaches for through FRED's
    # generic proxy, so the boost stays on the proxy alone, below the key's
    # hit (the clamp after scoring): a call that sends a FRED series id still
    # finds the proxy among the hits it selects from. A US key is US-macro
    # intent of its own ("US nonfarm payrolls" names no word of the boost's).
    proxy_beside_key = bool(macro_matches) and (boost_us_macro or any(
        key.params["country"] == "us" for found in macro_matches.values() for key in found
    ))
    if macro_matches:
        boost_us_macro = False
    # A question that names no place - no country in any spelling, no
    # currency's issuer, no market or port, no central bank, no listing - and
    # asks for a statistic every country reports asks for it for whichever
    # country the user means, in its own word or in other words.
    names_place = bool(
        penalty_countries or central_bank_prefixes or has_ticker_token
        or query_names_united_states(query)
    )
    # The euro area or the EU, as the macro keys read it, is the place of no
    # national source, "EU" as for the ECB, so every national source steps
    # down: "EU inflation" listed the inflation of Argentina and US TIPS among
    # its first answers. It names no place above, since the euro area is no
    # country the operations that take a country boost for.
    names_euro_area = query_names_euro_area(query)
    if names_euro_area:
        penalty_countries.add("EU")
    spelled = {} if names_place else spelled_country_statistics(query)
    any_country_cues = frozenset() if names_place else (
        country_statistic_words(query) | frozenset(spelled)
    )
    query_ratio_bases = _ratio_base_statistics([with_statistic_words(query)])
    ratio_cues = any_country_cues & query_ratio_bases
    ratio_numerators = _ratio_numerators(with_statistic_words(query)) if ratio_cues else {}

    scored: list[tuple[int, Endpoint, list[str]]] = []
    country_answers: set[str] = set()
    place_answers: set[str] = set()
    term_hits: dict[str, tuple[frozenset[str], frozenset[str], frozenset[str]]] = {}

    def score_endpoint(
        endpoint: Endpoint,
        key_operations: dict[str, str],
        answers: set[str],
        places_found: set[str],
        hits: dict[str, tuple[frozenset[str], frozenset[str], frozenset[str]]] | None,
    ) -> tuple[int, list[str]]:
        return _score(
            endpoint,
            [*terms, *sorted(frozenset(spelled) - set(terms))],
            aliases,
            boost_quotes_symbol=boost_quotes_symbol,
            boost_listing_history=listing_period,
            boost_markets_toolset=boost_markets_toolset,
            boost_symbol_input=has_ticker_token,
            boost_etf_symbol=has_ticker_token and not ETF_TICKERS.isdisjoint(tickers),
            boost_forex=boost_forex,
            boost_crypto=boost_crypto,
            boost_us_macro=boost_us_macro or (
                proxy_beside_key and endpoint.operation_id == US_MACRO_PROXY_OPERATION
            ),
            central_bank_prefixes=central_bank_prefixes,
            query_countries=query_countries,
            coverage_excluded=coverage_excluded,
            country_terms=country_terms,
            penalty_countries=penalty_countries,
            named_operations=named_operations,
            named_words=named_words,
            own_named_words=own_named_words.get(endpoint.operation_id, frozenset()),
            macro_key_operations=key_operations,
            country_answers=answers,
            any_country_cues=any_country_cues,
            ratio_cues=ratio_cues,
            ratio_numerators=ratio_numerators,
            spelled_statistics=frozenset(spelled),
            place_answers=places_found,
            term_hits=hits,
            query_ratio_bases=query_ratio_bases,
        )

    for endpoint in catalog.endpoints:
        if not _in_scope(endpoint, toolset, source):
            continue
        score, why = score_endpoint(endpoint, macro_key_operations, country_answers,
                                    place_answers, term_hits if any_country_cues else None)
        if score > 0:
            scored.append((score, endpoint, why))

    # Structural guarantee: beside a macro key's hit, FRED's generic proxy
    # ranks strictly below it, whatever its own text matches - the key names
    # the series, the proxy answers the series ids the key does not hold.
    key_hits = [(score, endpoint.operation_id) for score, endpoint, _ in scored
                if endpoint.operation_id in macro_key_operations]
    if proxy_beside_key and key_hits:
        key_score, key_operation = max(key_hits)
        for i, (score, endpoint, why) in enumerate(scored):
            if endpoint.operation_id == US_MACRO_PROXY_OPERATION and score >= key_score:
                scored[i] = (key_score - 1, endpoint, [*why, f"clamped-below:{key_operation}"])

    # Structural guarantee: a national source of a country the query does not
    # name ranks below every operation that answers for the country it names,
    # and below every other operation that is no such source too:
    # "Venezuelan GDP" found the GDP of Spain above the World Bank and the
    # composite country profile, which take Venezuela, and "Germany trade
    # balance" the US and UK trade balances right after the one operation
    # that answers it.
    answering = [score for score, endpoint, _ in scored
                 if endpoint.operation_id in country_answers]
    if answering:
        floor = min(score for score, endpoint, _ in scored
                    if not _is_foreign_source(endpoint, penalty_countries))
        for i, (score, endpoint, why) in enumerate(scored):
            foreign = _is_foreign_source(endpoint, penalty_countries) or _is_foreign_euro_area(
                endpoint, query_countries, penalty_countries, central_bank_prefixes)
            if foreign and score >= floor:
                scored[i] = (floor - 1, endpoint, [*why, "clamped-below:country-answers"])

    # Structural guarantee: beside a place and a statistic spelled in two
    # words, an operation that matches nothing but the place's words ranks
    # below the best operation that spells the statistic: "US current account"
    # ranked the US weather forecasts first, on the word "us" alone, and the
    # country profile, which answers it for the place by its country
    # parameter, sixth.
    spelled_scores = [score for score, endpoint, _ in scored
                      if endpoint.operation_id in spelling_operations]
    if spelled_scores and country_terms:
        best = max(spelled_scores)
        for i, (score, endpoint, why) in enumerate(scored):
            if (score >= best and endpoint.operation_id not in spelling_operations
                    and _matches_only_place_words(why, country_terms)):
                scored[i] = (best - 1, endpoint, [*why, "clamped-below:statistic"])

    # Equal scores: the default operation of a topic word the query names
    # comes first ("weather" -> the worldwide forecast), and in a listing
    # question the prices of several listings ("TLT SPY"), the price history
    # ("TLT since 2020") or the price ("TLT"), then operation_id.
    defaults = topic_default_operations(query)
    if boost_quotes_symbol:
        defaults |= {
            LISTINGS_DEFAULT_OPERATION if len(tickers) > 1
            else LISTING_HISTORY_OPERATION if listing_period
            else LISTING_DEFAULT_OPERATION
        }

    def tie_break(endpoint: Endpoint) -> tuple[bool, str]:
        return endpoint.operation_id not in defaults, endpoint.operation_id

    # Structural guarantee: a question that names no place and asks for a
    # statistic every country reports ranks a national source that answers it
    # below every operation that answers it for whichever country is meant,
    # for the national source answers for one country the question never
    # named: "how do oil prices affect inflation" found the inflation of
    # Argentina first. A national source keeps its rank when it answers a word
    # of the question that none of those operations answers: in "coffee prices
    # and inflation" FRED holds the coffee price. The two groups trade the
    # slots they hold in the ranking between them, each slot a score and a
    # tie-break, so every other operation keeps its slot and an equal score
    # never ranks a national source first: pushed below the weakest of those
    # operations instead, the national sources fell below operations that
    # answer nothing the question asks ("GDP" found a weather product first).
    # The words that name a statistic in other words are the statistic, which
    # every one of those operations answers: "consumer" and "prices" in
    # "consumer prices". A word named right before the statistic that an
    # operation answering the statistic names in its keywords, and none of
    # those operations answers, is the subject of the question, so that
    # operation takes the first of the slots: "coffee CPI" found the CPI of
    # every country above FRED, which holds the coffee price index. A word
    # only its other fields name is too weak for that ("forecast",
    # "history"), and so is a word elsewhere in the question ("CPI for
    # countries producing coffee" asks for the CPI): either keeps its rank.
    held: dict[str, tuple[bool, str]] = {}
    places = [i for i, (_, endpoint, _) in enumerate(scored)
              if endpoint.operation_id in place_answers]
    if places:
        answered = frozenset().union(
            *(term_hits[scored[i][1].operation_id][1] for i in places), *spelled.values())
        statistic_words = any_country_cues | frozenset().union(*spelled.values())
        # Right before means with only a space between: "coffee, CPI" asks
        # for two things. A question that names more than one statistic
        # ("coffee GDP; CPI") has no one statistic its subject belongs to.
        lowered = query.lower()
        named_before = frozenset(
            word.group() for word, after in pairwise(TOKEN_RE.finditer(lowered))
            if len(any_country_cues) == 1
            and word.group() not in statistic_words
            and not lowered[word.end():after.start()].strip()
            and (after.group() in statistic_words
                 or after.group().removesuffix("s") in statistic_words)
        )
        subjects: list[int] = []
        national: list[int] = []
        for i, (_, endpoint, _) in enumerate(scored):
            strong, matched, keywords = term_hits[endpoint.operation_id]
            if endpoint.operation_id in place_answers or not any_country_cues & matched:
                continue
            if (keywords - answered) & named_before:
                subjects.append(i)
            # The ECB answers for the euro area alone, one place the question
            # never named: "bond yield" found the euro area yield curve first.
            elif (_source_country(endpoint) is not None
                  or (not names_euro_area and endpoint.operation_id.startswith("ecb_"))
                  ) and not strong - answered:
                national.append(i)

        def slot_of(i: int) -> tuple[int, bool, str]:
            return -scored[i][0], *tie_break(scored[i][1])

        # A question that names what its ratio measures asks for that measure:
        # an operation that names it answers before one that names only the
        # base. "government debt to GDP" found GDP per capita first, an
        # operation that names no debt.
        numerators = frozenset().union(*ratio_numerators.values())

        def place_slot(i: int) -> tuple[bool, int, bool, str]:
            names = not numerators or _names_any(numerators, _profile(scored[i][1]).text)
            return not names, *slot_of(i)

        group = {i: "subject" for i in subjects} | {i: "place" for i in places} | {
            i: "national" for i in national}
        before = {i: slot_of(i) for i in group}
        after = dict(zip(
            [*sorted(subjects, key=slot_of), *sorted(places, key=place_slot),
             *sorted(national, key=slot_of)],
            sorted(before.values()), strict=True))
        for i, slot in after.items():
            if slot == before[i]:
                continue
            # The note names what the row moved past: the rows it overtook,
            # or the rows that overtook it.
            if slot < before[i]:
                passed = {group[j] for j in group if before[j] < before[i] and after[j] > slot}
                note = ("lifted-above:any-country-answers" if "place" in passed
                        else "lifted-above:national-sources")
            else:
                passed = {group[j] for j in group if before[j] > before[i] and after[j] < slot}
                note = ("clamped-below:any-country-answers" if "place" in passed
                        else "clamped-below:subject-answers")
            _, endpoint, why = scored[i]
            scored[i] = (-slot[0], endpoint, [*why, note])
            held[endpoint.operation_id] = slot[1:]

    # The unplaced key: a question that names no place, asks for a statistic
    # every country reports and names a word that no operation answering it
    # for whichever country is meant answers, asks for the series one place
    # alone holds under that word: "jobless claims" asks for the weekly US
    # claims, not for the unemployment rate. A word one of them answers asks
    # for that operation ("youth unemployment"). The key ranks right after
    # the first of those operations, never above it, for it answers for a
    # country the question never named, and its tie-break follows that
    # operation's directly, so it takes the next slot.
    if unplaced and places:
        answered = frozenset().union(
            *(term_hits[scored[i][1].operation_id][1] for i in places), *spelled.values())
        unanswered = {term for term in terms if len(term) >= 3} - answered - _QUERY_STOPWORDS
        named = frozenset(form for term in unanswered for form in _singulars(term))
        # The unanswered word names the series: it is the noun its title is
        # named for, not a word that qualifies it ("initial unemployment").
        unplaced = {
            op: found for op, found in unplaced.items()
            if named & _singulars(_head_word(found[0].title))
        }
        if unplaced:
            def order_of(i: int) -> tuple[int, bool, str]:
                return -scored[i][0], *held.get(scored[i][1].operation_id, tie_break(scored[i][1]))

            first = min(places, key=order_of)
            score, after_default, after_tie = order_of(first)
            after_id = scored[first][1].operation_id
            # An answer for any country, or a row already at or above the
            # first of them by score and tie-break, keeps its place.
            ahead = {scored[i][1].operation_id for i in range(len(scored))
                     if order_of(i) <= (score, after_default, after_tie)}
            for endpoint in catalog.endpoints:
                if endpoint.operation_id not in unplaced:
                    continue
                if endpoint.operation_id in place_answers or endpoint.operation_id in ahead:
                    continue
                macro_matches[endpoint.operation_id] = unplaced[endpoint.operation_id][:3]
                key = unplaced[endpoint.operation_id][0].key
                own, why = score_endpoint(endpoint, {endpoint.operation_id: key}, set(), set(), None)
                scored = [item for item in scored if item[1] is not endpoint]
                if own >= -score:
                    own, why = -score, [*why, f"clamped-below:{after_id}"]
                    held[endpoint.operation_id] = (after_default, f"{after_tie}\x00")
                scored.append((own, endpoint, why))

    # Structural guarantee: a deprecated route never outranks its live
    # replacement, whatever the token luck (a query built from the legacy
    # summary text otherwise always wins textually). Clamp strictly below.
    by_id = {endpoint.operation_id: score for score, endpoint, _ in scored}
    for i, (score, endpoint, why) in enumerate(scored):
        if endpoint.deprecated and endpoint.replaced_by:
            # A replacement matching NOTHING (absent from scored)
            # must not leave the deprecated route standing on its legacy
            # text - default 0 clamps it out of the results entirely; the
            # why-pointer to the successor still ships on any surviving entry.
            rep_score = by_id.get(endpoint.replaced_by, 0)
            if score >= rep_score:
                scored[i] = (rep_score - 1, endpoint,
                             [*why, f"clamped-below:{endpoint.replaced_by}"])
    # A deprecated route that names no replacement ranks below every live
    # answer: "geocode Berlin" ranked weather_geocode first, on the word its
    # id spells, above geocoding_search. It stays in the results, for it may
    # be the one operation that answers ("crude oil pipelines"): at a floor of
    # 1 it keeps that score and its tie-break sorts it after the live answers.
    live = [score for score, endpoint, _ in scored
            if score > 0 and not endpoint.deprecated]
    if live:
        floor = min(live)
        for i, (score, endpoint, why) in enumerate(scored):
            if endpoint.deprecated and not endpoint.replaced_by and score >= floor:
                scored[i] = (max(floor - 1, 1), endpoint, [*why, "clamped-below:live"])
                held[endpoint.operation_id] = (True, f"\U0010ffff{endpoint.operation_id}")

    scored = [item for item in scored if item[0] > 0]
    # A slot the trade above handed over keeps its tie-break.
    scored.sort(key=lambda item: (
        -item[0], *held.get(item[1].operation_id, tie_break(item[1])),
    ))
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
            **({"required_groups": [list(g) for g in endpoint.required_groups],
                "groups_mutually_exclusive": endpoint.groups_mutually_exclusive}
               if endpoint.required_groups else {}),
            # The series the query names, best first, with the values to call
            # them by.
            **({"macro_keys": [{**key.to_dict(), "params": key.params}
                               for key in macro_matches[endpoint.operation_id]]}
               if endpoint.operation_id in macro_matches else {}),
            "score": score,
            "why": why,
        }
        for score, endpoint, why in scored[: max(0, limit)]
    ]
