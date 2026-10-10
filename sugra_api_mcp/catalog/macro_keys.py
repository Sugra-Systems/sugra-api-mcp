"""Read a query against the curated macro series keys.

The macro country/section operation serves a few hundred curated series,
each under a fixed "<country>/<section>" key, but its own text names only
"country" and "section": "US nonfarm payrolls" or "Japan GDP growth"
matched it on no word at all, and search ranked a proxy or a wrong source
first. Every key carries its series' title ("All Employees: Total Nonfarm
Payrolls"), and this module reads the query against those titles.

The reading is strict on purpose, because a wrong series is worse than
none. The query must name a place that has keys, every topic word must be
found in a title or in its key, and a title that says much more than the
query asks for does not match, so "US interest rate" stays unanswered here
rather than turning into the 10-year real rate. A word this module does not
know is a topic word like any other: "wheat inflation" can never match the
consumer price index.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

from ._countries import COUNTRY_QUERY_TERMS
from .aliases import query_names_united_states
from .models import MacroKey

_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Spellings of one measure that a title and a query write differently.
_SHARED_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bnon[\s-]+farm\b"), "nonfarm"),
    (re.compile(r"\byear[\s-]+(?:on|over|to)[\s-]+year\b"), "yoy"),
    (re.compile(r"\bmonth[\s-]+(?:on|over|to)[\s-]+month\b"), "mom"),
    (re.compile(r"\bquarter[\s-]+(?:on|over|to)[\s-]+quarter\b"), "qoq"),
)

# A time window says WHEN, never WHICH series: "since 2020", "last 5 years",
# "over time". A hyphenated "10-year" stays, since it names a maturity.
# "Inflation rate" and "growth rate" name the measure, not a rate series.
_QUERY_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    *_SHARED_REWRITES,
    (re.compile(r"\b(?:last|past|previous|recent|next|coming|this)\s+(?:\d+\s+)?"
                r"(?:years?|months?|quarters?|weeks?|days?|decades?)\b"), " "),
    (re.compile(r"\b\d+\s+(?:years|months|quarters|weeks|days|decades)\b"), " "),
    (re.compile(r"\b(?:(?:since|after|before|until|till|through|between|during)\s+)?"
                r"(?:19|20)\d\ds?\b"), " "),
    (re.compile(r"\b(?:over\s+time|year\s+to\s+date|ytd)\b"), " "),
    (re.compile(r"\b(inflation|growth)\s+rates?\b"), r"\1"),
)

# "(19 countries)" in a euro area title counts the members; it names nothing.
_TITLE_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    *_SHARED_REWRITES,
    (re.compile(r"\([\d\s-]*countries\)"), " "),
)

# Function words a title or a query writes around the measure.
_STOPWORDS = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "of", "on", "or",
    "per", "than", "the", "to", "versus", "vs", "with",
})

# A title that subtracts one series from another answers only a query that
# asks for the difference.
_SUBTRACTION_WORDS = frozenset({
    "curve", "difference", "gap", "minus", "spread", "versus", "vs",
})


def _fold(word: str) -> str:
    """The word without a plural "s": "payrolls" reads as "payroll"."""
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _folded(words: Iterable[str]) -> frozenset[str]:
    return frozenset(_fold(word) for word in words)


def _words(text: str) -> list[str]:
    """Lowercase word tokens; a lone letter is dropped, a lone digit kept."""
    return [token for token in _TOKEN_RE.findall(text.lower())
            if len(token) >= 2 or token.isdigit()]


# Words that ask for data without naming a series.
_REQUEST_WORDS = _folded({
    "chart", "check", "current", "currently", "data", "dataset", "display",
    "download", "economic", "economy", "fetch", "figure", "find", "get",
    "give", "graph", "historical", "history", "indicator", "info",
    "information", "last", "latest", "like", "list", "look", "macro", "many",
    "me", "much", "need", "newest", "now", "number", "official", "please",
    "plot", "print", "reading", "recent", "recently", "release", "report",
    "see", "series", "show", "statistics", "stats", "tell", "time", "today",
    "trend", "update", "value", "want",
})

# The shape a measure is asked in. Optional in a query and neutral in a
# title; a title that carries the shape the query asks for ranks first.
_FORM_WORDS = _folded({
    "annual", "annualized", "change", "daily", "growth", "level", "mom",
    "monthly", "pct", "percent", "percentage", "qoq", "quarterly", "rate",
    "ratio", "weekly", "yearly", "yoy",
})

# Title words that say nothing about which measure the series is: a query
# may name them, but a title is not judged by them. "Total" and "all" also
# mark the aggregate, which comes before its parts when the query names none
# ("job openings" is the total, not construction).
_AGGREGATE_WORDS = _folded({"all", "total"})
_GENERIC_TITLE_WORDS = _AGGREGATE_WORDS | _folded({"items", "persons"})

# Title words that narrow a measure to part of it. A title with one the
# query does not ask for is a different measure: the youth unemployment rate
# is not the unemployment rate, income per capita is not income.
_NARROWING_WORDS = _folded({
    "black", "capita", "female", "male", "men", "women", "youth",
})

# Other spellings of a query word, each matched as consecutive title words.
# Keys are folded, like the query words they look up.
_ALTERNATIVES: dict[str, tuple[str, ...]] = {
    "gdp": ("gross domestic product",),
    "cpi": ("consumer price",),
    "inflation": ("cpi", "hicp", "pce", "consumer price"),
    "hicp": ("harmonised index of consumer price", "harmonized index of consumer price"),
    "pce": ("personal consumption expenditure",),
    "ppi": ("producer price",),
    "unemployment": ("unemployed", "jobless"),
    "unemployed": ("unemployment", "jobless"),
    "jobless": ("unemployment", "unemployed"),
    "yield": ("rate",),
    "job": ("payroll",),
}

# The euro area has keys under "eu" but no ISO country code of its own.
_EU_PHRASES: tuple[str, ...] = (
    "eu", "euro area", "euro zone", "eurozone", "european union",
)
# Spellings of the United States the country vocabulary leaves to the
# US-context pattern ("US" is a word as often as it is a country code).
_US_PHRASES: tuple[str, ...] = ("us", "usa", "united states", "america", "american")


@lru_cache(maxsize=256)
def _alternatives(term: str) -> tuple[tuple[str, ...], ...]:
    spellings = (term, *_ALTERNATIVES.get(term, ()))
    found: list[tuple[str, ...]] = []
    for spelling in spellings:
        words = tuple(_fold(word) for word in _words(spelling) if word not in _STOPWORDS)
        if words and words not in found:
            found.append(words)
    return tuple(found)


@lru_cache(maxsize=64)
def _place_phrases(place: str) -> tuple[tuple[str, ...], ...]:
    """Every spelling of a place, as words: "gb" -> ("united", "kingdom"), ("uk",), ..."""
    phrases = {place} | {
        phrase for phrase, code in COUNTRY_QUERY_TERMS.items() if code == place.upper()
    }
    if place == "us":
        phrases.update(_US_PHRASES)
    if place == "eu":
        phrases.update(_EU_PHRASES)
    return tuple(sorted({tuple(_words(phrase)) for phrase in phrases} - {()}))


def _spans(words: Sequence[str], phrase: tuple[str, ...]) -> list[int]:
    width = len(phrase)
    return [start for start in range(len(words) - width + 1)
            if tuple(words[start:start + width]) == phrase]


@dataclass(frozen=True)
class _Title:
    """One key's title, read once."""

    words: tuple[str, ...]          # folded, function words out
    content: frozenset[int]         # indexes of the words that name the measure
    forms: frozenset[str]
    narrowing: frozenset[str]
    subtracts: bool
    aggregate: bool
    parts: frozenset[str]           # the folded words of the section


