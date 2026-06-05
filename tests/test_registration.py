"""Sanity tests: tools import and register correctly."""

from __future__ import annotations


def test_tools_register(monkeypatch):
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    import asyncio

    from sugra_api_mcp import tools  # noqa: F401
    from sugra_api_mcp.server import mcp
    tool_list = asyncio.run(mcp.list_tools())
    assert len(tool_list) == 8
    names = {t.name for t in tool_list}
    expected = {
        "fetch_data",
        "search_endpoints",
        "describe_endpoint",
        "call_endpoint",
        "list_toolsets",
        "list_sources",
        "sugra_entity_screen",
        "sugra_entity_lookup",
    }
    assert names == expected, f"Mismatch: missing={expected - names}, extra={names - expected}"


def test_tools_advertise_oauth_security_schemes_for_chatgpt(monkeypatch):
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    import asyncio

    from sugra_api_mcp import tools  # noqa: F401
    from sugra_api_mcp.server import OAUTH_SECURITY_SCHEMES, mcp

    tool_list = asyncio.run(mcp.list_tools())

    assert tool_list
    for tool in tool_list:
        dumped = tool.model_dump(by_alias=True)
        assert dumped["securitySchemes"] == OAUTH_SECURITY_SCHEMES
        assert dumped["_meta"]["securitySchemes"] == OAUTH_SECURITY_SCHEMES


def test_streamable_http_public_discovery_exposes_oauth_security_schemes(monkeypatch):
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    import json
    import re

    from starlette.testclient import TestClient

    from sugra_api_mcp import tools  # noqa: F401
    from sugra_api_mcp.auth import Authenticator, AuthMiddleware
    from sugra_api_mcp.config import AuthConfig
    from sugra_api_mcp.server import OAUTH_SECURITY_SCHEMES, mcp

    app = mcp.streamable_http_app()
    app.add_middleware(
        AuthMiddleware,
        authenticator=Authenticator(
            AuthConfig(
                app_url="https://app.sugra.ai",
                jwks_url="https://app.sugra.ai/oauth/jwks.json",
                internal_token="test-internal-token",
            )
        ),
    )

    headers = {"accept": "application/json, text/event-stream"}

    with TestClient(app, base_url="http://localhost:8000") as client:
        init_response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            },
            headers=headers,
        )

        assert init_response.status_code == 200
        session_id = init_response.headers.get("mcp-session-id")
        assert session_id

        session_headers = {**headers, "mcp-session-id": session_id}
        initialized_response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            headers=session_headers,
        )

        assert initialized_response.status_code == 202

        tools_response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=session_headers,
        )

        assert tools_response.status_code == 200
        match = re.search(r"^data: (.+)$", tools_response.text, re.MULTILINE)
        assert match
        payload = json.loads(match.group(1))
        first_tool = payload["result"]["tools"][0]
        assert first_tool["securitySchemes"] == OAUTH_SECURITY_SCHEMES
        assert first_tool["_meta"]["securitySchemes"] == OAUTH_SECURITY_SCHEMES


def test_config_requires_api_key(monkeypatch):
    monkeypatch.delenv("SUGRA_API_KEY", raising=False)
    import pytest

    from sugra_api_mcp.config import load_config
    with pytest.raises(RuntimeError, match="SUGRA_API_KEY"):
        load_config()


def test_config_defaults(monkeypatch):
    monkeypatch.setenv("SUGRA_API_KEY", "sugra_test_123")
    monkeypatch.delenv("SUGRA_API_BASE", raising=False)
    monkeypatch.delenv("SUGRA_TIMEOUT", raising=False)
    from sugra_api_mcp.config import load_config
    cfg = load_config()
    assert cfg.api_key == "sugra_test_123"
    assert cfg.api_base == "https://sugra.ai"
    assert cfg.timeout == 30.0


def test_size_limit_truncates_list():
    import json

    from sugra_api_mcp.client import MAX_RESPONSE_CHARS, _enforce_size_limit

    payload = {
        "data": [{"id": i, "name": f"item_{i}", "desc": "x" * 100} for i in range(2000)],
        "meta": {"source": "test"},
    }
    result = _enforce_size_limit(payload, "test://url")

    assert len(json.dumps(result)) <= MAX_RESPONSE_CHARS
    assert "truncated" in result["meta"]
    assert result["meta"]["truncated"]["original_count"] == 2000
    assert result["meta"]["truncated"]["kept_count"] < 2000
    assert "retry_hint" in result["meta"]["truncated"]


def test_size_limit_errors_on_big_dict():
    from sugra_api_mcp.client import _enforce_size_limit

    payload = {"data": {"blob": "x" * 200_000}, "meta": {}}
    result = _enforce_size_limit(payload, "test://url")

    assert result.get("error") == "response_too_large"
    assert "estimated_tokens" in result


def test_size_limit_passthrough_small():
    from sugra_api_mcp.client import _enforce_size_limit

    payload = {"data": {"price": 75000}, "meta": {"source": "coingecko"}}
    result = _enforce_size_limit(payload, "test://url")
    assert result == payload
