"""call_endpoint refuses a params key the operation does not declare.

The API drops an undeclared query parameter without an error, so a
misnamed filter (country for countries) used to return the endpoint's
default data. The gateway now answers unknown_parameters BEFORE any HTTP
call, naming the accepted keys and the closest match. Operations whose
handler reads free dimension filters from the raw query string are the
one exception.
"""
from __future__ import annotations

from typing import Any

import pytest

from sugra_api_mcp.catalog.models import Endpoint


def _endpoint(**overrides: Any) -> Endpoint:
    data = {
        "operation_id": "labour_dataflow",
        "method": "GET",
        "path": "/api/v1/labour/dataflow/{dataflow_id}",
        "summary": "Labour dataflow",
        "description": "",
        "tags": [],
        "toolset": "statistics",
        "source_family": "statistics",
        "sources": ["statistics"],
        "parameters": [
            {"name": "dataflow_id", "location": "path", "required": True},
            {"name": "countries", "location": "query", "required": False},
            {"name": "freq", "location": "query", "required": False},
            {"name": "start", "location": "query", "required": False},
        ],
        "required_parameters": ["dataflow_id"],
        "request_body_required": False,
    }
    data.update(overrides)
    return Endpoint.from_dict(data)


def _patch(monkeypatch, gw, endpoint: Endpoint) -> dict[str, Any]:
    class _FakeCatalog:
        def get(self, operation_id):
            return endpoint

    seen: dict[str, Any] = {"calls": 0}

    class _FakeClient:
        async def get(self, path, params=None, **_kwargs):
            seen["calls"] += 1
            seen["path"] = path
            seen["params"] = params
            return {"data": [{"period": "2025", "value": 1.0}]}

        request = None

    monkeypatch.setattr(gw, "load_catalog", lambda: _FakeCatalog())
    monkeypatch.setattr(gw, "get_client", lambda: _FakeClient())
    return seen


@pytest.mark.anyio
async def test_misnamed_filter_is_refused_without_http(monkeypatch):
    import sugra_api_mcp.tools.gateway as gw

    seen = _patch(monkeypatch, gw, _endpoint())
    result = await gw.call_endpoint(
        operation_id="labour_dataflow",
        params={"dataflow_id": "DF_UNE", "country": "PRT"},
    )
    assert result["error"] == "unknown_parameters"
    assert result["operation_id"] == "labour_dataflow"
    assert result["unknown"] == ["country"]
    assert result["accepted"] == ["dataflow_id", "countries", "freq", "start"]
    assert result["did_you_mean"] == {"country": "countries"}
    assert "describe_endpoint" in result["hint"]
    assert seen["calls"] == 0


@pytest.mark.anyio
async def test_case_only_difference_is_suggested(monkeypatch):
    import sugra_api_mcp.tools.gateway as gw

    _patch(monkeypatch, gw, _endpoint())
    result = await gw.call_endpoint(
        operation_id="labour_dataflow",
        params={"dataflow_id": "DF_UNE", "FREQ": "A"},
    )
    assert result["did_you_mean"] == {"FREQ": "freq"}


@pytest.mark.anyio
async def test_no_close_match_omits_did_you_mean(monkeypatch):
    import sugra_api_mcp.tools.gateway as gw

    _patch(monkeypatch, gw, _endpoint())
    result = await gw.call_endpoint(
        operation_id="labour_dataflow",
        params={"dataflow_id": "DF_UNE", "zzz": 1},
    )
    assert result["error"] == "unknown_parameters"
    assert result["unknown"] == ["zzz"]
    assert "did_you_mean" not in result


@pytest.mark.anyio
async def test_unknown_key_is_reported_before_a_missing_required_one(monkeypatch):
    """The misnamed key is usually the missing parameter itself."""
    import sugra_api_mcp.tools.gateway as gw

    ep = _endpoint(
        parameters=[
            {"name": "countries", "location": "query", "required": True},
        ],
        path="/api/v1/labour/data",
        required_parameters=["countries"],
    )
    seen = _patch(monkeypatch, gw, ep)
    result = await gw.call_endpoint(operation_id="labour_dataflow",
                                    params={"country": "PRT"})
    assert result["error"] == "unknown_parameters"
    assert result["did_you_mean"] == {"country": "countries"}
    assert seen["calls"] == 0


@pytest.mark.anyio
async def test_a_none_value_is_not_an_unknown_key(monkeypatch):
    """A key sent as None is dropped before the check, as before."""
    import sugra_api_mcp.tools.gateway as gw

    seen = _patch(monkeypatch, gw, _endpoint())
    result = await gw.call_endpoint(
        operation_id="labour_dataflow",
        params={"dataflow_id": "DF_UNE", "countries": "PRT", "country": None},
    )
    assert "error" not in result
    assert seen["params"] == {"countries": "PRT"}


