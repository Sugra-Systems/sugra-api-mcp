"""Every outbound client in the process shares ONE TLS context.

These tests construct clients WITHOUT a transport on purpose: httpx skips
building a context entirely when a transport is passed, so a pin written the
usual (mock-transport) way would pass against the unfixed code too.
"""

from __future__ import annotations

import json
import os
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

from sugra_api_mcp import client as client_module
from sugra_api_mcp.auth import Authenticator
from sugra_api_mcp.client import SugraClient, shared_ssl_context
from sugra_api_mcp.config import AuthConfig, Config

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
async def own_pools(monkeypatch) -> AsyncIterator[None]:
    """The shared pools a test opens are its own, and closed after it."""
    monkeypatch.setattr(client_module, "_pools", {})
    yield
    await client_module.close_shared_pools()


def _real_client(api_key: str, api_base: str = "https://api.test") -> SugraClient:
    """A client on the DEFAULT transport - the only path that builds a context."""
    return SugraClient(Config(api_base=api_base, api_key=api_key, timeout=1.0))


def _pool_context(client: SugraClient) -> ssl.SSLContext:
    return client._http()._transport._pool._ssl_context


def _auth_config() -> AuthConfig:
    return AuthConfig(
        app_url="https://app.sugra.ai",
        jwks_url="https://app.sugra.ai/oauth/jwks.json",
        internal_token="unused",
    )


async def test_two_clients_share_one_context_object(own_pools) -> None:
    """Two API bases, so two pools: both on the one context."""
    first = _real_client("sugra_first")
    second = _real_client("sugra_second", "https://api-two.test")
    assert first._http() is not second._http()
    assert _pool_context(first) is shared_ssl_context()
    assert _pool_context(first) is _pool_context(second)


async def test_callers_share_one_pool_and_each_request_carries_its_own_key(monkeypatch, own_pools) -> None:
    """Sharing the pool does not merge the callers: each request carries its own key.

    The key rides on the request, never on the pool's own headers, so no
    request can carry the key of another caller.
    """
    sent: list[list[str]] = []

    async def handle(transport: httpx.AsyncHTTPTransport, request: httpx.Request) -> httpx.Response:
        sent.append(request.headers.get_list("x-api-key"))
        return httpx.Response(200, json={"data": [], "meta": {}})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", handle)
    first = _real_client("sugra_first")
    second = _real_client("sugra_second")
    assert first._http() is second._http()
    assert "x-api-key" not in first._http().headers
    for client in (first, second, first):
        await client.get("/api/v1/ping")
    assert sent == [["sugra_first"], ["sugra_second"], ["sugra_first"]]


def test_the_shared_context_is_the_one_an_unconfigured_httpx_client_builds() -> None:
    """The anchors are httpx's, not the stdlib's - the swap this change avoids.

    `ssl.create_default_context()` loads the SYSTEM store while httpx loads
    certifi's bundle. Comparing against a real unconfigured `httpx.Client`
    checks the shipped default path, not just the helper we happen to call.
    """
    context = shared_ssl_context()
    with httpx.Client() as unconfigured:
        httpx_default = unconfigured._transport._pool._ssl_context

    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.get_ca_certs() == httpx_default.get_ca_certs()


def test_the_stdlib_default_would_have_been_a_different_trust_store() -> None:
    """HOST EVIDENCE, not a regression pin: the two stores really do differ.

    What catches a regression to `ssl.create_default_context()` is the test
    above, which compares against a live unconfigured `httpx.Client`. This one
    records WHY that comparison matters here - and where the system store
    happens to carry certifi's exact anchors it SKIPS, because then this host
    has nothing to say either way. A skip reported as a pass is what the first
    version of this test did, which is no evidence at all.
    """
    system_anchors = ssl.create_default_context().get_ca_certs()
    httpx_anchors = shared_ssl_context().get_ca_certs()
    if system_anchors == httpx_anchors:
        pytest.skip("this host's system store carries certifi's exact anchors")
    assert httpx_anchors != system_anchors


