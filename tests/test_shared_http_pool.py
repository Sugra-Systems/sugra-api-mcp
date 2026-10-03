"""Callers share one connection pool to the Sugra API, and each request carries its own caller's key.

The pool holds no key and keeps no cookie. A caller's key rides on that
caller's own requests, as their x-api-key header, whichever path handed the
caller its client: an HTTP request with a resolved key, api_key_ctx, or
SUGRA_API_KEY. The tests read what reaches the wire: a stand-in for the
transport records every request to the test API, and a small HTTP server on
127.0.0.1 shows which connection carried which request.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import logging
import socket
from collections.abc import AsyncIterator, Callable, Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from mcp.server.lowlevel.server import request_ctx

from sugra_api_mcp import __version__, observability, server
from sugra_api_mcp import client as client_module
from sugra_api_mcp.client import SugraClient
from sugra_api_mcp.tools import gateway
from tests.test_request_credentials import _CaptureTracer, _operation_without_params

API = "https://api.test"
OK = {"data": [{"ok": 1}], "meta": {}}
# A key that appears nowhere but on the requests that carry it.
WIRE_KEY = "sugra_wire_only_5f2c"

Answer = Callable[[httpx.Request], Any]


@pytest.fixture(autouse=True)
async def _own_pools(monkeypatch) -> AsyncIterator[None]:
    """Each test starts with no pool open and leaves none behind."""
    monkeypatch.delenv("SUGRA_API_KEY", raising=False)
    monkeypatch.delenv("SUGRA_TIMEOUT", raising=False)
    monkeypatch.setenv("SUGRA_API_BASE", API)
    await server.close_clients()
    yield
    await server.close_clients()


class _Wire:
    """Stands in for the network at the transport.

    Every request to the test API is recorded and answered; any other host is
    refused, so nothing leaves the machine.
    """

    def __init__(self, monkeypatch, answer: Answer | None = None) -> None:
        self.requests: list[httpx.Request] = []
        # The transports that carried them. Held here, so that no two of them
        # can ever share an identity.
        self.transports: list[httpx.AsyncBaseTransport] = []
        self.answer = answer
        wire = self

        async def handle(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
            return await wire.receive(transport, request)

        monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)

    async def receive(self, transport: httpx.AsyncBaseTransport, request: httpx.Request) -> httpx.Response:
        if request.url.host != "api.test":
            raise httpx.ConnectError(f"no network in tests: {request.url.host}", request=request)
        self.requests.append(request)
        if not any(seen is transport for seen in self.transports):
            self.transports.append(transport)
        if self.answer is None:
            return httpx.Response(200, json=OK)
        response = self.answer(request)
        return await response if inspect.isawaitable(response) else response

    def keys(self) -> list[list[str]]:
        """The x-api-key values of each request, in the order they were sent."""
        return [request.headers.get_list("x-api-key") for request in self.requests]


@contextlib.contextmanager
def _keyed(path: str, key: str, monkeypatch) -> Iterator[None]:
    """Inside it, get_client serves a caller with this key, on one of its keyed paths."""
    if path == "http":
        # As the SDK hands a handler the request that carried its message, on
        # whose scope state AuthMiddleware stored the key it resolved.
        request = SimpleNamespace(scope={"type": "http", "state": {server.REQUEST_API_KEY_STATE: key}})
        previous = request_ctx.set(SimpleNamespace(request=request))
        try:
            yield
        finally:
            request_ctx.reset(previous)
    elif path == "in-process":
        previous_key = server.api_key_ctx.set(key)
        try:
            yield
        finally:
            server.api_key_ctx.reset(previous_key)
    else:
        monkeypatch.setenv("SUGRA_API_KEY", key)
        yield


def _local_network_only(monkeypatch) -> None:
    """No proxy between the pool and 127.0.0.1, whatever the machine running the tests uses."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")


