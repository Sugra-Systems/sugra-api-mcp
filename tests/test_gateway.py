"""Gateway tool tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from sugra_api_mcp.catalog.builder import build_catalog_from_openapi
from sugra_api_mcp.catalog.models import Catalog, Endpoint
from sugra_api_mcp.catalog.search import known_sources, known_toolsets
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


# ---- unknown filter values are a typed error, never a silent empty list ----


async def test_search_endpoints_unknown_toolset_returns_typed_error(monkeypatch) -> None:
    """A misspelled or unknown toolset must name the valid values, not return []
    (an empty list is indistinguishable from "nothing matched your query")."""
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)

    result = await gateway.search_endpoints("futures", toolset="definitely_not_a_toolset")

    assert result["error"] == "unknown_toolset"
    assert result["requested"] == "definitely_not_a_toolset"
    assert "results" not in result
    # the caller can correct the filter from the response alone
    assert "markets" in result["known_toolsets"]
    assert result["known_toolsets"] == sorted(result["known_toolsets"])


async def test_search_endpoints_toolset_absent_from_this_catalog_is_an_error(monkeypatch) -> None:
    """The taxonomy is versioned WITH the bundle: a toolset a client knows but
    this catalog vintage lacks must be diagnosable, not a silent zero."""
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    catalog = _fixture_catalog()
    missing = "statistics"
    assert missing not in {endpoint.toolset for endpoint in catalog.endpoints}

    result = await gateway.search_endpoints("population", toolset=missing)

    assert result["error"] == "unknown_toolset"
    assert result["requested"] == missing


async def test_search_endpoints_unknown_source_returns_typed_error(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)

    result = await gateway.search_endpoints("air quality", source="no_such_source")

    assert result["error"] == "unknown_source"
    assert result["requested"] == "no_such_source"
    assert "results" not in result
    assert "environment" in result["known_sources"]


async def test_valid_filter_still_searches_after_validation(monkeypatch) -> None:
    """Validation must not regress the happy path."""
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)

    ok = await gateway.search_endpoints("NASDAQ futures", toolset="markets")

    assert ok["results"] and "error" not in ok


async def test_empty_string_filters_still_mean_no_filter(monkeypatch) -> None:
    """The search filter activates on truthiness, so an empty string has always
    meant "no filter" - clients serialize unset optional strings that way.
    Validation must activate on the SAME predicate, or "" would regress from a
    working unfiltered search into a bogus unknown_* error."""
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)

    for kwargs in ({"toolset": ""}, {"source": ""}, {"toolset": "", "source": ""}):
        result = await gateway.search_endpoints("NASDAQ futures", **kwargs)
        assert "error" not in result, f"{kwargs} wrongly rejected: {result}"
        assert result["results"], f"{kwargs} should search unfiltered"


def _catalog_with_divergent_sources() -> Catalog:
    """A catalog where an endpoint's `sources` list carries a value its
    `source_family` does not.

    Neither the fixture nor the current bundle has such an endpoint (both have
    sources == [source_family]), so this property is invisible to them - yet the
    search filter matches on EITHER list. Build the divergent case explicitly so
    the accept-set contract below is actually exercised rather than passing by
    accident on degenerate data.
    """
    return Catalog(
        source="test-divergent",
        endpoints=[
            Endpoint(
                operation_id="vendor_quote",
                method="GET",
                path="/api/v1/vendor/quote",
                summary="Vendor quote lookup",
                toolset="markets",
                source_family="core",
                sources=["alpha_vendor"],
            )
        ],
    )


async def test_validation_accepts_every_source_the_filter_honours(monkeypatch) -> None:
    """The validator's accept-set must be exactly the search filter's: the filter
    matches a value in `sources` OR equal to `source_family`, so validating against
    source_family alone would reject a value the filter would have honoured -
    turning a WORKING query into a bogus unknown_source error."""
    catalog = _catalog_with_divergent_sources()
    monkeypatch.setattr(gateway, "load_catalog", lambda: catalog)

    # the divergent value is advertised as known ...
    assert "alpha_vendor" in known_sources(catalog)
    # ... the filter really does honour it (results, not an empty list) ...
    honoured = await gateway.search_endpoints("vendor quote", source="alpha_vendor")
    assert "error" not in honoured, honoured
    assert honoured["results"], "filter dropped a source it is documented to match"
    # ... and the source_family value keeps working too
    fam = await gateway.search_endpoints("vendor quote", source="core")
    assert "error" not in fam and fam["results"]


async def test_list_toolsets_count_equals_distinct_catalog_toolsets(monkeypatch) -> None:
    """list_toolsets is the surface a client reads to pick a filter value; it must
    report exactly the distinct toolsets the filter accepts."""
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    catalog = _fixture_catalog()

    payload = await gateway.list_toolsets()

    assert {t["name"] for t in payload["toolsets"]} == known_toolsets(catalog)


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


# ---- JSON-array request bodies (issue #51 / MCP-Imp-8) ----


async def test_call_endpoint_accepts_array_body(monkeypatch) -> None:
    """openfigi mapping POSTs a JSON ARRAY body. The dict-only `body`
    annotation made FastMCP reject the call before the request was sent,
    while describe_endpoint correctly advertised type: array (issue #51)."""
    fake = FakeClient()
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    jobs = [
        {"idType": "TICKER", "idValue": "AAPL"},
        {"idType": "TICKER", "idValue": "MSFT"},
    ]
    result = await gateway.call_endpoint("openfigi_mapping", body=jobs)

    assert fake.calls == [("POST", "/api/v1/openfigi/mapping", {}, jobs)]
    assert result["data"] == {"ok": True}


async def test_fetch_data_passes_array_body_through(monkeypatch) -> None:
    """fetch_data shares call_endpoint's body path: an array body must reach
    the client untouched. Search is pinned so ranking between the two
    openfigi fixtures cannot flake the test."""
    fake = FakeClient()
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: fake)
    monkeypatch.setattr(
        gateway,
        "search_catalog",
        lambda *args, **kwargs: [{"operation_id": "openfigi_mapping"}],
    )

    jobs = [{"idType": "TICKER", "idValue": "AAPL"}]
    result = await gateway.fetch_data("bulk map identifiers", body=jobs)

    assert fake.calls == [("POST", "/api/v1/openfigi/mapping", {}, jobs)]
    assert result["data"] == {"ok": True}


