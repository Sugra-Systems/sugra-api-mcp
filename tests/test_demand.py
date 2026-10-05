"""The demand count: initialize and tools/list at the gate, by caller class and status."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

import sugra_api_mcp.tools  # noqa: F401  (registers the tools on server.mcp)
from sugra_api_mcp import demand, gate, observability, server
from sugra_api_mcp.auth import AuthError, AuthMiddleware, ResolvedAuth
from tests.test_request_credentials import HEADERS, INITIALIZE, _authenticator

_HEADER = re.compile(
    r"sdemand1 side=(?P<side>\S+) requests=(?P<requests>\d+) "
    r"lines=(?P<lines>\d+) omitted=(?P<omitted>\d+) failed=(?P<failed>\d+) lost=(?P<lost>\d+)"
)
_OAUTH = {"authorization": "Bearer jwt-demand"}
_REVOKED = {"authorization": "Bearer revoked"}
# A scanner that names itself after its vendor in neither pattern list.
_SCANNER = {
    "host": "app.sugra.ai",
    "user-agent": "openai-mcp/1.0",
    "origin": "https://chatgpt.com",
}
_SCANNER_INITIALIZE = {
    **INITIALIZE,
    "params": {**INITIALIZE["params"], "clientInfo": {"name": "openai-mcp", "version": "1.0.0"}},
}


def _rid(number: int) -> str:
    """A request id as nginx writes it: 32 lowercase hex."""
    return f"{number:02x}" + "cd" * 15


def _message(number: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": number, "method": method, "params": params}


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def written():
    """Every message the demand logger writes during the test."""
    handler = _Collect()
    demand.logger.addHandler(handler)
    try:
        yield handler.messages
    finally:
        demand.logger.removeHandler(handler)


def _record(counter: demand.DemandCounter, written: list[str]) -> tuple[dict[tuple[str, ...], int], int]:
    """Write the counter and read its record back as ({key: count}, failed)."""
    written.clear()
    counter.write()
    counts: dict[tuple[str, ...], int] = {}
    failed = 0
    for message in written:
        header, *lines = message.split("\n")
        match = _HEADER.fullmatch(header)
        assert match is not None, header
        assert int(match["lines"]) == len(lines)
        for line in lines:
            *key, count = line.split(" ")
            assert len(key) == 6, line
            counts[tuple(key)] = int(count)
        failed += int(match["failed"])
        assert int(match["requests"]) == sum(counts.values()) + int(match["omitted"]) + failed
    return counts, failed


def _counts(counter: demand.DemandCounter, written: list[str]) -> dict[tuple[str, ...], int]:
    """Write the counter and read its record back as {key: count}; no request failed."""
    counts, failed = _record(counter, written)
    assert failed == 0
    return counts


@contextlib.asynccontextmanager
async def _served(
    monkeypatch, counter: demand.DemandCounter | None, *, gated: bool = True
) -> AsyncIterator[httpx.AsyncClient]:
    """The real app, auth and the SDK session manager, behind the gate when gated."""
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    # The hosted server admits its public host and the connector origins
    # (SUGRA_MCP_ALLOWED_HOSTS); the test admits every host, so the classes
    # under test can be the hosted ones.
    monkeypatch.setattr(server.mcp.settings, "transport_security", None)
    app = server.mcp.streamable_http_app()
    authenticator = _authenticator()

    async def resolve(token: str) -> ResolvedAuth:
        if token == "revoked":
            raise AuthError("token revoked")
        return ResolvedAuth(
            api_key="sugra_dummy",
            user_id=42,
            access_token_id=token.strip(),
            method="oauth",
            platform="openai",
        )

    monkeypatch.setattr(authenticator, "resolve", resolve)
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    if gated:
        app.add_middleware(
            gate.GateMiddleware,
            max_body_bytes=server.mcp.settings.max_request_body_size,
            summary=gate.GateSummary(),
            demand_counter=counter,
        )
    try:
        async with server.mcp.session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
                yield client
    finally:
        await authenticator.aclose()


async def _open(client: httpx.AsyncClient, number: int) -> dict[str, str]:
    opened = await client.post(
        "/mcp",
        json=_SCANNER_INITIALIZE,
        headers={**HEADERS, **_SCANNER, "x-request-id": _rid(number)},
    )
    assert opened.status_code == 200
    return {"mcp-session-id": opened.headers["mcp-session-id"]}


# ---- What is counted, and as what ----


async def test_initialize_and_tools_list_are_counted_by_their_real_classes(monkeypatch, written) -> None:
    counter = demand.DemandCounter()
    async with _served(monkeypatch, counter) as client:
        session = await _open(client, 1)
        headers = {**HEADERS, **_SCANNER, **session}
        notified = await client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            headers={**headers, "x-request-id": _rid(2)},
        )
        listed = await client.post(
            "/mcp", json=_message(3, "tools/list", {}), headers={**headers, "x-request-id": _rid(3)}
        )
        listed_again = await client.post(
            "/mcp", json=_message(4, "tools/list", {}), headers={**headers, "x-request-id": _rid(4)}
        )
    assert (notified.status_code, listed.status_code, listed_again.status_code) == (202, 200, 200)
    # The User-Agent names no class (other); the clientInfo name does (chatgpt).
    assert _counts(counter, written) == {
        ("initialize", "200", "app.sugra.ai", "other", "openai", "chatgpt"): 1,
        ("tools/list", "200", "app.sugra.ai", "other", "openai", "-"): 2,
    }


async def test_a_tool_call_is_not_counted_as_a_list(monkeypatch, written) -> None:
    counter = demand.DemandCounter()
    async with _served(monkeypatch, counter) as client:
        session = await _open(client, 1)
        called = await client.post(
            "/mcp",
            json=_message(2, "tools/call", {"name": "list_toolsets", "arguments": {}}),
            headers={**HEADERS, **_SCANNER, **_OAUTH, **session, "x-request-id": _rid(2)},
        )
        pinged = await client.post(
            "/mcp",
            json=_message(3, "ping", {}),
            headers={**HEADERS, **_SCANNER, **session, "x-request-id": _rid(3)},
        )
    assert (called.status_code, pinged.status_code) == (200, 200)
    counts = _counts(counter, written)
    assert [key[0] for key in counts] == ["initialize"]


async def test_a_refused_request_is_counted_with_its_status(monkeypatch, written) -> None:
    counter = demand.DemandCounter()
    tools_list = _message(2, "tools/list", {})
    async with _served(monkeypatch, counter) as client:
        session = await _open(client, 1)
        # A dead token: the auth layer answers before it reads the body.
        revoked = await client.post(
            "/mcp", json=tools_list, headers={**HEADERS, **_SCANNER, **_REVOKED, **session, "x-request-id": _rid(2)}
        )
        # A session the server does not know.
        unknown_session = await client.post(
            "/mcp",
            json=tools_list,
            headers={**HEADERS, **_SCANNER, **_OAUTH, "mcp-session-id": "f" * 32, "x-request-id": _rid(3)},
        )
        # No session at all: the SDK reads the body and then refuses it.
        no_session = await client.post(
            "/mcp", json=tools_list, headers={**HEADERS, **_SCANNER, **_OAUTH, "x-request-id": _rid(4)}
        )
        wrong_accept = await client.post(
            "/mcp",
            json=tools_list,
            headers={**HEADERS, **_SCANNER, **session, "accept": "text/html", "x-request-id": _rid(5)},
        )
    statuses = [r.status_code for r in (revoked, unknown_session, no_session, wrong_accept)]
    assert statuses == [401, 404, 400, 406]
    counts = _counts(counter, written)
    caller = ("app.sugra.ai", "other", "openai")
    assert counts == {
        ("initialize", "200", *caller, "chatgpt"): 1,
        ("unread", "401", *caller, "-"): 1,
        ("tools/list", "404", *caller, "-"): 1,
        ("tools/list", "400", *caller, "-"): 1,
        ("tools/list", "406", *caller, "-"): 1,
    }


async def test_only_a_tracked_post_to_mcp_is_counted(written) -> None:
    counter = demand.DemandCounter()

    async def answer(request: Request) -> JSONResponse:
        await request.body()
        return JSONResponse({})

    app = Starlette(routes=[Route("/mcp", answer, methods=["GET", "POST"]), Route("/token", answer, methods=["POST"])])
    app.add_middleware(gate.GateMiddleware, max_body_bytes=1024, summary=gate.GateSummary(), demand_counter=counter)
    tools_list = _message(1, "tools/list", {})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002") as client:
        await client.post("/mcp", json=tools_list)
        await client.post("/mcp", json=tools_list, headers={"x-request-id": "not-a-request-id"})
        await client.get("/mcp", headers={"x-request-id": _rid(1)})
        await client.post("/token", json=tools_list, headers={"x-request-id": _rid(2)})
        await client.post("/mcp", json=[tools_list], headers={"x-request-id": _rid(3)})
        await client.post("/mcp", json=tools_list, headers={"x-request-id": _rid(4)})
    assert _counts(counter, written) == {("tools/list", "200", "loopback", "python", "none", "-"): 1}


async def test_a_line_holds_classes_never_header_or_client_text(written) -> None:
    counter = demand.DemandCounter()

    async def answer(request: Request) -> JSONResponse:
        await request.body()
        return JSONResponse({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": "secret_tool_name"}]}})

    app = Starlette(routes=[Route("/mcp", answer, methods=["POST"])])
    app.add_middleware(gate.GateMiddleware, max_body_bytes=4096, summary=gate.GateSummary(), demand_counter=counter)
    marker = "user-marker-1234"
    initialize = {
        **INITIALIZE,
        "params": {**INITIALIZE["params"], "clientInfo": {"name": f"tool {marker}", "version": "1"}},
    }
    headers = {"x-request-id": _rid(1), "host": f"{marker}.example", "user-agent": marker, "origin": f"https://{marker}"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002") as client:
        await client.post("/mcp", json=initialize, headers=headers)
        await client.post("/mcp", json=_message(2, "tools/list", {}), headers=headers)
        await client.post(
            "/mcp", json={**INITIALIZE, "params": {"clientInfo": {"version": "1"}}}, headers=headers
        )
    counts = _counts(counter, written)
    assert counts == {
        ("initialize", "200", "other", "other", "other", "other"): 1,
        ("initialize", "200", "other", "other", "other", "none"): 1,
        ("tools/list", "200", "other", "other", "other", "-"): 1,
    }
    assert marker not in "\n".join(written)
    assert "secret_tool_name" not in "\n".join(written)


# ---- The tool list ----


async def test_the_tools_list_answer_is_byte_identical_behind_the_gate(monkeypatch, written) -> None:
    answers: list[bytes] = []
    for gated in (False, True):
        async with _served(monkeypatch, demand.DemandCounter(), gated=gated) as client:
            session = await _open(client, 1)
            listed = await client.post(
                "/mcp",
                json=_message(2, "tools/list", {}),
                headers={**HEADERS, **_SCANNER, **session, "x-request-id": _rid(2)},
            )
            assert listed.status_code == 200
            answers.append(listed.content)
    assert answers[0] == answers[1]
    assert b'"tools"' in answers[0]


async def test_a_long_tools_list_answer_passes_unchanged_and_is_counted(written) -> None:
    counter = demand.DemandCounter()
    names = [f"tool_{n:03d}" for n in range(40)]
    listed_tools = ",".join(
        f'{{"name":"{name}","description":"{"d" * 5000}","inputSchema":{{"type":"object"}}}}' for name in names
    )
    whole = f'{{"jsonrpc":"2.0","id":1,"result":{{"tools":[{listed_tools}]}}}}'.encode()
    # Well past the 64 KiB read for an error answer, in many chunks.
    assert len(whole) > 3 * gate._ANSWER_SCAN_BYTES
    chunks = [whole[start : start + 7000] for start in range(0, len(whole), 7000)]

    async def answer(request: Request) -> Response:
        await request.body()

        async def stream() -> AsyncIterator[bytes]:
            for chunk in chunks:
                yield chunk

        return StreamingResponse(stream(), media_type="application/json")

    app = Starlette(routes=[Route("/mcp", answer, methods=["POST"])])
    app.add_middleware(gate.GateMiddleware, max_body_bytes=4096, summary=gate.GateSummary(), demand_counter=counter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002") as client:
        listed = await client.post("/mcp", json=_message(1, "tools/list", {}), headers={"x-request-id": _rid(1)})
    assert listed.content == whole
    assert _counts(counter, written) == {("tools/list", "200", "loopback", "python", "none", "-"): 1}


def _list_answer(names: list[str]) -> bytes:
    listed = ",".join(f'{{"name":"{name}","inputSchema":{{"type":"object"}}}}' for name in names)
    return f'{{"jsonrpc":"2.0","id":1,"result":{{"tools":[{listed}]}}}}'.encode()


@pytest.mark.parametrize("read_to_end", [True, False])
async def test_a_body_over_the_limit_is_never_counted(written, read_to_end: bool) -> None:
    counter = demand.DemandCounter()
    tools_list = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"pad":"' + b"x" * 200 + b'"}}'

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        # The server reads past its limit, or answers 413 at the first chunk over it.
        while (await receive()).get("more_body", False) and read_to_end:
            pass
        await send({"type": "http.response.start", "status": 413 if not read_to_end else 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    chunks = [tools_list[:120], tools_list[120:]]

    async def receive() -> dict[str, Any]:
        chunk = chunks.pop(0)
        return {"type": "http.request", "body": chunk, "more_body": bool(chunks)}

    async def send(message: dict[str, Any]) -> None:
        return None

    middleware = gate.GateMiddleware(app, max_body_bytes=100, summary=gate.GateSummary(), demand_counter=counter)
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": [(b"x-request-id", _rid(1).encode())]}
    await middleware(scope, receive, send)
    assert _counts(counter, written) == {}


async def _answered_unread(written: list[str], length_headers: list[bytes]) -> dict[tuple[str, ...], int]:
    """A POST to /mcp the app refuses with 401 before reading any of its body."""
    counter = demand.DemandCounter()

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 401, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> dict[str, Any]:
        raise AssertionError("the body is never read")

    async def send(message: dict[str, Any]) -> None:
        return None

    middleware = gate.GateMiddleware(app, max_body_bytes=100, summary=gate.GateSummary(), demand_counter=counter)
    headers = [(b"x-request-id", _rid(1).encode()), *((b"content-length", value) for value in length_headers)]
    await middleware({"type": "http", "method": "POST", "path": "/mcp", "headers": headers}, receive, send)
    return _counts(counter, written)


@pytest.mark.parametrize("length", [b"101", b"5000", b" 5000 "])
async def test_an_unread_body_whose_valid_length_is_over_the_limit_is_not_counted(written, length: bytes) -> None:
    assert await _answered_unread(written, [length]) == {}


@pytest.mark.parametrize(
    "lengths",
    [
        [], [b"100"], [b"0"], [b"5000", b"5000"], [b"5e3"], [b"-5000"], [b""], [b"9" * 20],
        # 5000 in ARABIC-INDIC digits, UTF-8: digits to str.isdigit, not to bytes.isdigit.
        [b"\xd9\xa5\xd9\xa0\xd9\xa0\xd9\xa0"],
    ],
)
async def test_an_unread_body_of_unknown_or_allowed_size_is_counted_as_unread(written, lengths: list[bytes]) -> None:
    # Chunked (no Content-Length), within the limit, or a Content-Length that is
    # not one header of ASCII digits: the size is not known to be over the limit.
    assert await _answered_unread(written, lengths) == {("unread", "401", "none", "other", "none", "-"): 1}


# ---- The record ----


def test_each_write_carries_the_counts_since_the_last_one(written) -> None:
    counter = demand.DemandCounter()
    key = ("tools/list", "200", "app.sugra.ai", "other", "openai", "-")
    counter.add(key)
    counter.add(key)
    assert _counts(counter, written) == {key: 2}
    assert _counts(counter, written) == {}
    assert written == []
    counter.add(key)
    assert _counts(counter, written) == {key: 1}


def test_a_record_holds_at_most_max_lines_largest_first(monkeypatch, written) -> None:
    monkeypatch.setattr(demand, "MAX_LINES", 2)
    counter = demand.DemandCounter()
    keys = [("tools/list", str(status), "none", "other", "none", "-") for status in (400, 401, 404)]
    for times, key in zip((3, 1, 2), keys, strict=True):
        for _ in range(times):
            counter.add(key)
    written.clear()
    counter.write()
    header, *lines = written[0].split("\n")
    assert _HEADER.fullmatch(header)["omitted"] == "1"
    assert _HEADER.fullmatch(header)["requests"] == "6"
    assert lines == [" ".join((*keys[0], "3")), " ".join((*keys[2], "2"))]


@pytest.mark.parametrize("side_chars", [128, 5000])
def test_a_record_stays_inside_the_app_insights_bound_whatever_the_fields_hold(
    monkeypatch, written, side_chars: int
) -> None:
    # An absurd side, absurd fields and counts of 10**30: the header grows with
    # the count digits, and the lines left must still fit beside it.
    monkeypatch.setattr(observability, "process_side", lambda: "a" * side_chars)
    counter = demand.DemandCounter()
    for number in range(demand.MAX_LINES + 50):
        key = tuple(f"{number:04d}{field}" + "x" * 300 for field in range(6))
        counter._counts[key] = 10**30 + number
    counter._failed = 10**30
    counter._lost = 10**30
    written.clear()
    counter.write()
    (message,) = written
    assert len(message) <= demand.RECORD_MAX_CHARS < 32_768
    header, *lines = message.split("\n")
    match = _HEADER.fullmatch(header)
    assert match is not None
    assert match["side"] == "a" * min(side_chars, demand.SIDE_MAX)
    assert 0 < len(lines) == int(match["lines"]) < demand.MAX_LINES
    for line in lines:
        *fields, _count = line.split(" ")
        assert len(fields) == 6
        assert all(len(field) == demand.FIELD_MAX for field in fields)
    shown = sum(int(line.rsplit(" ", 1)[1]) for line in lines)
    assert int(match["requests"]) == shown + int(match["omitted"]) + int(match["failed"])
    assert int(match["failed"]) == int(match["lost"]) == 10**30
    # The room is measured, not a fixed reserve: at most a line or two short of the bound.
    assert len(message) + 2 * (len(line) + 1) > demand.RECORD_MAX_CHARS


class _Raising(logging.Handler):
    """A handler that raises on every record it is handed, as a broken exporter can."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.calls = 0

    def emit(self, record: logging.LogRecord) -> None:
        self.calls += 1
        raise OSError("exporter down")


