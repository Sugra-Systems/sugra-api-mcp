"""Curated macro keys: the model, the builder, the reading and the ranking.

The macro country/section operation serves curated series under fixed
"<country>/<section>" keys, and the spec lists them with their titles in
x-sugra-macro-keys. Search reads a query against those titles
(catalog/macro_keys.py), and beside a key's hit FRED's generic series proxy
keeps its US-macro boost, ranked below the key, so a call that sends a FRED
series id still finds the proxy among the hits fetch_data selects from.

The search tests give the operation keys of their own, so they hold whatever
key set the bundled catalog carries.
"""

from __future__ import annotations

from typing import Any

import pytest

from sugra_api_mcp.catalog import search as search_module
from sugra_api_mcp.catalog.aliases import detect_query_countries
from sugra_api_mcp.catalog.builder import build_catalog_from_openapi
from sugra_api_mcp.catalog.loader import load_catalog
from sugra_api_mcp.catalog.macro_keys import (
    _read_title,
    match_macro_keys,
    match_unplaced_macro_keys,
)
from sugra_api_mcp.catalog.models import Catalog, Endpoint, MacroKey
from sugra_api_mcp.catalog.search import US_MACRO_PROXY_OPERATION, search_catalog
from sugra_api_mcp.tools import gateway

MACRO_OPERATION = "macro_country_section"

# Real titles from the API's curated catalog.
KEYS = [
    MacroKey(key="us/cpi", title="Consumer Price Index: All Items", freq="monthly"),
    MacroKey(key="us/core-cpi", title="CPI: All Items Less Food & Energy", freq="monthly"),
    MacroKey(key="us/t10y", title="Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity"),
    MacroKey(key="us/t2y", title="Market Yield on U.S. Treasury Securities at 2-Year Constant Maturity"),
    MacroKey(key="us/t10y-t2y", title="10-Year Treasury Constant Maturity Minus 2-Year Treasury"),
    MacroKey(key="us/unrate", title="Unemployment Rate"),
    MacroKey(key="us/payrolls", title="All Employees: Total Nonfarm Payrolls"),
    MacroKey(key="us/jtsjol", title="Job Openings: Total Nonfarm"),
    MacroKey(key="eu/cpi", title="Harmonised Index of Consumer Prices: All Items for the Euro Area (19 countries)"),
    MacroKey(key="jp/gdp", title="Real Gross Domestic Product for Japan"),
    MacroKey(key="cn/sluem1524zschn", title="Youth Unemployment Rate for China"),
]


def _matched(query: str) -> list[str]:
    found = match_macro_keys(query, KEYS, query_countries=detect_query_countries(query))
    return [key.key for key in found]


@pytest.fixture(scope="module")
def keyed_catalog() -> Catalog:
    """The bundled catalog with KEYS on the macro operation and on no other."""
    catalog = load_catalog()
    endpoints: list[Endpoint] = [
        endpoint.model_copy(update={
            "macro_keys": KEYS if endpoint.operation_id == MACRO_OPERATION else [],
        })
        for endpoint in catalog.endpoints
    ]
    return catalog.model_copy(update={"endpoints": endpoints})


def _top_ids(results: list[dict[str, Any]]) -> list[str]:
    return [hit["operation_id"] for hit in results]


def _hit(results: list[dict[str, Any]], operation_id: str) -> dict[str, Any]:
    return next(hit for hit in results if hit["operation_id"] == operation_id)


# ---- The model and the builder ----


def test_macro_key_round_trips_and_names_its_params() -> None:
    key = MacroKey.from_dict({"key": "us/cpi", "title": "Consumer Price Index", "freq": "monthly"})

    assert key.to_dict() == {"key": "us/cpi", "title": "Consumer Price Index", "freq": "monthly"}
    assert MacroKey.from_dict(key.to_dict()) == key
    assert key.params == {"country": "us", "section": "cpi"}
    assert MacroKey.from_dict({"key": "jp/gdp"}).to_dict() == {"key": "jp/gdp", "title": ""}


