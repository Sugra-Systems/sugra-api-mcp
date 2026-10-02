"""The gate summary: which requests leave a line, what the line says, and when it is written.

The in-process tests drive the real app (the gate in front of AuthMiddleware in
front of the SDK session manager) and read the lines the summary logs.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import sugra_api_mcp.tools  # noqa: F401  (registers the tools on server.mcp)
from sugra_api_mcp import gate, observability, server
from sugra_api_mcp.auth import AuthMiddleware, ResolvedAuth
from tests.test_request_credentials import HEADERS, INITIALIZE, _authenticator

_HEADER = re.compile(
    r"sgate1 side=(?P<side>\S+) boot=(?P<boot>[0-9a-f]{8}) seq=(?P<seq>\d+) "
    r"part=(?P<part>\d+)/(?P<parts>\d+) lines=(?P<lines>\d+) dropped=(?P<dropped>\d+)"
)
_OAUTH = {"authorization": "Bearer jwt-gate"}


def _rid(number: int) -> str:
    """A request id as nginx writes it: 32 lowercase hex."""
    return f"{number:02x}" + "ab" * 15


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def written():
    """Every message the gate logger writes during the test."""
    handler = _Collect()
    gate.logger.addHandler(handler)
    try:
        yield handler.messages
    finally:
        gate.logger.removeHandler(handler)


def _records(messages: list[str]) -> list[tuple[dict[str, str], list[str]]]:
    records = []
    for message in messages:
        header, *lines = message.split("\n")
        match = _HEADER.fullmatch(header)
        assert match is not None, header
        records.append((match.groupdict(), lines))
    return records


def _summary_lines(summary: gate.GateSummary, written: list[str]) -> list[str]:
    written.clear()
    summary.write()
    return [line for _, lines in _records(written) for line in lines]


# ---- The summary record ----


def test_a_summary_with_no_lines_is_its_header_alone(written, monkeypatch) -> None:
    monkeypatch.delenv("CONTAINER_APP_REVISION", raising=False)
    summary = gate.GateSummary()
    summary.write()
    summary.write()
    [(first, first_lines), (second, second_lines)] = _records(written)
    assert first == {
        "side": "vm",
        "boot": summary.boot,
        "seq": "1",
        "part": "1",
        "parts": "1",
        "lines": "0",
        "dropped": "0",
    }
    assert (second["boot"], second["seq"]) == (summary.boot, "2")
    assert first_lines == second_lines == []


def test_a_summary_is_split_into_records_of_at_most_lines_per_record(written, monkeypatch) -> None:
    # A record of three lines: its header and two request lines.
    monkeypatch.setattr(gate, "LINES_PER_RECORD", 3)
    summary = gate.GateSummary()
    lines = [f"{_rid(index)} -" for index in range(5)]
    for line in lines:
        summary.add(line)
    summary.write()
    records = _records(written)
    assert [(h["seq"], h["part"], h["parts"], h["lines"]) for h, _ in records] == [
        ("1", "1", "3", "2"),
        ("1", "2", "3", "2"),
        ("1", "3", "3", "1"),
    ]
    assert [len(message.split("\n")) for message in written] == [3, 3, 2]
    assert [line for _, part in records for line in part] == lines
    summary.write()
    assert _records(written)[-1][0]["lines"] == "0"


@pytest.mark.parametrize(
    ("count", "sizes"),
    [(0, [0]), (1, [1]), (499, [499]), (500, [499, 1]), (998, [499, 499]), (999, [499, 499, 1])],
)
def test_a_record_holds_at_most_lines_per_record_lines_its_header_among_them(
    written, count: int, sizes: list[int]
) -> None:
    # At the real limit of 500 lines a record carries its header and 499 request lines.
    summary = gate.GateSummary()
    lines = [f"{index:032x} -" for index in range(count)]
    for line in lines:
        summary.add(line)
    summary.write()
    records = _records(written)
    assert [(int(h["part"]), int(h["parts"]), int(h["lines"])) for h, _ in records] == [
        (part, len(sizes), size) for part, size in enumerate(sizes, start=1)
    ]
    assert max(len(message.split("\n")) for message in written) <= gate.LINES_PER_RECORD
    assert [line for _, part in records for line in part] == lines


def test_lines_past_the_buffer_are_only_counted(written, monkeypatch) -> None:
    monkeypatch.setattr(gate, "MAX_BUFFERED_LINES", 3)
    # A record of three lines: its header and two request lines.
    monkeypatch.setattr(gate, "LINES_PER_RECORD", 3)
    summary = gate.GateSummary()
    for index in range(5):
        summary.add(f"{_rid(index)} -")
    summary.write()
    summary.write()
    assert [(h["seq"], h["part"], h["lines"], h["dropped"]) for h, _ in _records(written)] == [
        ("1", "1", "2", "2"),
        ("1", "2", "1", "2"),
        ("2", "1", "0", "0"),
    ]


@pytest.mark.parametrize(
    ("revision", "side"), [("sugra-mcp--r7", "sugra-mcp--r7"), ("Not A Revision", "unknown")]
)
def test_the_header_names_the_side(written, monkeypatch, revision: str, side: str) -> None:
    monkeypatch.setenv("CONTAINER_APP_REVISION", revision)
    gate.GateSummary().write()
    assert _records(written)[0][0]["side"] == side


def test_each_summary_draws_its_own_boot() -> None:
    boots = [gate.GateSummary().boot for _ in range(8)]
    assert all(re.fullmatch(r"[0-9a-f]{8}", boot) for boot in boots)
    assert len(set(boots)) == len(boots)


def test_the_summary_limits() -> None:
    assert gate.SUMMARY_INTERVAL_SECONDS == 60
    assert gate.LINES_PER_RECORD == 500
    assert gate.MAX_BUFFERED_LINES == 20_000
    assert gate.DRAIN_SECONDS == 1
    assert gate.FLUSH_TIMEOUT_SECONDS == 10


def test_a_full_record_fits_one_app_insights_message(written, monkeypatch) -> None:
    # The longest side a revision name can have, and lines far longer than
    # real ones: an hour-long call that made thousands of API requests.
    monkeypatch.setenv("CONTAINER_APP_REVISION", "a" * 128)
    summary = gate.GateSummary()
    for index in range(gate.LINES_PER_RECORD + 1):
        summary.add(f"{index:032x} TFA 999999999 9999")
    summary.write()
    first = written[0]
    # A full record: its header and LINES_PER_RECORD - 1 request lines.
    assert len(first.split("\n")) == gate.LINES_PER_RECORD
    assert len(first) < 32_768


def test_the_gate_logger_writes_info_on_its_own() -> None:
    assert gate.logger.name == "sugra_mcp.gate"
    assert gate.logger.level == logging.INFO
    assert gate.logger.propagate is True


def test_the_summary_reaches_a_root_handler_installed_before_fastmcp() -> None:
    """The hosted order: the Azure Monitor handler is on the root logger before
    FastMCP is built, so FastMCP's logging setup does nothing and the root
    logger stays at WARNING. The summary is INFO and must still get there."""
    root = logging.getLogger()
    collect = _Collect()
    saved_level = root.level
    root.setLevel(logging.WARNING)
    root.addHandler(collect)
    try:
        FastMCP("gate-logging-probe")
        assert root.level == logging.WARNING
        gate.GateSummary().write()
        logging.getLogger("tests.gate.control").info("an INFO record outside the gate")
    finally:
        root.removeHandler(collect)
        root.setLevel(saved_level)
    assert sum(message.startswith("sgate1 ") for message in collect.messages) == 1
    assert "an INFO record outside the gate" not in collect.messages


# ---- The line of one request ----


def _finish(record: gate.GateRecord, ms: int, api_requests: int, outcome: str) -> None:
    call = record.call_started(observability.ToolDispatch(api_requests=api_requests))
    call.started = ms
    record.call_finished(call, outcome)


@pytest.mark.parametrize(
    ("calls", "line"),
    [
        ([(120, 1, gate.SUCCEEDED)], "T 120 1"),
        ([(5, 0, gate.FAILED)], "TF 5 0"),
        ([(80, 1, gate.AUTH_REFUSED)], "TA 80 1"),
        ([(10, 1, gate.SUCCEEDED), (30, 2, gate.AUTH_REFUSED), (20, 3, gate.FAILED)], "TFA 30 3"),
        ([], "TF 0 0"),
    ],
    ids=["success", "failure", "auth-refused", "several-calls", "no-call-reached"],
)
def test_a_tool_line(monkeypatch, calls: list[tuple[int, int, str]], line: str) -> None:
    # Durations are read from each call's start: here the start IS the duration.
    monkeypatch.setattr(gate, "_elapsed_ms", lambda started: int(started))
    record = gate.GateRecord()
    record.carries_tool_call = True
    for ms, api_requests, outcome in calls:
        _finish(record, ms, api_requests, outcome)
    assert record.tool_line(_rid(1)) == f"{_rid(1)} {line}"


def test_a_call_still_running_when_the_answer_ended_is_a_failure(monkeypatch) -> None:
    monkeypatch.setattr(gate, "_elapsed_ms", lambda started: int(started))
    record = gate.GateRecord()
    _finish(record, 40, 1, gate.SUCCEEDED)
    running = record.call_started(observability.ToolDispatch(api_requests=2))
    running.started = 900
    assert record.tool_line(_rid(1)) == f"{_rid(1)} TF 900 2"


def test_a_duration_is_whole_milliseconds() -> None:
    assert 1500 <= gate._elapsed_ms(time.perf_counter() - 1.5) < 1600


def test_a_request_is_a_tool_call_by_its_body_or_by_a_call_it_carried() -> None:
    assert gate.GateRecord().is_tool_call() is False
    by_body = gate.GateRecord()
    by_body.carries_tool_call = True
    assert by_body.is_tool_call() is True
    by_call = gate.GateRecord()
    by_call.call_started(observability.ToolDispatch())
    assert by_call.is_tool_call() is True


def test_there_is_no_record_outside_a_request() -> None:
    assert gate.current_record() is None


class _IntLike(int):
    pass


class _RaisingDict(dict):
    def get(self, key: object, default: object = None) -> object:
        raise RuntimeError("no reading this")


@pytest.mark.parametrize(
    ("payload", "refused"),
    [
        ({"error": "Invalid API key", "status_code": 401}, True),
        ({"error": "Forbidden", "status_code": 403}, True),
        ({"error": "agent_plane_unavailable", "status_code": 401}, False),
        ({"error": "agent_plane_unavailable", "status_code": 403}, False),
        ({"error": "Too many requests", "status_code": 429}, False),
        ({"error": "Internal error", "status_code": 500}, False),
        ({"error": "x", "status_code": "401"}, False),
        ({"error": "x", "status_code": 401.0}, False),
        ({"error": "x", "status_code": True}, False),
        ({"error": "x", "status_code": _IntLike(401)}, False),
        ({"error": "upstream_timeout", "status_code": None}, False),
        ({"error": "x"}, False),
        (_RaisingDict(status_code=401), False),
        (None, False),
        ([401], False),
        ("401", False),
        (401, False),
    ],
)
def test_only_the_api_refusing_the_credential_is_an_auth_refusal(payload: object, refused: bool) -> None:
    assert gate.is_auth_refusal(payload) is refused


def _failed(payload: object) -> CallToolResult:
    return CallToolResult(isError=True, content=[TextContent(type="text", text="{}")], structuredContent=payload)


@pytest.mark.parametrize(
    ("result", "outcome"),
    [
        (_failed({"error": "Invalid API key", "status_code": 401}), gate.AUTH_REFUSED),
        (_failed({"error": "Internal error", "status_code": 500}), gate.FAILED),
        (_failed(None), gate.FAILED),
        (CallToolResult(isError=False, content=[], structuredContent={"status_code": 401}), gate.SUCCEEDED),
        (([TextContent(type="text", text="{}")], {"data": []}), gate.SUCCEEDED),
        ({"data": []}, gate.SUCCEEDED),
    ],
    ids=["auth-refused", "failed", "failed-without-payload", "success-result", "tuple", "dict"],
)
def test_how_a_call_that_returned_ended(result: object, outcome: str) -> None:
    assert gate.outcome_of(result) == outcome


# ---- Which requests leave a line, on the real app ----


@contextlib.asynccontextmanager
async def _served(
    monkeypatch, summary: gate.GateSummary, *, max_body: int | None = None
) -> AsyncIterator[httpx.AsyncClient]:
    if max_body is not None:
        monkeypatch.setattr(server.mcp.settings, "max_request_body_size", max_body)
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    app = server.mcp.streamable_http_app()
    authenticator = _authenticator()

    async def resolve(token: str) -> ResolvedAuth:
        return ResolvedAuth(
            api_key="sugra_dummy",
            user_id=42,
            access_token_id=token.strip(),
            method="oauth",
            platform="anthropic",
        )

    monkeypatch.setattr(authenticator, "resolve", resolve)
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    app.add_middleware(
        gate.GateMiddleware, max_body_bytes=server.mcp.settings.max_request_body_size, summary=summary
    )
    try:
        async with server.mcp.session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
                yield client
    finally:
        await authenticator.aclose()


async def _open(client: httpx.AsyncClient, request_id: str | None = None) -> dict[str, str]:
    headers = dict(HEADERS) if request_id is None else {**HEADERS, "x-request-id": request_id}
    opened = await client.post("/mcp", json=INITIALIZE, headers=headers)
    assert opened.status_code == 200
    return {"mcp-session-id": opened.headers["mcp-session-id"]}


def _message(number: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": number, "method": method, "params": params}


async def test_each_answered_post_with_a_request_id_leaves_one_line(monkeypatch, written) -> None:
    summary = gate.GateSummary()
    async with _served(monkeypatch, summary) as client:
        session = await _open(client, _rid(1))

        async def post(number: int, body: dict[str, Any], extra: dict[str, str] | None = None) -> int:
            headers = {**HEADERS, **session, "x-request-id": _rid(number), **(extra or {})}
            return (await client.post("/mcp", json=body, headers=headers)).status_code

        assert await post(2, {"jsonrpc": "2.0", "method": "notifications/initialized"}) == 202
        assert await post(3, _message(3, "ping", {})) == 200
        assert await post(4, _message(4, "tools/list", {})) == 200
        assert await post(5, _message(5, "resources/read", {"uri": "sugra://no-such-resource"}), _OAUTH) == 200
        assert await post(6, _message(6, "tools/call", {"name": "list_toolsets", "arguments": {}}), _OAUTH) == 200
        # A tools/call the SDK refuses as malformed before any tool sees it.
        assert await post(7, _message(7, "tools/call", {"arguments": {}}), _OAUTH) == 200

    lines = _summary_lines(summary, written)
    assert lines[:5] == [f"{_rid(1)} -", f"{_rid(2)} -", f"{_rid(3)} -", f"{_rid(4)} -", f"{_rid(5)} E"]
    assert re.fullmatch(rf"{_rid(6)} T \d+ 0", lines[5])
    assert lines[6:] == [f"{_rid(7)} TF 0 0"]
    assert summary.open_requests == 0


async def test_no_line_without_a_2xx_answer_or_a_valid_first_request_id(monkeypatch, written) -> None:
    summary = gate.GateSummary()
    call = _message(2, "tools/call", {"name": "list_toolsets", "arguments": {}})
    async with _served(monkeypatch, summary) as client:
        session = await _open(client)
        # Refused by AuthMiddleware: tools/call needs a credential.
        unauthenticated = await client.post(
            "/mcp", json=call, headers={**HEADERS, **session, "x-request-id": _rid(1)}
        )
        unknown_session = await client.post(
            "/mcp",
            json=call,
            headers={**HEADERS, **_OAUTH, "mcp-session-id": "f" * 32, "x-request-id": _rid(2)},
        )
        no_id = await client.post("/mcp", json=call, headers={**HEADERS, **_OAUTH, **session})
        uppercase = await client.post(
            "/mcp", json=call, headers={**HEADERS, **_OAUTH, **session, "x-request-id": _rid(3).upper()}
        )
        trimmed = await client.post(
            "/mcp", json=call, headers={**HEADERS, **_OAUTH, **session, "x-request-id": _rid(4)[:31]}
        )
        # Only the first X-Request-Id counts, as nginx's own is the first.
        second_valid = await client.post(
            "/mcp",
            json=call,
            headers=[
                *HEADERS.items(),
                *_OAUTH.items(),
                *session.items(),
                ("x-request-id", "not-a-request-id"),
                ("x-request-id", _rid(5)),
            ],
        )
        first_valid = await client.post(
            "/mcp",
            json=call,
            headers=[
                *HEADERS.items(),
                *_OAUTH.items(),
                *session.items(),
                ("x-request-id", _rid(6)),
                ("x-request-id", "not-a-request-id"),
            ],
        )

    assert (unauthenticated.status_code, unknown_session.status_code) == (401, 404)
    assert [r.status_code for r in (no_id, uppercase, trimmed, second_valid, first_valid)] == [200] * 5
    assert [line.split(" ", 1)[0] for line in _summary_lines(summary, written)] == [_rid(6)]


async def test_no_line_for_a_body_over_the_limit(monkeypatch, written) -> None:
    summary = gate.GateSummary()
    async with _served(monkeypatch, summary, max_body=2048) as client:
        session = await _open(client, _rid(1))
        too_large = await client.post(
            "/mcp",
            json=_message(2, "tools/call", {"name": "search_endpoints", "arguments": {"query": "x" * 4096}}),
            headers={**HEADERS, **_OAUTH, **session, "x-request-id": _rid(2)},
        )
    assert too_large.status_code == 413
    assert _summary_lines(summary, written) == [f"{_rid(1)} -"]


async def test_only_a_post_is_tracked_and_its_count_is_released(written) -> None:
    summary = gate.GateSummary()
    seen: list[tuple[str, bool, int]] = []

    async def probe(request: Request) -> JSONResponse:
        state = request.scope.get("state") or {}
        seen.append((request.method, gate.GATE_STATE in state, summary.open_requests))
        return JSONResponse({})

    async def broken(request: Request) -> JSONResponse:
        raise RuntimeError("handler failed")

    app = Starlette(routes=[Route("/mcp", probe, methods=["GET", "POST"]), Route("/broken", broken, methods=["POST"])])
    app.add_middleware(gate.GateMiddleware, max_body_bytes=1024, summary=summary)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
        await client.get("/mcp", headers={"x-request-id": _rid(1)})
        await client.post("/mcp", json={}, headers={"x-request-id": _rid(2)})
        failed = await client.post("/broken", json={}, headers={"x-request-id": _rid(3)})
    assert seen == [("GET", False, 0), ("POST", True, 1)]
    assert failed.status_code == 500
    assert summary.open_requests == 0
    assert _summary_lines(summary, written) == [f"{_rid(2)} -"]


# ---- When the summary is written ----


@contextlib.asynccontextmanager
async def _inner(app: object) -> AsyncIterator[dict[str, str]]:
    yield {"kept": "as is"}


@pytest.fixture
def flushes(monkeypatch) -> list[float]:
    """The timeout of every telemetry flush, which never touches a real exporter."""
    calls: list[float] = []

    def flush(timeout_s: float) -> bool:
        calls.append(timeout_s)
        return True

    monkeypatch.setattr(observability, "flush_telemetry", flush)
    return calls


async def test_a_summary_is_written_every_interval_and_once_more_on_exit(written, flushes) -> None:
    lifespan = gate.wrap_lifespan(_inner, gate.GateSummary(), interval=0.02, drain_seconds=0, flush_timeout=7)
    async with lifespan(object()) as state:
        assert state == {"kept": "as is"}
        deadline = time.monotonic() + 5
        while len(written) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        periodic = len(written)
    assert periodic >= 2
    assert [int(header["seq"]) for header, _ in _records(written)] == list(range(1, periodic + 2))
    assert flushes == [7]


async def test_the_exit_work_runs_in_order_and_past_a_failing_step(monkeypatch, caplog) -> None:
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def inner(app: object) -> AsyncIterator[None]:
        yield None
        events.append("app exited")

    summary = gate.GateSummary()
    monkeypatch.setattr(summary, "write", lambda: events.append("summary"))

    def flush(timeout_s: float) -> bool:
        events.append(f"flush {timeout_s:g}")
        return True

    monkeypatch.setattr(observability, "flush_telemetry", flush)

    async def failing() -> None:
        events.append("failing step")
        raise RuntimeError("could not close user@example.com")

    async def closing() -> None:
        events.append("next step")

    lifespan = gate.wrap_lifespan(
        inner, summary, interval=3600, drain_seconds=0, flush_timeout=4, on_exit=(failing, closing)
    )
    async with lifespan(object()):
        events.append("serving")
    assert events == ["serving", "app exited", "summary", "failing step", "next step", "flush 4"]
    assert "Shutdown step failed (RuntimeError)." in caplog.messages
    assert "user@example.com" not in caplog.text


async def test_the_last_summary_waits_for_a_request_still_finishing(written, flushes) -> None:
    summary = gate.GateSummary()
    lifespan = gate.wrap_lifespan(_inner, summary, interval=3600, drain_seconds=5, flush_timeout=1)

    async def finishing() -> None:
        await asyncio.sleep(0.1)
        summary.add(f"{_rid(1)} -")
        summary.open_requests -= 1

    summary.open_requests += 1
    async with lifespan(object()):
        task = asyncio.create_task(finishing())
    await task
    assert [lines for _, lines in _records(written)] == [[f"{_rid(1)} -"]]


async def test_the_last_summary_waits_no_longer_than_the_drain(written, flushes) -> None:
    summary = gate.GateSummary()
    # A request that never finishes.
    summary.open_requests = 1
    lifespan = gate.wrap_lifespan(_inner, summary, interval=3600, drain_seconds=0.2, flush_timeout=1)
    started = time.monotonic()
    async with lifespan(object()):
        pass
    assert 0.2 <= time.monotonic() - started < 2
    assert len(written) == 1
    assert flushes == [1]


async def test_an_app_that_fails_still_gets_its_last_summary_and_flush(written, flushes) -> None:
    lifespan = gate.wrap_lifespan(_inner, gate.GateSummary(), interval=3600, drain_seconds=0, flush_timeout=1)
    with pytest.raises(RuntimeError, match="serving failed"):
        async with lifespan(object()):
            raise RuntimeError("serving failed")
    assert len(written) == 1
    assert flushes == [1]


async def test_a_failed_summary_is_logged_and_the_next_one_still_runs(monkeypatch, caplog, flushes) -> None:
    summary = gate.GateSummary()
    attempts: list[int] = []

    def write() -> None:
        attempts.append(len(attempts))
        if len(attempts) == 1:
            raise OSError("handler failed")

    monkeypatch.setattr(summary, "write", write)
    lifespan = gate.wrap_lifespan(_inner, summary, interval=0.02, drain_seconds=0, flush_timeout=1)
    async with lifespan(object()):
        deadline = time.monotonic() + 5
        while len(attempts) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
    # At least two periodic attempts, then the last summary on exit.
    assert len(attempts) >= 3
    assert "Gate summary failed (OSError)." in caplog.messages


# ---- How the HTTP server is put together ----


def test_the_http_server_runs_behind_the_gate(monkeypatch) -> None:
    import uvicorn

    from sugra_api_mcp import __main__ as entry

    captured: dict[str, Any] = {}

    def run(app: Any, **kwargs: Any) -> None:
        captured.update(kwargs, app=app)

    monkeypatch.setattr(observability, "setup_observability", lambda: False)
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    monkeypatch.delenv("SUGRA_AGENT_INTERNAL_TOKEN", raising=False)
    monkeypatch.setenv("SUGRA_APP_URL", "http://127.0.0.1:9")

    entry._run_server(argparse.Namespace(transport="streamable-http", host="127.0.0.1", port=0))

    app = captured["app"]
    outermost = app.user_middleware[0]
    assert outermost.cls is gate.GateMiddleware
    assert outermost.kwargs == {"max_body_bytes": server.mcp.settings.max_request_body_size}
    assert captured["timeout_graceful_shutdown"] == entry.GRACEFUL_SHUTDOWN_SECONDS == 45
    assert app.router.lifespan_context.__qualname__ == "wrap_lifespan.<locals>.lifespan"