async def test_fetch_data_passes_dict_body_through(monkeypatch) -> None:
    """Widening body to dict | list must not regress plain object bodies
    on the fetch_data path (call_endpoint dict bodies are covered by
    test_call_endpoint_post_preserves_query_params_and_body)."""
    fake = FakeClient()
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: fake)
    monkeypatch.setattr(
        gateway,
        "search_catalog",
        lambda *args, **kwargs: [{"operation_id": "openfigi_map"}],
    )

    body = {"jobs": [{"idType": "TICKER", "idValue": "AAPL"}]}
    result = await gateway.fetch_data("map identifiers", body=body)

    assert fake.calls == [("POST", "/api/v1/openfigi/map", {}, body)]
    assert result["data"] == {"ok": True}


# The body schema both gateway tools advertise (MCP-24.2). The object and null
# branches are what the published listing already describes; the array branch
# types its items as objects, because the one catalog operation that takes a
# top-level array body (post_openfigi_mapping) takes an array of objects.
_BODY_SCHEMA_BRANCHES = [
    {"type": "object", "additionalProperties": True},
    {"type": "array", "items": {"type": "object", "additionalProperties": True}},
    {"type": "null"},
]


async def test_gateway_body_tool_schemas_accept_arrays(monkeypatch) -> None:
    """The regression lived in FastMCP validation, before tool code ran:
    the generated input schema for `body` must allow object AND array on
    both gateway tools. The array branch types its items as objects: an
    untyped `items: {}` advertised any JSON value as a body item (MCP-24.2)."""
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    from sugra_api_mcp.server import mcp

    tools = {tool.name: tool for tool in await mcp.list_tools()}
    for name in ("call_endpoint", "fetch_data"):
        body_schema = tools[name].inputSchema["properties"]["body"]
        types = {sub.get("type") for sub in body_schema.get("anyOf", [])}
        assert {"object", "array"} <= types, f"{name} body schema rejects arrays: {body_schema}"
        array = next(sub for sub in body_schema["anyOf"] if sub.get("type") == "array")
        assert array.get("items") == {"type": "object", "additionalProperties": True}, (
            f"{name} body array items are not typed as objects: {array}"
        )
        branches = body_schema["anyOf"]
        assert len(branches) == len(_BODY_SCHEMA_BRANCHES) and all(
            branch in branches for branch in _BODY_SCHEMA_BRANCHES
        ), f"{name} body is no longer object | array of objects | null: {body_schema}"


# ---- MCP-24.2: the typed array branch holds at the protocol, not only in the schema ----

_GATEWAY_BODY_TOOLS = ("call_endpoint", "fetch_data")


def _route_both_tools_to(monkeypatch, operation_id: str, fake: FakeClient) -> list[str]:
    """Pin both gateway tools to one fixture operation and record every
    catalog load. A load means the tool body ran, so a refusal that shows no
    load came from argument validation, before any tool code."""
    loads: list[str] = []

    def catalog():
        loads.append(operation_id)
        return _fixture_catalog()

    monkeypatch.setattr(gateway, "load_catalog", catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: fake)
    monkeypatch.setattr(
        gateway,
        "search_catalog",
        lambda *args, **kwargs: [{"operation_id": operation_id}],
    )
    return loads


def _body_arguments(tool: str, operation_id: str, body: Any) -> dict[str, Any]:
    """call_endpoint names the operation; fetch_data reaches it through the pinned search."""
    if tool == "call_endpoint":
        return {"operation_id": operation_id, "body": body}
    return {"query": "map identifiers", "body": body}