def test_builder_carries_x_sugra_macro_keys_into_the_bundle_only() -> None:
    """The keys reach the bundle and search, never the endpoint's description:
    describe_endpoint and the CLI print endpoint.to_dict(), and a few hundred
    keys there would bury the operation's own fields."""
    spec = {
        "openapi": "3.1.0",
        "info": {"title": "t", "version": "1"},
        "paths": {
            "/macro/{country}/{section}": {
                "get": {
                    "tags": ["Macro"],
                    "summary": "Macro series",
                    "operationId": "keyed_op",
                    "x-sugra-macro-keys": [
                        {"key": "us/cpi", "title": "Consumer Price Index", "freq": "monthly"},
                        {"title": "no key"},
                        "not an object",
                    ],
                }
            },
            "/plain": {
                "get": {
                    "tags": ["Macro"],
                    "summary": "Plain data",
                    "operationId": "plain_op",
                }
            },
        },
    }
    catalog = build_catalog_from_openapi(spec)
    keyed = catalog.get("keyed_op")

    assert keyed.macro_keys == [MacroKey(key="us/cpi", title="Consumer Price Index", freq="monthly")]
    assert catalog.get("plain_op").macro_keys == []
    assert "macro_keys" not in keyed.to_dict()
    assert keyed.to_dict(include_macro_keys=True)["macro_keys"] == [
        {"key": "us/cpi", "title": "Consumer Price Index", "freq": "monthly"},
    ]
    assert "macro_keys" not in catalog.get("plain_op").to_dict(include_macro_keys=True)

    round_tripped = Catalog.from_dict(catalog.to_dict())
    assert round_tripped.get("keyed_op").macro_keys == keyed.macro_keys
    assert round_tripped.get("plain_op").macro_keys == []


# ---- Reading a query against the titles ----


@pytest.mark.parametrize("query,expected", [
    ("US nonfarm payrolls", "us/payrolls"),
    ("US non-farm payrolls", "us/payrolls"),
    ("US jobs report", "us/payrolls"),
    ("US job openings", "us/jtsjol"),
    ("US CPI", "us/cpi"),
    ("US core CPI", "us/core-cpi"),
    ("US inflation", "us/cpi"),
    ("US inflation rate", "us/cpi"),
    ("US year over year inflation", "us/cpi"),
    # A time window says when, never which series.
    ("US inflation since 2020", "us/cpi"),
    ("US inflation last 5 years", "us/cpi"),
    ("US inflation between 2015 and 2020", "us/cpi"),
    ("US inflation over time", "us/cpi"),
    # A maturity is part of the series; the spread answers only a difference.
    ("US 10-year Treasury yield", "us/t10y"),
    ("US 2-year Treasury yield", "us/t2y"),
    ("US unemployment rate", "us/unrate"),
    ("US jobless rate", "us/unrate"),
    ("euro area HICP inflation", "eu/cpi"),
    ("eurozone inflation", "eu/cpi"),
    ("Japan GDP", "jp/gdp"),
    # "us" the pronoun names no place.
    ("show us Japan GDP", "jp/gdp"),
    ("China youth unemployment", "cn/sluem1524zschn"),
])
def test_a_query_naming_a_curated_series_matches_its_key(query: str, expected: str) -> None:
    assert _matched(query)[:1] == [expected]


@pytest.mark.parametrize("query", [
    # No place: the keys of every country would answer.
    "nonfarm payrolls",
    "wheat inflation",
    # A topic word no title holds: a wrong series is worse than none.
    "US wheat inflation",
    # A narrower series is no answer to the plain measure.
    "China unemployment",
    # A title that says much more than the query asks for.
    "US interest rate",
    "US yield curve",
    "US 10-year minus 2-year spread",
])
def test_a_query_naming_no_curated_series_matches_no_key(query: str) -> None:
    assert _matched(query) == []


# Real titles that name what their series leaves out.
EXCLUDING_KEYS = [
    MacroKey(key="us/cpi", title="Consumer Price Index: All Items"),
    MacroKey(key="us/core-cpi", title="CPI: All Items Less Food & Energy"),
    MacroKey(key="us/core-pce", title="PCE: Excluding Food and Energy"),
    MacroKey(key="us/dgorder", title="Manufacturers' New Orders: Durable Goods"),
    MacroKey(key="us/adxtno", title="Manufacturers' New Orders: Durable Goods Excluding Transportation"),
    MacroKey(key="us/pcu4841224841221",
             title="Producer Price Index by Industry: General Freight Trucking, Long-Distance Less Than Truckload"),
]


