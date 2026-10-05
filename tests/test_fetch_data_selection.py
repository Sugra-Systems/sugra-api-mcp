"""fetch_data runs the hit the params belong to, and says which operation ran.

fetch_data ran only the top search hit, so params meant for another hit were
refused with unknown_parameters: "Bitcoin price" with coin_id hit an operation
that takes no params at all. Another hit within the first five now runs
instead, but only on evidence: a sent key the top hit does not declare, that
is no misspelling of one of its names, and that at most five operations
declare. The hit must be a GET, declare every sent key, not be an open-query
operation, and score at least half the top. A mismatch on common keys only
(limit, currency) stays refused, and the refusal names the hits that would
take every key. A success carries meta.fetch_data. Offline: the bundled
catalog and the search, no HTTP.
"""

from __future__ import annotations

from typing import Any

from sugra_api_mcp.catalog.loader import load_catalog
from sugra_api_mcp.catalog.models import Catalog, Endpoint
from sugra_api_mcp.catalog.search import search_catalog
from sugra_api_mcp.tools import gateway


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get(
        self, path: str, params: dict[str, Any] | None = None, **_kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append((path, params))
        return {"data": [{"value": 1}], "meta": {"endpoint": path}}

    async def request(
        self, method: str, path: str, params: dict[str, Any] | None = None, **_kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append((path, params))
        return {"data": {"ok": True}, "meta": {}}


def _client(monkeypatch) -> _RecordingClient:
    client = _RecordingClient()
    monkeypatch.setattr(gateway, "get_client", lambda: client)
    return client


def _ranks(query: str) -> list[str]:
    return [hit["operation_id"] for hit in search_catalog(load_catalog(), query, limit=5)]


# ---- the bundled catalog ----


async def test_a_rare_key_of_another_hit_runs_that_hit(monkeypatch) -> None:
    client = _client(monkeypatch)

    result = await gateway.fetch_data(query="Bitcoin price", params={"coin_id": "bitcoin"})

    assert client.calls == [("/api/v1/crypto/bitcoin/price", {})]
    assert result["meta"]["fetch_data"] == {
        "operation_id": "crypto_coin_id_price",
        "selected_by": "params",
        "top_match": "onchain_bitcoin_price",
    }
    assert result["meta"]["endpoint"] == "/api/v1/crypto/bitcoin/price"


async def test_the_window_reaches_the_fifth_hit(monkeypatch) -> None:
    """The plan said the first three; the history operation ranks fifth."""
    assert _ranks("Bitcoin price history").index("crypto_coin_id_history") == 4
    client = _client(monkeypatch)

    result = await gateway.fetch_data(
        query="Bitcoin price history", params={"coin_id": "bitcoin", "days": 30})

    assert client.calls == [("/api/v1/crypto/bitcoin/history", {"days": 30})]
    assert result["meta"]["fetch_data"] == {
        "operation_id": "crypto_coin_id_history",
        "selected_by": "params",
        "top_match": "onchain_bitcoin_price_history",
    }


async def test_a_common_key_is_no_evidence_even_at_a_tie(monkeypatch) -> None:
    """currency is declared by ten operations; the history hit ties the top at 46."""
    client = _client(monkeypatch)

    result = await gateway.fetch_data(query="Bitcoin price", params={"currency": "usd"})

    assert client.calls == []
    assert result["error"] == "unknown_parameters"
    assert result["operation_id"] == "onchain_bitcoin_price"
    assert result["unknown"] == ["currency"]
    assert result["alternatives"] == ["onchain_bitcoin_price_history"]


async def test_limit_alone_never_moves_the_selection(monkeypatch) -> None:
    client = _client(monkeypatch)

    asked = await gateway.fetch_data(query="USDT price", params={"limit": 5})
    refused = await gateway.fetch_data(
        query="USDT price", params={"coin_id": "tether", "limit": 5})

    assert client.calls == []
    assert asked["needs_params"] == ["coin_id"]
    assert asked["selected_endpoint"]["operation_id"] == "crypto_coin_id_price"
    assert asked["hint"].startswith("The top match `crypto_coin_id_price`")
    assert refused["error"] == "unknown_parameters"
    assert refused["operation_id"] == "crypto_coin_id_price"
    assert refused["unknown"] == ["limit"]
    assert "alternatives" not in refused


async def test_a_misspelled_key_keeps_did_you_mean(monkeypatch) -> None:
    """vs_currency is rare and the fifth hit declares it, but it reads as currency."""
    client = _client(monkeypatch)

    result = await gateway.fetch_data(
        query="Bitcoin price history", params={"vs_currency": "usd"})

    assert client.calls == []
    assert result["error"] == "unknown_parameters"
    assert result["operation_id"] == "onchain_bitcoin_price_history"
    assert result["did_you_mean"] == {"vs_currency": "currency"}
    assert result["alternatives"] == ["crypto_coin_id_history"]


def test_an_open_query_operation_is_never_selectable() -> None:
    catalog = load_catalog()

    assert gateway._selectable(
        catalog, "statistical_agencies_statbank_dk_data_table_id", {"table_id"}) is None
    assert gateway._selectable(
        catalog, "statistical_agencies_statbank_dk_tableinfo_table_id", {"table_id"}) is not None


async def test_an_open_query_top_match_still_runs_its_filters(monkeypatch) -> None:
    client = _client(monkeypatch)

    result = await gateway.fetch_data(
        query="Denmark statbank table data", params={"table_id": "FOLK1A", "OMRÅDE": "000"})

    assert client.calls == [
        ("/api/v1/statistical-agencies/statbank-dk/data/FOLK1A", {"OMRÅDE": "000"})]
    assert result["meta"]["fetch_data"] == {
        "operation_id": "statistical_agencies_statbank_dk_data_table_id",
        "selected_by": "query",
    }


async def test_a_post_hit_is_never_chosen(monkeypatch) -> None:
    """post_openfigi_search ranks third and declares the rare marketSecDes."""
    catalog = load_catalog()
    assert "post_openfigi_search" in _ranks("OpenFIGI exchange codes")[:3]
    assert gateway._selectable(catalog, "post_openfigi_search", {"marketSecDes"}) is None
    client = _client(monkeypatch)

    result = await gateway.fetch_data(
        query="OpenFIGI exchange codes", params={"marketSecDes": "Equity"})

    assert client.calls == []
    assert result["selected_endpoint"]["operation_id"] == "market_exchange_code_holidays"
    assert result["needs_params"] == ["code"]


async def test_the_selected_hit_gets_the_missing_check(monkeypatch) -> None:
    """The top hit requires country and year; the selected one country only."""
    query = "International demography: one country's series"
    client = _client(monkeypatch)

    result = await gateway.fetch_data(query=query, params={"indicators": "POP"})

    assert client.calls == []
    assert result["needs_params"] == ["country"]
    assert result["selected_endpoint"]["operation_id"] == "census_population_international"
    assert result["hint"].startswith("The selected match `census_population_international`")
    assert result["candidate_endpoints"] == search_catalog(load_catalog(), query, limit=3)


async def test_the_selected_hit_gets_the_place_check(monkeypatch) -> None:
    """The top hit takes no country; the selected one does, and the query names Germany."""
    query = "Germany international demography one country series"
    client = _client(monkeypatch)

    result = await gateway.fetch_data(query=query, params={"section": "labour"})

    assert client.calls == []
    assert result["selected_endpoint"]["operation_id"] == "macro_country_section"
    assert result["query_countries"] == ["DE"]
    assert "country" in result["needs_params"]


async def test_the_query_selection_is_reported_on_success(monkeypatch) -> None:
    client = _client(monkeypatch)

    result = await gateway.fetch_data(query="Bitcoin price")

    assert client.calls == [("/api/v1/onchain/bitcoin/price", {})]
    assert result["meta"]["fetch_data"] == {
        "operation_id": "onchain_bitcoin_price",
        "selected_by": "query",
    }


# ---- a small catalog: groups and the score floor ----


def _small_catalog() -> Catalog:
    top = Endpoint.from_dict({
        "operation_id": "harbour_gauge_latest",
        "method": "GET",
        "path": "/api/v1/harbour/gauge/latest",
        "summary": "Harbour gauge latest",
        "parameters": [],
    })
    grouped = Endpoint.from_dict({
        "operation_id": "harbour_gauge_station",
        "method": "GET",
        "path": "/api/v1/harbour/gauge/station",
        "summary": "Harbour gauge by station",
        "parameters": [
            {"name": "station_code", "location": "query", "required": False},
            {"name": "latitude", "location": "query", "required": False},
            {"name": "longitude", "location": "query", "required": False},
            {"name": "city", "location": "query", "required": False},
        ],
        "required_groups": [["latitude", "longitude"], ["city"]],
    })
    return Catalog(source="test", endpoints=[top, grouped])


def _patch_small(monkeypatch, second_score: int) -> _RecordingClient:
    catalog = _small_catalog()
    hits = [
        {"operation_id": "harbour_gauge_latest", "score": 40},
        {"operation_id": "harbour_gauge_station", "score": second_score},
    ]

    async def _search(_catalog, _query, **_kwargs):
        return hits

    monkeypatch.setattr(gateway, "load_catalog", lambda: catalog)
    monkeypatch.setattr(gateway, "_search_off_loop", _search)
    return _client(monkeypatch)


async def test_the_selected_hit_gets_the_group_check(monkeypatch) -> None:
    client = _patch_small(monkeypatch, second_score=30)

    refused = await gateway.fetch_data(query="harbour gauge", params={"station_code": "OSL1"})
    served = await gateway.fetch_data(
        query="harbour gauge", params={"station_code": "OSL1", "city": "Oslo"})

    assert refused["error"] == "missing_required_parameter_groups"
    assert refused["operation_id"] == "harbour_gauge_station"
    assert client.calls == [("/api/v1/harbour/gauge/station", {"station_code": "OSL1", "city": "Oslo"})]
    assert served["meta"]["fetch_data"]["selected_by"] == "params"


async def test_a_hit_under_half_the_top_score_is_not_selected(monkeypatch) -> None:
    client = _patch_small(monkeypatch, second_score=19)

    result = await gateway.fetch_data(
        query="harbour gauge", params={"station_code": "OSL1", "city": "Oslo"})

    assert client.calls == []
    assert result["error"] == "unknown_parameters"
    assert result["operation_id"] == "harbour_gauge_latest"
    assert result["alternatives"] == ["harbour_gauge_station"]


async def test_the_candidate_list_stays_at_three(monkeypatch) -> None:
    """The selection reads five hits; the payloads still list the first three."""
    client = _client(monkeypatch)

    result = await gateway.fetch_data(query="Bitcoin price", params={"vs_currency": "usd"})

    assert client.calls == []
    assert result["selected_endpoint"]["operation_id"] == "crypto_coin_id_price"
    assert [hit["operation_id"] for hit in result["candidate_endpoints"]] == _ranks(
        "Bitcoin price")[:3]


async def test_the_score_floor_is_inclusive_at_half(monkeypatch) -> None:
    client = _patch_small(monkeypatch, second_score=20)

    result = await gateway.fetch_data(
        query="harbour gauge", params={"station_code": "OSL1", "city": "Oslo"})

    assert client.calls == [("/api/v1/harbour/gauge/station", {"station_code": "OSL1", "city": "Oslo"})]
    assert result["meta"]["fetch_data"]["operation_id"] == "harbour_gauge_station"


async def test_just_under_half_is_refused(monkeypatch) -> None:
    client = _patch_small(monkeypatch, second_score=19.999)

    result = await gateway.fetch_data(
        query="harbour gauge", params={"station_code": "OSL1", "city": "Oslo"})

    assert client.calls == []
    assert result["error"] == "unknown_parameters"


# ---- synthetic catalogs: rarity, non-finite scores, malformed hits, the cap ----


def _gauge(operation_id: str, *names: str) -> Endpoint:
    return Endpoint.from_dict({
        "operation_id": operation_id,
        "method": "GET",
        "path": f"/api/v1/{operation_id}",
        "summary": operation_id,
        "parameters": [{"name": name, "location": "query", "required": False} for name in names],
    })


def _patch(monkeypatch, endpoints: list[Endpoint], hits: list[Any]) -> _RecordingClient:
    catalog = Catalog(source="test", endpoints=endpoints)

    async def _search(_catalog, _query, **_kwargs):
        return hits

    monkeypatch.setattr(gateway, "load_catalog", lambda: catalog)
    monkeypatch.setattr(gateway, "_search_off_loop", _search)
    return _client(monkeypatch)


def _declaring(total: int) -> list[Endpoint]:
    """The top match, one tied candidate, and fillers so `total` ops declare gauge_id."""
    fillers = [_gauge(f"filler_{index}", "gauge_id") for index in range(total - 1)]
    return [_gauge("top_op"), _gauge("candidate_op", "gauge_id"), *fillers]


async def test_five_declaring_operations_are_rare(monkeypatch) -> None:
    hits = [{"operation_id": "top_op", "score": 40}, {"operation_id": "candidate_op", "score": 40}]
    client = _patch(monkeypatch, _declaring(5), hits)

    result = await gateway.fetch_data(query="gauge", params={"gauge_id": "G1"})

    assert client.calls == [("/api/v1/candidate_op", {"gauge_id": "G1"})]
    assert result["meta"]["fetch_data"]["selected_by"] == "params"


async def test_six_declaring_operations_are_not_rare(monkeypatch) -> None:
    hits = [{"operation_id": "top_op", "score": 40}, {"operation_id": "candidate_op", "score": 40}]
    client = _patch(monkeypatch, _declaring(6), hits)

    result = await gateway.fetch_data(query="gauge", params={"gauge_id": "G1"})

    assert client.calls == []
    assert result["error"] == "unknown_parameters"
    assert result["operation_id"] == "top_op"
    assert result["alternatives"] == ["candidate_op"]


def test_the_common_keys_of_the_bundled_catalog_are_not_rare() -> None:
    counts = gateway._operations_per_key(load_catalog())

    for key in ("limit", "symbol", "start_date", "currency"):
        assert counts.get(key, 0) > gateway._RARE_KEY_MAX_OPERATIONS, (key, counts.get(key))


async def test_a_non_finite_top_score_never_reselects(monkeypatch) -> None:
    for top_score in (float("nan"), float("inf")):
        hits = [{"operation_id": "top_op", "score": top_score},
                {"operation_id": "candidate_op", "score": 30}]
        client = _patch(monkeypatch, _declaring(2), hits)

        result = await gateway.fetch_data(query="gauge", params={"gauge_id": "G1"})

        assert client.calls == [], top_score
        assert result["error"] == "unknown_parameters", top_score
        assert result["operation_id"] == "top_op", top_score


async def test_a_non_finite_candidate_score_is_never_selected(monkeypatch) -> None:
    for candidate_score in (float("nan"), float("inf")):
        hits = [{"operation_id": "top_op", "score": 40},
                {"operation_id": "candidate_op", "score": candidate_score},
                {"operation_id": "filler_0", "score": 30}]
        client = _patch(monkeypatch, _declaring(2), hits)

        result = await gateway.fetch_data(query="gauge", params={"gauge_id": "G1"})

        assert client.calls == [("/api/v1/filler_0", {"gauge_id": "G1"})], candidate_score
        assert result["meta"]["fetch_data"]["operation_id"] == "filler_0", candidate_score


async def test_a_malformed_hit_is_skipped_not_fatal(monkeypatch) -> None:
    hits = [
        {"operation_id": "top_op", "score": 40},
        {"score": 40},
        {"operation_id": 7, "score": 40},
        {"operation_id": "no_such_operation", "score": 40},
        {"operation_id": "candidate_op", "score": 40},
    ]
    client = _patch(monkeypatch, _declaring(2), hits)

    result = await gateway.fetch_data(query="gauge", params={"gauge_id": "G1"})

    assert client.calls == [("/api/v1/candidate_op", {"gauge_id": "G1"})]
    assert result["meta"]["fetch_data"]["operation_id"] == "candidate_op"


async def test_alternatives_stop_at_three(monkeypatch) -> None:
    endpoints = [_gauge("top_op"), *(_gauge(f"filler_{index}", "gauge_id") for index in range(4))]
    hits = [{"operation_id": "top_op", "score": 40},
            *({"operation_id": f"filler_{index}", "score": 10} for index in range(4))]
    client = _patch(monkeypatch, endpoints, hits)

    result = await gateway.fetch_data(query="gauge", params={"gauge_id": "G1"})

    assert client.calls == []
    assert result["error"] == "unknown_parameters"
    assert result["alternatives"] == ["filler_0", "filler_1", "filler_2"]


def test_alternatives_need_a_refused_key() -> None:
    catalog = Catalog(source="test", endpoints=_declaring(3))
    hits = [{"operation_id": "top_op", "score": 40}, {"operation_id": "candidate_op", "score": 40}]

    assert gateway._alternatives(catalog, hits, "top_op", {}, ["gauge_id"]) == []
    assert gateway._alternatives(catalog, hits, "top_op", {"gauge_id": "G1"}, []) == []
    assert gateway._alternatives(catalog, hits, "top_op", {"gauge_id": "G1"}, None) == []
    assert gateway._alternatives(
        catalog, hits, "top_op", {"gauge_id": "G1"}, ["gauge_id"]) == ["candidate_op"]


def test_a_sent_body_skips_the_reselection() -> None:
    catalog = Catalog(source="test", endpoints=_declaring(2))
    hits = [{"operation_id": "top_op", "score": 40}, {"operation_id": "candidate_op", "score": 40}]
    top = catalog.get("top_op")
    params = {"gauge_id": "G1"}

    assert gateway._reselect(catalog, hits, top, params, None)[0] == "candidate_op"
    assert gateway._reselect(catalog, hits, top, params, {"item": 1}) is None


def test_the_meta_helper_copies_and_leaves_a_foreign_meta_alone() -> None:
    selection = {"operation_id": "op", "selected_by": "query"}
    original = {"data": [1], "meta": {"endpoint": "/x"}}

    updated = gateway._with_fetch_meta(original, selection)

    assert updated["meta"] == {"endpoint": "/x", "fetch_data": selection}
    assert original == {"data": [1], "meta": {"endpoint": "/x"}}
    assert gateway._with_fetch_meta({"data": [1]}, selection)["meta"] == {"fetch_data": selection}
    foreign = {"data": [1], "meta": ["not", "a", "dict"]}
    assert gateway._with_fetch_meta(foreign, selection) is foreign
