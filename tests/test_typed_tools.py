"""Typed tools: one tool per catalog operation, listed to the ChatGPT profile only, in place of the generic catalog tools."""

from __future__ import annotations

import json
import re
from types import SimpleNamespace
from typing import Any

import pytest
from mcp.server.lowlevel.server import request_ctx
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import Implementation

from sugra_api_mcp import tools  # noqa: F401
from sugra_api_mcp.catalog.loader import load_catalog
from sugra_api_mcp.catalog.models import Endpoint, EndpointParameter
from sugra_api_mcp.server import (
    CHATGPT_DROPPED_SENTENCES,
    CHATGPT_PROFILE,
    CHATGPT_UNLISTED_TOOLS,
    DEFAULT_PROFILE,
    OAUTH_SECURITY_SCHEMES,
    SERVER_INSTRUCTIONS,
    client_profile,
    mcp,
)
from sugra_api_mcp.tools import agent, gateway, typed

CHATGPT = Implementation(name="openai-mcp", version="1.0.0")
CLAUDE = Implementation(name="claude-ai", version="0.1.0")

# Commercial data vendors whose names never appear on a public surface.
_VENDOR_NAMES = re.compile(
    r"yahoo|finnhub|tomorrow\.io|coingecko|cboe|alpha ?vantage|polygon|\bfmp\b|tiingo"
    r"|stooq|binance|coinbase|dexscreener|defillama|alternative\.me",
    re.IGNORECASE,
)


class FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
        self.calls.append((path, params))
        return self.payload


async def _list(client_info: Implementation | None) -> list[dict[str, Any]]:
    async with create_connected_server_and_client_session(mcp, client_info=client_info) as session:
        listed = await session.list_tools()
    return [tool.model_dump(by_alias=True, exclude_none=True) for tool in listed.tools]


async def _call(name: str, arguments: dict[str, Any]) -> Any:
    async with create_connected_server_and_client_session(mcp, client_info=CHATGPT) as session:
        return await session.call_tool(name, arguments)


def _payload(result: Any) -> dict[str, Any]:
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


def _without(description: str, name: str) -> str:
    sentence = CHATGPT_DROPPED_SENTENCES.get(name)
    return description if sentence is None else description.replace(sentence + " ", "", 1)


def _registered_names() -> set[str]:
    return {tool.name for tool in mcp._tool_manager.list_tools()}


async def test_other_clients_keep_todays_list() -> None:
    claude = await _list(CLAUDE)
    assert {tool["name"] for tool in claude} == _registered_names()
    assert claude == await _list(None)


async def test_chatgpt_is_listed_the_typed_tools_in_place_of_the_generic_ones() -> None:
    chatgpt = await _list(CHATGPT)
    names = [tool["name"] for tool in chatgpt]
    expected = (_registered_names() - CHATGPT_UNLISTED_TOOLS) | set(typed.typed_tools())
    assert len(names) == len(set(names)) == len(expected)
    assert set(names) == expected
    kept = [
        {**tool, "description": _without(tool["description"], tool["name"])}
        for tool in await _list(CLAUDE)
        if tool["name"] not in CHATGPT_UNLISTED_TOOLS
    ]
    assert chatgpt[: len(kept)] == kept


async def test_nothing_chatgpt_is_listed_names_a_tool_it_is_not_listed() -> None:
    chatgpt = await _list(CHATGPT)
    for tool in chatgpt:
        text = json.dumps(tool)
        named = {name for name in CHATGPT_UNLISTED_TOOLS if re.search(rf"\b{re.escape(name)}\b", text)}
        assert not named, (tool["name"], named)
    claude = {tool["name"]: tool for tool in await _list(CLAUDE)}
    listed = {tool["name"]: tool for tool in chatgpt}
    for name, sentence in CHATGPT_DROPPED_SENTENCES.items():
        # The registered text still carries the sentence (a reword fails here
        # rather than silently keeping it for ChatGPT), every other client
        # keeps it, and ChatGPT's text is the registered one without it.
        assert sentence in claude[name]["description"]
        assert listed[name]["description"] == _without(claude[name]["description"], name)
        assert sentence not in listed[name]["description"]
    # The default profile is still told the search, describe, call loop by the
    # description of the tool that starts it, now that initialize does not.
    loop = claude["search_endpoints"]["description"]
    assert "describe_endpoint(operation_id)" in loop and "call_endpoint(operation_id" in loop


