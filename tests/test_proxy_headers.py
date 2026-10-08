"""SUGRA_MCP_TRUST_PROXY_HEADERS: the caller's host and address from the proxy's headers.

Off (the default), the server reads the Host header and the connection peer and
nothing else, exactly as before the setting existed. On, the host class comes
from X-Forwarded-Host and the network prefix from X-Real-IP, which the proxy in
front of the server sets on every request. X-Forwarded-For is never read, and
the host allow-list never sees X-Forwarded-Host.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server.lowlevel.server import request_ctx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import sugra_api_mcp.tools  # noqa: F401  (registers the tools on server.mcp)
from sugra_api_mcp import config, demand, gate, observability, server, teardown_filter
from sugra_api_mcp.auth import AuthError, AuthMiddleware
from tests.test_request_credentials import (
    HEADERS,
    INITIALIZE,
    _answer_api_requests,
    _authenticator,
    _CaptureTracer,
    _operation_without_params,
)

ENV = config.TRUST_PROXY_HEADERS_ENV
PROXY = "10.0.0.5"
ALLOWED = "app.sugra.ai,mcp.sugra.ai,py.internal.example"
# The longest name the host reader takes: 253 characters in labels of at most 63.
_LONGEST_NAME = ".".join(["a" * 63, "b" * 63, "c" * 63, "d" * 61])
assert len(_LONGEST_NAME) == 253


@pytest.fixture(autouse=True)
def _off_unless_a_test_turns_it_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV, raising=False)


# ---- The setting ----


def test_the_setting_is_off_by_default() -> None:
    assert config.proxy_headers_trusted() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " On ", "\ttrue\n"])
def test_the_values_that_turn_it_on(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ENV, value)
    assert config.proxy_headers_trusted() is True


@pytest.mark.parametrize("value", ["", " ", "0", "false", "no", "off", "2", "enabled", "truee", "on,off", "*"])
def test_every_other_value_keeps_it_off(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv(ENV, value)
    assert config.proxy_headers_trusted() is False


# ---- The two readers ----

MALFORMED_HOSTS = [
    None,
    5,
    b"app.sugra.ai",
    "",
    "   ",
    "mcp.sugra.ai, app.sugra.ai",
    "mcp.sugra.ai app.sugra.ai",
    "mcp.sugra.ai/path",
    "user@mcp.sugra.ai",
    "mcp.sugra.ai\r\nx-injected: 1",
    "mcpé.sugra.ai",
    "mcp.sugra.ai?x=1",
    "a" * 260,
    # Characters from the allowed set that do not make one host[:port].
    ":",
    "host:abc",
    "host:99999",
    "[::1",
    "a:b:c",
    "[::1]:0",
    "host:",
    "host:0",
    "host:65536",
    "host:123456",
    "host:+80",
    "[::1]:",
    "[::1]x",
    "[::1]:80:80",
    "[]",
    "[1.2.3.4]",
    "[fe80::1%eth0]",
    "[::g]",
    "::1",
    "-a.example",
    "a-.example",
    "a..b",
    ".example",
    "example.",
    "a" * 64 + ".example",
    "a" * 253 + ":80",
    "999.8.8.8",
    "10.1.2",
    "1.2.3.4.5",
    "8080",
    "example.com:80:",
    # Edge and inner control or non-ASCII characters are never trimmed away.
    "example.com\n",
    "example.com\r",
    "\x00example.com",
    "\texample.com\x0b",
    "\u00a0example.com\u00a0",
    "\u2003example.com",
    "example.com\x7f",
    "exam\tple.com",
]


def test_off_the_host_is_the_host_header_whatever_x_forwarded_host_says() -> None:
    for forwarded in ("mcp.sugra.ai", "evil.example", "", None, "a, b"):
        assert config.caller_host("app.sugra.ai", forwarded) == "app.sugra.ai"
    assert config.caller_host(None, "mcp.sugra.ai") is None


@pytest.mark.parametrize(
    ("forwarded", "used"),
    [
        ("mcp.sugra.ai", "mcp.sugra.ai"),
        ("  MCP.sugra.ai  ", "MCP.sugra.ai"),
        ("\texample.com", "example.com"),
        ("example.com\t", "example.com"),
        ("\texample.com\t", "example.com"),
        (" example.com ", "example.com"),
        ("mcp.sugra.ai:443", "mcp.sugra.ai:443"),
        ("[::1]:8002", "[::1]:8002"),
        (_LONGEST_NAME + ":65535", _LONGEST_NAME + ":65535"),
        ("example.com", "example.com"),
        ("example.com:8443", "example.com:8443"),
        ("10.1.2.3", "10.1.2.3"),
        ("255.0.0.1:1", "255.0.0.1:1"),
        ("[::1]", "[::1]"),
        ("[2001:db8::1]:443", "[2001:db8::1]:443"),
        ("py_internal.example", "py_internal.example"),
        ("a-b.c-d.example", "a-b.c-d.example"),
        ("localhost", "localhost"),
        ("a" * 63 + ".example", "a" * 63 + ".example"),
    ],
)
def test_on_a_well_formed_x_forwarded_host_is_the_host(
    monkeypatch: pytest.MonkeyPatch, forwarded: str, used: str
) -> None:
    monkeypatch.setenv(ENV, "1")
    assert config.caller_host("py.internal.example", forwarded) == used


@pytest.mark.parametrize("forwarded", MALFORMED_HOSTS)
def test_on_a_malformed_x_forwarded_host_falls_back_to_the_host_header(
    monkeypatch: pytest.MonkeyPatch, forwarded: object
) -> None:
    monkeypatch.setenv(ENV, "1")
    assert config.caller_host("py.internal.example", forwarded) == "py.internal.example"
    assert config.caller_host(None, forwarded) is None


MALFORMED_ADDRESSES = [
    None,
    8,
    b"8.8.8.8",
    "",
    "  ",
    "unknown",
    "8.8.8.8, 1.1.1.1",
    "8.8.8.8:443",
    "8.8.8.8\n1.1.1.1",
    "999.8.8.8",
    "fe80::1%eth0",
    "[::1]",
    "8.8.8.",
    "1" * 50,
    "8.8.8.8\n",
    "8.8.8.8\r",
    "\x008.8.8.8",
    "\t8.8.8.8\x0b",
    "\u00a08.8.8.8\u00a0",
    "\u20038.8.8.8",
    "8.8.8.8\x7f",
    "8.\t8.8.8",
]


def test_off_the_address_is_the_peer_and_the_header_as_received() -> None:
    assert config.caller_address("10.0.0.5", "8.8.8.8") == ("10.0.0.5", "8.8.8.8")
    assert config.caller_address("10.0.0.5", "junk") == ("10.0.0.5", "junk")
    assert config.caller_address(None, None) == (None, None)


@pytest.mark.parametrize(
    ("real", "used"),
    [
        ("8.8.8.8", "8.8.8.8"),
        (" 8.8.8.8 ", "8.8.8.8"),
        ("\t8.8.8.8\t", "8.8.8.8"),
        ("2001:4860:4860:0:0:0:0:8888", "2001:4860:4860::8888"),
        ("10.1.2.3", "10.1.2.3"),
    ],
)
def test_on_a_well_formed_x_real_ip_replaces_the_peer(
    monkeypatch: pytest.MonkeyPatch, real: str, used: str
) -> None:
    monkeypatch.setenv(ENV, "1")
    assert config.caller_address(PROXY, real) == (used, used)


@pytest.mark.parametrize("real", MALFORMED_ADDRESSES)
def test_on_a_malformed_x_real_ip_changes_nothing(monkeypatch: pytest.MonkeyPatch, real: object) -> None:
    monkeypatch.setenv(ENV, "1")
    assert config.caller_address(PROXY, real) == (PROXY, real)


# ---- The facts of a dispatched call ----


class _RecordedHeaders:
    """The headers of a request, remembering every name asked for."""

    def __init__(self, values: dict[str, str]) -> None:
        self._values = values
        self.asked: list[str] = []

    def get(self, name: str, default: object = None) -> object:
        self.asked.append(name.lower())
        return self._values.get(name.lower(), default)


def _facts(
    headers: dict[str, str], peer: tuple[str, int] | None = (PROXY, 4000)
) -> tuple[observability.CallerFacts, _RecordedHeaders]:
    recorded = _RecordedHeaders({name.lower(): value for name, value in headers.items()})
    request = SimpleNamespace(scope={"type": "http", "client": peer, "state": {}}, headers=recorded)
    restore = request_ctx.set(SimpleNamespace(session=None, request=request))  # type: ignore[arg-type]
    try:
        facts = server.current_caller_facts()
    finally:
        request_ctx.reset(restore)
    assert isinstance(facts, observability.CallerFacts)
    return facts, recorded


FORWARDED = {
    "host": "py.internal.example",
    "x-forwarded-host": "mcp.sugra.ai",
    "x-real-ip": "8.8.8.8",
    "x-forwarded-for": "1.1.1.1, 9.9.9.9",
}


def test_off_the_facts_are_the_host_header_the_peer_and_x_real_ip_as_received() -> None:
    facts, _ = _facts(FORWARDED)
    assert facts.host == "py.internal.example"
    assert facts.client_addr == PROXY
    assert facts.x_real_ip == "8.8.8.8"
    assert observability._caller_attrs(facts)["mcp.caller.host"] == "other"
    assert "mcp.caller.net" not in observability._caller_attrs(facts)


def test_off_a_garbage_x_forwarded_host_and_x_real_ip_change_nothing() -> None:
    plain, _ = _facts({"host": "app.sugra.ai"}, ("127.0.0.1", 1))
    noisy, _ = _facts(
        {"host": "app.sugra.ai", "x-forwarded-host": "evil.example", "x-forwarded-for": "6.6.6.6"},
        ("127.0.0.1", 1),
    )
    assert noisy == plain


def test_on_the_facts_name_the_forwarded_host_and_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "on")
    facts, _ = _facts(FORWARDED)
    assert facts.host == "mcp.sugra.ai"
    assert facts.client_addr == facts.x_real_ip == "8.8.8.8"
    attrs = observability._caller_attrs(facts)
    assert attrs["mcp.caller.host"] == "mcp.sugra.ai"
    assert attrs["mcp.caller.net"] == "8.8.8.0/24"


def test_on_an_ipv4_mapped_x_real_ip_names_the_ipv4_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "on")
    facts, _ = _facts({**FORWARDED, "x-real-ip": "::ffff:8.8.8.8"})
    assert observability._caller_attrs(facts)["mcp.caller.net"] == "8.8.8.0/24"


def test_on_without_the_headers_the_facts_are_the_unchanged_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "on")
    facts, _ = _facts({"host": "py.internal.example"})
    assert facts.host == "py.internal.example"
    assert facts.client_addr == PROXY
    assert facts.x_real_ip is None
    attrs = observability._caller_attrs(facts)
    assert attrs["mcp.caller.host"] == "other"
    assert "mcp.caller.net" not in attrs
    local, _ = _facts({"host": "127.0.0.1:8002"}, ("127.0.0.1", 1))
    assert observability._caller_attrs(local)["mcp.caller.net"] == "loopback"


def test_on_a_malformed_x_real_ip_names_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "on")
    for junk in ("8.8.8.8, 1.1.1.1", "not-an-ip", "", "8.8.8.8:80"):
        facts, _ = _facts({"host": "py.internal.example", "x-real-ip": junk})
        assert "mcp.caller.net" not in observability._caller_attrs(facts), junk


def test_on_a_malformed_x_forwarded_host_leaves_the_host_header(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV, "on")
    for junk in ("mcp.sugra.ai, app.sugra.ai", "", "mcp.sugra.ai/x"):
        facts, _ = _facts({"host": "app.sugra.ai", "x-forwarded-host": junk})
        assert observability._caller_attrs(facts)["mcp.caller.host"] == "app.sugra.ai", junk


@pytest.mark.parametrize("setting", [None, "1"])
def test_x_forwarded_for_is_never_read(monkeypatch: pytest.MonkeyPatch, setting: str | None) -> None:
    if setting is not None:
        monkeypatch.setenv(ENV, setting)
    facts, recorded = _facts(FORWARDED)
    assert "x-forwarded-for" not in recorded.asked
    assert "1.1.1.1" not in repr(facts) and "9.9.9.9" not in repr(facts)


def test_x_forwarded_for_is_not_a_name_anywhere_in_the_package() -> None:
    """No header lookup, comparison or constant in the code names X-Forwarded-For."""
    package = Path(server.__file__).parent
    found: list[str] = []
    for path in sorted(package.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
                text = node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value
                if text.strip().lower() == "x-forwarded-for":
                    found.append(f"{path.name}:{node.lineno}")
    assert found == []


# ---- The same through the real app ----


@pytest.fixture
async def upstream(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    async def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"ok": 1}], "meta": {}})

    _answer_api_requests(monkeypatch, answer)
    monkeypatch.delenv("SUGRA_API_KEY", raising=False)
    await server.close_clients()
    yield
    await server.close_clients()


def _hosted_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The host allow-list of the hosted server: its two names and the proxy's internal one."""
    monkeypatch.setenv("SUGRA_MCP_ALLOWED_HOSTS", ALLOWED)
    monkeypatch.setattr(server.mcp.settings, "transport_security", server._build_transport_security())
    monkeypatch.setattr(server.mcp, "_session_manager", None)