def _matched_excluding(query: str) -> list[str]:
    found = match_macro_keys(query, EXCLUDING_KEYS, query_countries=detect_query_countries(query))
    return [key.key for key in found]


@pytest.mark.parametrize("query", [
    "US food inflation",
    "US energy inflation",
    "US food and energy inflation",
    "US food and energy prices",
    # "Less than" in a query compares; it asks for nothing left out.
    "US food inflation less than energy inflation",
])
def test_what_a_series_leaves_out_is_not_found_in_its_title(query: str) -> None:
    """The core CPI holds no food or energy prices, so it answers no question
    about them."""
    assert not {"us/core-cpi", "us/core-pce"} & set(_matched_excluding(query))


@pytest.mark.parametrize("title,left_out", [
    ("CPI: All Items Less Food & Energy", {"less", "food", "energy"}),
    ("Sticky Price Consumer Price Index less Food, Energy, and Shelter",
     {"less", "food", "energy", "shelter"}),
    # The clause after what is left out still names the series.
    ("Consumer Price Index for All Urban Consumers: All Items Less Shelter in U.S. City Average",
     {"less", "shelter"}),
    ("Personal Consumption Expenditures (PCE) Excluding Food and Energy (Chain-Type Price Index)",
     {"excluding", "food", "energy"}),
    ("CPI: All Items Except for Food and Energy in U.S. City Average", {"except", "food", "energy"}),
    ("Producer Price Index by Industry: General Freight Trucking, Long-Distance Less Than Truckload",
     set()),
])
def test_only_the_clause_that_names_what_is_left_out_is_excluded(title: str, left_out: set[str]) -> None:
    read = _read_title("us/x", title)
    assert {read.words[i] for i in read.excluded} == left_out


def test_a_series_except_for_food_answers_no_food_question() -> None:
    keys = [MacroKey(key="us/core-cpi", title="CPI: All Items Except for Food and Energy")]
    assert match_macro_keys("US food inflation", keys, query_countries=detect_query_countries("US food inflation")) == []


@pytest.mark.parametrize("query,expected", [
    ("US core CPI", "us/core-cpi"),
    ("US core PCE", "us/core-pce"),
    ("US core inflation", "us/core-cpi"),
    # A query that asks for something left out names the series that leaves it out.
    ("US CPI less food and energy", "us/core-cpi"),
    ("US PCE excluding food and energy", "us/core-pce"),
    ("US durable goods excluding transportation", "us/adxtno"),
    ("US durable goods orders", "us/dgorder"),
    # "Less than" names the measure, not what it leaves out.
    ("US producer price index general freight trucking less than truckload", "us/pcu4841224841221"),
])
def test_a_series_that_leaves_something_out_keeps_its_own_name(query: str, expected: str) -> None:
    assert _matched_excluding(query)[:1] == [expected]


def test_a_question_about_what_the_core_cpi_leaves_out_ranks_no_core_key(keyed_catalog: Catalog) -> None:
    results = search_catalog(keyed_catalog, "US food inflation", limit=10)
    assert not any(note.startswith("macro-key:") for hit in results for note in hit["why"]), results


# ---- Ranking: the key's operation first, FRED's proxy beside it ----


def test_a_key_hit_ranks_first_and_says_how_to_call_it(keyed_catalog: Catalog) -> None:
    results = search_catalog(keyed_catalog, "US 10-year Treasury yield", limit=5)
    top = results[0]

    assert top["operation_id"] == MACRO_OPERATION, _top_ids(results)
    assert top["macro_keys"][0]["key"] == "us/t10y"
    assert top["macro_keys"][0]["params"] == {"country": "us", "section": "t10y"}
    assert "macro-key:us/t10y" in top["why"]


