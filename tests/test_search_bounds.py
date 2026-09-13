"""MCP-26.1: a catalog search can no longer hold the event loop.

Before this change a three-word query took about 400 ms on the workstation, and
a 20,000-character query of distinct words held the only event loop for 133 s.
It was reachable with a made-up sugra_ Bearer, because catalog tools make no
upstream call. The search now tokenizes each endpoint once, refuses a query over
the bounds before any scoring, runs on a worker thread with a bounded queue, and
every tool call counts against an in-flight cap.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from typing import Any

import pytest

from sugra_api_mcp import observability, tools  # noqa: F401  (registers the tools)
from sugra_api_mcp.catalog import aliases, search
from sugra_api_mcp.catalog.loader import load_catalog
from sugra_api_mcp.server import mcp
from sugra_api_mcp.tools import gateway

pytestmark = pytest.mark.anyio

QUOTE_CALL = {"operation_id": "quotes_symbol_price", "params": {"symbol": "AAPL"}}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _structured(result: Any) -> dict[str, Any]:
    if isinstance(result, tuple):
        return result[1]
    if getattr(result, "structuredContent", None) is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


def _vocabulary_query(words: int) -> str:
    vocab = sorted(
        {token for endpoint in load_catalog().endpoints for token in search._tokens(endpoint.summary) if len(token) >= 4}
    )
    return " ".join(vocab[i % len(vocab)] for i in range(words))


# ---- bounds -----------------------------------------------------------------


@pytest.mark.parametrize("tool", ["search_endpoints", "fetch_data"])
@pytest.mark.parametrize("shape", ["too_many_terms", "too_many_chars"])
async def test_an_oversized_query_is_refused_before_any_search(monkeypatch, tool, shape) -> None:
    def _must_not_search(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an oversized query reached search_catalog")

    monkeypatch.setattr(gateway, "search_catalog", _must_not_search)
    if shape == "too_many_terms":
        query = " ".join(f"zq{i}" for i in range(search.MAX_QUERY_TERMS + 1))
        assert len(query) <= search.MAX_QUERY_CHARS
    else:
        query = "x" * (search.MAX_QUERY_CHARS + 1)
    await mcp.call_tool(tool, {"query": query})  # the first call pays one-time tool setup
    started = time.perf_counter()
    result = await mcp.call_tool(tool, {"query": query})
    elapsed = time.perf_counter() - started

    payload = _structured(result)
    assert result.isError is True
    assert payload["error"] == "query_too_long"
    assert payload["max_terms"] == search.MAX_QUERY_TERMS
    assert payload["max_chars"] == search.MAX_QUERY_CHARS
    assert elapsed < 0.1, f"refusing an oversized query took {elapsed:.3f}s"


async def test_a_query_at_the_bounds_still_searches() -> None:
    query = _vocabulary_query(search.MAX_QUERY_TERMS)
    assert search.query_limit_error(query) is None
    result = await mcp.call_tool("search_endpoints", {"query": query, "limit": 3})
    payload = _structured(result)
    assert "error" not in payload
    assert payload["total_matched"] >= 1


def test_search_catalog_itself_refuses_an_oversized_query() -> None:
    with pytest.raises(ValueError, match="query_too_long"):
        search.search_catalog(load_catalog(), "x" * (search.MAX_QUERY_CHARS + 1))


def test_the_cli_search_refuses_an_oversized_query(monkeypatch, capsys) -> None:
    from sugra_api_mcp.__main__ import main

    monkeypatch.setattr(sys, "argv", ["sugra-api-mcp", "search", "x" * (search.MAX_QUERY_CHARS + 1)])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "query_too_long"


# ---- the cached profile answers exactly what re-tokenizing answered ----------


def test_every_endpoint_profile_equals_its_tokenized_fields() -> None:
    for endpoint in load_catalog().endpoints:
        profile = search._profile(endpoint)
        tag_text = " ".join([*endpoint.tags, endpoint.toolset, endpoint.source_family])
        param_text = " ".join(f"{p.name} {p.description}" for p in endpoint.parameters)
        text = search._endpoint_text(endpoint)
        assert profile.operation_id == set(search._tokens(endpoint.operation_id)), endpoint.operation_id
        assert profile.tags == set(search._tokens(tag_text)), endpoint.operation_id
        assert profile.summary == set(search._tokens(endpoint.summary)), endpoint.operation_id
        assert profile.path == set(search._tokens(endpoint.path)), endpoint.operation_id
        assert profile.params == set(search._tokens(param_text)), endpoint.operation_id
        assert profile.description == set(search._tokens(endpoint.description)), endpoint.operation_id
        assert profile.text == set(search._tokens(text)), endpoint.operation_id
        assert profile.text_normalized == " ".join(search._tokens(text)), endpoint.operation_id


def test_alias_matching_from_profiles_equals_the_text_form() -> None:
    expansions = sorted({expansion for values in aliases.ALIASES.values() for expansion in values})
    for endpoint in load_catalog().endpoints[::40]:
        text = search._endpoint_text(endpoint)
        profile = search._profile(endpoint)
        for expansion in expansions:
            assert search._alias_matches_profile(profile, expansion) == search._alias_matches(text, expansion), (
                endpoint.operation_id,
                expansion,
            )


def test_the_profile_cache_is_keyed_by_the_endpoint_object() -> None:
    endpoint = load_catalog().endpoints[0]
    assert search._profile(endpoint) is search._profile(endpoint)
    copy = endpoint.model_copy()
    assert search._profile(copy) is not search._profile(endpoint)


# ---- off the event loop, with a bounded queue --------------------------------


async def test_search_runs_off_the_event_loop(monkeypatch) -> None:
    def _slow_search(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        time.sleep(0.5)
        return []

    monkeypatch.setattr(gateway, "search_catalog", _slow_search)
    gaps: list[float] = []
    stop = asyncio.Event()

    async def _ticker() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(_ticker())
    await asyncio.sleep(0.05)
    result = await mcp.call_tool("search_endpoints", {"query": "US CPI"})
    stop.set()
    await ticker
    assert _structured(result)["results"] == []
    assert max(gaps) < 0.2, f"the event loop stalled {max(gaps):.2f}s during a 0.5s search"


def _blocking_search(started: threading.Event, release: threading.Event):
    def _search(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        started.set()
        release.wait(5)
        return []

    return _search


async def test_a_full_search_queue_refuses_with_server_busy(monkeypatch) -> None:
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(gateway, "search_catalog", _blocking_search(started, release))
    monkeypatch.setattr(gateway, "SEARCH_MAX_PENDING", 1)
    first = asyncio.create_task(mcp.call_tool("search_endpoints", {"query": "US CPI"}))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        assert gateway.search_pending() == 1
        for tool in ("search_endpoints", "fetch_data"):
            refused = await mcp.call_tool(tool, {"query": "US CPI"})
            payload = _structured(refused)
            assert refused.isError is True, tool
            assert payload["error"] == "server_busy", tool
            assert payload["scope"] == "search", tool
            assert payload["limit"] == 1, tool
    finally:
        release.set()
        await first
    assert gateway.search_pending() == 0


async def test_a_cancelled_search_holds_its_slot_until_the_worker_finishes(monkeypatch) -> None:
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(gateway, "search_catalog", _blocking_search(started, release))
    task = asyncio.create_task(mcp.call_tool("search_endpoints", {"query": "US CPI"}))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert gateway.search_pending() == 1, "a cancelled caller released the slot of a search still running"
    release.set()
    for _ in range(200):
        if gateway.search_pending() == 0:
            break
        await asyncio.sleep(0.01)
    assert gateway.search_pending() == 0


# ---- the in-flight cap on tool calls -----------------------------------------


class _GateClient:
    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self.entered = entered
        self.release = release

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.entered.set()
        await self.release.wait()
        return {"data": [{"ok": 1}]}

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.entered.set()
        await self.release.wait()
        return {"data": [{"ok": 1}]}


async def test_calls_beyond_the_in_flight_cap_are_refused(monkeypatch) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(gateway, "get_client", lambda: _GateClient(entered, release))
    monkeypatch.setattr(mcp, "max_in_flight_tool_calls", 1)
    first = asyncio.create_task(mcp.call_tool("call_endpoint", QUOTE_CALL))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        refused = await mcp.call_tool("list_toolsets", {})
        payload = _structured(refused)
        assert refused.isError is True
        assert payload["error"] == "server_busy"
        assert payload["scope"] == "tool_calls"
        assert payload["limit"] == 1
    finally:
        release.set()
        await first
    assert mcp._in_flight_tool_calls == 0
    assert "error" not in _structured(await mcp.call_tool("list_toolsets", {}))


async def test_the_in_flight_count_returns_to_zero_after_every_outcome(monkeypatch) -> None:
    class _Stall:
        async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
            await asyncio.sleep(5)

        async def request(self, method: str, path: str, **kwargs: Any) -> Any:
            await asyncio.sleep(5)

    def _raising_catalog() -> Any:
        raise RuntimeError("catalog exploded")

    monkeypatch.setenv("SUGRA_TOOL_DEADLINE", "0.2")
    monkeypatch.setattr(gateway, "get_client", lambda: _Stall())

    timed_out = await mcp.call_tool("call_endpoint", QUOTE_CALL)
    assert _structured(timed_out)["error"] == "deadline_exceeded"
    assert mcp._in_flight_tool_calls == 0

    cancelled = asyncio.create_task(mcp.call_tool("call_endpoint", QUOTE_CALL))
    await asyncio.sleep(0.05)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert mcp._in_flight_tool_calls == 0

    monkeypatch.setattr(gateway, "load_catalog", _raising_catalog)
    with pytest.raises(Exception, match="catalog exploded"):
        await mcp.call_tool("list_toolsets", {})
    assert mcp._in_flight_tool_calls == 0


class _CaptureSpan:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, object] = {}
        self.ended = False

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


async def test_a_refused_call_leaves_a_span_for_registered_tools_only(monkeypatch) -> None:
    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    monkeypatch.setattr(mcp, "max_in_flight_tool_calls", 0)

    refused = await mcp.call_tool("list_toolsets", {})
    assert _structured(refused)["error"] == "server_busy"
    unknown = await mcp.call_tool("no_such_tool_name", {})
    assert _structured(unknown)["error"] == "server_busy"

    assert [span.name for span in tracer.spans] == ["mcp.tool.list_toolsets"]
    assert tracer.spans[0].attributes == {
        "mcp.tool.name": "list_toolsets",
        "mcp.success": False,
        "mcp.error.code": "server_busy",
        "mcp.duration_ms": 0,
    }
    assert tracer.spans[0].ended is True


def test_the_new_error_codes_reach_telemetry() -> None:
    assert observability._error_code_of({"error": "query_too_long"}) == "query_too_long"
    assert observability._error_code_of({"error": "server_busy"}) == "server_busy"