def test_a_keyless_stdio_session_builds_no_context() -> None:
    """A REAL stdio session builds none.

    Not an import probe: this starts the module's own entry point with the
    default transport, speaks JSON-RPC over its pipes, and requires both
    calls against an unconfigured install to come back answered. Only then
    does the exit report mean anything - a process that never served would
    also report no context.

    The report rides on an `atexit` hook because the module global lives in
    the CHILD; the parent cannot read it. The child's own `c.__file__` comes
    back with it, since a probe reporting on an installed copy would be
    reporting on somebody else's global.
    """
    boot = (
        "import atexit, runpy, sys\n"
        "import sugra_api_mcp.client as c\n"
        "atexit.register(lambda: print(\n"
        "    'PROBE %s %s' % (c.__file__, 'BUILT' if c._ssl_context is not None else 'NONE'),\n"
        "    file=sys.stderr, flush=True))\n"
        "sys.argv = ['sugra-api-mcp']\n"
        "runpy.run_module('sugra_api_mcp', run_name='__main__')\n"
    )
    env = dict(os.environ)
    env.pop("SUGRA_API_KEY", None)  # keyless: an unconfigured install
    process = subprocess.Popen(
        [sys.executable, "-c", boot],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=REPO_ROOT,
        env=env,
        text=True,
        bufsize=1,
    )
    # Drained in a thread: the server logs to stderr, and a full pipe would
    # block the child mid-session.
    stderr_lines: list[str] = []
    drain = threading.Thread(target=stderr_lines.extend, args=(process.stderr,), daemon=True)
    drain.start()

    def send(message: dict) -> None:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def answer_to(request_id: int) -> dict:
        """Read until THIS id comes back.

        Writing all three messages and closing stdin instead is a race the
        server is entitled to win: it may shut down on EOF before it answers
        the last one. That passed locally and failed on three of four CI
        interpreters.
        """
        while True:
            line = process.stdout.readline()
            if not line:
                raise AssertionError(f"stdout closed before id {request_id}")
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("id") == request_id and "result" in message:
                return message["result"]

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "ssl-context-probe", "version": "0"},
                },
            }
        )
        initialized = answer_to(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = answer_to(2)
        process.stdin.close()
        returncode = process.wait(timeout=120)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
    drain.join(timeout=30)

    # The session really happened: a named server and a non-empty tool list.
    assert initialized.get("serverInfo", {}).get("name")
    assert listed.get("tools")
    assert returncode == 0, "".join(stderr_lines)[-2000:]

    probe = [line for line in stderr_lines if line.startswith("PROBE ")]
    assert probe, "".join(stderr_lines)[-2000:]
    imported_from, built = probe[-1].strip().split(" ", 1)[1].rsplit(" ", 1)
    assert Path(imported_from).resolve().is_relative_to(REPO_ROOT), imported_from
    assert built == "NONE", probe[-1]


async def test_the_factory_runs_exactly_once_however_many_clients_ask(monkeypatch, own_pools) -> None:
    """Built ONCE: the point of the accessor, measured by counting the factory.

    Through real clients, because that is what the name claims. The
    middleware's own direct call is pinned separately, by the Authenticator
    test below.
    """
    calls: list[int] = []
    real_factory = httpx.create_ssl_context

    def counting_factory(*args, **kwargs):
        calls.append(1)
        return real_factory(*args, **kwargs)

    monkeypatch.setattr(client_module.httpx, "create_ssl_context", counting_factory)
    monkeypatch.setattr(client_module, "_ssl_context", None)

    assert calls == []
    # One pool per API base: three bases, three pools, one context.
    clients = [_real_client(f"sugra_{n}", f"https://api-{n}.test") for n in range(3)]
    contexts = {id(_pool_context(client)) for client in clients}

    assert len(client_module._pools) == 3
    assert len(calls) == 1
    assert len(contexts) == 1


def test_a_thread_race_on_the_first_call_still_yields_one_context(monkeypatch) -> None:
    """The reason this is not `lru_cache`, whose miss path is not atomic.

    The factory is slowed so every thread is inside the window that a
    cache-style accessor leaves open; all of them must still get one object.
    """
    barrier = threading.Barrier(8)
    real_factory = httpx.create_ssl_context
    calls: list[int] = []

    def slow_factory(*args, **kwargs):
        calls.append(1)
        time.sleep(0.05)
        return real_factory(*args, **kwargs)

    monkeypatch.setattr(client_module.httpx, "create_ssl_context", slow_factory)
    monkeypatch.setattr(client_module, "_ssl_context", None)

    seen: list[ssl.SSLContext] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait(timeout=30)
        context = shared_ssl_context()
        with lock:
            seen.append(context)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(seen) == 8
    assert len({id(context) for context in seen}) == 1
    # Identity alone would also pass for an accessor that builds eight and
    # keeps one; what this change claims is that it builds ONE.
    assert len(calls) == 1


async def test_the_middleware_client_is_on_the_same_context() -> None:
    """The OTHER construction site: the Authenticator's pooled client."""
    auth = Authenticator(_auth_config())
    try:
        assert auth._http._transport._pool._ssl_context is shared_ssl_context()
    finally:
        await auth.aclose()


async def test_no_client_in_this_package_enables_http2(own_pools) -> None:
    """The condition that makes ONE shared context safe, as a test.

    httpcore calls `set_alpn_protocols` on the context before every TLS
    connect, with the list taken from THAT pool's http2 flag - so the object
    is written to, not merely read. All pools writing the same list is what
    makes the write harmless, and that holds only while every client here
    leaves HTTP/2 off. A client that turned it on would need its own context;
    this test is what turns that from a comment into a failure.
    """
    client = _real_client("sugra_alpn")
    auth = Authenticator(_auth_config())
    try:
        assert client._http()._transport._pool._http2 is False
        assert auth._http._transport._pool._http2 is False
    finally:
        await auth.aclose()


async def test_a_supplied_transport_builds_no_context_at_all(monkeypatch) -> None:
    """Passing verify must not disturb - or cost anything on - the test paths."""
    calls: list[int] = []
    # Bound BEFORE the patch: `httpx.create_ssl_context` inside the replacement
    # would resolve to the replacement itself (same module object), so a client
    # that did build one would recurse instead of failing the assertion below.
    real_factory = httpx.create_ssl_context
    monkeypatch.setattr(
        client_module.httpx,
        "create_ssl_context",
        lambda *a, **k: calls.append(1) or real_factory(*a, **k),
    )
    monkeypatch.setattr(client_module, "_ssl_context", None)

    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    client = SugraClient(
        Config(api_base="https://api.test", api_key="k", timeout=1.0), transport=transport
    )
    try:
        assert client._http()._transport is transport
        assert calls == []
        assert client_module._ssl_context is None
    finally:
        await client.aclose()
