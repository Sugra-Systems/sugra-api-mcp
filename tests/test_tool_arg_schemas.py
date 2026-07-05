"""Lock the tightened tool-argument schemas an OpenAI Apps review asked for.

The submission review flagged loosely-typed arguments ("Unclear Arguments") on
five tools. These assertions pin the generated MCP inputSchema so a future
signature change cannot silently loosen them back to generic string / object.
"""
from __future__ import annotations

import asyncio
import os

import pytest


def _enum_of(prop: dict) -> list | None:
    if prop.get("enum"):
        return prop["enum"]
    for branch in prop.get("anyOf", []):
        if branch.get("enum"):
            return branch["enum"]
    return None


def _has_ref(prop: dict) -> bool:
    if prop.get("$ref"):
        return True
    return any(b.get("$ref") for b in prop.get("anyOf", []))


def _props(tools):
    return {t.name: (t.inputSchema or {}).get("properties", {}) for t in tools}


@pytest.fixture(scope="module")
def tool_schemas():
    """Base-tool schemas from the global server, agent-tool schemas from a FRESH
    FastMCP instance. Registering the hosted agent tools on the global would
    pollute the tool count that test_metadata_sync asserts (EXPECTED_TOOL_COUNT
    = 8), so keep them off the global here."""
    os.environ.setdefault("SUGRA_API_KEY", "x")
    os.environ["SUGRA_AGENT_INTERNAL_TOKEN"] = "x"
    from mcp.server.fastmcp import FastMCP

    import sugra_api_mcp.tools  # noqa: F401  (registers base tools on the global)
    from sugra_api_mcp.server import mcp
    from sugra_api_mcp.tools.agent import register_agent_tools

    base = _props(asyncio.run(mcp.list_tools()))
    fresh = FastMCP("arg-schema-test")
    assert register_agent_tools(fresh) is True
    agent_tools = _props(asyncio.run(fresh.list_tools()))
    return {**base, **agent_tools}


def test_entity_lookup_anchor_is_enum(tool_schemas):
    assert _enum_of(tool_schemas["sugra_entity_lookup"]["anchor"]) == ["lei", "vat"]


def test_get_timeseries_metric_is_enum(tool_schemas):
    assert _enum_of(tool_schemas["get_timeseries"]["metric"]) == [
        "price", "macro_series", "etf_flows",
    ]


def test_agent_entity_params_are_structured(tool_schemas):
    for tool in ("get_snapshot", "get_timeseries"):
        assert _has_ref(tool_schemas[tool]["entity"]), f"{tool}.entity lost its typed shape"


def test_gateway_dynamic_params_carry_schema_guidance(tool_schemas):
    # params/body stay open (dynamic gateway) but must point at how to get the
    # per-operation schema (describe_endpoint / required_parameters / request_body_schema).
    guidance = ("describe_endpoint", "required_parameters", "request_body_schema")
    for tool in ("call_endpoint", "fetch_data"):
        for arg in ("params", "body"):
            desc = tool_schemas[tool][arg].get("description", "")
            assert desc and any(g in desc for g in guidance), (
                f"{tool}.{arg} missing schema guidance"
            )