def test_every_unlisted_name_is_a_registered_tool() -> None:
    # A renamed tool would otherwise slip back into ChatGPT's list unnoticed.
    assert _registered_names() >= CHATGPT_UNLISTED_TOOLS
    assert _registered_names() >= CHATGPT_DROPPED_SENTENCES.keys()


async def test_the_texts_every_client_reads_name_only_tools_every_profile_is_listed() -> None:
    # initialize is answered before the client names itself, and the plane
    # error reaches every profile, so neither may point at an unlisted tool.
    every_tool = _registered_names() | set(typed.typed_tools())
    listed_to_all = {tool["name"] for tool in await _list(CHATGPT)} & {
        tool["name"] for tool in await _list(CLAUDE)
    }
    plane_error = agent._map_plane_error({"status_code": 403})
    assert mcp._mcp_server.create_initialization_options().instructions == SERVER_INSTRUCTIONS
    for text in (SERVER_INSTRUCTIONS, plane_error["retry_hint"]):
        named = {word for word in re.findall(r"[a-z]+(?:_[a-z]+)+", text) if word in every_tool}
        assert named <= listed_to_all, named - listed_to_all
    assert {"list_toolsets", "list_sources"} <= set(re.findall(r"[a-z_]+", SERVER_INSTRUCTIONS))
    assert not _VENDOR_NAMES.search(SERVER_INSTRUCTIONS)
    # The instructions tell every client each listed tool describes itself.
    for tool in [*await _list(CHATGPT), *await _list(CLAUDE)]:
        assert (tool.get("description") or "").strip(), tool["name"]
    # No profile is told of raw endpoint access it may not be listed.
    assert "endpoint" not in plane_error["retry_hint"]


def test_the_profile_follows_client_info_then_user_agent() -> None:
    def profile(client_name: str | None, user_agent: str | None) -> str:
        session = SimpleNamespace(
            client_params=SimpleNamespace(clientInfo=SimpleNamespace(name=client_name))
        )
        request = SimpleNamespace(headers={"user-agent": user_agent} if user_agent else {})
        previous = request_ctx.set(SimpleNamespace(session=session, request=request))  # type: ignore[arg-type]
        try:
            return client_profile()
        finally:
            request_ctx.reset(previous)

    assert client_profile() == DEFAULT_PROFILE
    assert profile("openai-mcp", None) == CHATGPT_PROFILE
    assert profile(None, "openai-mcp/1.0.0") == CHATGPT_PROFILE
    assert profile(None, "Claude-User") == DEFAULT_PROFILE
    # The session's own name decides when it has one.
    assert profile("claude-ai", "openai-mcp/1.0.0") == DEFAULT_PROFILE
    assert profile(None, None) == DEFAULT_PROFILE


def test_every_typed_tool_names_a_catalog_operation_once() -> None:
    spec = typed.load_spec()
    built = typed.typed_tools()
    assert len(built) == len(spec)
    catalog_ids = load_catalog().operation_ids
    for entry in spec:
        assert entry["operation_id"] in catalog_ids, entry["name"]
        assert re.fullmatch(r"(get|list|search|compare|convert)_[a-z_]+", entry["name"]), entry["name"]
        assert entry["title"] and entry["description"]
    assert not set(built) & _registered_names()


def test_no_typed_tool_asks_for_a_location() -> None:
    for name, tool in typed.typed_tools().items():
        properties = tool.tool.inputSchema["properties"]
        assert not set(properties) & typed.LOCATION_PARAMETERS, name
        assert set(tool.required) <= set(properties), name


