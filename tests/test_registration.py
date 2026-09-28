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


def test_size_limit_top_level_single_oversized_item_is_cut_to_empty_not_forced_through():
    """The old cutter floored `kept` at 1, so a single item bigger than the
    entire cap was still retained and shipped over MAX_RESPONSE_CHARS. It
    must now be cut to zero rows, with an accurate marker, instead."""
    import json

    from sugra_api_mcp.client import MAX_RESPONSE_CHARS, _enforce_size_limit

    payload = {"data": [{"blob": "x" * (MAX_RESPONSE_CHARS * 2)}], "meta": {}}
    result = _enforce_size_limit(payload, "test://url")

    assert len(json.dumps(result)) <= MAX_RESPONSE_CHARS
    assert "error" not in result
    assert result["meta"]["truncated"]["kept_count"] == 0
    assert result["meta"]["truncated"]["original_count"] == 1


def test_size_limit_nested_cutter_accounts_for_marker_overhead_beyond_the_reserve():
    """The old cutter measured the trimmed payload BEFORE adding
    meta.truncated and never re-checked after. A field name long enough to
    make that marker bigger than the fixed 500-char reserve used to leave
    the final payload over the cap regardless."""
    import json

    from sugra_api_mcp.client import MAX_RESPONSE_CHARS, _enforce_size_limit

    huge_key = "k" * 5000
    payload = {
        "data": {huge_key: [{"v": i, "pad": "x" * 40} for i in range(3000)]},
        "meta": {},
    }
    assert len(json.dumps(payload)) > MAX_RESPONSE_CHARS

    result = _enforce_size_limit(payload, "test://url")

    assert len(json.dumps(result)) <= MAX_RESPONSE_CHARS


def test_size_error_names_exhausted_lists_not_missing_ones_when_a_list_existed():
    """A list that WAS shrunk to nothing, with a huge fixed sibling still
    over the cap on its own, must not be reported as "no list field was
    found" - that list existed and was exhausted, which misdirects a retry
    into hunting for a list that was never the problem."""
    import json

    from sugra_api_mcp.client import MAX_RESPONSE_CHARS, _enforce_size_limit

    payload = {
        "data": {
            "small_list": [{"i": i} for i in range(5)],
            "big_sibling": "y" * (MAX_RESPONSE_CHARS * 2),
        },
        "meta": {},
    }
    assert len(json.dumps(payload)) > MAX_RESPONSE_CHARS

    result = _enforce_size_limit(payload, "test://url")

    assert result["error"] == "response_too_large"
    assert "no list field was found" not in result["message"]
    assert "shrunk to nothing" in result["message"]


def test_truncated_reason_names_the_gateway_cap_not_the_token_ceiling():
    """Both truncation shapes used to keep the same token-ceiling reason
    this change corrects for outright rejections - contradictory for a
    response whose estimate is under 25000 tokens. Both must name the
    gateway's own size cap instead."""
    from sugra_api_mcp.client import _enforce_size_limit

    top_level = _enforce_size_limit(
        {
            "data": [{"id": i, "name": f"item_{i}", "desc": "x" * 100} for i in range(2000)],
            "meta": {"source": "test"},
        },
        "test://url",
    )
    nested = _enforce_size_limit(_forecast_payload(16))

    assert "error" not in top_level
    assert "error" not in nested
    assert top_level["meta"]["truncated"]["reason"] != "exceeds_mcp_25k_token_limit"
    assert nested["meta"]["truncated"]["reason"] != "exceeds_mcp_25k_token_limit"
    assert "25k" not in top_level["meta"]["truncated"]["reason"]
    assert "25k" not in nested["meta"]["truncated"]["reason"]