@contextlib.contextmanager
def _broken_demand_logger() -> Any:
    handler = _Raising()
    demand.logger.addHandler(handler)
    try:
        yield handler
    finally:
        demand.logger.removeHandler(handler)


def _lost(written: list[str]) -> list[int]:
    """The lost total of every record written."""
    return [int(_HEADER.fullmatch(message.split("\n")[0])["lost"]) for message in written]


def test_a_write_the_logger_fails_on_is_dropped_and_reported_as_lost(written) -> None:
    counter = demand.DemandCounter()
    key = ("tools/list", "200", "app.sugra.ai", "other", "openai", "-")
    counter.add(key)
    counter.add(key)
    counter.add_failure()
    with _broken_demand_logger():
        with pytest.raises(OSError):
            counter.write()
        # Counted while the write was failing: the next interval's own.
        counter.add(key)
    # Never the dropped two again: the next record holds the new request only.
    assert _record(counter, written) == ({key: 1}, 0)
    assert _lost(written) == [3]
    # Nothing new and lost already reported: nothing is written.
    assert _record(counter, written) == ({}, 0)
    assert written == []


def test_a_lost_interval_with_no_new_request_is_still_reported(written) -> None:
    counter = demand.DemandCounter()
    counter.add(("initialize", "200", "none", "other", "none", "none"))
    with _broken_demand_logger(), pytest.raises(OSError):
        counter.write()
    assert _record(counter, written) == ({}, 0)
    assert _lost(written) == [1]
    assert _record(counter, written) == ({}, 0)
    assert written == []
    # Lost is a total since the process started, carried on every later record.
    counter.add(("initialize", "200", "none", "other", "none", "none"))
    _record(counter, written)
    assert _lost(written) == [1]


