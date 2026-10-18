"""Typed tools: one tool per catalog operation, listed to the ChatGPT profile only, in place of the generic catalog tools."""

from __future__ import annotations

import inspect
import json
import logging
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

# Typed inputs whose catalog schema carries a pattern, and inputs with an
# example to show, in the catalog this package bundles.
PATTERNED_INPUTS = 32
EXAMPLED_INPUTS = 278

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
    # The instructions tell every client each listed tool describes itself
    # and each of its inputs.
    for tool in [*await _list(CHATGPT), *await _list(CLAUDE)]:
        assert (tool.get("description") or "").strip(), tool["name"]
        for name, schema in tool["inputSchema"].get("properties", {}).items():
            assert str(schema.get("description") or "").strip(), (tool["name"], name)
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


def test_a_bad_spec_entry_is_left_out_and_the_rest_are_built(monkeypatch, caplog) -> None:
    good = typed.load_spec()[0]
    spec = {
        "tools": [
            good,
            {**good, "title": "Again"},
            {**good, "name": "get_nothing", "operation_id": "no_such_operation"},
            {"name": "get_half"},
            "not an entry",
        ]
    }
    monkeypatch.setattr(typed, "_load_raw", lambda: spec)
    typed.typed_tools.cache_clear()
    try:
        with caplog.at_level(logging.ERROR, logger="sugra_mcp.typed"):
            built = typed.typed_tools()
        assert list(built) == [good["name"]]
        assert built[good["name"]].tool.title == good["title"]
        left_out = [record.getMessage() for record in caplog.records if record.name == "sugra_mcp.typed"]
        assert len(left_out) == 4
        assert left_out[0].startswith(f"typed_tools.json: entry 1 ({good['name']}) left out (ValueError")
        assert "entry 2 (get_nothing) left out (KeyError" in left_out[1]
        assert "entry 3 (get_half) left out (KeyError" in left_out[2]
        assert "entry 4 (None) left out" in left_out[3]
    finally:
        typed.typed_tools.cache_clear()


@pytest.mark.parametrize(
    "spec",
    [
        {},
        {"parameter_descriptions": {}},
        {"tools": []},
        {"tools": {}},
        {"tools": {"name": "x"}},
        {"tools": "tools"},
        {"tools": None},
        [],
    ],
)
def test_a_spec_without_a_list_of_entries_raises(spec) -> None:
    with pytest.raises(ValueError, match="not a list of entries"):
        typed._checked_spec(spec)


def test_the_shipped_spec_is_loaded_through_the_check(monkeypatch) -> None:
    checked: list[Any] = []

    def check(spec: Any) -> Any:
        checked.append(spec)
        return spec

    monkeypatch.setattr(typed, "_checked_spec", check)
    spec = typed._load_raw()
    assert checked == [spec]


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


def _feed_endpoint() -> Endpoint:
    return Endpoint(
        operation_id="feed_items",
        method="GET",
        path="/api/v1/feed",
        summary="",
        description="",
        tags=[],
        toolset="core",
        parameters=[
            EndpointParameter(name="q", location="query", required=True, description="Words.",
                              schema_={"type": "string", "minLength": 2}),
            EndpointParameter(name="topic", location="query", description="Topic.",
                              schema_={"anyOf": [{"type": "string"}, {"type": "null"}]}),
            EndpointParameter(name="sort", location="query", description="Order.",
                              schema_={"type": "string", "enum": ["new", "old"], "default": "new"}),
            EndpointParameter(name="limit", location="query", description="Count.",
                              schema_={"type": "integer", "default": 50}),
        ],
    )


def test_an_entry_narrows_its_inputs_and_leaves_the_catalog_as_it_was() -> None:
    endpoint = _feed_endpoint()
    entry = {
        "name": "get_feed",
        "title": "Get feed",
        "description": "Feed.",
        "omit_parameters": ["sort"],
        "parameters": {
            "topic": {"required": True, "enum": ["a", "b"], "description": "The topic: a or b."},
            "q": {"description": "One name.", "example": "Airbus"},
        },
    }

    built = typed._build(entry, endpoint, {})

    assert built.tool.inputSchema == {
        "type": "object",
        "properties": {
            "q": {"type": "string", "minLength": 2, "description": "One name.", "examples": ["Airbus"]},
            "topic": {"type": "string", "enum": ["a", "b"], "description": "The topic: a or b."},
            "limit": {"type": "integer", "default": 50, "description": "Count."},
        },
        "additionalProperties": False,
        "required": ["q", "topic"],
    }
    assert built.accepted == {"q", "topic", "limit"}
    assert built.required == ("q", "topic")
    # call_endpoint reads the same catalog objects for every other client.
    assert endpoint == _feed_endpoint()


