"""Bounds on a fields projection, and shaping off the event loop.

fields had no bound of its own, and every dotted path was split again for
every record while the projection ran on the event loop that serves every
session. Now the count, length and depth of fields are refused before any
request, a projection visits a bounded number of list items within a time
bound, a response too large to project is refused before it is copied, and
shaping runs on its own small pool of worker threads.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import types
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.routing import Route

from sugra_api_mcp import errors, observability, tools  # noqa: F401  (registers the tools)
from sugra_api_mcp.catalog import response
from sugra_api_mcp.client import MAX_RESPONSE_CHARS
from sugra_api_mcp.server import mcp
from sugra_api_mcp.tools import gateway
from sugra_api_mcp.web import health
from tests.test_forecast_size_gate import _forecast_body
from tests.test_search_bounds import (
    _CALLER,
    _as_caller,
    _CaptureTracer,
    _LoopClosingAtTheWake,
    _structured,
    _until,
    _until_sync,
)

QUOTE_CALL = {"operation_id": "quotes_symbol_price", "params": {"symbol": "AAPL"}}
# 85 dotted parts in 254 characters: inside the length bound, far past the depth bound.
DEEP_PATH = ".".join(["zq"] * 85)
_REAL_SHAPE = response.shape_response
_RECORDS = {"data": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]}
_ERROR_KEYS = {"error", "limit_kind", "limit", "actual", "field_index", "operation_id", "elapsed_ms", "hint"}


class _StaticClient:
    """Answers every request with a fresh copy of one payload, and counts them."""

    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls = 0

    def _answer(self) -> Any:
        self.calls += 1
        return json.loads(json.dumps(self.payload))

    async def get(self, path: str, params: Any = None, **kwargs: Any) -> Any:
        return self._answer()

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        return self._answer()


def _serve(monkeypatch, payload: Any) -> _StaticClient:
    client = _StaticClient(payload)
    monkeypatch.setattr(gateway, "get_client", lambda: client)
    return client


def _no_client() -> Any:
    raise AssertionError("a refused call made an API request")


class _BlockingShape:
    """Shapes as shape_response does; the first call blocks until released.
    Every call records the thread it ran on."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0
        self.threads: list[str] = []
        self._lock = threading.Lock()

    def __call__(self, payload: Any, **kwargs: Any) -> Any:
        with self._lock:
            self.calls += 1
            first = self.calls == 1
            self.threads.append(threading.current_thread().name)
        if first:
            self.started.set()
            self.release.wait(5)
        return _REAL_SHAPE(payload, **kwargs)


def _occupy(busy: threading.Event, hold: threading.Event) -> None:
    busy.set()
    hold.wait(5)


async def _occupy_both_workers(hold: threading.Event) -> list[Any]:
    """Keep every shaping worker busy until hold is set, so a job submitted
    meanwhile stays queued. The occupying jobs hold no slot."""
    busy = [threading.Event() for _ in range(gateway.SHAPING_WORKERS)]
    occupying = [gateway._shaping_executor.submit(_occupy, event, hold) for event in busy]
    for event in busy:
        assert await asyncio.to_thread(event.wait, 5)
    return occupying


def _assert_no_shaping_left() -> None:
    assert gateway.shaping_pending() == 0
    assert gateway.shaping_waiting() == 0
    assert gateway._shaping_pending_by_caller == {}
    assert gateway._shaping_waiting_by_caller == {}


def _refusal(call: Any) -> tuple[str, int, int, int | None]:
    """Run call and return the bound its ProjectionTooLargeError names."""
    with pytest.raises(response.ProjectionTooLargeError) as refused:
        call()
    return refused.value.kind, refused.value.limit, refused.value.actual, refused.value.field_index


def _fake_clock(monkeypatch) -> list[float]:
    """Each read of the clock catalog.response uses is one second after the last."""
    reads: list[float] = []

    def _monotonic() -> float:
        reads.append(float(len(reads) + 1))
        return reads[-1]

    monkeypatch.setattr(response, "time", types.SimpleNamespace(monotonic=_monotonic))
    return reads