def test_fred_proxy_stays_below_the_key_and_inside_the_selection_window(keyed_catalog: Catalog) -> None:
    """A model that sends a FRED series id still runs the proxy: fetch_data
    selects from the first five hits, and the key's own operation does not
    declare series_id."""
    results = search_catalog(keyed_catalog, "US 10-year Treasury yield", limit=gateway._SELECTION_WINDOW)
    ids = _top_ids(results)

    assert ids[0] == MACRO_OPERATION, ids
    assert US_MACRO_PROXY_OPERATION in ids, ids
    fred = _hit(results, US_MACRO_PROXY_OPERATION)
    assert fred["score"] < results[0]["score"]
    assert "pattern:us-macro->fred" in fred["why"]
    # Its boost alone would tie or pass the key's hit.
    assert f"clamped-below:{MACRO_OPERATION}" in fred["why"]
    assert "macro_keys" not in fred

    top = keyed_catalog.get(ids[0])
    reselected = gateway._reselect(keyed_catalog, results, top, {"series_id": "DGS10"}, None)
    assert reselected is not None and reselected[0] == US_MACRO_PROXY_OPERATION
    # The key's params run the key's operation.
    assert gateway._reselect(keyed_catalog, results, top, {"country": "us", "section": "t10y"}, None) is None


def test_beside_a_key_only_the_proxy_keeps_the_us_macro_boost(keyed_catalog: Catalog) -> None:
    """The key names the series: the other FRED and Federal Reserve
    operations lose the boost that reached for it."""
    results = search_catalog(keyed_catalog, "US CPI inflation", limit=50)
    boosted = [
        hit["operation_id"] for hit in results
        if {"pattern:us-macro->fred", "pattern:us-macro->fed"} & set(hit["why"])
    ]

    assert results[0]["operation_id"] == MACRO_OPERATION, _top_ids(results)
    assert boosted == [US_MACRO_PROXY_OPERATION]


@pytest.mark.parametrize("query", ["US nonfarm payrolls", "US job openings"])
def test_a_us_key_keeps_the_proxy_in_the_window_without_a_us_macro_word(
    keyed_catalog: Catalog, query: str,
) -> None:
    """Payrolls and job openings name none of the US-macro boost's words; the
    US key is that intent of its own."""
    results = search_catalog(keyed_catalog, query, limit=gateway._SELECTION_WINDOW)
    ids = _top_ids(results)

    assert ids[0] == MACRO_OPERATION, ids
    assert US_MACRO_PROXY_OPERATION in ids, ids
    assert "pattern:us-macro->fred" in _hit(results, US_MACRO_PROXY_OPERATION)["why"]


def test_a_key_of_another_country_gives_the_proxy_no_us_boost(keyed_catalog: Catalog) -> None:
    results = search_catalog(keyed_catalog, "Japan GDP", limit=10)

    assert results[0]["operation_id"] == MACRO_OPERATION, _top_ids(results)
    assert results[0]["macro_keys"][0]["key"] == "jp/gdp"
    for hit in results:
        assert "pattern:us-macro->fred" not in hit["why"], hit["operation_id"]


def test_a_filter_that_leaves_the_key_out_leaves_the_us_macro_boost(keyed_catalog: Catalog) -> None:
    """Keys of an operation the toolset filter drops are not read: the US
    series are then answered inside the filter, every FRED operation keeps
    the US-macro boost and the proxy is clamped below nothing."""
    fred_toolset = keyed_catalog.get(US_MACRO_PROXY_OPERATION).toolset
    assert keyed_catalog.get(MACRO_OPERATION).toolset != fred_toolset

    results = search_catalog(keyed_catalog, "US CPI inflation", toolset=fred_toolset, limit=50)
    fred_hits = [hit for hit in results if hit["operation_id"].startswith("fred_")]

    assert all("macro_keys" not in hit for hit in results)
    assert len(fred_hits) >= 2, _top_ids(results)
    for hit in fred_hits:
        assert "pattern:us-macro->fred" in hit["why"], hit["operation_id"]
        assert not any(reason.startswith("clamped-below:") for reason in hit["why"]), hit["operation_id"]


# ---- A question that names no place: the series one place alone holds ----

CLAIMS_KEYS = [
    MacroKey(key="us/initial-claims", title="Initial Claims for Unemployment Insurance", freq="weekly"),
    MacroKey(key="us/continued-claims", title="Continued Claims (Insured Unemployment)", freq="weekly"),
]