@pytest.mark.parametrize(
    ("narrowing", "refusal"),
    [
        ({"omit_parameters": "sort"}, "omit_parameters is not a list of names"),
        ({"omit_parameters": ["q"]}, "omits required parameter q"),
        ({"omit_parameters": ["region"]}, "omits region, which it does not expose"),
        ({"parameters": ["topic"]}, "parameters is not an object"),
        ({"parameters": {"region": {"required": True}}}, "changes region, which it does not take"),
        ({"omit_parameters": ["sort"], "parameters": {"sort": {"enum": ["new"]}}},
         "changes sort, which it does not take"),
        ({"parameters": {"topic": {}}}, "parameter topic has no changes"),
        ({"parameters": {"topic": {"type": "integer"}}}, "parameter topic cannot change type"),
        ({"parameters": {"q": {"required": False}}}, "parameter q required can only be true"),
        ({"parameters": {"topic": {"enum": []}}}, "parameter topic enum is not a list of distinct strings"),
        ({"parameters": {"topic": {"enum": ["a", "a"]}}}, "parameter topic enum is not a list of distinct strings"),
        ({"parameters": {"topic": {"enum": [1]}}}, "parameter topic enum is not a list of distinct strings"),
        ({"parameters": {"limit": {"enum": ["10"]}}}, "parameter limit enum needs a string parameter"),
        ({"parameters": {"sort": {"enum": ["new", "top"]}}}, "parameter sort enum takes values the catalog does not"),
        ({"parameters": {"sort": {"enum": ["old"]}}}, "parameter sort enum leaves out the catalog default new"),
        ({"parameters": {"topic": {"description": " "}}}, "parameter topic description is not a text"),
        ({"parameters": {"topic": {"description": "x" * (typed.MAX_PARAMETER_DESCRIPTION + 1)}}},
         "parameter topic description is longer than 300 characters"),
        ({"parameters": {"topic": {"enum": ["a"], "example": "a"}}},
         "parameter topic example would not be shown beside an enum"),
        ({"parameters": {"sort": {"example": "new"}}}, "parameter sort example would not be shown beside an enum"),
        ({"parameters": {"q": {"example": "x"}}}, "parameter q example is outside its own schema"),
    ],
)
def test_a_narrowing_the_catalog_does_not_allow_is_refused(narrowing: dict[str, Any], refusal: str) -> None:
    entry = {"name": "get_feed", "title": "Get feed", "description": "Feed.", **narrowing}
    with pytest.raises(ValueError, match=f"^typed_tools.json: get_feed {re.escape(refusal)}$"):
        typed._build(entry, _feed_endpoint(), {})


def test_the_news_tools_take_one_required_scope_and_a_count() -> None:
    # OpenAI's review held the open-ended news feed and searches while
    # get_stock_news, one required symbol and a count, passed.
    built = typed.typed_tools()
    shapes = {
        name: (sorted(built[name].tool.inputSchema["properties"]), built[name].tool.inputSchema["required"])
        for name in ("get_stock_news", "get_latest_news", "search_news", "search_global_news_articles")
    }
    assert shapes == {
        "get_stock_news": (["count", "symbol", "tab"], ["symbol"]),
        "get_latest_news": (["category", "limit"], ["category"]),
        "search_news": (["limit", "q"], ["q"]),
        "search_global_news_articles": (["max_records", "q"], ["q"]),
    }
    topic = built["get_latest_news"].tool.inputSchema["properties"]["category"]
    assert topic["enum"] == ["business", "tech", "science", "energy"]
    assert "examples" not in topic
    for name in ("search_news", "search_global_news_articles"):
        assert built[name].tool.inputSchema["properties"]["q"]["examples"] == ["Airbus"], name


async def test_get_latest_news_takes_one_listed_topic(monkeypatch) -> None:
    fake = FakeClient({"data": []})
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    missing = _payload(await _call("get_latest_news", {}))
    unlisted = _payload(await _call("get_latest_news", {"category": "defense"}))
    region = _payload(await _call("get_latest_news", {"category": "tech", "region": "us"}))
    assert fake.calls == []
    assert missing["error"] == "missing_required_parameters" and missing["missing"] == ["category"]
    assert unlisted["error"] == "invalid_arguments"
    assert [item["parameter"] for item in unlisted["invalid"]] == ["category"]
    assert region["error"] == "unknown_parameters" and region["unknown"] == ["region"]

    assert (await _call("get_latest_news", {"category": "tech"})).isError is False
    # call_endpoint still takes every input of the operation.
    await gateway.call_endpoint("news_latest", params={"region": "us"})
    path = load_catalog().get("news_latest").path
    assert fake.calls == [(path, {"category": "tech"}), (path, {"region": "us"})]


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
        # A refusal keeps the API's own account of its cause.
        ({"error": "x", "status_code": 422, "detail": [{"loc": "query.s", "msg": "bad"}],
          "detail_truncated": True, "url": "u", "elapsed_ms": 3},
         {"error": "x", "status_code": 422, "detail": [{"loc": "query.s", "msg": "bad"}],
          "detail_truncated": True}),
        ({"error": "invalid_window", "status_code": 400, "detail": {"error": "invalid_window",
          "allowed": ["1d"], "received": "2w"}, "url": "u"},
         {"error": "invalid_window", "status_code": 400, "detail": {"error": "invalid_window",
          "allowed": ["1d"], "received": "2w"}}),
        ({"error": "The daily request limit has been reached.", "reason": "daily_limit_reached",
          "daily_limit": 50, "plan": "Free", "status_code": 429, "url": "u"},
         {"error": "The daily request limit has been reached.", "reason": "daily_limit_reached",
          "daily_limit": 50, "plan": "Free", "status_code": 429}),
    ],
)
def test_minimize_response(raw: Any, kept: Any) -> None:
    assert typed.minimize_response(raw) == kept


