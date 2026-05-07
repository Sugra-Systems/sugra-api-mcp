"""Sanity tests: tools import and register correctly."""

from __future__ import annotations


def test_tools_register(monkeypatch):
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    import asyncio

    from sugra_api_mcp import tools  # noqa: F401
    from sugra_api_mcp.server import mcp
    tool_list = asyncio.run(mcp.list_tools())
    assert len(tool_list) == 5
    names = {t.name for t in tool_list}
    expected = {
        "search_endpoints",
        "describe_endpoint",
        "call_endpoint",
        "list_toolsets",
        "list_sources",
    }
    assert names == expected, f"Mismatch: missing={expected - names}, extra={names - expected}"


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
