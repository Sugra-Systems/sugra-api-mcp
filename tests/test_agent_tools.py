"""Tests for the hosted-only Agent Context Layer tools (MCP-2.3, v0.8.0).

Covers the three layers Codex plan-review flagged as risky:

1. Registration gate - transport-aware + env-gated, never mutating the global
   ``mcp`` singleton in tests (a fresh FastMCP instance is passed explicitly).
2. Tool behavior - correct plane paths, X-Internal-Token header injection,
   envelope pass-through, and the distinct ``agent_plane_unavailable`` mapping
   for infra-level 403 (the agent cannot fix the internal token; a generic
   HTTP error would invite pointless retries).
3. Observability extraction - span attributes come from RESPONSE envelope
   metadata only (recipe_version / units / downstream_calls / status / stale),
   never from request values.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp.server.fastmcp import FastMCP

from sugra_api_mcp.tools import agent as agent_tools
from sugra_api_mcp.tools.agent import (
    _agent_result_attrs,
    _map_plane_error,
    get_snapshot,
    get_timeseries,
    register_agent_tools,
    resolve_entity,
)


class _FakeClient:
    """Records post() calls and returns a canned response."""

    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"path": path, "json": json, "headers": headers})
        return self.response


@pytest.fixture()
def fake_client(monkeypatch):
    def _install(response: dict[str, Any]) -> _FakeClient:
        client = _FakeClient(response)
        monkeypatch.setattr(agent_tools, "get_client", lambda: client)
        return client

    return _install


# ---------------------------------------------------------------------------
# Registration gate
# ---------------------------------------------------------------------------


def _tool_names(instance: FastMCP) -> set[str]:
    return {t.name for t in asyncio.run(instance.list_tools())}


def test_register_skips_without_token(monkeypatch):
    monkeypatch.delenv("SUGRA_AGENT_INTERNAL_TOKEN", raising=False)
    instance = FastMCP("probe")
    assert register_agent_tools(instance) is False
    assert _tool_names(instance) == set()


def test_register_skips_on_whitespace_token(monkeypatch):
    monkeypatch.setenv("SUGRA_AGENT_INTERNAL_TOKEN", "   ")
    instance = FastMCP("probe")
    assert register_agent_tools(instance) is False
    assert _tool_names(instance) == set()


def test_register_adds_three_tools_with_token(monkeypatch):
    monkeypatch.setenv("SUGRA_AGENT_INTERNAL_TOKEN", "secret")
    instance = FastMCP("probe")
    assert register_agent_tools(instance) is True
    assert _tool_names(instance) == {"resolve_entity", "get_snapshot", "get_timeseries"}


def test_register_explicit_instance_does_not_touch_global(monkeypatch):
    """Passing an instance must never flip the module-level idempotence latch
    nor register into the global ``mcp`` singleton (Codex P1: classic tests
    assert exactly EXPECTED_TOOL_COUNT on the global)."""
    monkeypatch.setenv("SUGRA_AGENT_INTERNAL_TOKEN", "secret")
    from sugra_api_mcp.server import mcp as global_mcp

    before = _tool_names(global_mcp)
    instance = FastMCP("probe")
    register_agent_tools(instance)
    assert _tool_names(global_mcp) == before
    assert agent_tools._registered_global is False


def test_stdio_surface_does_not_include_agent_tools_even_with_token():
    """A stdio process with SUGRA_AGENT_INTERNAL_TOKEN leaked into its env
    must STILL not expose the agent tools - registration is invoked only from
    the streamable-http branch of __main__, never as an import side effect.
    Runs in a SUBPROCESS with the token SET before the import (Codex code
    review P2: an in-process check on a token-less interpreter cannot catch a
    regression where import-time registration sneaks back in)."""
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import asyncio, os\n"
        "os.environ['SUGRA_AGENT_INTERNAL_TOKEN'] = 'leaked-into-stdio'\n"
        "import sugra_api_mcp.tools  # what stdio startup does\n"
        "from sugra_api_mcp.server import mcp\n"
        "names = {t.name for t in asyncio.run(mcp.list_tools())}\n"
        "assert 'resolve_entity' not in names, names\n"
        "assert 'get_snapshot' not in names, names\n"
        "assert 'get_timeseries' not in names, names\n"
        "print(len(names))\n"
    )
    repo_root = Path(__file__).resolve().parent.parent
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=repo_root, check=True,
    )
    from tests.test_metadata_sync import EXPECTED_TOOL_COUNT

    assert int(out.stdout.strip()) == EXPECTED_TOOL_COUNT


# ---------------------------------------------------------------------------
# Tool behavior
# ---------------------------------------------------------------------------


_ENVELOPE = {
    "schema_version": "1",
    "recipe_version": "company_snapshot@1",
    "status": "full",
    "data": {"price": {"price": 123.4}},
    "freshness": {"class": "live_upstream", "stale": False},
    "provenance": [{"component": "price", "source": "Sugra Finance"}],
    "coverage": [{"name": "price", "status": "ok"}],
    "billing": {"rate_limit_cost": 2, "downstream_calls": 4, "remaining": 998},
}


def test_resolve_entity_calls_plane_with_internal_token(monkeypatch, fake_client):
    monkeypatch.setenv("SUGRA_AGENT_INTERNAL_TOKEN", "tok-123")
    client = fake_client({"status": "resolved", "entity": {"namespace": "equity"}})
    result = asyncio.run(resolve_entity("AAPL"))
    assert result["status"] == "resolved"
    call = client.calls[0]
    assert call["path"] == "/internal/agent/v1/resolve"
    assert call["json"] == {"query": "AAPL", "type_hint": None}
    assert call["headers"] == {"X-Internal-Token": "tok-123"}


def test_get_snapshot_passes_envelope_through(monkeypatch, fake_client):
    monkeypatch.setenv("SUGRA_AGENT_INTERNAL_TOKEN", "tok-123")
    client = fake_client(_ENVELOPE)
    entity = {"namespace": "equity", "ids": {"symbol": "AAPL"}}
    result = asyncio.run(get_snapshot("company_snapshot", entity))
    assert result == _ENVELOPE
    call = client.calls[0]
    assert call["path"] == "/internal/agent/v1/snapshot"
    assert call["json"] == {"recipe": "company_snapshot", "entity": entity}


def test_get_timeseries_defaults(monkeypatch, fake_client):
    monkeypatch.setenv("SUGRA_AGENT_INTERNAL_TOKEN", "tok-123")
    client = fake_client({"data": {"points": []}})
    entity = {"namespace": "etf", "ids": {"symbol": "SPY"}}
    asyncio.run(get_timeseries("etf_flows", entity))
    call = client.calls[0]
    assert call["path"] == "/internal/agent/v1/timeseries"
    assert call["json"] == {
        "metric": "etf_flows",
        "entity": entity,
        "granularity": "1d",
        "max_points": 500,
    }


def test_plane_403_maps_to_agent_plane_unavailable(monkeypatch, fake_client):
    monkeypatch.setenv("SUGRA_AGENT_INTERNAL_TOKEN", "tok-123")
    fake_client(
        {"error": "internal access denied", "status_code": 403,
         "url": "https://sugra.ai/internal/agent/v1/resolve", "elapsed_ms": 12}
    )
    result = asyncio.run(resolve_entity("AAPL"))
    assert result["error"] == "agent_plane_unavailable"
    assert result["status_code"] == 403
    assert "API key" in result["retry_hint"]


def test_plane_401_stays_generic(monkeypatch, fake_client):
    """401 = the USER's API key problem (fixable by the caller) - must NOT be
    masked as an infra error."""
    monkeypatch.setenv("SUGRA_AGENT_INTERNAL_TOKEN", "tok-123")
    fake_client({"error": "invalid_api_key", "status_code": 401, "elapsed_ms": 5})
    result = asyncio.run(resolve_entity("AAPL"))
    assert result["error"] == "invalid_api_key"
    assert result["status_code"] == 401


def test_map_plane_error_passthrough_on_success():
    assert _map_plane_error({"status": "full"}) == {"status": "full"}


# ---------------------------------------------------------------------------
# Observability extraction
# ---------------------------------------------------------------------------


def test_agent_result_attrs_extracts_envelope_metadata():
    attrs = _agent_result_attrs(_ENVELOPE)
    assert attrs == {
        "mcp.agent.recipe_version": "company_snapshot@1",
        "mcp.agent.status": "full",
        "mcp.agent.units": 2,
        "mcp.agent.downstream_calls": 4,
        "mcp.agent.stale": False,
    }


def test_agent_result_attrs_honest_on_sparse_envelope():
    assert _agent_result_attrs({"status": "partial"}) == {"mcp.agent.status": "partial"}
    assert _agent_result_attrs("not a dict") == {}
    # free-text / unexpected values never pass through
    assert _agent_result_attrs({"status": "weird custom text"}) == {}
    assert _agent_result_attrs({"recipe_version": 42}) == {}


def test_trace_decorator_result_attrs_callback_failure_is_safe():
    """A crashing extractor must never break the tool result."""
    from sugra_api_mcp.observability import trace_mcp_tool

    def _boom(result: Any) -> dict[str, Any]:
        raise RuntimeError("extractor crash")

    @trace_mcp_tool("probe_tool", result_attrs=_boom)
    async def probe() -> dict[str, Any]:
        return {"ok": True}

    assert asyncio.run(probe()) == {"ok": True}