async def _span_caller(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str], peer: str = PROXY
) -> dict[str, object]:
    """The caller attributes of the span of one tool call sent with these headers."""
    _hosted_settings(monkeypatch)
    inner = server.mcp.streamable_http_app()
    authenticator = _authenticator()
    inner.add_middleware(AuthMiddleware, authenticator=authenticator)
    tracer = _CaptureTracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)
    try:
        async with server.mcp.session_manager.run():
            transport = httpx.ASGITransport(app=inner, client=(peer, 4000))
            async with httpx.AsyncClient(transport=transport, base_url="http://py.internal.example") as client:
                opened = await client.post("/mcp", json=INITIALIZE, headers={**HEADERS, **headers})
                assert opened.status_code == 200
                session = {"mcp-session-id": opened.headers["mcp-session-id"]}
                await client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers={**HEADERS, **headers, **session},
                )
                called = await client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": "call_endpoint", "arguments": {"operation_id": _operation_without_params()}},
                    },
                    headers={**HEADERS, **headers, **session, "authorization": "Bearer sugra_proxy_probe"},
                )
                assert called.status_code == 200
    finally:
        await authenticator.aclose()
    spans = [span for span in tracer.spans if span.name == "mcp.tool.call_endpoint"]
    assert spans
    return {key: value for key, value in spans[0].attributes.items() if key.startswith("mcp.caller.")}