class _LocalApi:
    """An HTTP/1.1 server on 127.0.0.1 that keeps each connection open between requests.

    "echo" answers every request with what it carried, in the body only: the
    caller named in its query, its x-api-key and Cookie headers, and the
    number of the connection it came on. "silent" reads a request and never
    answers it. "hangup" reads a request and closes the connection unanswered.
    """

    def __init__(self, mode: str = "echo", hold: float = 0.0) -> None:
        self.mode = mode
        self.hold = hold
        self.connections = 0
        self.in_flight = 0
        self.peak = 0
        self._writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.Server | None = None

    @property
    def base(self) -> str:
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    async def __aenter__(self) -> _LocalApi:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        assert self._server is not None
        self._server.close()
        for writer in self._writers:
            writer.close()
        await asyncio.wait_for(self._server.wait_closed(), 5)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        connection = self.connections
        self._writers.append(writer)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                if self.mode == "hangup":
                    return
                if self.mode == "silent":
                    await reader.read()
                    return
                request_line, *lines = head.decode("latin-1").split("\r\n")
                fields = [line.split(":", 1) for line in lines if ":" in line]
                carried = httpx.Headers([(name.strip(), value.strip()) for name, value in fields])
                self.in_flight += 1
                self.peak = max(self.peak, self.in_flight)
                try:
                    if self.hold:
                        await asyncio.sleep(self.hold)
                    echo = {
                        "caller": httpx.URL(request_line.split(" ")[1]).params.get("caller"),
                        "key": ", ".join(carried.get_list("x-api-key")),
                        "cookie": ", ".join(carried.get_list("cookie")),
                        "connection": connection,
                    }
                    body = json.dumps({"data": [echo], "meta": {}}).encode()
                    writer.write(
                        b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                        + f"content-length: {len(body)}\r\n\r\n".encode()
                        + body
                    )
                    await writer.drain()
                finally:
                    self.in_flight -= 1
        except (asyncio.IncompleteReadError, ConnectionError):
            return
        finally:
            writer.close()


async def _echo(caller: str, monkeypatch) -> dict[str, Any]:
    """What the local server saw of one request by caller a or b, each with a key of its own."""
    with _keyed("in-process", f"sugra_caller_{caller}", monkeypatch):
        client = server.get_client()
    result = await client.get("/api/v1/ping", {"caller": caller})
    [echo] = result["data"]
    return echo


# ---- One pool, every key on its own requests ----


@pytest.mark.parametrize("path", ["http", "in-process"])
async def test_a_thousand_keys_share_one_pool(monkeypatch, path: str) -> None:
    wire = _Wire(monkeypatch)
    keys = [f"sugra_caller_{n:04d}" for n in range(1000)]
    for key in keys:
        with _keyed(path, key, monkeypatch):
            client = server.get_client()
        assert isinstance(client, SugraClient)
        assert await client.get("/api/v1/ping") == OK
    assert wire.keys() == [[key] for key in keys]
    assert len(wire.transports) == 1


async def test_every_keyed_path_sends_through_the_same_pool(monkeypatch) -> None:
    """An HTTP request's key, api_key_ctx and SUGRA_API_KEY each go out on
    their own requests, all three through one pool."""
    wire = _Wire(monkeypatch)
    for path, key in (("http", "sugra_by_http"), ("in-process", "sugra_by_context"), ("env", "sugra_by_env")):
        with _keyed(path, key, monkeypatch):
            client = server.get_client()
            assert isinstance(client, SugraClient)
            assert await client.get("/api/v1/ping") == OK
    assert wire.keys() == [["sugra_by_http"], ["sugra_by_context"], ["sugra_by_env"]]
    assert len(wire.transports) == 1


async def test_concurrent_callers_never_mix_their_keys(monkeypatch) -> None:
    """Forty requests in flight at once, twenty for each of two callers, all
    through one pool: each carries its own caller's key and no other."""
    released = asyncio.Event()

    async def answer(request: httpx.Request) -> httpx.Response:
        if len(wire.requests) == 40:
            released.set()
        await asyncio.wait_for(released.wait(), 5)
        return httpx.Response(200, json=OK)

    wire = _Wire(monkeypatch, answer)
    keys = {"a": "sugra_caller_a", "b": "sugra_caller_b"}

    async def call(caller: str) -> None:
        with _keyed("in-process", keys[caller], monkeypatch):
            client = server.get_client()
        assert await client.get("/api/v1/ping", {"caller": caller}) == OK

    await asyncio.gather(*(call(caller) for caller in ["a", "b"] * 20))
    assert len(wire.requests) == 40
    for request in wire.requests:
        assert request.headers.get_list("x-api-key") == [keys[request.url.params["caller"]]]
    assert len(wire.transports) == 1