# Location-shaped input names, kept apart from typed.LOCATION_PARAMETERS on purpose.
_LOCATION_SHAPED = re.compile(
    r"lat|lon|lng|coord|addr|zip|postal|post_?code|city|station|place|near|bbox|radius|point|geo"
)
# Inputs that match it but take a country or region CODE, which the rules allow.
_CODE_INPUTS = {
    "geo": "a country code such as DE",
}
# Names the pattern matches that say nothing about a place.
_NOT_LOCATIONS = {"latest"}


def test_no_typed_input_is_shaped_like_a_location() -> None:
    for name, tool in typed.typed_tools().items():
        for parameter, schema in tool.tool.inputSchema["properties"].items():
            if parameter in _NOT_LOCATIONS:
                continue
            if _LOCATION_SHAPED.search(parameter):
                assert parameter in _CODE_INPUTS, (name, parameter)
                assert "code" in schema["description"].lower(), (name, parameter)


def test_an_operation_that_needs_a_location_has_no_typed_tool() -> None:
    endpoint = Endpoint(
        operation_id="weather_point",
        method="GET",
        path="/api/v1/weather",
        summary="",
        description="",
        tags=[],
        toolset="core",
        parameters=[
            EndpointParameter(name="lat", location="query", required=True, schema_={"type": "number"}),
            EndpointParameter(name="units", location="query", required=False, schema_={"type": "string"}),
        ],
    )
    entry = {"name": "get_weather", "title": "Get weather", "description": "Weather."}
    with pytest.raises(ValueError, match="drops required parameters lat"):
        typed._build(entry, endpoint, {})


@pytest.mark.parametrize(
    ("method", "parameter", "refusal"),
    [
        ("GET", EndpointParameter(name="x-sugra-tenant", location="header", required=True), "drops required parameters x-sugra-tenant"),
        ("POST", EndpointParameter(name="units", location="query", required=False), "runs a POST operation"),
    ],
)
def test_an_operation_a_read_only_typed_tool_cannot_run_has_none(
    method: str, parameter: EndpointParameter, refusal: str
) -> None:
    endpoint = Endpoint(
        operation_id="screen_names",
        method=method,
        path="/api/v1/screen",
        summary="",
        description="",
        tags=[],
        toolset="core",
        parameters=[parameter],
    )
    entry = {"name": "screen_names", "title": "Screen names", "description": "Screen."}
    with pytest.raises(ValueError, match=refusal):
        typed._build(entry, endpoint, {})


def test_typed_metadata_names_no_commercial_vendor() -> None:
    for name, tool in typed.typed_tools().items():
        text = json.dumps(tool.tool.model_dump(by_alias=True, exclude_none=True))
        assert not _VENDOR_NAMES.search(text), name


async def test_typed_tools_are_read_only_with_described_inputs_and_oauth() -> None:
    typed_names = set(typed.typed_tools())
    for tool in await _list(CHATGPT):
        if tool["name"] not in typed_names:
            continue
        annotations = tool["annotations"]
        assert annotations["readOnlyHint"] is True
        assert annotations["destructiveHint"] is False
        assert annotations["idempotentHint"] is True
        assert annotations["openWorldHint"] is True
        assert tool["inputSchema"]["additionalProperties"] is False
        for parameter, schema in tool["inputSchema"]["properties"].items():
            assert schema.get("description"), f"{tool['name']}.{parameter}"
            assert "anyOf" not in schema or {"type": "null"} not in schema["anyOf"]
        assert tool["securitySchemes"] == OAUTH_SECURITY_SCHEMES
        assert tool["_meta"]["securitySchemes"] == OAUTH_SECURITY_SCHEMES


async def test_a_typed_call_runs_its_operation_and_drops_request_metadata(monkeypatch) -> None:
    fake = FakeClient({
        "data": [{"symbol": "AAPL", "price": 200, "date": "2026-10-13"}],
        "meta": {"source": "upstream", "quota": {"remaining": 3}, "count": 1, "request_id": "r1"},
        "request_id": "r1",
        "elapsed_ms": 12,
    })
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    result = await _call("get_stock_quotes", {"symbols": "AAPL"})

    assert result.isError is False
    path = load_catalog().get("market_batch_quotes").path
    assert fake.calls == [(path, {"symbols": "AAPL"})]
    assert _payload(result) == {
        "data": [{"symbol": "AAPL", "price": 200, "date": "2026-10-13"}],
        "meta": {"count": 1},
    }