PROXIED = {
    "host": "py.internal.example",
    "x-forwarded-host": "mcp.sugra.ai",
    "x-real-ip": "8.8.8.8",
    "x-forwarded-for": "1.1.1.1, 9.9.9.9",
}


async def test_off_a_span_names_the_proxy_host_and_no_network(upstream, monkeypatch) -> None:
    attrs = await _span_caller(monkeypatch, PROXIED)
    assert attrs["mcp.caller.host"] == "other"
    assert "mcp.caller.net" not in attrs


async def test_on_a_span_names_the_real_host_and_network(upstream, monkeypatch) -> None:
    monkeypatch.setenv(ENV, "1")
    attrs = await _span_caller(monkeypatch, PROXIED)
    assert attrs["mcp.caller.host"] == "mcp.sugra.ai"
    assert attrs["mcp.caller.net"] == "8.8.8.0/24"


async def test_on_a_forwarded_for_alone_names_no_network(upstream, monkeypatch) -> None:
    monkeypatch.setenv(ENV, "1")
    attrs = await _span_caller(monkeypatch, {"host": "py.internal.example", "x-forwarded-for": "8.8.8.8"})
    assert "mcp.caller.net" not in attrs


async def test_on_the_network_follows_x_real_ip_not_x_forwarded_for(upstream, monkeypatch) -> None:
    monkeypatch.setenv(ENV, "1")
    attrs = await _span_caller(monkeypatch, {**PROXIED, "x-real-ip": "1.2.3.4", "x-forwarded-for": "8.8.8.8"})
    assert attrs["mcp.caller.net"] == "1.2.3.0/24"