def test_writing_the_counts_never_raises_even_when_the_failure_report_fails(written) -> None:
    counter = demand.DemandCounter()
    key = ("initialize", "200", "none", "other", "none", "none")
    counter.add(key)
    with _broken_demand_logger() as broken:
        demand.write_logged(counter)
    # The record and the warning about it both reached the broken handler.
    assert broken.calls == 2
    # Its request was dropped and reported, never written twice.
    assert _counts(counter, written) == {}
    assert _lost(written) == [1]


@pytest.mark.parametrize("side", ["unknown", "app-vm"])
def test_the_header_names_the_side(monkeypatch, written, side: str) -> None:
    monkeypatch.setattr(observability, "process_side", lambda: side)
    counter = demand.DemandCounter()
    counter.add(("initialize", "200", "none", "other", "none", "none"))
    written.clear()
    counter.write()
    assert _HEADER.fullmatch(written[0].split("\n")[0])["side"] == side


def test_the_demand_logger_writes_info_on_its_own() -> None:
    assert demand.logger.name == "sugra_mcp.demand"
    assert demand.logger.level == logging.INFO


@pytest.mark.parametrize(
    ("status", "text"),
    [(200, "200"), (404, "404"), (None, "none"), (True, "none"), (99, "none"), ("200", "none")],
)
def test_the_status_is_digits_or_none(status: object, text: str) -> None:
    assert demand.status_of(status) == text