async def test_a_typed_call_leaves_a_span_under_its_own_name(monkeypatch) -> None:
    from sugra_api_mcp import observability
    from tests.test_request_credentials import _CaptureTracer

    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    monkeypatch.setattr(gateway, "get_client", lambda: FakeClient({"data": [{"symbol": "AAPL"}]}))

    result = await _call("get_stock_quotes", {"symbols": "AAPL"})

    assert result.isError is False
    [span] = tracer.spans
    assert span.name == "mcp.tool.get_stock_quotes"
    assert span.attributes["mcp.tool.name"] == "get_stock_quotes"
    assert span.attributes["mcp.operation_id"] == "market_batch_quotes"
    assert span.attributes["mcp.success"] is True


async def test_a_typed_call_refuses_a_location_or_unknown_argument(monkeypatch) -> None:
    fake = FakeClient({"data": []})
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    result = await _call("get_hazard_alerts", {"min_lat": 10, "max_lat": 20})

    assert result.isError is True
    payload = _payload(result)
    assert payload["error"] == "unknown_parameters"
    assert payload["unknown"] == ["max_lat", "min_lat"]
    assert fake.calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"symbol": "AAPL", "limit": 0},
        {"symbol": "AAPL", "limit": "many"},
        {"symbol": 42},
    ],
)
async def test_a_typed_call_refuses_arguments_outside_its_schema(monkeypatch, arguments) -> None:
    fake = FakeClient({"data": []})
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    result = await _call("get_price_history", arguments)

    assert result.isError is True
    payload = _payload(result)
    assert payload["error"] == "invalid_arguments"
    assert [item["parameter"] for item in payload["invalid"]] == [
        name for name in ("limit", "symbol") if name in arguments and arguments[name] != "AAPL"
    ]
    assert all(len(item["problem"]) <= typed.MAX_VIOLATION_CHARS + 3 for item in payload["invalid"])
    assert fake.calls == []


async def test_a_typed_call_reports_a_missing_required_argument(monkeypatch) -> None:
    fake = FakeClient({"data": []})
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    result = await _call("get_price_history", {})

    assert result.isError is True
    assert _payload(result)["missing"] == ["symbol"]
    assert fake.calls == []


async def test_a_typed_failure_keeps_only_what_went_wrong(monkeypatch) -> None:
    fake = FakeClient({
        "error": "upstream error",
        "status_code": 502,
        "url": "https://sugra.ai/api/v1/x",
        "elapsed_ms": 40,
        "request_id": "r2",
        "retry_hint": "Retry once.",
    })
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    result = await _call("get_price_history", {"symbol": "AAPL"})

    assert result.isError is True
    assert _payload(result) == {"error": "upstream error", "status_code": 502, "retry_hint": "Retry once."}


@pytest.mark.parametrize(
    ("raw", "kept"),
    [
        ([1, 2], [1, 2]),
        ({"data": {"a": 1}, "generated_at": "t", "cached": True}, {"data": {"a": 1}}),
        ({"data": [], "meta": {"shaped": {}, "quota": {}}}, {"data": []}),
        ({"data": [], "meta": {"truncated": True, "retry_hint": "narrow"}},
         {"data": [], "meta": {"truncated": True, "retry_hint": "narrow"}}),
        ({"error": "x", "exception_type": "ValueError", "reason": "r"}, {"error": "x", "reason": "r"}),
    ],
)
def test_minimize_response(raw: Any, kept: Any) -> None:
    assert typed.minimize_response(raw) == kept


async def test_other_clients_can_still_call_a_typed_name(monkeypatch) -> None:
    fake = FakeClient({"data": [{"value": 1}]})
    monkeypatch.setattr(gateway, "get_client", lambda: fake)
    async with create_connected_server_and_client_session(mcp, client_info=CLAUDE) as session:
        result = await session.call_tool("get_economic_series", {"series_id": "UNRATE"})
    assert result.isError is False
    assert _payload(result) == {"data": [{"value": 1}]}
