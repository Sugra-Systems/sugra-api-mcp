"""A failure the tools build themselves is held to _ERROR_CHARS, every key kept.

The client bounds its own failures; the gateway and the entity tools build or
restate others, and some echo what the caller sent: an operation_id, a
parameter key, a path value. Each such exit is driven with one value of
200,000 characters. The failure keeps its code, cuts only the long value, marks
it `<key>_truncated`, and stays within _ERROR_CHARS. Offline: the bundled
catalog or a small one, no HTTP.
"""

from __future__ import annotations

from typing import Any

from sugra_api_mcp.catalog.models import Catalog, Endpoint
from sugra_api_mcp.client import _ERROR_CHARS, response_chars
from sugra_api_mcp.tools import entities, gateway

LONG = 200_000


def _held(result: dict[str, Any], code: str, long_key: str) -> None:
    assert result["error"] == code
    assert result[f"{long_key}_truncated"] is True
    assert response_chars(result) <= _ERROR_CHARS


def _no_client(monkeypatch) -> None:
    def _refuse() -> Any:
        raise AssertionError("no request may be made")

    monkeypatch.setattr(gateway, "get_client", _refuse)


async def test_describe_endpoint_unknown_operation_id_is_held() -> None:
    result = await gateway.describe_endpoint(operation_id="x" * LONG)

    _held(result, "unknown_operation_id", "operation_id")


async def test_call_endpoint_unknown_operation_id_is_held(monkeypatch) -> None:
    _no_client(monkeypatch)

    result = await gateway.call_endpoint(operation_id="x" * LONG)

    _held(result, "unknown_operation_id", "operation_id")


async def test_unknown_parameters_is_held(monkeypatch) -> None:
    _no_client(monkeypatch)

    result = await gateway.call_endpoint(
        operation_id="crypto_coin_id_price", params={"coin_id": "bitcoin", "k" * LONG: 1})

    _held(result, "unknown_parameters", "unknown")
    assert result["operation_id"] == "crypto_coin_id_price"
    assert result["accepted"] == ["coin_id", "vs_currency"]


async def test_unresolved_path_parameters_is_held(monkeypatch) -> None:
    _no_client(monkeypatch)

    result = await gateway.call_endpoint(
        operation_id="crypto_coin_id_price", params={"coin_id": "{" + "a" * LONG})

    _held(result, "unresolved_path_parameters", "path")
    assert result["operation_id"] == "crypto_coin_id_price"


async def test_call_endpoint_tool_execution_failed_is_held(monkeypatch) -> None:
    """With the catalog failing to load, the operation_id echoed is the caller's."""
    def _broken() -> Any:
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(gateway, "load_catalog", _broken)

    result = await gateway.call_endpoint(operation_id="x" * LONG)

    _held(result, "tool_execution_failed", "operation_id")
    assert result["exception_type"] == "RuntimeError"
    assert isinstance(result["elapsed_ms"], int)


def _gauge(operation_id: str, *names: str) -> Endpoint:
    return Endpoint.from_dict({
        "operation_id": operation_id,
        "method": "GET",
        "path": f"/api/v1/{operation_id}",
        "summary": operation_id,
        "parameters": [{"name": name, "location": "query", "required": False} for name in names],
    })


async def test_fetch_data_alternatives_are_added_within_the_bound(monkeypatch) -> None:
    """A refusal already at the bound stays within it once alternatives join."""
    endpoints = [_gauge("top_op"), *(_gauge(f"filler_{index}", "gauge_id") for index in range(3))]
    hits = [{"operation_id": "top_op", "score": 40},
            *({"operation_id": f"filler_{index}", "score": 10} for index in range(3))]
    catalog = Catalog(source="test", endpoints=endpoints)

    async def _search(_catalog, _query, **_kwargs):
        return hits

    async def _call_endpoint(*, operation_id: str, **_kwargs: Any) -> dict[str, Any]:
        return {"error": "unknown_parameters", "operation_id": operation_id,
                "unknown": ["gauge_id"], "hint": "h" * LONG}

    monkeypatch.setattr(gateway, "load_catalog", lambda: catalog)
    monkeypatch.setattr(gateway, "_search_off_loop", _search)
    monkeypatch.setattr(gateway, "call_endpoint", _call_endpoint)
    _no_client(monkeypatch)

    result = await gateway.fetch_data(query="gauge", params={"gauge_id": "G1"})

    _held(result, "unknown_parameters", "hint")
    assert result["alternatives"] == ["filler_0", "filler_1", "filler_2"]
    assert result["unknown"] == ["gauge_id"]


def test_clean_error_is_held() -> None:
    cleaned = entities._clean_error(
        {"error": "e" * LONG, "status_code": 502, "url": "https://api.test/" + "u" * LONG})

    assert cleaned["error"].startswith("eee") and cleaned["error"].endswith("...")
    assert cleaned["error_truncated"] is True
    assert cleaned["detail_truncated"] is True
    assert response_chars(cleaned) <= _ERROR_CHARS
    assert cleaned["status_code"] == 502


def test_clean_error_of_an_error_that_is_not_text_is_held() -> None:
    """str() of a dict can outgrow the client's bounded original; the restated
    error and the detail that repeats it are both held."""
    cleaned = entities._clean_error({"error": {"message": "m" * LONG}})

    assert cleaned["error_truncated"] is True
    assert cleaned["detail_truncated"] is True
    assert response_chars(cleaned) <= _ERROR_CHARS