# ---- The host allow-list ----


async def _initialize_status(
    monkeypatch: pytest.MonkeyPatch, headers: dict[str, str], host: str
) -> int:
    _hosted_settings(monkeypatch)
    app = server.mcp.streamable_http_app()
    async with server.mcp.session_manager.run():
        transport = httpx.ASGITransport(app=app, client=(PROXY, 4000))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
            response = await client.post("/mcp", json=INITIALIZE, headers={**HEADERS, **headers, "host": host})
    return response.status_code


@pytest.mark.parametrize("setting", [None, "1"])
async def test_a_forwarded_host_never_gets_a_disallowed_host_past_the_allow_list(
    monkeypatch: pytest.MonkeyPatch, setting: str | None
) -> None:
    if setting is not None:
        monkeypatch.setenv(ENV, setting)
    for forwarded in ("app.sugra.ai", "mcp.sugra.ai", "py.internal.example", "evil.example", "a, b"):
        status = await _initialize_status(monkeypatch, {"x-forwarded-host": forwarded}, "evil.example")
        assert status == 421, forwarded


@pytest.mark.parametrize("setting", [None, "1"])
async def test_a_forwarded_host_never_turns_an_allowed_host_away(
    monkeypatch: pytest.MonkeyPatch, setting: str | None
) -> None:
    if setting is not None:
        monkeypatch.setenv(ENV, setting)
    for forwarded in ("evil.example", "mcp.sugra.ai", "", "a, b"):
        status = await _initialize_status(monkeypatch, {"x-forwarded-host": forwarded}, "py.internal.example")
        assert status == 200, forwarded


