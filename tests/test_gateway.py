"""Gateway tool tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sugra_api_mcp.catalog.builder import build_catalog_from_openapi
from sugra_api_mcp.tools import gateway

FIXTURE = Path(__file__).parent / "fixtures" / "openapi_minimal.json"


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("GET", path, params, None))
        return {"data": [{"symbol": "AAPL", "price": 200, "extra": "drop"}], "meta": {}}

    async def post(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("POST", path, None, json))
        return {"data": {"ok": True}, "meta": {}}

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, path, params, json))
        return {"data": {"ok": True}, "meta": {}}


def _fixture_catalog():
    return build_catalog_from_openapi(json.loads(FIXTURE.read_text(encoding="utf-8")))


async def test_call_endpoint_builds_correct_get_request(monkeypatch) -> None:
    fake = FakeClient()
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    result = await gateway.call_endpoint(
        "quotes_symbol_price",
        params={"symbol": "AAPL"},
        fields=["symbol", "price"],
        limit=1,
    )

    assert fake.calls == [("GET", "/api/v1/quotes/AAPL/price", {}, None)]
    assert result["data"] == [{"symbol": "AAPL", "price": 200}]


async def test_call_endpoint_post_preserves_query_params_and_body(monkeypatch) -> None:
    fake = FakeClient()
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    result = await gateway.call_endpoint(
        "openfigi_map",
        params={"limit": 10},
        body={"jobs": [{"idType": "TICKER", "idValue": "AAPL"}]},
    )

    assert fake.calls == [
        (
            "POST",
            "/api/v1/openfigi/map",
            {"limit": 10},
            {"jobs": [{"idType": "TICKER", "idValue": "AAPL"}]},
        )
    ]
    assert result["data"] == {"ok": True}


async def test_call_endpoint_validates_missing_required_params(monkeypatch) -> None:
    fake = FakeClient()
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    result = await gateway.call_endpoint("quotes_symbol_price", params={})

    assert result == {
        "error": "missing_required_parameters",
        "operation_id": "quotes_symbol_price",
        "missing": ["symbol"],
    }
    assert fake.calls == []


async def test_gateway_lists_toolsets_and_sources(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)

    toolsets = await gateway.list_toolsets()
    sources = await gateway.list_sources()

    assert any(toolset["name"] == "markets" for toolset in toolsets["toolsets"])
    assert sources["source_families"]
    assert sources["source_families"][0]["name"] != "fixture"


async def test_search_endpoints_accepts_toolset_filter(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)

    result = await gateway.search_endpoints("NASDAQ futures", toolset="markets")

    assert result["results"]
    assert {item["toolset"] for item in result["results"]} == {"markets"}


async def test_search_endpoints_accepts_source_family_filter(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)

    result = await gateway.search_endpoints("air quality", source="environment")

    assert result["results"][0]["operation_id"] == "air_quality_current"
    assert {item["source_family"] for item in result["results"]} == {"environment"}


# ---- fetch_data: combined search+call MCP tool ----


async def test_fetch_data_calls_top_endpoint_when_all_required_params_provided(monkeypatch) -> None:
    """Happy path: query routes to quotes_symbol_price, params satisfy the
    required `symbol` field, fetch_data short-circuits straight to the API
    without forcing the LLM to call describe_endpoint first.
    """
    fake = FakeClient()
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    result = await gateway.fetch_data(
        query="AAPL stock price",
        params={"symbol": "AAPL"},
    )

    # Exactly one downstream HTTP call - same path call_endpoint would take.
    assert len(fake.calls) == 1
    assert fake.calls[0] == ("GET", "/api/v1/quotes/AAPL/price", {}, None)
    # Response shape matches call_endpoint (data + meta), no envelope wrapping.
    assert "data" in result


async def test_fetch_data_calls_endpoint_with_zero_required_params(monkeypatch) -> None:
    """For endpoints that take no required parameters (cot_financial only has
    optional `market` and `limit`), fetch_data must still call cleanly with
    no `params` argument from the LLM.
    """
    fake = FakeClient()
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    result = await gateway.fetch_data(query="NASDAQ futures positioning")

    assert len(fake.calls) == 1
    assert fake.calls[0][0] == "GET"
    assert "data" in result


async def test_fetch_data_returns_needs_params_when_required_missing(monkeypatch) -> None:
    """If the search top-1 requires `symbol` but the LLM forgot to pass it,
    fetch_data must NOT crash and must NOT silently call with no params -
    it returns the candidate endpoints and the missing-params list so the
    LLM has everything it needs to retry in one round trip.
    """
    fake = FakeClient()
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    result = await gateway.fetch_data(query="AAPL stock price")

    # Did NOT call the downstream API.
    assert fake.calls == []
    # Surfaced what the LLM needs to provide.
    assert "needs_params" in result
    assert "symbol" in result["needs_params"]
    assert result["selected_endpoint"]["operation_id"] == "quotes_symbol_price"
    # Parameter schema with examples surfaced so LLM can fill correctly.
    examples = result["selected_endpoint"]["parameter_examples"]
    symbol_param = next(p for p in examples if p["name"] == "symbol")
    assert symbol_param["example"] == "AAPL"
    # Alternative candidates also surfaced - LLM can switch endpoints if needed.
    assert "candidate_endpoints" in result
    assert len(result["candidate_endpoints"]) >= 1


async def test_fetch_data_returns_error_when_no_endpoint_matches(monkeypatch) -> None:
    fake = FakeClient()
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    result = await gateway.fetch_data(query="xyzqrs zzzzzzzzz nonsense")

    assert fake.calls == []
    assert result["error"] == "no_endpoint_found"
    assert "hint" in result  # actionable guidance for the LLM

