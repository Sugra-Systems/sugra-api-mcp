"""Sanity tests: tools import and register correctly."""

from __future__ import annotations


def test_tools_register(monkeypatch):
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    import asyncio

    from sugra_api_mcp import tools  # noqa: F401
    from sugra_api_mcp.server import mcp
    tool_list = asyncio.run(mcp.list_tools())
    assert len(tool_list) == 10
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
        "list_plans",
        "buy_plan",
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


def test_config_allows_missing_api_key(monkeypatch):
    """Startup must never fail on a missing key (keyless stdio start).

    The key requirement is enforced at call time by server.get_client, which
    hands out the keyless stand-in client returning the structured
    missing_api_key error - see tests/test_keyless.py.
    """
    monkeypatch.delenv("SUGRA_API_KEY", raising=False)

    from sugra_api_mcp.config import load_config

    cfg = load_config()
    assert cfg.api_key == ""
    # The legacy keyword is accepted and still does not raise.
    assert load_config(require_api_key=True).api_key == ""


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


def _forecast_row(i: int, *, daily: bool) -> dict:
    key = "date" if daily else "time"
    row = {key: f"2026-09-{28 + i:02d}" if daily else f"2026-09-{28 + i // 24:02d}T{i % 24:02d}:00"}
    for name in ("temperature_2m", "apparent_temperature", "relative_humidity_2m",
                 "precipitation", "wind_speed_10m", "wind_direction_10m", "uv_index",
                 "cloud_cover", "visibility", "pressure_msl"):
        row[name] = round(10.0 + i * 0.37, 2)
    row["condition"] = "Partly cloudy"
    return row


def _forecast_payload(days: int) -> dict:
    """A recorded-shape (not live) v2 weather forecast envelope: `data.hourly`
    and `data.daily` are RECORD LISTS, neither name in the fields/limit
    records-list allowlist - the shape this fix targets."""
    return {
        "data": {
            "latitude": 35.68,
            "longitude": 139.69,
            "timezone": "Asia/Tokyo",
            "hourly": [_forecast_row(i, daily=False) for i in range(days * 24)],
            "daily": [_forecast_row(i, daily=True) for i in range(days)],
        },
        "meta": {"source": "openmeteo", "cached": False},
    }


def test_size_limit_cuts_nested_record_list_with_marker_naming_the_untouched_sibling():
    """A dict `data` with a giant `hourly` list and a small, untouched
    `daily` list must be CUT (hourly shrunk, marker attached) rather than
    rejected wholesale, and the marker must point at the sibling that a
    narrower request would have kept whole."""
    import json

    from sugra_api_mcp.client import MAX_RESPONSE_CHARS, _enforce_size_limit

    payload = _forecast_payload(16)
    assert len(json.dumps(payload)) > MAX_RESPONSE_CHARS

    result = _enforce_size_limit(payload)

    assert "error" not in result
    assert len(json.dumps(result)) <= MAX_RESPONSE_CHARS
    truncated = result["meta"]["truncated"]
    assert truncated["fields"]["hourly"]["kept_count"] < truncated["fields"]["hourly"]["original_count"]
    assert "daily" not in truncated["fields"]
    assert len(result["data"]["daily"]) == 16  # untouched, whole
    assert "daily" in truncated["retry_hint"]


def test_size_limit_error_message_agrees_with_the_char_gate_it_enforces():
    """The old message named only the 25000-token directory ceiling, so a
    response that tripped the actual (char-based) gate while estimating
    under 25000 tokens read as self-contradictory: a real-world call once
    saw a 21659-token estimate rejected against a stated 25000 limit."""
    from sugra_api_mcp.client import MAX_RESPONSE_CHARS, _enforce_size_limit

    # Just over OUR char cap but still under the 25000-token directory
    # ceiling in estimated tokens - the exact band the old message
    # contradicted itself in.
    payload = {"data": {"blob": "x" * 90_000}, "meta": {}}
    result = _enforce_size_limit(payload)

    assert str(MAX_RESPONSE_CHARS) in result["message"]
    assert result["estimated_tokens"] < 25_000  # the actual defect: under the directory
    assert result["estimated_tokens"] > MAX_RESPONSE_CHARS // 4  # ceiling, over OUR cap