# ---- The other places a host is read ----


def _request(headers: dict[str, str]) -> Request:
    return Request({
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [(name.encode(), value.encode()) for name, value in headers.items()],
    })


def test_the_teardown_count_follows_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _request({"host": "py.internal.example", "x-forwarded-host": "mcp.sugra.ai"})
    assert teardown_filter._request_classes(request)[0] == "other"
    monkeypatch.setenv(ENV, "1")
    assert teardown_filter._request_classes(request)[0] == "mcp.sugra.ai"
    garbage = _request({"host": "app.sugra.ai", "x-forwarded-host": "a, b"})
    assert teardown_filter._request_classes(garbage)[0] == "app.sugra.ai"


async def _counted_host(headers: dict[str, str]) -> str:
    counter = demand.DemandCounter()

    async def answer(request: Request) -> JSONResponse:
        await request.body()
        return JSONResponse({})

    app = Starlette(routes=[Route("/mcp", answer, methods=["POST"])])
    app.add_middleware(gate.GateMiddleware, max_body_bytes=4096, summary=gate.GateSummary(), demand_counter=counter)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002") as client:
        await client.post(
            "/mcp", json={**INITIALIZE, "id": 1}, headers={"x-request-id": "ab" * 16, **headers}
        )
    [key] = counter._counts
    return key[2]


async def test_the_gate_counts_the_host_class_the_setting_names(monkeypatch: pytest.MonkeyPatch) -> None:
    headers = {"host": "py.internal.example", "x-forwarded-host": "app.sugra.ai"}
    assert await _counted_host(headers) == "other"
    monkeypatch.setenv(ENV, "1")
    assert await _counted_host(headers) == "app.sugra.ai"
    assert await _counted_host({"host": "mcp.sugra.ai", "x-forwarded-host": "a, b"}) == "mcp.sugra.ai"


def _failed_host(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, headers: dict[str, str]) -> str:
    authenticator = _authenticator()
    monkeypatch.setattr(authenticator, "resolve", AsyncMock(side_effect=AuthError("refused", status=401)))

    async def ok(_request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    caplog.clear()
    caplog.set_level(logging.WARNING, logger="sugra_mcp.auth")
    response = TestClient(app).post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call"},
        headers={"authorization": "Bearer sugra_QZXKWVMRTNBLPJGH", **headers},
    )
    assert response.status_code == 401
    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith("auth_failed ")]
    return next(part.removeprefix("host=") for part in line.split() if part.startswith("host="))


def test_the_auth_failure_line_names_the_host_class_the_setting_names(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    headers = {"host": "py.internal.example", "x-forwarded-host": "mcp.sugra.ai"}
    assert _failed_host(monkeypatch, caplog, headers) == "other"
    monkeypatch.setenv(ENV, "1")
    assert _failed_host(monkeypatch, caplog, headers) == "mcp.sugra.ai"
    assert _failed_host(monkeypatch, caplog, {"host": "app.sugra.ai", "x-forwarded-host": "a, b"}) == "app.sugra.ai"


# ---- The operator's guide ----


def test_the_self_hosting_guide_names_the_setting_and_the_proxy_it_assumes() -> None:
    guide = (Path(__file__).resolve().parent.parent / "docs" / "self-hosting.md").read_text(encoding="utf-8")
    assert f"`{ENV}`" in guide
    for text in ("X-Real-IP", "X-Forwarded-Host", "X-Forwarded-For", "SUGRA_MCP_ALLOWED_HOSTS"):
        assert text in guide