@pytest.mark.anyio
async def test_declared_keys_dispatch_unchanged(monkeypatch):
    import sugra_api_mcp.tools.gateway as gw

    seen = _patch(monkeypatch, gw, _endpoint())
    result = await gw.call_endpoint(
        operation_id="labour_dataflow",
        params={"dataflow_id": "DF_UNE", "countries": "PRT", "freq": "A"},
    )
    assert "error" not in result
    assert seen["path"] == "/api/v1/labour/dataflow/DF_UNE"
    assert seen["params"] == {"countries": "PRT", "freq": "A"}


@pytest.mark.anyio
async def test_open_query_operation_still_forwards_dimension_filters(monkeypatch):
    import sugra_api_mcp.tools.gateway as gw

    op = "statistical_agencies_statbank_dk_data_table_id"
    ep = _endpoint(
        operation_id=op,
        path="/api/v1/statistical-agencies/statbank-dk/data/{table_id}",
        parameters=[
            {"name": "table_id", "location": "path", "required": True},
            {"name": "lang", "location": "query", "required": False},
            {"name": "last_n", "location": "query", "required": False},
        ],
        required_parameters=["table_id"],
        open_query=True,
    )
    seen = _patch(monkeypatch, gw, ep)
    result = await gw.call_endpoint(
        operation_id=op,
        params={"table_id": "FOLK1A", "OMRÅDE": "000", "Tid": "2025K1"},
    )
    assert "error" not in result
    assert seen["params"] == {"OMRÅDE": "000", "Tid": "2025K1"}


@pytest.mark.anyio
async def test_the_marker_not_the_name_opens_the_query(monkeypatch):
    """The same operation without the catalog's open_query flag is refused."""
    import sugra_api_mcp.tools.gateway as gw

    op = "statistical_agencies_statbank_dk_data_table_id"
    ep = _endpoint(
        operation_id=op,
        path="/api/v1/statistical-agencies/statbank-dk/data/{table_id}",
        parameters=[{"name": "table_id", "location": "path", "required": True}],
        required_parameters=["table_id"],
    )
    seen = _patch(monkeypatch, gw, ep)
    result = await gw.call_endpoint(
        operation_id=op, params={"table_id": "FOLK1A", "OMRÅDE": "000"})
    assert result["error"] == "unknown_parameters"
    assert seen["calls"] == 0


@pytest.mark.anyio
async def test_fetch_data_delegation_gets_the_same_refusal(monkeypatch):
    import sugra_api_mcp.tools.gateway as gw

    ep = _endpoint()
    seen = _patch(monkeypatch, gw, ep)

    async def _one_hit(_catalog, _query, **_kwargs):
        return [{"operation_id": "labour_dataflow"}]

    monkeypatch.setattr(gw, "_search_off_loop", _one_hit)
    result = await gw.fetch_data(
        query="unemployment Portugal",
        params={"dataflow_id": "DF_UNE", "country": "PRT"},
    )
    assert result["error"] == "unknown_parameters"
    assert result["did_you_mean"] == {"country": "countries"}
    assert seen["calls"] == 0


def test_the_bundled_catalog_marks_exactly_the_open_query_operations():
    """The bundle carries the API's x-sugra-open-query marker; a dropped
    marker would turn the StatBank dimension filters into refusals."""
    from sugra_api_mcp.catalog.loader import load_catalog

    catalog = load_catalog()
    marked = {e.operation_id for e in catalog.endpoints if e.open_query}
    assert marked == {"statistical_agencies_statbank_dk_data_table_id"}


def test_the_builder_reads_only_a_literal_true_marker():
    from sugra_api_mcp.catalog.builder import build_catalog_from_openapi

    def _spec(marker: Any) -> dict[str, Any]:
        op: dict[str, Any] = {"operationId": "op", "tags": ["Statistical Agencies"]}
        if marker is not None:
            op["x-sugra-open-query"] = marker
        return {"openapi": "3.1.0", "paths": {"/api/v1/x": {"get": op}}}

    assert build_catalog_from_openapi(_spec(True), source="t").get("op").open_query is True
    for marker in (None, False, "true", 1):
        assert build_catalog_from_openapi(_spec(marker), source="t").get("op").open_query is False, marker
    endpoint = build_catalog_from_openapi(_spec(True), source="t").get("op")
    assert endpoint.to_dict()["open_query"] is True
    assert type(endpoint).from_dict(endpoint.to_dict()).open_query is True
    assert "open_query" not in build_catalog_from_openapi(_spec(None), source="t").get("op").to_dict()


def test_unknown_parameters_code_in_observability_allowlist():
    from sugra_api_mcp.observability import _KNOWN_ERROR_CODES

    assert "unknown_parameters" in _KNOWN_ERROR_CODES
