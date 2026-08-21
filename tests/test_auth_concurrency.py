"""MCP-10 (audit P1-4/P1-5): the auth layer must not serialize the process.

The audit observed a fast endpoint held for 120.4s and the NEXT search taking
107.5s. Three code-visible mechanisms could produce that: one Authenticator-wide
lock held ACROSS an internal HTTP call, synchronous JWKS network I/O executed on
the event loop, and an uncached activity validation paying an internal
round-trip on every tool call. Each is pinned here with a timing or call-count
assertion that fails on the pre-MCP-10 implementation.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from sugra_api_mcp.auth import Authenticator, _JwtClaims
from sugra_api_mcp.config import AuthConfig

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._payload


class _FakeHttp:
    """Pooled-client stand-in with a controllable per-call delay."""

    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.get_calls = 0
        self.post_calls = 0

    async def get(self, url, headers=None):
        self.get_calls += 1
        await asyncio.sleep(self.delay)
        return _FakeResponse({"api_key": "sugra_test_key"})

    async def post(self, url, headers=None, json=None):
        self.post_calls += 1
        await asyncio.sleep(self.delay)
        return _FakeResponse({})

    async def aclose(self):
        return None


def _authenticator(delay: float = 0.0) -> tuple[Authenticator, _FakeHttp]:
    auth = Authenticator(AuthConfig(
        app_url="https://app.example", jwks_url="https://app.example/jwks",
        internal_token="tkn"))
    fake = _FakeHttp(delay)
    auth._http = fake
    return auth, fake


async def test_cold_cache_lookups_do_not_serialize_across_users() -> None:
    """Two DIFFERENT users' cold-cache key lookups must run concurrently.

    Pre-MCP-10 a single Authenticator-wide lock was held across the internal
    HTTP call, so N users paid N sequential round-trips."""
    auth, _ = _authenticator(delay=0.3)
    started = time.monotonic()
    await asyncio.gather(auth._lookup_api_key(1), auth._lookup_api_key(2))
    elapsed = time.monotonic() - started
    assert elapsed < 0.5, (
        f"cold lookups for two users took {elapsed:.2f}s - they serialized")


async def test_same_user_lookups_single_flight() -> None:
    """Concurrent lookups for the SAME user share one fetch."""
    auth, fake = _authenticator(delay=0.1)
    await asyncio.gather(auth._lookup_api_key(7), auth._lookup_api_key(7))
    assert fake.get_calls == 1, (
        f"same-user concurrent lookups fetched {fake.get_calls} times")


async def test_stalled_jwks_does_not_block_the_raw_key_path() -> None:
    """A cold/stalled JWKS fetch must not stall unrelated requests.

    Pre-MCP-10 the PyJWKClient network fetch ran synchronously ON the event
    loop, so every request in the process - including plain sugra_ API keys
    that need no JWT at all - waited behind it."""
    auth, _ = _authenticator()

    def _stall(_token):
        time.sleep(0.8)  # sync stall, as PyJWKClient does on a cold cache
        raise RuntimeError("no key")

    auth._validate_jwt = _stall  # type: ignore[method-assign]

    async def _jwt_resolve():
        try:
            await auth.resolve("not-a-sugra-token")
        except Exception:
            pass

    jwt_task = asyncio.create_task(_jwt_resolve())
    await asyncio.sleep(0.05)  # let the JWT path enter its stall
    started = time.monotonic()
    resolved = await auth.resolve("sugra_direct_key")
    elapsed = time.monotonic() - started
    await jwt_task
    assert resolved.api_key == "sugra_direct_key"
    assert elapsed < 0.3, (
        f"raw-key resolve took {elapsed:.2f}s behind a JWKS stall")


async def test_activity_validation_cached_per_jti() -> None:
    """A PASSING activity validation is trusted for the TTL window."""
    auth, fake = _authenticator()
    claims = _JwtClaims(user_id=9, access_token_id="jti-1")
    await auth._validate_mcp_access(claims)
    await auth._validate_mcp_access(claims)
    assert fake.post_calls == 1, (
        f"activity validation hit the internal endpoint {fake.post_calls} "
        "times for one jti inside the TTL")


async def test_activity_validation_failure_is_never_cached() -> None:
    auth, fake = _authenticator()

    class _Deny(_FakeHttp):
        async def post(self, url, headers=None, json=None):
            self.post_calls += 1
            resp = _FakeResponse({"error": "no access"})
            resp.status_code = 403
            return resp

    deny = _Deny()
    auth._http = deny
    claims = _JwtClaims(user_id=9, access_token_id="jti-2")
    for _ in range(2):
        with pytest.raises(Exception):
            await auth._validate_mcp_access(claims)
    assert deny.post_calls == 2, "a DENIAL must never be served from cache"
