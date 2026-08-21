"""MCP-11: MCP halves of shipped audit items.

1. x-sugra-required-groups carried into the bundle and pre-validated in
   the gateway with a typed error BEFORE any HTTP call (the API half
   shipped the spec extension on 29 operations).
2. limit semantics documented truthfully (top-level list / envelope data
   list only) and pinned against shape_response's actual behavior.
3. The low_confidence resolve status reaches App Insights spans.
"""
from __future__ import annotations

from typing import Any

import pytest

from sugra_api_mcp.catalog.models import Endpoint


def _endpoint(**overrides: Any) -> Endpoint:
    data = {
        "operation_id": "weather_current",
        "method": "GET",
        "path": "/api/v1/weather/current",
        "summary": "Current weather",
        "description": "",
        "tags": [],
        "toolset": "weather",
        "source_family": "weather",
        "sources": ["weather"],
        "parameters": [
            {"name": "latitude", "location": "query", "required": False},
            {"name": "longitude", "location": "query", "required": False},
            {"name": "city", "location": "query", "required": False},
        ],
        "required_parameters": [],
        "request_body_required": False,
    }
    data.update(overrides)
    return Endpoint.from_dict(data)


def test_endpoint_carries_required_groups_roundtrip():
    ep = _endpoint(required_groups=[["latitude", "longitude"], ["city"]],
                   groups_mutually_exclusive=False)
    assert ep.required_groups == (("latitude", "longitude"), ("city",))
    dumped = ep.to_dict()
    assert dumped["required_groups"] == [["latitude", "longitude"], ["city"]]
    assert Endpoint.from_dict(dumped).required_groups == ep.required_groups


def test_endpoint_without_groups_omits_key():
    dumped = _endpoint().to_dict()
    assert "required_groups" not in dumped
    assert "groups_mutually_exclusive" not in dumped


def test_builder_parses_spec_extension():
    from sugra_api_mcp.catalog.builder import build_catalog_from_openapi

    spec = {"paths": {"/api/v1/weather/current": {"get": {
        "operationId": "weather_current",
        "summary": "Current weather",
        "tags": ["Weather"],
        "parameters": [
            {"name": "latitude", "in": "query", "schema": {"type": "number"}},
            {"name": "longitude", "in": "query", "schema": {"type": "number"}},
            {"name": "city", "in": "query", "schema": {"type": "string"}},
        ],
        "x-sugra-required-groups": {
            "groups": [["latitude", "longitude"], ["city"]],
            "mutually_exclusive": False,
        },
    }}}}
    catalog = build_catalog_from_openapi(spec)
    ep = catalog.get("weather_current")
    assert ep.required_groups == (("latitude", "longitude"), ("city",))
    assert ep.groups_mutually_exclusive is False


@pytest.mark.anyio
async def test_incomplete_group_is_typed_without_http(monkeypatch):
    """No group fully covered -> typed error, upstream never contacted."""
    import sugra_api_mcp.tools.gateway as gw

    ep = _endpoint(required_groups=[["latitude", "longitude"], ["city"]])
    catalog_calls = []

    class _FakeCatalog:
        def get(self, operation_id):
            catalog_calls.append(operation_id)
            return ep

    monkeypatch.setattr(gw, "load_catalog", lambda: _FakeCatalog())

    http_calls = []

    class _NoClient:
        async def get(self, *a, **k):
            http_calls.append(1)
            raise AssertionError("upstream must not be contacted")

        request = get

    monkeypatch.setattr(gw, "get_client", lambda: _NoClient())
    result = await gw.call_endpoint(operation_id="weather_current",
                                       params={"latitude": 60.2})
    assert result["error"] == "missing_required_parameter_groups"
    assert result["groups"] == [["latitude", "longitude"], ["city"]]
    assert http_calls == []


@pytest.mark.anyio
async def test_complete_group_dispatches(monkeypatch):
    import sugra_api_mcp.tools.gateway as gw

    ep = _endpoint(required_groups=[["latitude", "longitude"], ["city"]])

    class _FakeCatalog:
        def get(self, operation_id):
            return ep

    monkeypatch.setattr(gw, "load_catalog", lambda: _FakeCatalog())

    seen = {}

    class _FakeClient:
        async def get(self, path, params=None):
            seen["path"] = path
            seen["params"] = params
            return {"data": {"temp": 21.5}}

    monkeypatch.setattr(gw, "get_client", lambda: _FakeClient())
    result = await gw.call_endpoint(operation_id="weather_current",
                                       params={"city": "Helsinki"})
    assert "error" not in result
    assert seen["params"] == {"city": "Helsinki"}


def test_group_error_code_in_observability_allowlist():
    from sugra_api_mcp.observability import _KNOWN_ERROR_CODES

    assert "missing_required_parameter_groups" in _KNOWN_ERROR_CODES