# ---- When it is written ----


@contextlib.asynccontextmanager
async def _inner(app: object) -> AsyncIterator[None]:
    yield None


@pytest.fixture
def flushes(monkeypatch) -> list[float]:
    calls: list[float] = []
    monkeypatch.setattr(observability, "flush_telemetry", lambda timeout_s: calls.append(timeout_s) or True)
    return calls


async def test_the_counts_are_written_every_interval_and_once_more_on_exit(written, flushes) -> None:
    counter = demand.DemandCounter()
    key = ("tools/list", "200", "none", "other", "none", "-")
    periodic = gate.wrap_lifespan(
        _inner, gate.GateSummary(), interval=0.02, drain_seconds=0, flush_timeout=1, demand_counter=counter
    )
    async with periodic(object()):
        counter.add(key)
        deadline = time.monotonic() + 5
        while not written and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert written == [
            f"sdemand1 side={observability.process_side()} requests=1 lines=1 omitted=0 failed=0 lost=0\n"
            f"{' '.join(key)} 1"
        ]
    written.clear()
    on_exit_only = gate.wrap_lifespan(
        _inner, gate.GateSummary(), interval=3600, drain_seconds=0, flush_timeout=1, demand_counter=counter
    )
    async with on_exit_only(object()):
        counter.add(key)
        counter.add(key)
        assert written == []
    assert written[0].split("\n")[1:] == [f"{' '.join(key)} 2"]
    assert flushes == [1, 1]