async def _call_over_protocol(tool: str, arguments: dict[str, Any]):
    from sugra_api_mcp.server import mcp

    async with create_connected_server_and_client_session(mcp) as session:
        return await session.call_tool(tool, arguments)


def _payload(result) -> dict[str, Any]:
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


@pytest.mark.parametrize("tool", _GATEWAY_BODY_TOOLS)
@pytest.mark.parametrize(
    "body",
    [["x"], [7], [None], [["idType", "TICKER"]], [{"idType": "TICKER", "idValue": "AAPL"}, "x"]],
    ids=["string", "number", "null", "array", "object-then-string"],
)
async def test_array_body_with_a_non_object_item_is_refused_before_the_tool_runs(
    monkeypatch, tool: str, body: list[Any]
) -> None:
    """The schema says a top-level array body holds objects, and the server
    holds the same line: a client that ignores the schema gets a protocol
    error naming the body, and neither the catalog nor the upstream client is
    touched. The last case puts the bad item second, so every item is judged,
    not only the first."""
    fake = FakeClient()
    loads = _route_both_tools_to(monkeypatch, "openfigi_mapping", fake)

    result = await _call_over_protocol(tool, _body_arguments(tool, "openfigi_mapping", body))

    assert result.isError is True, f"{tool} accepted a non-object body item: {body!r}"
    assert result.content and "body" in result.content[0].text, result.content
    assert loads == [], "the tool body ran: the refusal must come from argument validation"
    assert fake.calls == []


@pytest.mark.parametrize("tool", _GATEWAY_BODY_TOOLS)
async def test_array_body_of_objects_reaches_the_client_over_the_protocol(
    monkeypatch, tool: str
) -> None:
    """Typing the items must not cost the operation they exist for: an array of
    objects, with any JSON values inside each object, is delivered to the
    client exactly as sent."""
    fake = FakeClient()
    loads = _route_both_tools_to(monkeypatch, "openfigi_mapping", fake)
    jobs = [
        {"idType": "TICKER", "idValue": "AAPL"},
        {"idType": "TICKER", "idValue": "MSFT", "exchCode": None, "tags": ["x", 1], "rank": 2},
    ]

    result = await _call_over_protocol(tool, _body_arguments(tool, "openfigi_mapping", jobs))

    assert result.isError is False, result.content
    assert loads, "the tool body never ran"
    assert fake.calls == [("POST", "/api/v1/openfigi/mapping", {}, jobs)]


@pytest.mark.parametrize("tool", _GATEWAY_BODY_TOOLS)
async def test_object_and_null_bodies_keep_the_published_contract_over_the_protocol(
    monkeypatch, tool: str
) -> None:
    """The published listing describes body as object or null, and typing the
    array branch narrows neither. An object body carrying nested arrays of
    scalars (as several catalog bodies do) reaches the client unchanged, and a
    null body still passes argument validation, so the tool itself answers
    that this operation needs a body."""
    fake = FakeClient()
    loads = _route_both_tools_to(monkeypatch, "openfigi_map", fake)
    body = {
        "jobs": [{"idType": "TICKER", "idValue": "AAPL"}],
        "symbols": ["AAPL", "MSFT"],
        "asns": [15169],
    }

    sent = await _call_over_protocol(tool, _body_arguments(tool, "openfigi_map", body))

    assert sent.isError is False, sent.content
    assert fake.calls == [("POST", "/api/v1/openfigi/map", {}, body)]

    loads.clear()
    absent = await _call_over_protocol(tool, _body_arguments(tool, "openfigi_map", None))

    assert loads, "a null body was refused before the tool ran"
    assert len(fake.calls) == 1, "a null body reached the client"
    missing = {"call_endpoint": "missing", "fetch_data": "needs_params"}[tool]
    assert _payload(absent)[missing] == ["body"]


# ---- MCP-19: the call_endpoint span behind fetch_data names its operation ----


class _CaptureSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def set_status(self, status: object) -> None:
        self.status = status

    def end(self) -> None:
        self.ended = True


class _CaptureTracer:
    def __init__(self) -> None:
        self.spans: list[_CaptureSpan] = []

    def start_span(self, name: str) -> _CaptureSpan:
        span = _CaptureSpan(name)
        self.spans.append(span)
        return span


async def test_fetch_data_delegation_names_the_operation_on_the_inner_span(monkeypatch) -> None:
    """fetch_data delegates to the DECORATED call_endpoint, so every delegated
    call emits a second span. The decorator reads operation_id from kwargs
    only (a positional first argument may be a raw query on other tools), and
    fetch_data passed it positionally - 155 call_endpoint failures over 90
    days carried no operation at all, one for one with fetch_data's own."""
    from sugra_api_mcp import observability

    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: FakeClient())

    result = await gateway.fetch_data(query="AAPL stock price", params={"symbol": "AAPL"})

    assert "data" in result
    inner = [span for span in tracer.spans if span.name == "mcp.tool.call_endpoint"]
    assert len(inner) == 1
    assert inner[0].attributes.get("mcp.operation_id") == "quotes_symbol_price"