async def test_a_cookie_set_for_one_caller_is_sent_with_no_later_request(monkeypatch) -> None:
    """The pool keeps no cookie (RFC 6265 jar that allows no domain), so the
    Set-Cookie answered to A goes out with no later request, B's included."""

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=OK, headers={"set-cookie": "session=for-a; Path=/"})

    wire = _Wire(monkeypatch, answer)
    for key in ("sugra_caller_a", "sugra_caller_b", "sugra_caller_a"):
        with _keyed("in-process", key, monkeypatch):
            client = server.get_client()
        await client.get("/api/v1/ping")
    assert wire.keys() == [["sugra_caller_a"], ["sugra_caller_b"], ["sugra_caller_a"]]
    assert [request.headers.get_list("cookie") for request in wire.requests] == [[], [], []]
    assert len(client_module.shared_pool(API).cookies.jar) == 0


# ---- On a real connection ----


async def test_callers_in_turn_reuse_one_connection(monkeypatch) -> None:
    """A, then B, then A again: one kept-open connection carries all three
    requests, and each carries the key of its own caller."""
    _local_network_only(monkeypatch)
    async with _LocalApi() as api:
        monkeypatch.setenv("SUGRA_API_BASE", api.base)
        echoed = [await _echo(caller, monkeypatch) for caller in ("a", "b", "a")]
        await server.close_clients()
    assert [(echo["caller"], echo["key"], echo["cookie"], echo["connection"]) for echo in echoed] == [
        ("a", "sugra_caller_a", "", 1),
        ("b", "sugra_caller_b", "", 1),
        ("a", "sugra_caller_a", "", 1),
    ]
    assert api.connections == 1


async def test_a_burst_stays_within_the_pool_limit_and_keeps_each_key_with_its_caller(monkeypatch) -> None:
    """Forty requests at once, twenty for each of two callers, each held for a
    moment by the server: no more than 32 of them reach the server at a time,
    and every answer is its own caller's.

    The count is of requests in flight, not of connections opened: once the
    first answers come back, the pool may close a spare connection and open
    another for a waiting request, never holding more than 32 at once.
    """
    _local_network_only(monkeypatch)
    callers = ["a", "b"] * 20
    async with _LocalApi(hold=0.3) as api:
        monkeypatch.setenv("SUGRA_API_BASE", api.base)
        echoed = await asyncio.gather(*(_echo(caller, monkeypatch) for caller in callers))
        await server.close_clients()
    assert [(echo["caller"], echo["key"]) for echo in echoed] == [
        (caller, f"sugra_caller_{caller}") for caller in callers
    ]
    assert api.peak <= 32


# ---- Where the key is never written ----


async def test_the_key_stays_out_of_the_debug_logs(monkeypatch, caplog) -> None:
    """httpx and httpcore logging at DEBUG on a real connection: the key goes
    out on the request, and no log record names it."""
    _local_network_only(monkeypatch)
    caplog.set_level(logging.DEBUG, logger="httpx")
    caplog.set_level(logging.DEBUG, logger="httpcore")
    async with _LocalApi() as api:
        monkeypatch.setenv("SUGRA_API_BASE", api.base)
        with _keyed("in-process", WIRE_KEY, monkeypatch):
            client = server.get_client()
        result = await client.get("/api/v1/ping", {"caller": "a"})
        await server.close_clients()
    assert result["data"][0]["key"] == WIRE_KEY
    records = [record for record in caplog.records if record.name.split(".")[0] in ("httpx", "httpcore")]
    assert any(record.name == "httpx" for record in records)
    assert any(record.name.startswith("httpcore.") for record in records)
    for record in records:
        assert WIRE_KEY not in record.getMessage()


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        ("timeout", "upstream_timeout"),
        ("hangup", "upstream_transport_error"),
        ("refused", "upstream_connect_error"),
    ],
)
async def test_the_key_stays_out_of_a_transport_error(monkeypatch, failure: str, code: str) -> None:
    """A request the server never answers, one it hangs up on, and one no
    server accepts: each comes back as its structured error, without the key."""
    _local_network_only(monkeypatch)
    monkeypatch.setenv("SUGRA_TIMEOUT", "0.5" if failure == "timeout" else "10")
    with socket.socket() as unused:
        # Bound but never listening, so a connect to its port is refused.
        unused.bind(("127.0.0.1", 0))
        async with _LocalApi(mode="silent" if failure == "timeout" else "hangup") as api:
            base = f"http://127.0.0.1:{unused.getsockname()[1]}" if failure == "refused" else api.base
            monkeypatch.setenv("SUGRA_API_BASE", base)
            with _keyed("in-process", WIRE_KEY, monkeypatch):
                client = server.get_client()
            result = await client.get("/api/v1/ping", {"caller": "a"})
            await server.close_clients()
    assert result["error"] == code
    assert result["status_code"] is None
    assert result["url"] == f"{base}/api/v1/ping?caller=a"
    if failure == "timeout":
        assert result["timeout_s"] == 0.5
    assert WIRE_KEY not in json.dumps(result)