def _with_keys(keys: list[MacroKey]) -> Catalog:
    """The bundled catalog with ``keys`` on the macro operation and on no other."""
    catalog = load_catalog()
    return catalog.model_copy(update={"endpoints": [
        endpoint.model_copy(update={"macro_keys": keys if endpoint.operation_id == MACRO_OPERATION else []})
        for endpoint in catalog.endpoints
    ]})


@pytest.fixture(scope="module")
def claims_catalog() -> Catalog:
    return _with_keys([*KEYS, *CLAIMS_KEYS])


@pytest.mark.parametrize("query", [
    "jobless claims",
    "unemployment claims",
    "initial jobless claims",
    "weekly jobless claims",
])
def test_a_claims_question_ranks_the_claims_key_right_after_the_first_answer(
    claims_catalog: Catalog, query: str,
) -> None:
    """Only the US holds a claims series: its key ranks second, right after
    the first answer for whichever country is meant, never in its place."""
    results = search_catalog(claims_catalog, query, limit=5)

    assert results[0]["operation_id"] != MACRO_OPERATION, _top_ids(results)
    assert results[1]["operation_id"] == MACRO_OPERATION, _top_ids(results)
    assert results[1]["macro_keys"][0]["key"] == "us/initial-claims"
    assert f"clamped-below:{results[0]['operation_id']}" in results[1]["why"], results[1]["why"]
    # The key names the US series; FRED's proxy gets no US boost from it.
    for hit in results:
        assert "pattern:us-macro->fred" not in hit["why"], hit["operation_id"]


@pytest.mark.parametrize("query", [
    "unemployment",
    "unemployment rate",
    "jobless rate",
    # A word an operation for any country answers asks for that operation.
    "youth unemployment",
    # The core CPI leaves food out, so it holds no food inflation.
    "food inflation",
    # An operation naming a word in one number names it in the other: the
    # government payroll operation names "payroll".
    "payrolls",
    # The unanswered word is in no key's title.
    "daily unemployment",
    "weekly unemployment",
    "unemployment benefits",
    # It is in a title, but only qualifies the series the title is named for.
    "initial unemployment",
    "continued unemployment",
    "insured unemployment",
    "unemployment insurance",
    # A named place reads only its own keys.
    "germany jobless claims",
    "Germany unemployment claims",
])
def test_a_question_that_names_no_place_ranks_no_key_otherwise(claims_catalog: Catalog, query: str) -> None:
    results = search_catalog(claims_catalog, query, limit=10)
    assert all("macro_keys" not in hit for hit in results), _top_ids(results)


US_ONLY_KEYS = [
    MacroKey(key="us/housing-starts", title="Housing Starts: Total - New Privately Owned", freq="monthly"),
    MacroKey(key="us/polvoilusdm", title="Global price of Olive Oil", freq="monthly"),
    MacroKey(key="us/retail-sales", title="Advance Retail Sales: Retail Trade", freq="monthly"),
]


@pytest.fixture(scope="module")
def us_only_catalog() -> Catalog:
    return _with_keys([*KEYS, *CLAIMS_KEYS, *US_ONLY_KEYS])


@pytest.mark.parametrize(("query", "key"), [
    ("nonfarm payrolls", "us/payrolls"),
    ("housing starts", "us/housing-starts"),
    ("housing start", "us/housing-starts"),
    ("housing starts numbers", "us/housing-starts"),
    ("olive oil price", "us/polvoilusdm"),
    ("initial claims", "us/initial-claims"),
    ("job openings", "us/jtsjol"),
])
def test_a_series_only_the_us_holds_is_the_answer_to_a_question_naming_no_place(
    us_only_catalog: Catalog, query: str, key: str,
) -> None:
    """A word of its title no operation names as what it answers, in a series
    the US alone holds, reads as the US series (owner, 2026-10-10)."""
    results = search_catalog(us_only_catalog, query, limit=5)

    assert results[0]["operation_id"] == MACRO_OPERATION, _top_ids(results)
    assert results[0]["macro_keys"][0]["key"] == key, results[0]["macro_keys"]


@pytest.mark.parametrize("query", [
    # An operation for any country names every word: the national series of
    # whichever country is meant answers first.
    "retail sales",
    # The word no operation names is not in the series' title.
    "retail sales numbers",
    "inflation",
    "unemployment",
    "government payroll",
])
def test_a_question_an_operation_answers_keeps_that_answer(us_only_catalog: Catalog, query: str) -> None:
    results = search_catalog(us_only_catalog, query, limit=5)
    assert results[0]["operation_id"] != MACRO_OPERATION, _top_ids(results)