def _catalog_option(parameter: EndpointParameter) -> dict[str, Any]:
    schema = dict(parameter.schema_)
    concrete = [
        option
        for option in schema.get("anyOf") or []
        if isinstance(option, dict) and option.get("type") != "null"
    ]
    return {**concrete[0], **schema} if len(concrete) == 1 else schema


def test_a_typed_input_keeps_the_catalog_pattern_and_example() -> None:
    patterns = examples = 0
    entries = {entry["name"]: entry for entry in typed.load_spec()}
    for tool in typed.typed_tools().values():
        properties = tool.tool.inputSchema["properties"]
        entry = entries[tool.name]
        for parameter in typed.exposed_parameters(load_catalog().get(tool.operation_id)):
            if parameter.name in entry.get("omit_parameters", []):
                assert parameter.name not in properties, (tool.name, parameter.name)
                continue
            option = _catalog_option(parameter)
            listed = properties[parameter.name]
            if "pattern" in option:
                patterns += 1
                assert listed["pattern"] == option["pattern"], (tool.name, parameter.name)
            # Some catalog inputs carry their example only in the schema, and
            # an entry may give its own.
            example = parameter.example
            if example is None and option.get("examples"):
                example = option["examples"][0]
            example = entry.get("parameters", {}).get(parameter.name, {}).get("example", example)
            if example is not None and "enum" not in listed and example != listed.get("default"):
                examples += 1
                assert listed["examples"] == [example], (tool.name, parameter.name)
    assert patterns >= PATTERNED_INPUTS
    assert examples >= EXAMPLED_INPUTS


async def test_a_typed_call_refuses_a_value_outside_its_pattern(monkeypatch) -> None:
    fake = FakeClient({"data": []})
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    result = await _call("get_economic_series", {"series_id": "not a series"})

    assert result.isError is True
    payload = _payload(result)
    assert payload["error"] == "invalid_arguments"
    assert [item["parameter"] for item in payload["invalid"]] == ["series_id"]
    assert fake.calls == []


async def test_call_endpoint_and_a_typed_call_run_one_named_body(monkeypatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    async def body(operation_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append((operation_id, kwargs))
        return {"data": []}

    monkeypatch.setattr(gateway, "call_operation", body)

    assert await gateway.call_endpoint("market_batch_quotes", params={"symbols": "AAPL"}, limit=2) == {
        "data": []
    }
    assert (await _call("get_stock_quotes", {"symbols": "AAPL"})).isError is False
    assert calls == [
        ("market_batch_quotes", {"params": {"symbols": "AAPL"}, "body": None, "limit": 2,
                                 "fields": None, "include_raw": False}),
        ("market_batch_quotes", {"params": {"symbols": "AAPL"}}),
    ]


def test_call_endpoint_and_its_body_take_one_parameter_list() -> None:
    # A default changed on one side only would reach only call_endpoint or
    # only the typed tools. call_endpoint's annotations also carry the tool's
    # input descriptions, so names, kinds and defaults are compared.
    def shape(function: Any) -> list[tuple[str, Any, Any]]:
        return [
            (parameter.name, parameter.kind, parameter.default)
            for parameter in inspect.signature(function).parameters.values()
        ]

    assert shape(gateway.call_operation) == shape(gateway.call_endpoint)


async def test_other_clients_can_still_call_a_typed_name(monkeypatch) -> None:
    fake = FakeClient({"data": [{"value": 1}]})
    monkeypatch.setattr(gateway, "get_client", lambda: fake)
    async with create_connected_server_and_client_session(mcp, client_info=CLAUDE) as session:
        result = await session.call_tool("get_economic_series", {"series_id": "UNRATE"})
    assert result.isError is False
    assert _payload(result) == {"data": [{"value": 1}]}