@pytest.mark.parametrize("answer", ["ok", "refused", "timeout"])
async def test_the_key_stays_out_of_the_spans(monkeypatch, answer: str) -> None:
    """A traced call the API answers, one it refuses and one that times out:
    the key goes out on the request and appears on no span."""
    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)

    def respond(request: httpx.Request) -> httpx.Response:
        if answer == "timeout":
            raise httpx.ReadTimeout("no answer", request=request)
        if answer == "refused":
            return httpx.Response(503, json={"detail": "refused"})
        return httpx.Response(200, json=OK)

    wire = _Wire(monkeypatch, respond)
    with _keyed("in-process", WIRE_KEY, monkeypatch):
        await gateway.call_endpoint(operation_id=_operation_without_params())
    assert wire.keys() == [[WIRE_KEY]]
    spans = [span for span in tracer.spans if span.name == "mcp.tool.call_endpoint"]
    assert len(spans) == 1
    assert WIRE_KEY not in repr(spans[0].attributes)


# ---- The pool itself ----


async def test_one_pool_per_api_base_with_no_key_and_fixed_limits() -> None:
    """The shared client: no key and no cookie of its own, the package's
    User-Agent, JSON accepted, at most 32 connections with 20 kept open while
    idle for 5 seconds, HTTP/1.1 only, and one per API base."""
    pool = client_module.shared_pool(API)
    assert "x-api-key" not in pool.headers
    assert pool.headers["user-agent"] == f"sugra-api-mcp/{__version__}"
    assert pool.headers["accept"] == "application/json"
    assert len(pool.cookies.jar) == 0
    connections = pool._transport._pool
    assert (
        connections._max_connections,
        connections._max_keepalive_connections,
        connections._keepalive_expiry,
    ) == (32, 20, 5.0)
    assert connections._http2 is False
    assert client_module.shared_pool(API) is pool
    other = client_module.shared_pool("https://api-two.test")
    assert other is not pool
    await server.close_clients()
    assert pool.is_closed and other.is_closed
    assert client_module._pools == {}


async def test_a_request_after_the_pools_close_goes_out_on_a_new_pool(monkeypatch) -> None:
    """A client handed out before the close still sends after it: on a new
    pool, never on the closed one."""
    wire = _Wire(monkeypatch)
    with _keyed("in-process", "sugra_caller_a", monkeypatch):
        client = server.get_client()
    assert await client.get("/api/v1/ping") == OK
    first = client_module.shared_pool(API)
    await server.close_clients()
    assert first.is_closed
    assert await client.get("/api/v1/ping") == OK
    second = client_module.shared_pool(API)
    assert second is not first
    assert not second.is_closed
    assert wire.keys() == [["sugra_caller_a"], ["sugra_caller_a"]]


# ---- Each event loop on pools of its own ----


