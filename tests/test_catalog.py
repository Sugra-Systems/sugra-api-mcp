"""Catalog builder, loader, search, and response shaping tests."""

from __future__ import annotations

import json
from pathlib import Path

from sugra_api_mcp.catalog.builder import build_catalog_from_openapi
from sugra_api_mcp.catalog.loader import load_catalog
from sugra_api_mcp.catalog.response import shape_response
from sugra_api_mcp.catalog.search import search_catalog

FIXTURE = Path(__file__).parent / "fixtures" / "openapi_minimal.json"


def test_catalog_builder_from_fixture() -> None:
    catalog = build_catalog_from_openapi(json.loads(FIXTURE.read_text(encoding="utf-8")))

    assert catalog.endpoint_count == 6
    assert catalog.source == "fixture"
    assert catalog.get("cot_financial").path == "/api/v1/cot/financial"
    assert catalog.get("quotes_symbol_price").required_parameters == ["symbol"]
    assert catalog.get("cot_financial").source_family == "markets"
    assert catalog.get("bis_cb_rates").toolset == "central_banks"


def test_load_bundled_catalog_has_endpoints() -> None:
    catalog = load_catalog()

    assert catalog.endpoint_count > 0
    assert catalog.get("cot_financial").operation_id == "cot_financial"


def test_search_nasdaq_futures_returns_market_relevant_candidate() -> None:
    catalog = build_catalog_from_openapi(json.loads(FIXTURE.read_text(encoding="utf-8")))

    results = search_catalog(catalog, "NASDAQ futures", limit=3)

    assert results
    assert results[0]["operation_id"] == "cot_financial"
    assert results[0]["toolset"] == "markets"
    assert results[0]["why"]
    assert any(reason.startswith("alias:") for reason in results[0]["why"])
    assert "central_banks_bcra_cotizaciones" not in {
        result["operation_id"] for result in results
    }


def test_search_supports_toolset_and_source_family_filters() -> None:
    catalog = build_catalog_from_openapi(json.loads(FIXTURE.read_text(encoding="utf-8")))

    market_results = search_catalog(catalog, "NASDAQ futures", toolset="markets", limit=3)
    environment_results = search_catalog(catalog, "air quality", source="environment", limit=3)
    central_bank_results = search_catalog(
        catalog, "central bank rates", toolset="central_banks", limit=3
    )

    assert [result["operation_id"] for result in market_results] == ["cot_financial"]
    assert environment_results[0]["operation_id"] == "air_quality_current"
    assert environment_results[0]["source_family"] == "environment"
    assert central_bank_results[0]["operation_id"] == "bis_cb_rates"
    assert {result["toolset"] for result in central_bank_results} == {"central_banks"}


def test_describe_known_operation() -> None:
    catalog = build_catalog_from_openapi(json.loads(FIXTURE.read_text(encoding="utf-8")))

    endpoint = catalog.get("quotes_symbol_price")

    assert endpoint.summary == "Quote price"
    assert endpoint.parameters[0].name == "symbol"
    assert endpoint.parameters[0].location == "path"


def test_response_shaping_limit_fields_and_include_raw() -> None:
    payload = {
        "data": [
            {"symbol": "AAPL", "price": 200, "volume": 10},
            {"symbol": "MSFT", "price": 300, "volume": 20},
        ],
        "meta": {"source": "fixture"},
    }

    shaped = shape_response(payload, limit=1, fields=["symbol", "price"], include_raw=True)

    assert shaped["data"] == [{"symbol": "AAPL", "price": 200}]
    assert shaped["meta"]["shaped"]["limit"] == 1
    assert shaped["meta"]["shaped"]["fields"] == ["symbol", "price"]
    assert shaped["raw"] == payload


def test_response_shaping_omits_oversized_raw_payload() -> None:
    payload = {"data": [{"blob": "x" * 1000}], "meta": {}}

    shaped = shape_response(payload, include_raw=True, max_raw_chars=200)

    assert "raw" not in shaped
    assert shaped["meta"]["raw_omitted"]["reason"] == "exceeds_raw_size_limit"


def test_catalog_builder_fails_on_missing_operation_id() -> None:
    openapi = {
        "paths": {
            "/api/v1/missing": {
                "get": {
                    "summary": "Missing operation",
                    "parameters": [],
                }
            }
        }
    }

    import pytest

    with pytest.raises(ValueError, match="missing operationId"):
        build_catalog_from_openapi(openapi)


def test_catalog_builder_fails_on_duplicate_operation_id() -> None:
    openapi = {
        "paths": {
            "/api/v1/one": {"get": {"operationId": "duplicate_op"}},
            "/api/v1/two": {"get": {"operationId": "duplicate_op"}},
        }
    }

    import pytest

    with pytest.raises(ValueError, match="duplicate operationId"):
        build_catalog_from_openapi(openapi)
