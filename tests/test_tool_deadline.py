"""MCP-10 (audit P1-4): one end-to-end budget bounds every tool call.

The audit saw deliveries at 90-100s and a 45s client cut with no typed envelope:
the outbound httpx timeout bounded only its own leg, and nothing cancelled the
server-side work when the caller had long given up. The deadline wraps dispatch
in SugraFastMCP.call_tool, so what is asserted here - over a real in-memory MCP
session - is what a client actually receives at the budget boundary.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from sugra_api_mcp import tools  # noqa: F401  (registers the tools)
from sugra_api_mcp.server import mcp
from sugra_api_mcp.tools import gateway

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _StallingClient:
    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        await asyncio.sleep(5.0)
        return {"data": []}

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        await asyncio.sleep(5.0)
        return {"data": []}


def _structured(result) -> dict[str, Any]:
    if result.structuredContent is not None:
        return result.structuredContent
    assert result.content
    return json.loads(result.content[0].text)


async def test_deadline_fires_with_a_typed_envelope(monkeypatch) -> None:
    monkeypatch.setenv("SUGRA_TOOL_DEADLINE", "0.5")
    monkeypatch.setattr(gateway, "get_client", lambda: _StallingClient())
    started = time.monotonic()
    async with create_connected_server_and_client_session(mcp) as session:
        result = await session.call_tool(
            "call_endpoint", {"operation_id": "quotes_symbol_price",
                              "params": {"symbol": "AAPL"}})
    elapsed = time.monotonic() - started
    assert result.isError is True
    payload = _structured(result)
    assert payload["error"] == "deadline_exceeded"
    assert payload["deadline_s"] == 0.5
    assert payload["elapsed_ms"] >= 450
    assert payload.get("retry_hint")
    assert elapsed < 2.0, (
        f"deadline envelope arrived after {elapsed:.1f}s - the call was not "
        "cancelled at the budget")


async def test_fast_call_is_untouched_by_the_deadline(monkeypatch) -> None:
    monkeypatch.setenv("SUGRA_TOOL_DEADLINE", "5")

    class _Fast:
        async def get(self, path, params=None):
            return {"data": [{"symbol": "AAPL"}]}

        async def request(self, method, path, **kwargs):
            return {"data": [{"symbol": "AAPL"}]}

    monkeypatch.setattr(gateway, "get_client", lambda: _Fast())
    async with create_connected_server_and_client_session(mcp) as session:
        result = await session.call_tool(
            "call_endpoint", {"operation_id": "quotes_symbol_price",
                              "params": {"symbol": "AAPL"}})
    assert result.isError is not True


async def test_next_call_after_a_deadline_is_not_delayed(monkeypatch) -> None:
    """The audit's wedge signature: a timed-out call held the NEXT command
    for 107.5s. After a deadline fires, an immediately following fast call
    must complete at normal latency."""
    monkeypatch.setenv("SUGRA_TOOL_DEADLINE", "0.4")
    stall = _StallingClient()

    class _Fast:
        async def get(self, path, params=None):
            return {"data": [{"ok": True}]}

        async def request(self, method, path, **kwargs):
            return {"data": [{"ok": True}]}

    clients = [stall, _Fast()]
    monkeypatch.setattr(gateway, "get_client", lambda: clients.pop(0))
    async with create_connected_server_and_client_session(mcp) as session:
        first = await session.call_tool(
            "call_endpoint", {"operation_id": "quotes_symbol_price",
                              "params": {"symbol": "AAPL"}})
        started = time.monotonic()
        second = await session.call_tool(
            "call_endpoint", {"operation_id": "quotes_symbol_price",
                              "params": {"symbol": "AAPL"}})
        elapsed = time.monotonic() - started
    assert first.isError is True
    assert second.isError is not True
    assert elapsed < 1.0, (
        f"the call AFTER a deadline took {elapsed:.1f}s - the wedge survived")


async def test_the_configured_budget_is_what_wraps_dispatch(monkeypatch) -> None:
    """MCP-17: an ambient stamp never shortens the tool's budget.

    This replaces test_total_budget_is_never_exceeded_by_the_floor, which
    stamped 5s into the past against a 1.0s budget and asserted the
    pre-dispatch refusal - pinning as correct the very defect that took the
    hosted gateway down. Auth is bounded on its own side; it no longer eats
    the tool's budget, because on the streamable-HTTP transport the stamp
    visible here belongs to the request that OPENED the session and never to
    this call.

    codex F3: assert the ARGUMENT handed to asyncio.timeout, not the wall
    clock. The measured-elapsed assertion it replaces was satisfied by a
    reintroduced floor - max(0.75, total - elapsed) passed it - because its
    bound was wider than the value it meant to pin.
    """
    import sugra_api_mcp.server as server_mod
    from sugra_api_mcp.auth import request_started_at

    captured: list[float] = []
    real_timeout = asyncio.timeout

    def _capture(delay):
        captured.append(delay)
        return real_timeout(delay)

    monkeypatch.setenv("SUGRA_TOOL_DEADLINE", "7")
    monkeypatch.setattr(server_mod.asyncio, "timeout", _capture)
    # An inherited stamp far older than the budget: the shape that refused
    # every call in production.
    token = request_started_at.set(time.monotonic() - 30.0)
    try:
        async with create_connected_server_and_client_session(mcp) as session:
            result = await session.call_tool("list_toolsets", {})
    finally:
        request_started_at.reset(token)

    assert result.isError is not True, _structured(result)
    assert 7.0 in captured, (
        f"dispatch was wrapped in {captured}, not the configured 7s - a stamp "
        "30s old still shortened the budget")


async def test_a_long_lived_session_still_dispatches_tools(monkeypatch) -> None:
    """MCP-17: a session older than the budget must still call tools.

    The streamable-HTTP session loop is started with task_group.start() from
    inside the request that creates the session, so it INHERITS that request's
    contextvars - including the stamp AuthMiddleware just set. Every later tool
    call on that session therefore read the SESSION's age instead of its own
    auth leg, and past one budget every call was refused before dispatch,
    permanently. Measured live 2026-09-11: elapsed_ms grew 45s -> 167s on one
    connector session while the server answered unauthenticated probes in
    240ms.
    """
    from sugra_api_mcp.auth import request_started_at

    monkeypatch.setenv("SUGRA_TOOL_DEADLINE", "40")
    # A five-minute-old session. No auth leg can be this old: the auth slice is
    # bounded by AUTH_BUDGET_SECONDS, so this stamp cannot belong to this call.
    token = request_started_at.set(time.monotonic() - 300.0)
    try:
        async with create_connected_server_and_client_session(mcp) as session:
            first = await session.call_tool("list_toolsets", {})
            second = await session.call_tool("list_sources", {})
    finally:
        request_started_at.reset(token)

    assert first.isError is not True, (
        f"a 300s-old session was refused before dispatch: {_structured(first)}"
    )
    assert second.isError is not True, (
        f"the second call on one session was refused: {_structured(second)}"
    )


def test_a_budget_that_cannot_bound_anything_is_refused(monkeypatch) -> None:
    """MCP-17 (codex F2): a nonpositive budget must not reach asyncio.timeout.

    Zero and negative values parsed fine and went straight through, cancelling
    every call the instant it started. The operator saw tools that "always
    time out" and nothing pointed at the configuration. Rejecting them at load
    keeps the failure where the mistake is.
    """
    from sugra_api_mcp.config import load_config

    for bad in ("0", "-1", "nan", "inf", "abc"):
        monkeypatch.setenv("SUGRA_TOOL_DEADLINE", bad)
        with pytest.raises(ValueError, match="SUGRA_TOOL_DEADLINE"):
            load_config(require_api_key=False)

    monkeypatch.setenv("SUGRA_TOOL_DEADLINE", "40")
    assert load_config(require_api_key=False).tool_deadline == 40.0