async def test_another_event_loop_is_handed_a_pool_of_its_own() -> None:
    """A pool's connections belong to the event loop that opened them, so a
    loop in another thread gets a pool of its own, and this loop keeps its
    own. Once that loop has closed, closing this loop's pools forgets its pool
    as well."""

    async def pool_there() -> httpx.AsyncClient:
        return client_module.shared_pool(API)

    here = client_module.shared_pool(API)
    there = await asyncio.to_thread(asyncio.run, pool_there())
    assert there is not here
    assert client_module.shared_pool(API) is here
    await server.close_clients()
    assert here.is_closed
    assert client_module._pools == {}


async def test_the_next_event_loop_never_sends_on_a_connection_an_ended_one_left_open(monkeypatch) -> None:
    """Caller A's event loop ends with its connection still open in its pool,
    as when `asyncio.run` returns. Caller B's request, on the next loop, goes
    out on a connection of its own, and each carries its own caller's key."""
    _local_network_only(monkeypatch)

    def each_caller_on_a_loop_of_its_own() -> list[dict[str, Any]]:
        return [asyncio.run(_echo(caller, monkeypatch)) for caller in ("a", "b")]

    async with _LocalApi() as api:
        monkeypatch.setenv("SUGRA_API_BASE", api.base)
        echoed = await asyncio.to_thread(each_caller_on_a_loop_of_its_own)
    assert [(echo["caller"], echo["key"], echo["connection"]) for echo in echoed] == [
        ("a", "sugra_caller_a", 1),
        ("b", "sugra_caller_b", 2),
    ]


# ---- What a request carries ----


async def test_requests_keep_their_urls_bodies_and_headers(monkeypatch) -> None:
    wire = _Wire(monkeypatch)
    with _keyed("in-process", "sugra_caller_a", monkeypatch):
        client = server.get_client()
    await client.get("/api/v1/quotes/AAPL/price")
    await client.get("/api/v1/series", {"a": 1, "b": None, "c": "2"})
    await client.post("/api/v1/bulk/items", json={"items": [1, 2]}, headers={"X-Internal-Token": "internal"})
    assert [(request.method, str(request.url)) for request in wire.requests] == [
        ("GET", "https://api.test/api/v1/quotes/AAPL/price"),
        ("GET", "https://api.test/api/v1/series?a=1&c=2"),
        ("POST", "https://api.test/api/v1/bulk/items"),
    ]
    posted = wire.requests[2]
    assert json.loads(posted.content) == {"items": [1, 2]}
    assert posted.headers["content-type"] == "application/json"
    assert posted.headers.get_list("x-internal-token") == ["internal"]
    assert wire.keys() == [["sugra_caller_a"]] * 3
    for request in wire.requests:
        assert request.headers["user-agent"] == f"sugra-api-mcp/{__version__}"
        assert request.headers["accept"] == "application/json"


@pytest.mark.parametrize("path", ["http", "in-process", "env"])
async def test_a_key_among_the_extra_headers_never_replaces_the_calls_own(monkeypatch, path: str) -> None:
    """Extra headers ride alongside the key resolved for the call, and the
    request carries that key alone: a key header among the extras, in any
    case, is replaced by it and never sent."""
    wire = _Wire(monkeypatch)
    with _keyed(path, "sugra_caller_a", monkeypatch):
        client = server.get_client()
    for name in ("x-api-key", "X-API-KEY", "X-Api-Key"):
        await client.post(
            "/api/v1/bulk/items",
            json={"items": [1]},
            headers={name: "sugra_caller_b", "X-Internal-Token": "internal"},
        )
    await client.request("GET", "/api/v1/ping", headers={"X-API-KEY": "sugra_caller_b"})
    assert wire.keys() == [["sugra_caller_a"]] * 4
    assert [request.headers.get_list("x-internal-token") for request in wire.requests] == [
        ["internal"]
    ] * 3 + [[]]


async def test_an_api_base_with_a_path_keeps_its_path(monkeypatch) -> None:
    wire = _Wire(monkeypatch)
    monkeypatch.setenv("SUGRA_API_BASE", "https://api.test/prefix/")
    with _keyed("in-process", "sugra_caller_a", monkeypatch):
        client = server.get_client()
    await client.get("/api/v1/ping")
    assert [str(request.url) for request in wire.requests] == ["https://api.test/prefix/api/v1/ping"]
