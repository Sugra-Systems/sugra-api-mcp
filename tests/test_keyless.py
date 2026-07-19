"""Keyless stdio start: the server must boot and introspect without SUGRA_API_KEY.

Directory evaluators and several MCP clients launch the server WITHOUT env
configured, expect initialize + tools/list introspection to work, and only then
let the user configure credentials. The key requirement therefore lives at call
time (server.get_client hands out a keyless stand-in returning the structured
missing_api_key error), never at process startup.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from sugra_api_mcp import server
from sugra_api_mcp.catalog.builder import build_catalog_from_openapi
from sugra_api_mcp.client import SugraClient
from sugra_api_mcp.config import MISSING_API_KEY_HINT
from sugra_api_mcp.tools import entities, gateway

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "openapi_minimal.json"

EXPECTED_TOOL_COUNT = 8
EXPECTED_PROMPT_COUNT = 6
EXPECTED_RESOURCE_COUNT = 4


def _keyless_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("SUGRA_API_KEY", None)
    return env


def _fixture_catalog():
    return build_catalog_from_openapi(json.loads(FIXTURE.read_text(encoding="utf-8")))


@pytest.fixture
def keyless(monkeypatch):
    """No key in env, no per-request key, no cached shared client."""
    monkeypatch.delenv("SUGRA_API_KEY", raising=False)
    monkeypatch.setattr(server, "_shared_client", None)
    return monkeypatch


# ---------------------------------------------------------------------------
# Startup and introspection succeed keyless (subprocess = clean stdio shape).
# ---------------------------------------------------------------------------


def test_stdio_registration_and_introspection_keyless_subprocess():
    """Registration + list_tools/list_prompts/list_resources with no key set.

    Runs in a SUBPROCESS (pattern from test_metadata_sync) so the import-time
    path is exercised exactly as an MCP client launching the process would."""
    code = (
        "import asyncio, os\n"
        "assert not os.environ.get('SUGRA_API_KEY'), 'test env leaked a key'\n"
        "import sugra_api_mcp.tools\n"
        "from sugra_api_mcp.server import mcp\n"
        "tools = asyncio.run(mcp.list_tools())\n"
        "prompts = asyncio.run(mcp.list_prompts())\n"
        "resources = asyncio.run(mcp.list_resources())\n"
        "print(len(tools), len(prompts), len(resources))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=_keyless_env(),
        check=True,
    )
    counts = [int(part) for part in out.stdout.split()]
    assert counts == [EXPECTED_TOOL_COUNT, EXPECTED_PROMPT_COUNT, EXPECTED_RESOURCE_COUNT]


def test_doctor_keyless_warns_instead_of_crashing():
    result = subprocess.run(
        [sys.executable, "-m", "sugra_api_mcp", "doctor"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=_keyless_env(),
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["api_key"] == "missing"
    assert payload["endpoint_count"] > 0
    assert "WARNING: SUGRA_API_KEY is not set" in result.stderr
    assert MISSING_API_KEY_HINT in result.stderr


def test_doctor_with_key_reports_set_and_stays_quiet():
    env = _keyless_env()
    env["SUGRA_API_KEY"] = "sugra_test_dummy"
    result = subprocess.run(
        [sys.executable, "-m", "sugra_api_mcp", "doctor"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["api_key"] == "set"
    assert "WARNING" not in result.stderr


# ---------------------------------------------------------------------------
# Network-backed tools return the structured missing_api_key error keyless.
# ---------------------------------------------------------------------------


async def test_call_endpoint_keyless_returns_missing_api_key(keyless) -> None:
    keyless.setattr(gateway, "load_catalog", _fixture_catalog)

    result = await gateway.call_endpoint("quotes_symbol_price", params={"symbol": "AAPL"})

    assert result == {"error": "missing_api_key", "hint": MISSING_API_KEY_HINT}


async def test_fetch_data_keyless_returns_missing_api_key(keyless) -> None:
    keyless.setattr(gateway, "load_catalog", _fixture_catalog)

    result = await gateway.fetch_data("quote price for a symbol", params={"symbol": "AAPL"})

    assert result == {"error": "missing_api_key", "hint": MISSING_API_KEY_HINT}


async def test_sugra_entity_screen_keyless_returns_missing_api_key(keyless) -> None:
    result = await entities.sugra_entity_screen("ACME HOLDINGS")

    assert result["error"] == "missing_api_key"
    assert result["hint"] == MISSING_API_KEY_HINT


async def test_sugra_entity_lookup_keyless_returns_missing_api_key(keyless) -> None:
    result = await entities.sugra_entity_lookup("lei", "529900T8BM49AURSDO55")

    assert result["error"] == "missing_api_key"
    assert result["hint"] == MISSING_API_KEY_HINT


# ---------------------------------------------------------------------------
# Catalog-only tools keep returning real results keyless (no network needed).
# ---------------------------------------------------------------------------


async def test_search_endpoints_keyless_returns_results(keyless) -> None:
    result = await gateway.search_endpoints("NASDAQ futures")

    assert result["total_matched"] > 0
    assert any(hit["operation_id"] == "cot_financial" for hit in result["results"])


async def test_describe_endpoint_keyless_returns_schema(keyless) -> None:
    described = await gateway.describe_endpoint("cot_financial")

    assert described.get("error") is None
    assert described["operation_id"] == "cot_financial"
    assert "agent_hints" in described


async def test_list_toolsets_keyless_returns_counts(keyless) -> None:
    result = await gateway.list_toolsets()

    assert result["total_endpoints"] > 0
    assert result["toolsets"]


async def test_list_sources_keyless_returns_families(keyless) -> None:
    result = await gateway.list_sources()

    assert result["endpoint_count"] > 0
    assert result["source_families"]


# ---------------------------------------------------------------------------
# With a key available, behavior is unchanged.
# ---------------------------------------------------------------------------


class FakeClient:
    """Fake client pattern from test_gateway: records calls, returns data."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("GET", path, params))
        return {"data": [{"symbol": "AAPL", "price": 200}], "meta": {}}


async def test_call_endpoint_with_key_behavior_unchanged(monkeypatch) -> None:
    fake = FakeClient()
    monkeypatch.setenv("SUGRA_API_KEY", "sugra_test_dummy")
    monkeypatch.setattr(gateway, "load_catalog", _fixture_catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: fake)

    result = await gateway.call_endpoint("quotes_symbol_price", params={"symbol": "AAPL"})

    assert fake.calls == [("GET", "/api/v1/quotes/AAPL/price", {})]
    assert result["data"] == [{"symbol": "AAPL", "price": 200}]


async def test_get_client_with_env_key_builds_real_client(keyless) -> None:
    keyless.setenv("SUGRA_API_KEY", "sugra_test_dummy")

    client = server.get_client()
    try:
        assert isinstance(client, SugraClient)
        assert client._config.api_key == "sugra_test_dummy"
    finally:
        await client.aclose()


def test_get_client_keyless_is_not_cached_as_shared(keyless) -> None:
    """The keyless stand-in must never occupy the shared-client slot: once the
    environment gains a key, the next call builds a real client."""
    first = server.get_client()
    assert isinstance(first, server._KeylessClient)
    assert server._shared_client is None