def test_limit_bounds_only_the_top_level_list():
    """The documented semantics pinned against actual behavior: limit
    bounds the envelope data list (or a bare top-level array); nested
    lists are untouched and meta.shaped records what applied."""
    from sugra_api_mcp.catalog.response import shape_response

    payload = {"data": [{"i": i, "nested": [1, 2, 3]} for i in range(5)]}
    shaped = shape_response(payload, limit=2, fields=None, include_raw=False)
    assert len(shaped["data"]) == 2
    assert shaped["data"][0]["nested"] == [1, 2, 3]
    assert shaped["meta"]["shaped"]["limit_applied"] is True

    scalar_env = {"data": {"nested": [1, 2, 3, 4]}}
    shaped2 = shape_response(scalar_env, limit=2, fields=None, include_raw=False)
    assert shaped2["data"]["nested"] == [1, 2, 3, 4]
    assert shaped2["meta"]["shaped"]["limit_applied"] is False


def test_limit_field_descriptions_document_scope():
    """The tool schemas must TELL clients the top-level-only rule."""
    import typing

    import sugra_api_mcp.tools.gateway as gw

    for tool in (gw.call_endpoint, gw.fetch_data):
        hints = typing.get_type_hints(tool, include_extras=True)
        meta = hints["limit"].__metadata__[0]
        desc = getattr(meta, "description", "") or ""
        assert "top-level" in desc, tool


def test_low_confidence_status_reaches_telemetry():
    from sugra_api_mcp.tools.agent import _KNOWN_STATUSES

    assert "low_confidence" in _KNOWN_STATUSES


def test_resolve_entity_docstring_mentions_new_statuses():
    import sugra_api_mcp.tools.agent as agent

    doc = agent.resolve_entity.__doc__ or ""
    assert "low_confidence" in doc
    assert "crypto" in doc.lower()


@pytest.mark.anyio
async def test_mutually_exclusive_groups_reject_multiple(monkeypatch):
    """codex r1: exclusivity is ENFORCED, not just recorded - completing
    more than one exclusive group refuses before any HTTP call."""
    import sugra_api_mcp.tools.gateway as gw

    ep = _endpoint(required_groups=[["latitude", "longitude"], ["city"]],
                   groups_mutually_exclusive=True)

    class _FakeCatalog:
        def get(self, operation_id):
            return ep

    monkeypatch.setattr(gw, "load_catalog", lambda: _FakeCatalog())
    http = []

    class _NoClient:
        async def get(self, *a, **k):
            http.append(1)
            raise AssertionError("no HTTP")

    monkeypatch.setattr(gw, "get_client", lambda: _NoClient())
    result = await gw.call_endpoint(
        operation_id="weather_current",
        params={"latitude": 60.2, "longitude": 24.9, "city": "Helsinki"})
    assert result["error"] == "missing_required_parameter_groups"
    assert "EXACTLY one" in result["hint"]
    assert http == []


@pytest.mark.anyio
async def test_non_exclusive_multiple_groups_dispatch(monkeypatch):
    import sugra_api_mcp.tools.gateway as gw

    ep = _endpoint(required_groups=[["latitude", "longitude"], ["city"]],
                   groups_mutually_exclusive=False)

    class _FakeCatalog:
        def get(self, operation_id):
            return ep

    monkeypatch.setattr(gw, "load_catalog", lambda: _FakeCatalog())

    class _FakeClient:
        async def get(self, path, params=None):
            return {"data": {"ok": True}}

    monkeypatch.setattr(gw, "get_client", lambda: _FakeClient())
    result = await gw.call_endpoint(
        operation_id="weather_current",
        params={"latitude": 60.2, "longitude": 24.9, "city": "Helsinki"})
    assert "error" not in result


@pytest.mark.anyio
async def test_fetch_data_group_precheck(monkeypatch):
    """agy r1: fetch_data carries the same precheck as call_endpoint."""
    import sugra_api_mcp.tools.gateway as gw

    ep = _endpoint(required_groups=[["latitude", "longitude"], ["city"]])

    class _FakeCatalog:
        def get(self, operation_id):
            return ep

    monkeypatch.setattr(gw, "load_catalog", lambda: _FakeCatalog())
    monkeypatch.setattr(gw, "search_catalog",
                        lambda *a, **k: [{"operation_id": "weather_current",
                                          "summary": "Current weather"}])
    http = []

    class _NoClient:
        async def get(self, *a, **k):
            http.append(1)
            raise AssertionError("no HTTP")

    monkeypatch.setattr(gw, "get_client", lambda: _NoClient())
    result = await gw.fetch_data(query="current weather",
                                 params={"latitude": 60.2})
    assert result["error"] == "missing_required_parameter_groups"
    assert http == []
