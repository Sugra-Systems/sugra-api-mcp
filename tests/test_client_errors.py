"""SugraClient transport-failure error contract (MCP-Imp-1).

Field-test defect D2: httpx exceptions propagated through FastMCP and
surfaced as "Error executing tool call_endpoint:" with an empty message,
making timeout vs connect-refused vs 5xx indistinguishable. The client must
catch every transport failure class and return a structured dict instead.
One test per failure class: timeout, connect error, mid-stream disconnect,
4xx, 5xx (+ Retry-After), plus the success path staying unmodified.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx

from sugra_api_mcp.client import SugraClient
from sugra_api_mcp.config import Config

_TRANSPORT_ERROR_KEYS = {"error", "reason", "status_code", "elapsed_ms", "url", "retry_hint"}


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> SugraClient:
    config = Config(api_base="https://api.test", api_key="test-key", timeout=0.25)
    return SugraClient(config, transport=httpx.MockTransport(handler))


# ---- transport failures: timeout class ----


async def test_read_timeout_returns_structured_error() -> None:
    """httpx.ReadTimeout stringifies to "" - the exact defect-D2 trigger.

    The structured dict must carry the error code, the class name as reason
    (never an empty string), elapsed telemetry, and the configured budget.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    client = _client(handler)
    try:
        result = await client.get("/api/v1/network/asn", params={"country": "GE"})
    finally:
        await client.aclose()

    assert result["error"] == "upstream_timeout"
    assert result["reason"] == "ReadTimeout"  # empty message -> class name only
    assert result["status_code"] is None
    assert isinstance(result["elapsed_ms"], int)
    assert result["elapsed_ms"] >= 0
    assert result["timeout_s"] == 0.25
    assert "/api/v1/network/asn" in result["url"]
    assert "retry" in result["retry_hint"].lower()
    assert set(result) >= _TRANSPORT_ERROR_KEYS


async def test_connect_timeout_maps_to_upstream_timeout() -> None:
    """httpx.ConnectTimeout subclasses TimeoutException, NOT ConnectError.

    Exception-handler ordering must classify it as a timeout; getting this
    wrong would silently re-route connect timeouts into the generic branch.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    client = _client(handler)
    try:
        result = await client.get("/api/v1/quotes/AAPL/price")
    finally:
        await client.aclose()

    assert result["error"] == "upstream_timeout"
    assert result["reason"] == "ConnectTimeout: timed out"


# ---- transport failures: connect / mid-stream classes ----


async def test_connect_error_returns_structured_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 111] Connection refused", request=request)

    client = _client(handler)
    try:
        result = await client.get("/api/v1/quotes/AAPL/price")
    finally:
        await client.aclose()

    assert result["error"] == "upstream_connect_error"
    assert result["reason"].startswith("ConnectError")
    assert result["status_code"] is None
    assert "timeout_s" not in result
    assert set(result) >= _TRANSPORT_ERROR_KEYS


async def test_mid_stream_disconnect_returns_structured_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.RemoteProtocolError("peer closed connection", request=request)

    client = _client(handler)
    try:
        result = await client.post("/api/v1/network/bulk/ip", json={"ips": ["1.1.1.1"]})
    finally:
        await client.aclose()

    assert result["error"] == "upstream_transport_error"
    assert result["reason"] == "RemoteProtocolError: peer closed connection"
    assert result["status_code"] is None
    assert set(result) >= _TRANSPORT_ERROR_KEYS


async def test_transport_error_without_request_falls_back_to_config_url() -> None:
    """httpx raises RuntimeError from exc.request when no request is attached;
    the error dict must still carry a usable URL built from config."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("socket read failure")  # no request= kwarg

    client = _client(handler)
    try:
        result = await client.get("/api/v1/fred/series")
    finally:
        await client.aclose()

    assert result["error"] == "upstream_transport_error"
    assert result["url"] == "https://api.test/api/v1/fred/series"


# ---- HTTP status errors keep their structure and gain telemetry ----


async def test_http_4xx_keeps_payload_error_and_gains_elapsed_ms() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "Unknown ticker"}, request=request)

    client = _client(handler)
    try:
        result = await client.get("/api/v1/quotes/NOPE/price")
    finally:
        await client.aclose()

    assert result["error"] == "Unknown ticker"
    assert result["status_code"] == 404
    assert isinstance(result["elapsed_ms"], int)
    assert "retry_after" not in result


async def test_http_5xx_non_json_body_is_truncated_to_500_chars() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text="<html>" + "x" * 600, request=request)

    client = _client(handler)
    try:
        result = await client.get("/api/v1/network/asn")
    finally:
        await client.aclose()

    assert result["status_code"] == 502
    assert len(result["error"]) <= 500
    assert isinstance(result["elapsed_ms"], int)


async def test_retry_after_seconds_header_is_parsed_to_int() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": "Rate limit exceeded"},
            headers={"Retry-After": "12"},
            request=request,
        )

    client = _client(handler)
    try:
        result = await client.get("/api/v1/quotes/AAPL/price")
    finally:
        await client.aclose()

    assert result["status_code"] == 429
    assert result["retry_after"] == 12


async def test_retry_after_http_date_header_passes_through_raw() -> None:
    http_date = "Wed, 21 Oct 2026 07:28:00 GMT"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"error": "Service unavailable"},
            headers={"Retry-After": http_date},
            request=request,
        )

    client = _client(handler)
    try:
        result = await client.get("/api/v1/network/asn")
    finally:
        await client.aclose()

    assert result["status_code"] == 503
    assert result["retry_after"] == http_date


# ---- success path stays pristine ----


async def test_success_payload_is_unmodified_no_telemetry_keys() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"v": 1}], "meta": {}}, request=request)

    client = _client(handler)
    try:
        result = await client.get("/api/v1/quotes/AAPL/price")
    finally:
        await client.aclose()

    assert result == {"data": [{"v": 1}], "meta": {}}