async def test_the_last_count_follows_the_last_summary_and_precedes_the_exit_work(monkeypatch, flushes) -> None:
    events: list[str] = []
    summary = gate.GateSummary()
    counter = demand.DemandCounter()
    monkeypatch.setattr(summary, "write", lambda: events.append("summary"))
    monkeypatch.setattr(counter, "write", lambda: events.append("demand"))

    async def closing() -> None:
        events.append("close")

    lifespan = gate.wrap_lifespan(
        _inner, summary, interval=3600, drain_seconds=0, flush_timeout=1, on_exit=(closing,), demand_counter=counter
    )
    monkeypatch.setattr(observability, "flush_telemetry", lambda timeout_s: events.append("flush") or True)
    async with lifespan(object()):
        events.append("serving")
    assert events == ["serving", "summary", "demand", "close", "demand", "flush"]


async def test_a_request_that_finishes_during_the_closers_is_written_before_the_flush(monkeypatch, written) -> None:
    counter = demand.DemandCounter()
    key = ("tools/list", "200", "none", "other", "none", "-")
    flushed_after: list[list[str]] = []

    def flush(timeout_s: float) -> bool:
        flushed_after.append(list(written))
        return True

    monkeypatch.setattr(observability, "flush_telemetry", flush)

    async def closing() -> None:
        # A request still open past the drain, answered while the clients close.
        counter.add(key)

    lifespan = gate.wrap_lifespan(
        _inner, gate.GateSummary(), interval=3600, drain_seconds=0, flush_timeout=1, on_exit=(closing,),
        demand_counter=counter,
    )
    async with lifespan(object()):
        pass
    (seen,) = flushed_after
    assert [message.split("\n")[1:] for message in seen] == [[f"{' '.join(key)} 1"]]