@lru_cache(maxsize=4096)
def _read_title(key: str, title: str) -> _Title:
    country, _, section = key.partition("/")
    text = title.lower()
    for pattern, replacement in _TITLE_REWRITES:
        text = pattern.sub(replacement, text)
    raw = _words(text)
    generic = _GENERIC_TITLE_WORDS | {
        _fold(word) for phrase in _place_phrases(country) for word in phrase
    }
    words = tuple(_fold(token) for token in raw if token not in _STOPWORDS)
    return _Title(
        words=words,
        content=frozenset(i for i, word in enumerate(words)
                          if word not in _FORM_WORDS and word not in generic),
        forms=frozenset(word for word in words if word in _FORM_WORDS),
        narrowing=frozenset(word for word in words if word in _NARROWING_WORDS),
        subtracts="minus" in raw,
        aggregate=any(word in _AGGREGATE_WORDS for word in words),
        parts=frozenset(_fold(part) for part in section.split("-") if part),
    )


@dataclass(frozen=True)
class _Query:
    places: frozenset[str]
    topic: tuple[str, ...]
    forms: frozenset[str]
    subtraction: bool


def _query_words(query: str) -> list[str]:
    text = query.lower()
    for pattern, replacement in _QUERY_REWRITES:
        text = pattern.sub(replacement, text)
    return _words(text)


