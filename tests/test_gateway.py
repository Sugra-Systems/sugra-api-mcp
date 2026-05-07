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