async def test_the_periodic_writer_survives_a_logger_that_always_raises(monkeypatch, written, flushes) -> None:
    summary = gate.GateSummary()
    summaries: list[int] = []
    monkeypatch.setattr(summary, "write", lambda: summaries.append(1))
    counter = demand.DemandCounter()
    key = ("tools/list", "200", "none", "other", "none", "-")
    counter.add(key)
    lifespan = gate.wrap_lifespan(
        _inner, summary, interval=0.02, drain_seconds=0, flush_timeout=1, demand_counter=counter
    )
    with _broken_demand_logger() as broken:
        async with lifespan(object()):
            deadline = time.monotonic() + 5
            while len(summaries) < 3 and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
    # Every interval ran its summary after the failing count before it.
    assert len(summaries) >= 4
    assert broken.calls >= 6
    assert flushes == [1]
    # The count was dropped once, never retried, and the next record reports it.
    assert _counts(counter, written) == {}
    assert _lost(written) == [1]


async def test_a_failed_count_is_logged_and_the_summary_still_runs(monkeypatch, caplog, flushes) -> None:
    summary = gate.GateSummary()
    counter = demand.DemandCounter()
    summaries: list[int] = []
    monkeypatch.setattr(summary, "write", lambda: summaries.append(1))

    def fail() -> None:
        raise OSError("handler failed for user@example.com")

    monkeypatch.setattr(counter, "write", fail)
    lifespan = gate.wrap_lifespan(
        _inner, summary, interval=0.02, drain_seconds=0, flush_timeout=1, demand_counter=counter
    )
    async with lifespan(object()):
        deadline = time.monotonic() + 5
        while len(summaries) < 2 and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
    assert len(summaries) >= 3
    assert "Demand count failed (OSError)." in caplog.messages
    assert "user@example.com" not in caplog.text
    assert flushes == [1]