# ---- the count, length and depth of fields: refused before any request -----


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ([DEEP_PATH] * 2000, ("fields", 32, 2000, None)),
        ([DEEP_PATH] * 32, ("path_parts", 16, 85, 0)),
        (["zq" * 500_000], ("path_chars", 256, 1_000_000, 0)),
    ],
    ids=["2000-fields", "deep-paths", "long-path"],
)
async def test_oversized_fields_are_refused_before_any_request_in_under_100_ms(monkeypatch, fields, expected) -> None:
    monkeypatch.setattr(gateway, "get_client", _no_client)
    # The first call loads the catalog, so the timed call measures the refusal alone.
    await mcp.call_tool("call_endpoint", {**QUOTE_CALL, "fields": ["a"] * 33})
    began = time.perf_counter()
    result = await mcp.call_tool("call_endpoint", {**QUOTE_CALL, "fields": fields})
    elapsed = time.perf_counter() - began

    payload = _structured(result)
    assert result.isError is True
    assert set(payload) == _ERROR_KEYS
    assert payload["error"] == "projection_too_large"
    assert (payload["limit_kind"], payload["limit"], payload["actual"], payload["field_index"]) == expected
    assert payload["operation_id"] == "quotes_symbol_price"
    assert type(payload["elapsed_ms"]) is int
    assert payload["hint"] == errors._PROJECTION_HINTS[expected[0]]
    assert "zq" not in json.dumps(payload)
    assert elapsed < 0.1, f"the refusal took {elapsed * 1000:.1f} ms"


def test_compile_fields_takes_every_path_up_to_each_bound() -> None:
    assert response.compile_fields(None) == []
    assert response.compile_fields([]) == []
    assert len(response.compile_fields(["a"] * 32)) == 32
    assert response.compile_fields(["a" * 256]) == [("a" * 256, (), None)]
    sixteen = ".".join(["a"] * 16)
    assert response.compile_fields([sixteen])[0][1:] == (("a",) * 15, "a")
    # Empty parts do not count: a leading, trailing or doubled dot changes nothing.
    for spelling in ("." + sixteen, sixteen + ".", sixteen.replace(".", "..", 1)):
        assert response.compile_fields([spelling])[0][1:] == (("a",) * 15, "a")


def test_compile_fields_names_the_bound_a_list_is_past() -> None:
    assert _refusal(lambda: response.compile_fields(["a"] * 33)) == ("fields", 32, 33, None)
    assert _refusal(lambda: response.compile_fields(["ok", "a" * 257])) == ("path_chars", 256, 257, 1)
    assert _refusal(lambda: response.compile_fields(["ok", ".".join(["a"] * 17)])) == ("path_parts", 16, 17, 1)


def test_a_path_over_the_length_bound_is_never_split(monkeypatch) -> None:
    real_split = response._split_field_path

    def _checked_split(field: str) -> list[str]:
        assert len(field) <= response.MAX_FIELD_PATH_CHARS, "a path over the length bound was split"
        return real_split(field)

    monkeypatch.setattr(response, "_split_field_path", _checked_split)
    assert _refusal(lambda: response.compile_fields(["a." * 1000])) == ("path_chars", 256, 2000, 0)


def test_the_bounds_are_the_documented_numbers() -> None:
    assert (
        response.MAX_FIELDS,
        response.MAX_FIELD_PATH_CHARS,
        response.MAX_FIELD_PATH_PARTS,
        response.MAX_PROJECTION_ROWS,
        response.MAX_SHAPING_SECONDS,
        response.MAX_PROJECTION_RAW_CHARS,
    ) == (32, 256, 16, 100_000, 5.0, 2_000_000)
    assert (
        gateway.SHAPING_WORKERS,
        gateway.SHAPING_MAX_PENDING,
        gateway.SHAPING_MAX_PENDING_PER_CALLER,
        gateway.SHAPING_WAIT_SECONDS,
    ) == (2, 8, 4, 2.0)
    assert gateway._shaping_executor._max_workers == gateway.SHAPING_WORKERS
    assert gateway._shaping_executor is not gateway._search_executor


# ---- while shaping: list items visited, time, and the response size --------


@pytest.mark.parametrize(
    "wrap",
    [
        lambda rows: {"data": rows},
        lambda rows: {"data": {"total": len(rows), "items": rows}},
        lambda rows: rows,
    ],
    ids=["data-list", "data-items", "bare-array"],
)
def test_one_projection_visits_at_most_the_row_bound(wrap) -> None:
    within = response.shape_response(wrap([0] * 100_000), fields=["a"])
    assert within["meta"]["shaped"]["fields_unmatched"] == ["a"]
    over = wrap([0] * 100_001)
    assert _refusal(lambda: response.shape_response(over, fields=["a"])) == ("rows", 100_000, 100_001, None)


def test_the_row_bound_counts_every_list_one_projection_walks() -> None:
    response.shape_response({"data": [[0] for _ in range(50_000)]}, fields=["a"])
    over = {"data": [[0] for _ in range(50_001)]}
    assert _refusal(lambda: response.shape_response(over, fields=["a"])) == ("rows", 100_000, 100_001, None)