def test_a_series_another_country_alone_holds_is_no_default() -> None:
    """The owner's rule reads the US alone: a series only Japan holds is not
    the answer to a question that names no place."""
    catalog = _with_keys([MacroKey(key="jp/housing-starts", title="Housing Starts: Total - New Privately Owned")])
    results = search_catalog(catalog, "housing starts", limit=10)
    assert all("macro_keys" not in hit for hit in results), _top_ids(results)


def _macro_scores_before_its_key(monkeypatch: pytest.MonkeyPatch, score: int) -> None:
    """The macro operation scores ``score`` until a key is read for it."""
    real = search_module._score

    def scored(endpoint: Endpoint, *args: Any, **kwargs: Any) -> tuple[int, list[str]]:
        own, why = real(endpoint, *args, **kwargs)
        if endpoint.operation_id == MACRO_OPERATION and not kwargs["macro_key_operations"]:
            return score, ["test:fixed-score"]
        return own, why

    monkeypatch.setattr(search_module, "_score", scored)


def test_a_key_operation_tied_with_the_first_answer_but_after_it_still_takes_the_key(
    claims_catalog: Catalog, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An equal score that sorts after the first answer is below it, not at it."""
    first = search_catalog(claims_catalog, "jobless claims", limit=1)[0]
    assert first["operation_id"] < MACRO_OPERATION, first["operation_id"]
    _macro_scores_before_its_key(monkeypatch, first["score"])

    results = search_catalog(claims_catalog, "jobless claims", limit=5)

    assert results[0]["operation_id"] == first["operation_id"], _top_ids(results)
    assert results[1]["operation_id"] == MACRO_OPERATION, _top_ids(results)
    assert results[1]["macro_keys"][0]["key"] == "us/initial-claims"
    assert f"clamped-below:{first['operation_id']}" in results[1]["why"], results[1]["why"]


def test_a_key_operation_already_above_the_first_answer_keeps_its_place(
    claims_catalog: Catalog, monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = search_catalog(claims_catalog, "jobless claims", limit=1)[0]
    _macro_scores_before_its_key(monkeypatch, first["score"] + 1)

    results = search_catalog(claims_catalog, "jobless claims", limit=5)

    assert results[0]["operation_id"] == MACRO_OPERATION, _top_ids(results)
    assert "macro_keys" not in results[0], results[0]
    assert results[0]["why"] == ["test:fixed-score"], results[0]["why"]


@pytest.mark.parametrize("query", [
    "germany jobless claims",
    "jobless claims in Canada",
    "euro area jobless claims",
    "US jobless claims",
])
def test_the_unplaced_reader_reads_nothing_for_a_query_that_names_a_place(query: str) -> None:
    assert match_unplaced_macro_keys(query, CLAIMS_KEYS) == []


def test_the_unplaced_reader_reads_the_series_for_a_query_that_names_no_place() -> None:
    found = match_unplaced_macro_keys("jobless claims", CLAIMS_KEYS)
    assert [key.key for key in found][:1] == ["us/initial-claims"], found


@pytest.mark.parametrize(("title", "head"), [
    ("Initial Claims for Unemployment Insurance", "claims"),
    ("Continued Claims (Insured Unemployment)", "claims"),
    ("Federal Surplus or Deficit [-]", "deficit"),
    ("Retail Sales [Weekly]", "sales"),
    ("Unemployment Rate", "rate"),
])
def test_a_title_is_named_for_the_last_word_before_its_first_clause(title: str, head: str) -> None:
    assert search_module._head_word(title) == head


def test_a_series_two_places_hold_ranks_no_key_without_a_place() -> None:
    """Claims series of two countries: the question does not say which."""
    two = _with_keys([*CLAIMS_KEYS, MacroKey(key="ca/initial-claims", title="Initial Claims for Unemployment Insurance")])
    results = search_catalog(two, "jobless claims", limit=10)
    assert all("macro_keys" not in hit for hit in results), _top_ids(results)
