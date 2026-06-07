"""Gateway tool tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

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


async def test_describe_endpoint_includes_request_body_schema(monkeypatch) -> None:
    """Clients used to guess POST body keys: the builder discarded the
    requestBody schema (field-test defect, S3/MCP-Imp-6)."""
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)

    described = await gateway.describe_endpoint("openfigi_map")

    schema = described["request_body_schema"]
    assert schema["required"] == ["jobs"]
    assert schema["properties"]["jobs"]["items"]["properties"]["idType"] == {"type": "string"}
    # GET endpoints stay lean - no empty schema noise.
    quote = await gateway.describe_endpoint("quotes_symbol_price")
    assert "request_body_schema" not in quote


async def test_fetch_data_needs_params_exposes_request_body_schema(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: FakeClient())

    result = await gateway.fetch_data("Map identifiers to OpenFIGI")

    assert result["needs_params"] == ["body"]
    schema = result["selected_endpoint"]["request_body_schema"]
    assert schema["required"] == ["jobs"]


async def test_call_endpoint_applies_fields_to_envelope_less_payload(monkeypatch) -> None:
    """Field test 2026-06-07: Net Atlas endpoints return flat dicts (no data
    envelope) and `fields` was a silent no-op - the full payload came back
    while meta.shaped echoed the requested fields.
    """

    class FlatClient(FakeClient):
        async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            self.calls.append(("GET", path, params, None))
            return {
                "ip": "8.8.8.8",
                "asn": 15169,
                "rdns": "dns.google",
                "geo": {"city": "Ashburn", "country": "US"},
                "_meta": {"atlas_built_at": "2026-06-01"},
            }

    fake = FlatClient()
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    result = await gateway.call_endpoint("air_quality_current", fields=["ip", "geo.city"])

    assert result["ip"] == "8.8.8.8"
    assert result["geo"] == {"city": "Ashburn"}
    assert "rdns" not in result
    assert "asn" not in result
    assert result["_meta"] == {"atlas_built_at": "2026-06-01"}
    assert result["meta"]["shaped"]["fields_applied"] == ["ip", "geo.city"]
    assert result["meta"]["shaped"]["fields_unmatched"] == []


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


# ---- error contract: structured failures must reach the agent untouched ----


class StructuredErrorClient:
    """Mimics SugraClient returning a transport-error dict (MCP-Imp-1)."""

    ERROR: ClassVar[dict[str, Any]] = {
        "error": "upstream_timeout",
        "reason": "ReadTimeout",
        "status_code": None,
        "elapsed_ms": 30012,
        "url": "https://sugra.ai/api/v1/quotes/AAPL/price",
        "retry_hint": "Retry once.",
        "timeout_s": 30.0,
    }

    async def get(self, path, params=None):
        return dict(self.ERROR)

    async def request(self, method, path, params=None, json=None):
        return dict(self.ERROR)


async def test_call_endpoint_returns_structured_error_without_shaping(monkeypatch) -> None:
    """A transport-error dict must pass through unmodified: shaping it would
    add a misleading meta.shaped block to an error payload."""
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: StructuredErrorClient())

    result = await gateway.call_endpoint(
        "quotes_symbol_price",
        params={"symbol": "AAPL"},
        fields=["symbol"],
        limit=1,
    )

    assert result == StructuredErrorClient.ERROR
    assert "meta" not in result


class RaisingClient:
    """Mimics an unexpected non-httpx failure inside the call path."""

    async def get(self, path, params=None):
        raise RuntimeError("unexpected internal failure")

    async def request(self, method, path, params=None, json=None):
        raise RuntimeError("unexpected internal failure")


async def test_call_endpoint_catches_unexpected_exception(monkeypatch) -> None:
    """Safety net (defect D2): nothing may raise through FastMCP as an
    empty 'Error executing tool call_endpoint:' string."""
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: RaisingClient())

    result = await gateway.call_endpoint("quotes_symbol_price", params={"symbol": "AAPL"})

    assert result["error"] == "tool_execution_failed"
    assert result["operation_id"] == "quotes_symbol_price"
    assert result["exception_type"] == "RuntimeError"
    assert "unexpected internal failure" in result["reason"]
    # Codex finding: the README contract promises elapsed_ms on ALL error
    # payloads - the safety-net path must carry it too.
    assert isinstance(result["elapsed_ms"], int)


async def test_call_endpoint_catches_catalog_load_failure(monkeypatch) -> None:
    """The safety net covers the WHOLE tool body: a failure in catalog load
    or parameter resolution (before the HTTP call) must also return the
    structured contract, not raise through FastMCP."""

    def broken_catalog():
        raise ValueError("corrupt bundled catalog")

    monkeypatch.setattr(gateway, "load_catalog", broken_catalog)

    result = await gateway.call_endpoint("quotes_symbol_price", params={"symbol": "AAPL"})

    assert result["error"] == "tool_execution_failed"
    assert result["exception_type"] == "ValueError"


async def test_fetch_data_catches_search_path_failure(monkeypatch) -> None:
    """fetch_data's search/selection path sits in the same safety net."""
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)

    def broken_search(*args, **kwargs):
        raise RuntimeError("search index corrupted")

    monkeypatch.setattr(gateway, "search_catalog", broken_search)

    result = await gateway.fetch_data(query="AAPL stock price")

    assert result["error"] == "tool_execution_failed"
    assert result["exception_type"] == "RuntimeError"
    assert isinstance(result["elapsed_ms"], int)


async def test_call_endpoint_shapes_success_payload_containing_error_key(monkeypatch) -> None:
    """The error-bypass requires ABSENCE of "data" (mirrors entities._is_error):
    a hypothetical 200 partial-degradation payload carrying both data and a
    top-level error note must still get shaped (limit applied), not returned raw."""

    class PartialClient:
        async def get(self, path, params=None):
            return {"data": [{"v": 1}, {"v": 2}], "error": "partial", "meta": {}}

    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: PartialClient())

    result = await gateway.call_endpoint("quotes_symbol_price", params={"symbol": "AAPL"}, limit=1)

    assert result["data"] == [{"v": 1}]  # limit applied -> shaping ran
    assert result["meta"]["shaped"]["limit"] == 1


async def test_fetch_data_propagates_structured_error(monkeypatch) -> None:
    """fetch_data delegates to call_endpoint: the structured error contract
    must survive the combined search+call round trip too."""
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: StructuredErrorClient())

    result = await gateway.fetch_data(query="AAPL stock price", params={"symbol": "AAPL"})

    assert result["error"] == "upstream_timeout"
    assert result["elapsed_ms"] == 30012


# ---- agent hints surface in discovery tools (MCP-Imp-3) ----


async def test_describe_endpoint_includes_agent_hints(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)

    result = await gateway.describe_endpoint("quotes_symbol_price")

    hints = result["agent_hints"]
    assert hints["duration_class"] == "fast"
    assert hints["max_concurrency"] == 4
    assert "duration_note" in hints


async def test_fetch_data_needs_params_includes_agent_hints(monkeypatch) -> None:
    fake = FakeClient()
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    result = await gateway.fetch_data(query="AAPL stock price")

    assert fake.calls == []
    assert "agent_hints" in result["selected_endpoint"]
    assert result["selected_endpoint"]["agent_hints"]["duration_class"] == "fast"