def test_limit_cuts_the_records_before_the_projection_counts_them() -> None:
    shaped = response.shape_response({"data": [0] * 200_000}, limit=10, fields=["a"])
    assert shaped["data"] == [0] * 10


def test_a_lower_row_bound_refuses_the_first_list_past_it(monkeypatch) -> None:
    monkeypatch.setattr(response, "MAX_PROJECTION_ROWS", 3)
    three = {"data": [{"a": 1, "b": 2}, {"a": 3, "b": 4}, {"a": 5, "b": 6}]}
    assert response.shape_response(three, fields=["a"])["data"] == [{"a": 1}, {"a": 3}, {"a": 5}]
    four = {"data": [*three["data"], {"a": 7, "b": 8}]}
    assert _refusal(lambda: response.shape_response(four, fields=["a"])) == ("rows", 3, 4, None)


def test_a_projection_past_the_time_bound_is_refused(monkeypatch) -> None:
    reads = _fake_clock(monkeypatch)
    records = {"data": [{"a": i, "b": i} for i in range(10)]}
    # Read once as the projection starts, then once before each record: the
    # sixth record is read at 7 s, 6 s in, the first read past the 5 s bound.
    assert _refusal(lambda: response.shape_response(records, fields=["a"])) == ("shaping_ms", 5000, 6000, None)
    assert len(reads) == 7
    # Without fields there is no projection, and the clock is never read.
    assert response.shape_response(records, limit=3)["data"] == records["data"][:3]
    assert len(reads) == 7


async def test_call_endpoint_answers_a_projection_past_the_time_bound(monkeypatch) -> None:
    _fake_clock(monkeypatch)
    _serve(monkeypatch, {"data": [{"a": i, "b": i} for i in range(10)]})
    result = await gateway.call_endpoint(**QUOTE_CALL, fields=["a"])
    assert set(result) == _ERROR_KEYS
    assert (result["error"], result["limit_kind"], result["limit"], result["actual"]) == (
        "projection_too_large",
        "shaping_ms",
        5000,
        6000,
    )
    assert result["field_index"] is None
    _assert_no_shaping_left()


def test_the_raw_bound_applies_only_with_fields(monkeypatch) -> None:
    payload = {"data": [{"a": i, "b": i} for i in range(50)]}
    size = len(json.dumps(payload))
    monkeypatch.setattr(response, "MAX_PROJECTION_RAW_CHARS", size - 1)
    assert _refusal(lambda: response.shape_response(payload, fields=["a"])) == ("raw_chars", size - 1, size, None)
    assert response.shape_response(payload, limit=5) == {
        "data": payload["data"][:5],
        "meta": {
            "shaped": {
                "limit": 5,
                "limit_applied": True,
                "fields": [],
                "fields_applied": [],
                "fields_unmatched": [],
                "records_path": "data",
                "order": "unknown",
                "kept_end": "first",
            }
        },
    }
    monkeypatch.setattr(response, "MAX_PROJECTION_RAW_CHARS", size)
    assert response.shape_response(payload, fields=["a"])["data"] == [{"a": i} for i in range(50)]


def test_the_raw_bound_sits_far_above_the_size_cap_and_the_readme_names_it() -> None:
    # The 16-day forecast is over the size cap until fields=["daily"] cuts it to fit.
    forecast = len(json.dumps(_forecast_body(16)))
    assert MAX_RESPONSE_CHARS < forecast < response.MAX_PROJECTION_RAW_CHARS
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert "2,000,000" in readme
    assert "projection_too_large" in readme


# ---- shaping runs on its own pool, never on the event loop -----------------


async def test_health_answers_while_a_response_is_shaped(monkeypatch) -> None:
    started, release = threading.Event(), threading.Event()
    released_in_time: list[bool] = []
    threads: list[str] = []

    def _slow_shape(payload: Any, **kwargs: Any) -> Any:
        threads.append(threading.current_thread().name)
        started.set()
        released_in_time.append(release.wait(5))
        return _REAL_SHAPE(payload, **kwargs)

    monkeypatch.setattr(gateway, "shape_response", _slow_shape)
    _serve(monkeypatch, _RECORDS)
    app = Starlette(routes=[Route("/health", health, methods=["GET"])])
    call = asyncio.create_task(mcp.call_tool("call_endpoint", {**QUOTE_CALL, "fields": ["a"]}))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            answer = await asyncio.wait_for(client.get("/health"), 2)
        assert answer.status_code == 200
        assert answer.json()["status"] == "ok"
    finally:
        release.set()
    result = await call
    assert released_in_time == [True], "the shaping held the event loop until it gave up waiting"
    assert threads[0].startswith("response-shaping")
    assert _structured(result)["data"] == [{"a": 1}, {"a": 3}]


