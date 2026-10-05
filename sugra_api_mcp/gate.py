"""Gate summary: one line per answered POST, logged every minute and at shutdown.

nginx gives every request it proxies a 32 lowercase hex X-Request-Id and logs
it with the status it returned. This module keeps one line for each POST that
carried such an id and was answered with a 2xx, and every
SUMMARY_INTERVAL_SECONDS, and once more at shutdown, logs the lines gathered
since the previous summary on the sugra_mcp.gate logger, so the two logs can
be matched request by request:

    sgate1 side=<side> boot=<8 hex> seq=<n> part=<i>/<k> lines=<m> dropped=<d>
    <rid> -                    a 2xx answer to a POST that carried no tool call
    <rid> E                    a JSON-RPC error answering a message that was not a tool call
    <rid> T[F][A] <ms> <api>   a tool call: F when it failed, A when the Sugra API
                               refused the caller's credential (401 or 403)

side is the deployment that served the requests (observability.process_side).
boot is drawn at random once per process and seq numbers its summaries from
1, so a summary that never arrived shows as a gap. A summary is split into
records of at most LINES_PER_RECORD lines, its header among them, part i of
k; lines counts the request lines under that header. At most MAX_BUFFERED_LINES
lines wait for the next summary: past that a line is only counted, and
dropped, repeated in every part, is that count. A summary with no lines is
the header alone.

On a T line ms is the duration of the longest tool call the request carried
and api the most Sugra API requests one of them made. F is every outcome but
success and a credential refusal: a call refused before its tool ran, a call
still running when the answer ended, and a tools/call the SDK refused as
malformed before any tool saw it, which reads 0 0.

A line holds the request id, a class and two integers. Never a tool name, an
argument, a header or a payload.

The same requests feed the demand count (demand.py), written beside each
summary on its own logger.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from typing import Any

from mcp.types import CallToolResult
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import demand, observability

logger = logging.getLogger("sugra_mcp.gate")
# Set here, not inherited: on the hosted server the Azure Monitor handler is
# on the root logger before FastMCP starts, so FastMCP's basicConfig does
# nothing and the root logger stays at WARNING, which would drop every summary.
logger.setLevel(logging.INFO)

SUMMARY_INTERVAL_SECONDS = 60.0
# App Insights keeps at most 32,768 characters of a log message; 500 lines of
# about 50 characters each stay well inside it. The header is one of a
# record's lines, so a record holds at most LINES_PER_RECORD - 1 request lines.
LINES_PER_RECORD = 500
MAX_BUFFERED_LINES = 20_000
# How long the last summary waits at shutdown for requests still finishing,
# and how long the telemetry flush after it may take.
DRAIN_SECONDS = 1.0
FLUSH_TIMEOUT_SECONDS = 10.0

# The request scope state key under which GateMiddleware stores the request's
# GateRecord, read at dispatch the way server.py reads the request's key.
GATE_STATE = "sugra_gate"

# How a tool call ended, as call_tool reports it to the request's record.
SUCCEEDED = "succeeded"
FAILED = "failed"
AUTH_REFUSED = "auth_refused"

# The most of an answer that is read for a JSON-RPC error; an error answer is
# far shorter.
_ANSWER_SCAN_BYTES = 64 * 1024
# The most of a tools/list answer that is read for its tool names (the demand
# count's digest); the served list is far shorter.
_TOOLS_LIST_SCAN_BYTES = 1024 * 1024
_MCP_PATH = "/mcp"
_SSE_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


class _OpenCall:
    """One tool call of a request, from call_started until call_finished."""

    __slots__ = ("dispatch", "started")

    def __init__(self, dispatch: observability.ToolDispatch) -> None:
        self.dispatch = dispatch
        self.started = time.perf_counter()


class GateRecord:
    """What one tracked POST carried, and how the tool calls it carried ended."""

    __slots__ = (
        "_open",
        "auth_refused",
        "calls",
        "carries_tool_call",
        "failed",
        "longest_ms",
        "most_api_requests",
    )

    def __init__(self) -> None:
        self.carries_tool_call = False
        self.calls = 0
        self.failed = False
        self.auth_refused = False
        self.longest_ms = 0
        self.most_api_requests = 0
        self._open: set[_OpenCall] = set()

    def call_started(self, dispatch: observability.ToolDispatch) -> _OpenCall:
        """Open a tool call whose API requests the dispatch counts; pass it to call_finished."""
        call = _OpenCall(dispatch)
        self._open.add(call)
        return call

    def call_finished(self, call: _OpenCall, outcome: str) -> None:
        self._open.discard(call)
        self.calls += 1
        self.longest_ms = max(self.longest_ms, _elapsed_ms(call.started))
        self.most_api_requests = max(self.most_api_requests, call.dispatch.api_requests)
        if outcome == AUTH_REFUSED:
            self.auth_refused = True
        elif outcome != SUCCEEDED:
            self.failed = True

    def is_tool_call(self) -> bool:
        return self.carries_tool_call or self.calls > 0 or bool(self._open)

    def tool_line(self, request_id: str) -> str:
        """The T line; a call still open counts as failed, timed up to now."""
        longest_ms = self.longest_ms
        most_api_requests = self.most_api_requests
        for call in tuple(self._open):
            longest_ms = max(longest_ms, _elapsed_ms(call.started))
            most_api_requests = max(most_api_requests, call.dispatch.api_requests)
        failed = self.failed or self.calls == 0 or bool(self._open)
        flags = ("F" if failed else "") + ("A" if self.auth_refused else "")
        return f"{request_id} T{flags} {longest_ms} {most_api_requests}"


def current_record() -> GateRecord | None:
    """The gate record of the request that carried the message being dispatched, or None.

    Read from the SDK request context of this one message, like the request's
    API key (server._dispatching_http_request). None outside HTTP and for a
    request GateMiddleware does not track.
    """
    from mcp.server.lowlevel.server import request_ctx

    try:
        scope = request_ctx.get().request.scope
        record = scope["state"][GATE_STATE]
    except Exception:
        return None
    return record if isinstance(record, GateRecord) else None


def is_auth_refusal(payload: object) -> bool:
    """True when a failed call's payload is the Sugra API refusing the caller's credential.

    That is an int status_code of 401 or 403, except agent_plane_unavailable:
    the agent plane refusing the server's own credential, which no caller can fix.
    """
    try:
        if not isinstance(payload, dict):
            return False
        status = payload.get("status_code")
        return (
            type(status) is int
            and status in (401, 403)
            and payload.get("error") != "agent_plane_unavailable"
        )
    except Exception:
        return False


def outcome_of(result: object) -> str:
    """How a tool call that returned ended: AUTH_REFUSED, FAILED or SUCCEEDED."""
    if isinstance(result, CallToolResult) and result.isError:
        return AUTH_REFUSED if is_auth_refusal(result.structuredContent) else FAILED
    return SUCCEEDED


class GateSummary:
    """The lines gathered since the last summary, and the numbering of the summaries."""

    def __init__(self) -> None:
        self.boot = os.urandom(4).hex()
        # Tracked requests not answered yet, for the drain at shutdown.
        self.open_requests = 0
        self._seq = 0
        self._lines: list[str] = []
        self._dropped = 0
        self._lock = threading.Lock()

    def add(self, line: str) -> None:
        with self._lock:
            if len(self._lines) < MAX_BUFFERED_LINES:
                self._lines.append(line)
            else:
                self._dropped += 1

    def write(self) -> None:
        """Log every line gathered since the last summary, and start the next one."""
        with self._lock:
            lines, self._lines = self._lines, []
            dropped, self._dropped = self._dropped, 0
            self._seq += 1
            seq = self._seq
        # The header is one of a record's lines.
        per_record = LINES_PER_RECORD - 1
        parts = [
            lines[start:start + per_record] for start in range(0, len(lines), per_record)
        ] or [[]]
        side = observability.process_side()
        for index, part in enumerate(parts, start=1):
            header = (
                f"sgate1 side={side} boot={self.boot} seq={seq} "
                f"part={index}/{len(parts)} lines={len(part)} dropped={dropped}"
            )
            logger.info("%s", "\n".join([header, *part]))


default_summary = GateSummary()


def _first_header(scope: Scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value.decode("latin-1")
    return None


def _parsed(body: bytes) -> object:
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        return None


def _is_tool_call(message: object) -> bool:
    return isinstance(message, dict) and message.get("method") == "tools/call"


def _sse_data(text: str) -> list[str]:
    """The data of each event of an SSE stream, multi-line data joined as the spec says."""
    events: list[str] = []
    data: list[str] = []
    for line in _SSE_LINE_BREAK.split(text):
        if not line:
            if data:
                events.append("\n".join(data))
                data = []
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        events.append("\n".join(data))
    return events


def _answers_with_error(body: bytes) -> bool:
    """True when an answer, plain JSON or SSE, holds a JSON-RPC error response."""
    text = body.decode("utf-8", errors="replace")
    candidates = [text] if text.lstrip().startswith("{") else _sse_data(text)
    for candidate in candidates:
        try:
            message = json.loads(candidate)
        except (ValueError, RecursionError):
            continue
        if isinstance(message, dict) and isinstance(message.get("error"), dict) and "method" not in message:
            return True
    return False


def _listed_tools_digest(body: bytes) -> str:
    """demand.tools_digest of the tools a tools/list answer, plain JSON or SSE, listed; - without one."""
    text = body.decode("utf-8", errors="replace")
    candidates = [text] if text.lstrip().startswith("{") else _sse_data(text)
    for candidate in candidates:
        try:
            message = json.loads(candidate)
        except (ValueError, RecursionError):
            continue
        result = message.get("result") if isinstance(message, dict) else None
        tools = result.get("tools") if isinstance(result, dict) else None
        if isinstance(tools, list):
            return demand.tools_digest(
                tool["name"] for tool in tools if isinstance(tool, dict) and type(tool.get("name")) is str
            )
    return demand.NOT_APPLICABLE


class GateMiddleware:
    """ASGI middleware that gives each tracked POST a gate record and its summary line.

    A request is tracked when it is a POST whose first X-Request-Id is a valid
    request id (observability.request_id_of); every other request passes
    through untouched. The request body, up to max_body_bytes (the server's
    own body limit, so nothing larger is ever answered with a 2xx), is read as
    it streams past to learn whether it carries a tools/call, and a tracked
    POST to /mcp is counted in the demand count whatever its status. Added
    after every other middleware, so it is the outermost layer and sees the
    status the client was sent.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        summary: GateSummary | None = None,
        demand_counter: demand.DemandCounter | None = None,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.summary = summary if summary is not None else default_summary
        self.demand = demand_counter if demand_counter is not None else demand.default_counter

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return
        request_id = observability.request_id_of(_first_header(scope, b"x-request-id"))
        if request_id is None:
            await self.app(scope, receive, send)
            return

        record = GateRecord()
        scope.setdefault("state", {})[GATE_STATE] = record
        body = bytearray()
        body_read = False
        body_too_large = False
        status: int | None = None
        answer = bytearray()
        answer_limit = _ANSWER_SCAN_BYTES
        counted = scope.get("path", "").rstrip("/") == _MCP_PATH
        demand_method: str | None = None
        demand_client = demand.NOT_APPLICABLE

        async def gate_receive() -> Message:
            nonlocal body_read, body_too_large, answer_limit, demand_method, demand_client
            message = await receive()
            if message["type"] == "http.request" and not body_read:
                chunk = message.get("body", b"")
                if not body_too_large and len(body) + len(chunk) <= self.max_body_bytes:
                    body.extend(chunk)
                else:
                    body_too_large = True
                    body.clear()
                if not message.get("more_body", False):
                    body_read = True
                    if not body_too_large:
                        request_message = _parsed(bytes(body))
                        record.carries_tool_call = _is_tool_call(request_message)
                        if counted:
                            demand_method, demand_client = demand.request_facts(request_message)
                            if demand_method == demand.TOOLS_LIST:
                                answer_limit = _TOOLS_LIST_SCAN_BYTES
                    body.clear()
            return message

        async def gate_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message.get("status")
            elif (
                message["type"] == "http.response.body"
                and not record.is_tool_call()
                and len(answer) < answer_limit
            ):
                answer.extend(message.get("body", b"")[: answer_limit - len(answer)])
            await send(message)

        self.summary.open_requests += 1
        try:
            await self.app(scope, gate_receive, gate_send)
        finally:
            self.summary.open_requests -= 1
            # isinstance, not type: the SDK sends some statuses as HTTPStatus members.
            if isinstance(status, int) and 200 <= status < 300:
                try:
                    if record.is_tool_call():
                        line = record.tool_line(request_id)
                    else:
                        line = f"{request_id} {'E' if _answers_with_error(bytes(answer)) else '-'}"
                    self.summary.add(line)
                except Exception as e:
                    logger.warning("Gate line failed (%s).", type(e).__name__)
            # A body never read means an answer before it: its method is unknown.
            method = demand_method if body_read else demand.UNREAD
            if counted and method is not None:
                try:
                    self.demand.add(self._demand_key(scope, method, demand_client, status, answer))
                except Exception as e:
                    demand.logger.warning("Demand count failed (%s).", type(e).__name__)

    @staticmethod
    def _demand_key(
        scope: Scope, method: str, client: str, status: int | None, answer: bytearray
    ) -> demand.Key:
        host, ua, origin = demand.caller_classes(
            _first_header(scope, b"host"),
            _first_header(scope, b"user-agent"),
            _first_header(scope, b"origin"),
        )
        answered = isinstance(status, int) and 200 <= status < 300
        tools = (
            _listed_tools_digest(bytes(answer))
            if method == demand.TOOLS_LIST and answered
            else demand.NOT_APPLICABLE
        )
        return (method, demand.status_of(status), host, ua, origin, client, tools)