class _Interrupting(logging.Handler):
    """A handler that raises a BaseException that is no Exception on every record."""

    def __init__(self, raised: type[BaseException]) -> None:
        super().__init__(logging.DEBUG)
        self.raised = raised

    def emit(self, record: logging.LogRecord) -> None:
        raise self.raised()


def _exit_with_interrupt(monkeypatch, raised: type[BaseException]) -> list[str]:
    """Run a lifespan whose exit-time demand writes raise `raised`; the exit steps it ran."""
    events: list[str] = []
    counter = demand.DemandCounter()
    counter.add(("initialize", "200", "none", "other", "none", "none"))
    monkeypatch.setattr(observability, "flush_telemetry", lambda timeout_s: events.append("flush") or True)

    async def closing() -> None:
        events.append("close")

    lifespan = gate.wrap_lifespan(
        _inner, gate.GateSummary(), interval=3600, drain_seconds=0, flush_timeout=1, on_exit=(closing,),
        demand_counter=counter,
    )

    async def run() -> None:
        handler = _Interrupting(raised)
        demand.logger.addHandler(handler)
        try:
            async with lifespan(object()):
                events.append("serving")
        finally:
            demand.logger.removeHandler(handler)

    try:
        asyncio.run(run())
    except BaseException as e:
        events.append(f"raised {type(e).__name__}")
    return events


@pytest.mark.parametrize("raised", [SystemExit, KeyboardInterrupt])
def test_an_interrupt_from_an_exit_write_is_raised_after_the_closers_and_the_flush(
    monkeypatch, raised: type[BaseException]
) -> None:
    assert _exit_with_interrupt(monkeypatch, raised) == ["serving", "close", "flush", f"raised {raised.__name__}"]


def test_any_other_base_exception_from_an_exit_write_is_dropped(monkeypatch) -> None:
    class _Odd(BaseException):
        pass

    assert _exit_with_interrupt(monkeypatch, _Odd) == ["serving", "close", "flush"]


def test_the_exit_write_hands_an_interrupt_back_and_drops_the_rest(monkeypatch) -> None:
    counter = demand.DemandCounter()
    interrupt = KeyboardInterrupt()

    def interrupted() -> None:
        raise interrupt

    monkeypatch.setattr(counter, "write", interrupted)
    assert gate._write_at_exit(counter) is interrupt

    def cancelled() -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(counter, "write", cancelled)
    assert gate._write_at_exit(counter) is None