async def test_a_full_shaping_pool_answers_server_busy_after_the_wait(monkeypatch) -> None:
    blocking = _BlockingShape()
    monkeypatch.setattr(gateway, "shape_response", blocking)
    monkeypatch.setattr(gateway, "SHAPING_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SHAPING_WAIT_SECONDS", 0.1)
    _serve(monkeypatch, _RECORDS)
    call = {**QUOTE_CALL, "fields": ["a"]}
    first = asyncio.create_task(mcp.call_tool("call_endpoint", call))
    try:
        assert await asyncio.to_thread(blocking.started.wait, 5)
        assert gateway.shaping_pending() == 1
        refused = await mcp.call_tool("call_endpoint", call)
        payload = _structured(refused)
        assert refused.isError is True
        assert (payload["error"], payload["scope"], payload["limit"]) == ("server_busy", "shaping", 1)
        assert payload["elapsed_ms"] >= 50
    finally:
        blocking.release.set()
        await first
    assert await _until(lambda: gateway.shaping_pending() == 0)
    _assert_no_shaping_left()


async def test_one_caller_cannot_take_every_shaping_slot(monkeypatch) -> None:
    blocking = _BlockingShape()
    monkeypatch.setattr(gateway, "shape_response", blocking)
    monkeypatch.setattr(gateway, "current_caller", _CALLER.get)
    monkeypatch.setattr(gateway, "SHAPING_MAX_PENDING_PER_CALLER", 1)
    monkeypatch.setattr(gateway, "SHAPING_WAIT_SECONDS", 0.1)
    _serve(monkeypatch, _RECORDS)
    call = {**QUOTE_CALL, "fields": ["a"]}
    first = _as_caller("caller-a", mcp.call_tool("call_endpoint", call))
    try:
        assert await asyncio.to_thread(blocking.started.wait, 5)
        refused = _structured(await _as_caller("caller-a", mcp.call_tool("call_endpoint", call)))
        assert (refused["error"], refused["scope"], refused["limit"]) == ("server_busy", "caller_shaping", 1)
        served = _structured(await _as_caller("caller-b", mcp.call_tool("call_endpoint", call)))
        assert served["data"] == [{"a": 1}, {"a": 3}], "another caller was not served"
    finally:
        blocking.release.set()
        await first
    assert await _until(lambda: gateway.shaping_pending() == 0)
    _assert_no_shaping_left()


@pytest.mark.parametrize(
    ("bound", "scope"),
    [("SHAPING_MAX_PENDING", "shaping"), ("SHAPING_MAX_PENDING_PER_CALLER", "caller_shaping")],
    ids=["shaping", "caller_shaping"],
)
async def test_a_shaping_refusal_names_its_bound_on_the_span(monkeypatch, bound, scope) -> None:
    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    monkeypatch.setattr(gateway, bound, 0)
    monkeypatch.setattr(gateway, "SHAPING_WAIT_SECONDS", 0.05)
    _serve(monkeypatch, _RECORDS)

    payload = _structured(await mcp.call_tool("call_endpoint", {**QUOTE_CALL, "fields": ["a"]}))

    assert (payload["error"], payload["scope"], payload["limit"]) == ("server_busy", scope, 0)
    assert [span.name for span in tracer.spans] == ["mcp.tool.call_endpoint"]
    assert tracer.spans[0].attributes["mcp.error.code"] == "server_busy"
    assert tracer.spans[0].attributes["mcp.busy.scope"] == scope
    assert tracer.spans[0].ended is True
    _assert_no_shaping_left()


async def test_a_shaping_slot_freed_during_the_wait_serves_the_job(monkeypatch) -> None:
    blocking = _BlockingShape()
    monkeypatch.setattr(gateway, "shape_response", blocking)
    monkeypatch.setattr(gateway, "SHAPING_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SHAPING_WAIT_SECONDS", 5.0)
    _serve(monkeypatch, _RECORDS)
    call = {**QUOTE_CALL, "fields": ["a"]}
    first = asyncio.create_task(mcp.call_tool("call_endpoint", call))
    try:
        assert await asyncio.to_thread(blocking.started.wait, 5)
        second = asyncio.create_task(mcp.call_tool("call_endpoint", call))
        assert await _until(lambda: gateway.shaping_waiting() == 1)
    finally:
        blocking.release.set()
    await first
    payload = _structured(await second)
    assert payload["data"] == [{"a": 1}, {"a": 3}]
    assert blocking.calls == 2
    assert await _until(lambda: gateway.shaping_pending() == 0)
    _assert_no_shaping_left()