async def _write_every(
    summary: GateSummary, interval: float, demand_counter: demand.DemandCounter
) -> None:
    while True:
        await asyncio.sleep(interval)
        try:
            summary.write()
        except Exception as e:
            logger.warning("Gate summary failed (%s).", type(e).__name__)
        demand.write_logged(demand_counter)


async def _drain(summary: GateSummary, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while summary.open_requests > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.05)


def wrap_lifespan(
    inner: Callable[[Any], contextlib.AbstractAsyncContextManager[Any]],
    summary: GateSummary | None = None,
    *,
    interval: float = SUMMARY_INTERVAL_SECONDS,
    drain_seconds: float = DRAIN_SECONDS,
    flush_timeout: float = FLUSH_TIMEOUT_SECONDS,
    on_exit: Iterable[Callable[[], Awaitable[None]]] = (),
    demand_counter: demand.DemandCounter | None = None,
) -> Callable[[Any], contextlib.AbstractAsyncContextManager[Any]]:
    """The app lifespan inner, plus the summaries and the exit work around it.

    While the app runs, a summary and the demand count are written every
    interval seconds. On the way out, after inner has exited, it waits up to
    drain_seconds for tracked requests still finishing, writes the last
    summary and the last demand count, awaits each on_exit
    callable in order (anything else that must close before the process
    does; a failure is logged by its class and the rest still run), and then
    flushes buffered telemetry for at most flush_timeout seconds
    (observability.flush_telemetry).

    uvicorn runs the lifespan exit on SIGTERM and SIGINT once connections
    have finished or its graceful shutdown timeout has passed, and skips it
    only on a forced exit (a second SIGINT).
    """
    gate_summary = summary if summary is not None else default_summary
    counter = demand_counter if demand_counter is not None else demand.default_counter
    closers = tuple(on_exit)

    @contextlib.asynccontextmanager
    async def lifespan(app: Any) -> AsyncIterator[Any]:
        writer = asyncio.create_task(_write_every(gate_summary, interval, counter))
        try:
            async with inner(app) as state:
                yield state
        finally:
            writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writer
            await _drain(gate_summary, drain_seconds)
            try:
                gate_summary.write()
            except Exception as e:
                logger.warning("Gate summary failed (%s).", type(e).__name__)
            demand.write_logged(counter)
            for close in closers:
                try:
                    await close()
                except Exception as e:
                    logger.warning("Shutdown step failed (%s).", type(e).__name__)
            await asyncio.to_thread(observability.flush_telemetry, flush_timeout)

    return lifespan
