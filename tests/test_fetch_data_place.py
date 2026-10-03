"""fetch_data asks for the place a query names instead of answering for another.

"unemployment rate Germany" ran an operation that takes its place through an
optional countries parameter, with no params at all, and the answer was the
operation's default country. A country the query names now turns that
parameter into a needs_params entry, unless params carry it or the
operation's parameter groups already make the caller choose a place. A
misnamed place key keeps its unknown_parameters refusal
(test_unknown_params.py).
"""

from __future__ import annotations

from typing import Any

import pytest

from sugra_api_mcp.catalog.loader import load_catalog
from sugra_api_mcp.catalog.models import Endpoint
from sugra_api_mcp.catalog.place import PLACE_PARAMETERS, place_gap
from sugra_api_mcp.tools import gateway

_COUNTRIES = {
    "name": "countries",
    "location": "query",
    "required": False,
    "description": "ISO3 codes joined with +",
    "example": "USA+GBR+DEU",
}
_COUNTRY = {
    "name": "country",
    "location": "query",
    "required": False,
    "description": "One country by name",
    "example": "Portugal",
}


def _endpoint(**overrides: Any) -> Endpoint:
    data = {
        "operation_id": "labour_unemployment",
        "method": "GET",
        "path": "/api/v1/labour/unemployment",
        "summary": "Unemployment rate",
        "toolset": "statistics",
        "parameters": [_COUNTRIES, {"name": "start", "location": "query", "required": False}],
    }
    data.update(overrides)
    return Endpoint.from_dict(data)


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get(
        self, path: str, params: dict[str, Any] | None = None, **_kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append((path, params))
        return {"data": [{"period": "2025", "value": 3.4}]}

    async def request(
        self, method: str, path: str, params: dict[str, Any] | None = None, **_kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append((path, params))
        return {"data": [{"period": "2025", "value": 3.4}]}


def _patch(monkeypatch, endpoint: Endpoint) -> _RecordingClient:
    class _FakeCatalog:
        def get(self, operation_id: str) -> Endpoint:
            return endpoint

    async def _one_hit(_catalog, _query, **_kwargs):
        return [{"operation_id": endpoint.operation_id}]

    client = _RecordingClient()
    monkeypatch.setattr(gateway, "load_catalog", lambda: _FakeCatalog())
    monkeypatch.setattr(gateway, "_search_off_loop", _one_hit)
    monkeypatch.setattr(gateway, "get_client", lambda: client)
    return client


async def test_a_named_country_is_asked_for_without_http(monkeypatch) -> None:
    client = _patch(monkeypatch, _endpoint())

    result = await gateway.fetch_data(query="unemployment rate Germany")

    assert client.calls == []
    assert result["needs_params"] == ["countries"]
    assert result["query_countries"] == ["DE"]
    examples = result["selected_endpoint"]["parameter_examples"]
    assert [(p["name"], p["example"]) for p in examples] == [("countries", "USA+GBR+DEU")]
    assert "DE" in result["hint"] and "call_endpoint" in result["hint"]


async def test_the_place_in_params_runs_the_call(monkeypatch) -> None:
    client = _patch(monkeypatch, _endpoint())

    result = await gateway.fetch_data(
        query="unemployment rate Germany", params={"countries": "DEU"}
    )

    assert "error" not in result and "needs_params" not in result
    assert client.calls == [("/api/v1/labour/unemployment", {"countries": "DEU"})]


async def test_a_query_without_a_country_runs_as_before(monkeypatch) -> None:
    client = _patch(monkeypatch, _endpoint())

    result = await gateway.fetch_data(query="unemployment rate")

    assert "needs_params" not in result
    assert client.calls == [("/api/v1/labour/unemployment", {})]


async def test_a_place_sent_as_none_is_still_asked_for(monkeypatch) -> None:
    client = _patch(monkeypatch, _endpoint())

    result = await gateway.fetch_data(
        query="unemployment rate Germany", params={"countries": None}
    )

    assert result["needs_params"] == ["countries"]
    assert client.calls == []


async def test_every_country_the_query_names_is_reported(monkeypatch) -> None:
    _patch(monkeypatch, _endpoint())

    result = await gateway.fetch_data(query="unemployment rate Germany France")

    assert result["query_countries"] == ["DE", "FR"]


@pytest.mark.parametrize(
    ("parameters", "asked"),
    [
        ([_COUNTRY], "country"),
        # Both declared, as on the ILOSTAT operations: the list form is asked.
        ([_COUNTRY, _COUNTRIES], "countries"),
    ],
)
async def test_the_declared_place_parameter_is_the_one_asked(
    monkeypatch, parameters: list[dict[str, Any]], asked: str
) -> None:
    _patch(monkeypatch, _endpoint(parameters=parameters))

    result = await gateway.fetch_data(query="Portugal unemployment")

    assert result["needs_params"] == [asked]
    assert result["query_countries"] == ["PT"]


async def test_a_required_place_is_listed_once(monkeypatch) -> None:
    _patch(monkeypatch, _endpoint(parameters=[{**_COUNTRY, "required": True}]))

    result = await gateway.fetch_data(query="Portugal unemployment")

    assert result["needs_params"] == ["country"]
    assert result["query_countries"] == ["PT"]


async def test_parameter_groups_keep_their_own_check(monkeypatch) -> None:
    """Coordinates or a city choose a weather operation's place; asking for
    country on top would cost a round trip and still fail the group check."""
    weather = _endpoint(
        operation_id="weather_current",
        path="/api/v1/weather/current",
        parameters=[
            {"name": "latitude", "location": "query", "required": False},
            {"name": "longitude", "location": "query", "required": False},
            {"name": "city", "location": "query", "required": False},
            _COUNTRY,
        ],
        required_groups=[["latitude", "longitude"], ["city"]],
    )
    client = _patch(monkeypatch, weather)

    refused = await gateway.fetch_data(query="Portugal weather")
    assert refused["error"] == "missing_required_parameter_groups"

    answered = await gateway.fetch_data(query="Portugal weather", params={"city": "Lisbon"})
    assert "error" not in answered and "needs_params" not in answered
    assert client.calls == [("/api/v1/weather/current", {"city": "Lisbon"})]


def test_an_operation_without_a_place_parameter_is_never_asked() -> None:
    quote = _endpoint(parameters=[{"name": "symbol", "location": "query", "required": True}])

    assert place_gap(quote, "Germany DAX price", {}) is None


def test_every_grouped_operation_with_a_place_chooses_it_through_its_groups() -> None:
    """place_gap leaves grouped operations alone because each of their groups
    names the place. A rebuilt catalog that adds a group naming no place
    would leave that operation's country unguarded."""
    place_names = {
        "latitude", "longitude", "lat", "lon", "city", "street", "postalcode",
        "port_id", "country", "countries", "asn", "continent",
    }
    grouped = [
        endpoint
        for endpoint in load_catalog().endpoints
        if endpoint.required_groups
        and {p.name for p in endpoint.parameters} & set(PLACE_PARAMETERS)
    ]

    assert grouped
    for endpoint in grouped:
        for group in endpoint.required_groups:
            assert place_names & set(group), (endpoint.operation_id, group)


async def test_unemployment_rate_germany_on_the_bundled_catalog(monkeypatch) -> None:
    """The defect as reported: the real catalog must pick the unemployment
    rate and ask for Germany, then call it filtered once the place is given."""
    client = _RecordingClient()
    monkeypatch.setattr(gateway, "get_client", lambda: client)

    asked = await gateway.fetch_data(query="unemployment rate Germany")

    assert asked["selected_endpoint"]["operation_id"] == "ilostat_unemployment"
    assert asked["needs_params"] == ["countries"]
    assert asked["query_countries"] == ["DE"]
    assert client.calls == []

    answered = await gateway.fetch_data(
        query="unemployment rate Germany", params={"countries": "DEU"}
    )

    assert "error" not in answered
    assert client.calls == [("/api/v1/ilostat/unemployment", {"countries": "DEU"})]