async def test_a_failing_count_never_fails_the_request(monkeypatch, written) -> None:
    counter = demand.DemandCounter()

    def fail(key: demand.Key) -> None:
        raise RuntimeError("count failed")

    monkeypatch.setattr(counter, "add", fail)

    async def answer(request: Request) -> JSONResponse:
        await request.body()
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", answer, methods=["POST"])])
    app.add_middleware(gate.GateMiddleware, max_body_bytes=1024, summary=gate.GateSummary(), demand_counter=counter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002") as client:
        answered = await client.post("/mcp", json=_message(1, "tools/list", {}), headers={"x-request-id": _rid(1)})
    assert answered.status_code == 200
    assert answered.json() == {"ok": True}
    assert _record(counter, written) == ({}, 1)


@pytest.mark.parametrize("step", ["read", "sent"])
async def test_a_raising_capture_leaves_the_answer_intact_and_counts_a_failure(monkeypatch, written, step: str) -> None:
    counter = demand.DemandCounter()

    def fail(self: object, value: object) -> None:
        raise MemoryError

    monkeypatch.setattr(gate._DemandCapture, step, fail)
    whole = _list_answer([f"tool_{n:03d}" for n in range(300)])
    chunks = [whole[start : start + 1000] for start in range(0, len(whole), 1000)]

    async def respond(request: Request) -> Response:
        await request.body()

        async def stream() -> AsyncIterator[bytes]:
            for chunk in chunks:
                yield chunk

        return StreamingResponse(stream(), media_type="application/json", headers={"x-kept": "1"})

    app = Starlette(routes=[Route("/mcp", respond, methods=["POST"])])
    app.add_middleware(gate.GateMiddleware, max_body_bytes=4096, summary=gate.GateSummary(), demand_counter=counter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002") as client:
        listed = await client.post("/mcp", json=_message(1, "tools/list", {}), headers={"x-request-id": _rid(1)})
    assert (listed.status_code, listed.headers["x-kept"], listed.content) == (200, "1", whole)
    assert _record(counter, written) == ({}, 1)


@pytest.mark.parametrize(
    ("failing", "status"), [(None, "200"), ("http.response.start", "none"), ("http.response.body", "200")]
)
async def test_a_failed_send_is_not_counted_as_sent(written, failing: str | None, status: str) -> None:
    counter = demand.DemandCounter()

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        await receive()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": _list_answer(["a"])})

    middleware = gate.GateMiddleware(app, max_body_bytes=4096, summary=gate.GateSummary(), demand_counter=counter)
    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": [(b"x-request-id", _rid(1).encode())]}
    json_bytes = b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": json_bytes, "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        # The client went away: this send never reached it.
        if message["type"] == failing:
            raise OSError("client disconnected")

    if failing is None:
        await middleware(scope, receive, send)
    else:
        with pytest.raises(OSError):
            await middleware(scope, receive, send)
    assert _counts(counter, written) == {("tools/list", status, "none", "other", "none", "-"): 1}


def test_the_classes_demand_takes_from_observability_are_pinned(monkeypatch) -> None:
    """demand.py reads private observability members; a rename or a changed class fails here, not silently."""
    for name in ("_host_class", "_text_class", "_origin_class", "_UA_PATTERNS", "_CLIENT_NAME_PATTERNS", "process_side"):
        assert hasattr(observability, name), name
    assert demand.caller_classes("app.sugra.ai", "Claude-User", "https://claude.ai") == (
        "app.sugra.ai", "claude", "anthropic",
    )
    assert demand.caller_classes("mcp.sugra.ai:443", "ChatGPT-User/1.0", "https://chatgpt.com") == (
        "mcp.sugra.ai", "chatgpt", "openai",
    )
    assert demand.caller_classes("localhost:8001", "python-httpx/0.28", None) == ("loopback", "python", "none")
    assert demand.caller_classes("x.example", "Mozilla/5.0", "https://cursor.sh") == ("other", "browser", "cursor")
    assert demand.caller_classes(None, None, "https://evil.example") == ("none", "other", "other")

    def client_of(name: object) -> str:
        return demand.request_facts({"method": "initialize", "params": {"clientInfo": {"name": name}}})[1]

    assert [client_of(name) for name in ("Claude", "openai-mcp", "Cursor", "grok-cli", "unknown", "")] == [
        "claude", "chatgpt", "cursor", "grok", "other", "none",
    ]
    monkeypatch.delenv("CONTAINER_APP_REVISION", raising=False)
    assert observability.process_side() == "vm"
    monkeypatch.setenv("CONTAINER_APP_REVISION", "ca-sugra-mcp--r1")
    assert observability.process_side() == "ca-sugra-mcp--r1"


def test_the_host_field_holds_one_of_two_names_or_a_fixed_class() -> None:
    assert demand.caller_classes("APP.sugra.ai:443", None, None)[0] == "app.sugra.ai"
    assert demand.caller_classes("mcp.sugra.ai", None, None)[0] == "mcp.sugra.ai"
    for host in ("evil.app.sugra.ai", "app.sugra.ai.example", "sugra.ai", "app.sugra.ai:x"):
        assert demand.caller_classes(host, None, None)[0] == "other"
    assert demand.caller_classes("127.0.0.1:8002", None, None)[0] == "loopback"
    assert demand.caller_classes(None, None, None)[0] == "none"


def test_the_hosted_server_counts_through_the_gate_defaults(monkeypatch) -> None:
    """The entry point passes no counter: the middleware and the lifespan share the default one."""
    middleware = gate.GateMiddleware(lambda scope, receive, send: None, max_body_bytes=1)
    assert middleware.demand is demand.default_counter