async def test_a_shaping_job_cancelled_while_running_holds_its_slot_until_it_ends(monkeypatch) -> None:
    _assert_no_shaping_left()
    blocking = _BlockingShape()
    monkeypatch.setattr(gateway, "shape_response", blocking)
    _serve(monkeypatch, _RECORDS)
    task = asyncio.create_task(mcp.call_tool("call_endpoint", {**QUOTE_CALL, "fields": ["a"]}))
    try:
        assert await asyncio.to_thread(blocking.started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert gateway.shaping_pending() == 1, "a cancelled caller released the slot of a job still running"
    finally:
        blocking.release.set()
    assert await _until(lambda: gateway.shaping_pending() == 0)
    _assert_no_shaping_left()


async def test_a_shaping_job_cancelled_while_queued_never_runs_and_frees_its_slot() -> None:
    ran: list[str] = []
    hold = threading.Event()
    try:
        occupying = await _occupy_both_workers(hold)
        queued = asyncio.create_task(gateway._shape_off_loop(partial(ran.append, "queued")))
        assert await _until(lambda: gateway.shaping_pending() == 1)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        assert await _until(lambda: gateway.shaping_pending() == 0), "a dropped queued job kept its slot"
    finally:
        hold.set()
    for future in occupying:
        await asyncio.wrap_future(future)
    assert ran == [], "a job cancelled while queued still ran"
    _assert_no_shaping_left()


async def test_a_shaping_job_that_raises_frees_its_slot(monkeypatch) -> None:
    def _broken_shape(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("shaping failed")

    monkeypatch.setattr(gateway, "shape_response", _broken_shape)
    _serve(monkeypatch, _RECORDS)
    failed = _structured(await mcp.call_tool("call_endpoint", {**QUOTE_CALL, "fields": ["a"]}))
    assert (failed["error"], failed["exception_type"]) == ("tool_execution_failed", "RuntimeError")
    assert await _until(lambda: gateway.shaping_pending() == 0)
    _assert_no_shaping_left()


# ---- waiting for a shaping slot: woken by the release, oldest first --------


async def test_a_waiting_shaping_job_cancelled_leaves_the_line(monkeypatch) -> None:
    blocking = _BlockingShape()
    monkeypatch.setattr(gateway, "SHAPING_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SHAPING_WAIT_SECONDS", 5.0)
    holder = asyncio.create_task(gateway._shape_off_loop(partial(blocking, _RECORDS)))
    try:
        assert await asyncio.to_thread(blocking.started.wait, 5)
        cancelled = asyncio.create_task(gateway._shape_off_loop(partial(blocking, _RECORDS)))
        later = asyncio.create_task(gateway._shape_off_loop(partial(blocking, _RECORDS)))
        assert await _until(lambda: gateway.shaping_waiting() == 2)
        assert gateway.shaping_pending() == 1
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert (gateway.shaping_waiting(), gateway.shaping_pending()) == (1, 1)
    finally:
        blocking.release.set()
    assert await asyncio.wait_for(asyncio.gather(holder, later), 5) == [_RECORDS, _RECORDS]
    assert blocking.calls == 2, "a job cancelled while it waited still ran"
    assert await _until(lambda: gateway.shaping_pending() == 0)
    _assert_no_shaping_left()


async def test_the_slot_a_finished_job_frees_goes_to_the_waiting_job_in_the_release(monkeypatch) -> None:
    """The release that runs on the shaping worker when a job ends hands its
    slot to the waiting job before it returns. Checked by state, not by timing:
    the waiting job's own work waits until that state is recorded."""
    blocking = _BlockingShape()
    monkeypatch.setattr(gateway, "SHAPING_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SHAPING_WAIT_SECONDS", 60.0)
    real_release = gateway._release_shaping_slot
    recorded = threading.Event()
    seen: list[tuple[int, int, str]] = []

    def _release(caller: str) -> None:
        real_release(caller)
        seen.append((gateway.shaping_pending(), gateway.shaping_waiting(), threading.current_thread().name))
        recorded.set()

    monkeypatch.setattr(gateway, "_release_shaping_slot", _release)
    holder = asyncio.create_task(gateway._shape_off_loop(partial(blocking, _RECORDS)))
    try:
        assert await asyncio.to_thread(blocking.started.wait, 5)
        waiter = asyncio.create_task(gateway._shape_off_loop(partial(recorded.wait, 5)))
        assert await _until(lambda: gateway.shaping_waiting() == 1)
    finally:
        blocking.release.set()
    assert await asyncio.wait_for(asyncio.gather(holder, waiter), 5) == [_RECORDS, True]
    assert seen[0][:2] == (1, 0), seen
    assert seen[0][2].startswith("response-shaping"), seen
    _assert_no_shaping_left()


async def test_a_release_never_hands_a_shaping_slot_to_a_job_whose_wait_has_ended(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "SHAPING_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SHAPING_WAIT_SECONDS", 60.0)
    ran: list[str] = []
    assert gateway._claim_shaping_slot("caller-a") is None
    started = time.monotonic()
    waiter = gateway._admit_shaping("caller-a", started, partial(ran.append, "waiter"))
    assert isinstance(waiter, gateway._ShapingWaiter)
    waiter.deadline = time.monotonic() - 0.001
    gateway._release_shaping_slot("caller-a")
    assert gateway.shaping_pending() == 0, "the release handed its slot to a job whose wait had ended"
    assert gateway.shaping_waiting() == 0, "a job whose wait had ended kept its place in line"
    refused = await asyncio.wait_for(gateway._wait_for_shaping_slot(waiter, started), 5)
    assert (refused["error"], refused["scope"], refused["limit"]) == ("server_busy", "shaping", 1)
    assert ran == []
    _assert_no_shaping_left()


async def test_a_shaping_job_handed_a_slot_as_its_event_loop_closes_is_cancelled(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "SHAPING_MAX_PENDING", 1)
    ran: list[str] = []
    hold = threading.Event()
    try:
        occupying = await _occupy_both_workers(hold)
        assert gateway._claim_shaping_slot("caller-a") is None
        loop = _LoopClosingAtTheWake(asyncio.get_running_loop())
        waiter = gateway._ShapingWaiter("caller-a", partial(ran.append, "waiter"), loop, time.monotonic() + 60, "shaping")
        with gateway._shaping_lock:
            gateway._shaping_waiters.append(waiter)
            gateway._shaping_waiting_by_caller["caller-a"] = 1
        gateway._release_shaping_slot("caller-a")
        assert waiter.future is not None and waiter.future.cancelled()
        assert gateway.shaping_pending() == 0, "the slot handed to a job nobody can read was kept"
    finally:
        hold.set()
    for future in occupying:
        await asyncio.wrap_future(future)
    assert ran == []
    _assert_no_shaping_left()


def _park_on_own_loop(loop: asyncio.AbstractEventLoop, call: Any) -> tuple[threading.Thread, list[asyncio.Task]]:
    """Start a thread running loop with one task that joins the shaping line as
    caller-a and waits there. The task is kept, so the collector cannot close it
    first, and the thread is a daemon, so a park that fails cannot hold the run."""
    parked = threading.Event()
    kept: list[asyncio.Task] = []

    async def _wait() -> None:
        started = time.monotonic()
        waiter = gateway._admit_shaping("caller-a", started, call)
        assert isinstance(waiter, gateway._ShapingWaiter)
        parked.set()
        await gateway._wait_for_shaping_slot(waiter, started)

    def _run() -> None:
        kept.append(loop.create_task(_wait()))
        loop.run_forever()
        loop.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert parked.wait(5)
    return thread, kept


def _close_parked_task(kept: list[asyncio.Task]) -> None:
    """Close the parked task's coroutine, as the collector would; it must give nothing back twice."""
    kept[0].get_coro().close()
    kept[0]._log_destroy_pending = False
    _assert_no_shaping_left()


def test_a_shaping_job_whose_event_loop_closed_while_it_waited_leaves_the_line(monkeypatch) -> None:
    """A job still waiting in line when its event loop closes can never answer.
    The next release takes it out of the line without handing it a slot."""
    monkeypatch.setattr(gateway, "SHAPING_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SHAPING_WAIT_SECONDS", 60.0)
    loop = asyncio.new_event_loop()
    ran: list[str] = []
    assert gateway._claim_shaping_slot("caller-a") is None
    thread, kept = _park_on_own_loop(loop, partial(ran.append, "caller-a"))
    loop.call_soon_threadsafe(loop.stop)
    thread.join(5)
    assert loop.is_closed()
    assert gateway.shaping_waiting("caller-a") == 1
    gateway._release_shaping_slot("caller-a")
    assert (gateway.shaping_pending(), gateway.shaping_waiting()) == (0, 0)
    assert ran == []
    _close_parked_task(kept)


def test_a_shaping_slot_handed_to_a_job_whose_event_loop_closed_comes_back(monkeypatch) -> None:
    """The release hands its slot over and schedules the wake, then the waiting
    job's loop stops and closes before the wake runs. The release has already
    submitted that job to the pool, so the slot comes back when it ends."""
    monkeypatch.setattr(gateway, "SHAPING_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SHAPING_WAIT_SECONDS", 60.0)
    loop = asyncio.new_event_loop()
    holding, go, finish = threading.Event(), threading.Event(), threading.Event()
    ran: list[str] = []

    def _shape() -> dict[str, Any]:
        ran.append(threading.current_thread().name)
        finish.wait(5)
        return {}

    def _hold_then_stop() -> None:
        # The wake scheduled while this runs is left for the next loop pass,
        # and stop() ends the loop before that pass.
        holding.set()
        go.wait(5)
        loop.stop()

    assert gateway._claim_shaping_slot("caller-a") is None
    thread, kept = _park_on_own_loop(loop, _shape)
    try:
        loop.call_soon_threadsafe(_hold_then_stop)
        assert holding.wait(5)
        gateway._release_shaping_slot("caller-a")
        assert (gateway.shaping_pending("caller-a"), gateway.shaping_waiting()) == (1, 0)
    finally:
        go.set()
        thread.join(5)
    try:
        assert loop.is_closed()
        assert not kept[0].done(), "the waiting job ran after all"
    finally:
        finish.set()
    assert _until_sync(lambda: gateway.shaping_pending() == 0), "the slot handed to the closed loop's job was lost"
    assert len(ran) == 1 and ran[0].startswith("response-shaping")
    _close_parked_task(kept)


async def test_the_oldest_waiting_job_is_served_first_past_a_caller_at_its_bound(monkeypatch) -> None:
    """A waiting job whose caller is at its own bound keeps its place without
    holding up the jobs of other callers behind it."""
    monkeypatch.setattr(gateway, "SHAPING_MAX_PENDING", 2)
    monkeypatch.setattr(gateway, "SHAPING_MAX_PENDING_PER_CALLER", 1)
    monkeypatch.setattr(gateway, "SHAPING_WAIT_SECONDS", 60.0)
    monkeypatch.setattr(gateway, "current_caller", _CALLER.get)
    order: list[str] = []
    assert gateway._claim_shaping_slot("caller-a") is None
    assert gateway._claim_shaping_slot("caller-c") is None
    waiting = []
    for count, (caller, name) in enumerate((("caller-a", "a2"), ("caller-b", "b1"), ("caller-d", "d1")), start=1):
        waiting.append(_as_caller(caller, gateway._shape_off_loop(partial(order.append, name))))
        assert await _until(lambda count=count: gateway.shaping_waiting() == count)
    gateway._release_shaping_slot("caller-c")
    assert await _until(lambda: order == ["b1", "d1"]), order
    assert gateway.shaping_waiting() == 1
    gateway._release_shaping_slot("caller-a")
    assert await asyncio.wait_for(asyncio.gather(*waiting), 5) == [None, None, None]
    assert order == ["b1", "d1", "a2"]
    assert await _until(lambda: gateway.shaping_pending() == 0)
    _assert_no_shaping_left()


async def test_a_waiting_shaping_job_cancelled_after_the_hand_off_gives_its_slot_back(monkeypatch) -> None:
    """A release has already submitted the job when its caller is cancelled:
    the job is cancelled like a queued one, never runs, and frees its slot."""
    monkeypatch.setattr(gateway, "SHAPING_MAX_PENDING", 1)
    monkeypatch.setattr(gateway, "SHAPING_WAIT_SECONDS", 60.0)
    monkeypatch.setattr(gateway, "current_caller", lambda: "caller-a")
    ran: list[str] = []
    hold = threading.Event()
    try:
        occupying = await _occupy_both_workers(hold)
        assert gateway._claim_shaping_slot("caller-a") is None
        waiter = asyncio.create_task(gateway._shape_off_loop(partial(ran.append, "waiter")))
        assert await _until(lambda: gateway.shaping_waiting() == 1)
        gateway._release_shaping_slot("caller-a")
        assert (gateway.shaping_pending(), gateway.shaping_waiting()) == (1, 0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert await _until(lambda: gateway.shaping_pending() == 0), "the slot handed to a cancelled job was kept"
    finally:
        hold.set()
    for future in occupying:
        await asyncio.wrap_future(future)
    assert ran == []
    _assert_no_shaping_left()


# ---- what the refusal carries, and what does not change --------------------


async def test_the_field_text_never_reaches_the_result_or_the_span(monkeypatch) -> None:
    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    monkeypatch.setattr(gateway, "get_client", _no_client)
    result = await mcp.call_tool("call_endpoint", {**QUOTE_CALL, "fields": ["zqfieldtext"] * 33})
    assert _structured(result)["error"] == "projection_too_large"
    assert [span.name for span in tracer.spans] == ["mcp.tool.call_endpoint"]
    assert tracer.spans[0].attributes["mcp.error.code"] == "projection_too_large"
    assert "zqfieldtext" not in repr(result)
    assert "zqfieldtext" not in repr(tracer.spans[0].attributes)


def test_each_field_path_is_split_once_per_projection(monkeypatch) -> None:
    real_split = response._split_field_path
    calls: list[str] = []

    def _counting_split(field: str) -> list[str]:
        calls.append(field)
        return real_split(field)

    monkeypatch.setattr(response, "_split_field_path", _counting_split)
    payload = {"data": [{"id": i, "geo": {"city": f"c{i}"}} for i in range(500)]}
    shaped = response.shape_response(payload, fields=["geo.city", "id"])
    assert shaped["data"][1] == {"geo": {"city": "c1"}, "id": 1}
    assert sorted(calls) == ["geo.city", "id"]


async def test_fetch_data_refuses_oversized_fields_before_any_request(monkeypatch) -> None:
    monkeypatch.setattr(gateway, "search_catalog", lambda catalog, query, **kwargs: [{"operation_id": "quotes_symbol_price"}])
    client = _serve(monkeypatch, _RECORDS)
    arguments = {"query": "AAPL price", "params": {"symbol": "AAPL"}}
    refused = _structured(await mcp.call_tool("fetch_data", {**arguments, "fields": ["zq"] * 33}))
    assert (refused["error"], refused["limit_kind"]) == ("projection_too_large", "fields")
    assert client.calls == 0
    served = _structured(await mcp.call_tool("fetch_data", {**arguments, "fields": ["a"]}))
    assert served["data"] == [{"a": 1}, {"a": 3}]
    assert client.calls == 1


async def test_an_error_payload_is_returned_without_reaching_the_pool(monkeypatch) -> None:
    def _must_not_run(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("an error payload was shaped")

    monkeypatch.setattr(gateway, "shape_response", _must_not_run)
    monkeypatch.setattr(gateway, "_shape_off_loop", _must_not_run)
    failure = {
        "error": "upstream_timeout",
        "reason": "The upstream did not answer in time.",
        "status_code": None,
        "elapsed_ms": 30000,
        "url": "https://api.example.test/quote",
        "retry_hint": "Retry once.",
    }
    _serve(monkeypatch, failure)
    assert await gateway.call_endpoint(**QUOTE_CALL, fields=["a"]) == failure


async def test_a_projection_within_every_bound_is_shaped_as_before(monkeypatch) -> None:
    _serve(
        monkeypatch,
        {
            "data": {
                "total": 3,
                "items": [
                    {"date": "2026-01-01", "geo": {"city": "Oslo", "zip": "0150"}, "value": 1},
                    {"date": "2026-01-02", "geo": {"city": "Bergen", "zip": "5003"}, "value": 2},
                    {"date": "2026-01-03", "geo": {"city": "Tromso", "zip": "9008"}, "value": 3},
                ],
            },
            "meta": {"source": "test"},
        },
    )
    result = await gateway.call_endpoint(**QUOTE_CALL, limit=2, fields=["geo.city", "date"])
    assert result == {
        "data": {
            "total": 3,
            "items": [
                {"geo": {"city": "Bergen"}, "date": "2026-01-02"},
                {"geo": {"city": "Tromso"}, "date": "2026-01-03"},
            ],
        },
        "meta": {
            "source": "test",
            "shaped": {
                "limit": 2,
                "limit_applied": True,
                "fields": ["geo.city", "date"],
                "fields_applied": ["geo.city", "date"],
                "fields_unmatched": [],
                "records_path": "data.items",
                "order": "asc",
                "kept_end": "newest",
            },
        },
    }


async def test_include_raw_keeps_the_payload_beside_the_projection(monkeypatch) -> None:
    _serve(monkeypatch, _RECORDS)
    result = await gateway.call_endpoint(**QUOTE_CALL, fields=["a"], include_raw=True)
    assert result["data"] == [{"a": 1}, {"a": 3}]
    assert result["raw"] == _RECORDS