def query_names_euro_area(query: str) -> bool:
    """Whether the query names the euro area or the European Union, the place
    the "eu" keys answer for."""
    words = _query_words(query)
    return any(_spans(words, tuple(_words(phrase))) for phrase in _EU_PHRASES)


def _read_query(query: str, query_countries: set[str], ignore: frozenset[str]) -> _Query:
    words = _query_words(query)
    places = {code.lower() for code in query_countries}
    if query_names_euro_area(query):
        places.add("eu")
    # A bare "US" is a country only when no other place is named: "show us
    # Japan GDP" asks about Japan.
    if not places - {"us"} and query_names_united_states(query):
        places.add("us")
    kept = list(words)
    for place in places:
        for phrase in _place_phrases(place):
            for start in _spans(words, phrase):
                kept[start:start + len(phrase)] = [""] * len(phrase)
    topic: list[str] = []
    forms: set[str] = set()
    for token in kept:
        # A "us" left here names no place: the pronoun of "show us Japan GDP".
        if not token or token == "us" or token in ignore or token in _STOPWORDS:
            continue
        word = _fold(token)
        if word in _REQUEST_WORDS:
            continue
        if word in _FORM_WORDS:
            forms.add(word)
        elif word not in topic:
            topic.append(word)
    return _Query(
        places=frozenset(places),
        topic=tuple(topic),
        forms=frozenset(forms),
        subtraction=any(word in _SUBTRACTION_WORDS for word in words),
    )


def _rank(title: _Title, query: _Query) -> tuple[bool, int, int, bool, int] | None:
    """The key's rank for the query, lower first; None when it does not match."""
    if title.narrowing - set(query.topic):
        return None
    if title.subtracts and not query.subtraction:
        return None
    covered: set[int] = set()
    singles: set[str] = set()
    for term in query.topic:
        found = False
        for alternative in _alternatives(term):
            if len(alternative) == 1:
                singles.add(alternative[0])
                found = found or alternative[0] in title.parts
            for start in _spans(title.words, alternative):
                covered.update(range(start, start + len(alternative)))
                found = True
        if not found:
            return None
    # Every word of the key itself named by the query ("US core CPI" ->
    # us/core-cpi) is the strongest reading there is.
    whole_key = bool(title.parts) and title.parts <= singles
    hits = len(covered & title.content)
    # Otherwise at least half of what the title says must be what was asked.
    if not whole_key and hits * 2 < len(title.content):
        return None
    return (not whole_key, -len(query.forms & title.forms), len(title.content) - hits,
            not title.aggregate, -hits)


def match_macro_keys(
    query: str,
    keys: Sequence[MacroKey],
    *,
    query_countries: set[str],
    ignore: frozenset[str] = frozenset(),
    limit: int = 3,
) -> list[MacroKey]:
    """The keys the query names, best first; empty when it names none.

    ``query_countries`` are the ISO codes search read from the query, and
    ``ignore`` the filler words search drops from it. Ties keep the order of
    ``keys``, which lists the headline series of each country first.
    """
    read = _read_query(query, query_countries, ignore)
    if not read.places or not read.topic:
        return []
    ranked: list[tuple[tuple[bool, int, int, bool, int], int, MacroKey]] = []
    for index, key in enumerate(keys):
        if key.key.partition("/")[0] not in read.places:
            continue
        rank = _rank(_read_title(key.key, key.title), read)
        if rank is not None:
            ranked.append((rank, index, key))
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [key for _, _, key in ranked[: max(0, limit)]]
