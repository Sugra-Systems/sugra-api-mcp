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


async def test_the_deadline_leaves_a_verdict_on_the_span(monkeypatch) -> None:
    """MCP-19.1, over a real in-memory session: the budget's cancellation used
    to end the tool's span with no mcp.success and no code (CancelledError is
    a BaseException the wrapper never caught), so the one failure that fires
    when the API is slowest was invisible to every failure query. The client
    still receives the deadline_exceeded envelope, and now the span says the
    same thing."""
    from sugra_api_mcp import observability

    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    monkeypatch.setenv("SUGRA_TOOL_DEADLINE", "0.5")
    monkeypatch.setattr(gateway, "get_client", lambda: _StallingClient())
    async with create_connected_server_and_client_session(mcp) as session:
        result = await session.call_tool(
            "call_endpoint", {"operation_id": "quotes_symbol_price",
                              "params": {"symbol": "AAPL"}})
    assert result.isError is True
    assert _structured(result)["error"] == "deadline_exceeded"

    spans = [span for span in tracer.spans if span.name == "mcp.tool.call_endpoint"]
    assert len(spans) == 1
    assert spans[0].attributes["mcp.success"] is False
    assert spans[0].attributes["mcp.error.code"] == "deadline_exceeded"
    assert spans[0].attributes["mcp.operation_id"] == "quotes_symbol_price"
    assert spans[0].ended is True


class _ErrorClient:
    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return {"error": "HTTP 404", "status_code": 404, "url": path, "elapsed_ms": 1}

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return {"error": "HTTP 404", "status_code": 404, "url": path, "elapsed_ms": 1}


class _FastClient:
    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return {"data": [{"symbol": "AAPL"}], "meta": {}}

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return {"data": [], "meta": {}}


@pytest.mark.parametrize(
    ("client", "cancel_after"),
    [(_FastClient(), None), (_ErrorClient(), None), (_StallingClient(), None), (_StallingClient(), 0.05)],
    ids=["success", "tool-error", "budget-timeout", "external-cancel"],
)
async def test_the_dispatch_timeout_is_reset_after_every_outcome(monkeypatch, client, cancel_after) -> None:
    """The published timeout must not outlive its dispatch on the task: after
    a success, a tool error, a budget timeout and an external cancel, the
    ContextVar is back to unset in the dispatch's own context (a mutation
    dropping the reset survived every other test, codex r1)."""
    import contextvars

    from sugra_api_mcp import observability

    monkeypatch.setenv("SUGRA_TOOL_DEADLINE", "0.3")
    monkeypatch.setattr(gateway, "get_client", lambda: client)
    ctx = contextvars.copy_context()
    task = asyncio.get_running_loop().create_task(
        mcp.call_tool("call_endpoint", {"operation_id": "quotes_symbol_price",
                                        "params": {"symbol": "AAPL"}}),
        context=ctx,
    )
    if cancel_after is not None:
        await asyncio.sleep(cancel_after)
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        assert cancel_after is not None
    assert ctx.get(observability.dispatch_timeout) is None


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

    # codex round 2: the budget must sit BELOW any floor a regression could
    # introduce, or the capture cannot see it. A 7s capture passed happily
    # while asyncio.timeout(max(0.75, deadline)) was in place; 0.25s does not.
    monkeypatch.setenv("SUGRA_TOOL_DEADLINE", "0.25")
    monkeypatch.setattr(server_mod.asyncio, "timeout", _capture)
    # An inherited stamp far older than the budget: the shape that refused
    # every call in production. Under the old code no timeout was entered at
    # all, so 0.25 would be missing from the capture for that reason instead.
    token = request_started_at.set(time.monotonic() - 30.0)
    try:
        async with create_connected_server_and_client_session(mcp) as session:
            await session.call_tool("list_toolsets", {})
    finally:
        request_started_at.reset(token)

    assert 0.25 in captured, (
        f"dispatch was wrapped in {captured}, not the configured 0.25s - "
        "either a stamp 30s old still shortened the budget, or a floor was "
        "applied underneath it")


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


def test_startup_refuses_a_budget_that_cannot_bound_anything(monkeypatch) -> None:
    """MCP-17 (codex F5): the refusal must happen where an operator sees it.

    The guard lived in load_config, but no startup path called load_config, so
    SUGRA_TOOL_DEADLINE=0 started cleanly, answered /health with 200, and first
    surfaced as an unstructured HTTP 500 raised out of AuthMiddleware on the
    first authenticated request. This drives the real entry point and proves
    the transport is never reached.
    """
    import argparse

    from sugra_api_mcp.__main__ import _run_server
    from sugra_api_mcp.server import mcp as server_mcp

    def _must_not_run(*args, **kwargs):
        raise AssertionError("the transport started on an invalid budget")

    monkeypatch.setattr(server_mcp, "run", _must_not_run)
    monkeypatch.setenv("SUGRA_TOOL_DEADLINE", "0")
    with pytest.raises(ValueError, match="SUGRA_TOOL_DEADLINE"):
        _run_server(argparse.Namespace(transport="stdio"))


def test_startup_refuses_a_budget_the_client_would_outlive(monkeypatch) -> None:
    """MCP-17 (codex F4): the tool budget and the auth budget are sequential.

    Dispatch is bounded by SUGRA_TOOL_DEADLINE and auth by its own slice, so
    the server-side worst case is their SUM, reached on a cold auth. If that
    sum passes the floor of documented client timeouts, the client cuts the
    connection before the typed envelope arrives - which is the failure the
    budget exists to prevent. The relationship is now checked rather than
    assumed.
    """
    from sugra_api_mcp.auth import AUTH_BUDGET_SECONDS
    from sugra_api_mcp.config import (
        CLIENT_TIMEOUT_FLOOR_SECONDS,
        validate_startup_budgets,
    )

    monkeypatch.setenv("SUGRA_TOOL_DEADLINE", "40")
    validate_startup_budgets()  # the shipped default must pass

    too_big = CLIENT_TIMEOUT_FLOOR_SECONDS - AUTH_BUDGET_SECONDS + 1
    monkeypatch.setenv("SUGRA_TOOL_DEADLINE", str(too_big))
    with pytest.raises(ValueError, match="client timeouts"):
        validate_startup_budgets()
