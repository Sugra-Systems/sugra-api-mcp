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
