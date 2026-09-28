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


async def test_retry_after_non_ascii_digit_header_does_not_raise() -> None:
    """str.isdigit() is True for unicode digits (superscript two) that int()
    rejects with ValueError. _retry_after runs OUTSIDE the transport
    try/except, so without the isascii() gate a malformed proxy header would
    raise through client.request() - reintroducing defect D2 on surfaces
    without a gateway safety net (the entity tools call the client directly).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # Header value as BYTES - the wire form. httpx decodes b"\xc2\xb2"
        # into "²" (superscript two), for which str.isdigit() is True but
        # int() raises ValueError.
        return httpx.Response(
            503,
            json={"error": "Service unavailable"},
            headers=[(b"Retry-After", "²".encode())],
            request=request,
        )

    client = _client(handler)
    try:
        result = await client.get("/api/v1/entity/lei/X/screen")
    finally:
        await client.aclose()

    assert result["status_code"] == 503
    assert result["retry_after"] == "²"  # raw passthrough, no crash


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


# ---- the request id that ties a failure to the server's own logs ----


async def test_error_carries_the_request_id_when_the_api_sends_one() -> None:
    """A user reporting a failure can quote one value and have the exact call
    located, instead of describing what they think happened."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"error": "upstream_unavailable"},
            headers={"X-Request-ID": "req_01HZY7"},
            request=request,
        )

    client = _client(handler)
    try:
        result = await client.get("/api/v1/quotes/AAPL/price")
    finally:
        await client.aclose()

    assert result["request_id"] == "req_01HZY7"
    assert result["status_code"] == 503


async def test_the_request_id_key_is_absent_when_the_header_is_not_sent() -> None:
    """Absent rather than null: a key that is always present but usually empty
    trains readers to ignore it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not_found"}, request=request)

    client = _client(handler)
    try:
        result = await client.get("/api/v1/quotes/AAPL/price")
    finally:
        await client.aclose()

    assert "request_id" not in result


# ---- A redirect is an HTTP failure with a status, not an empty string ----


async def test_http_3xx_is_a_structured_http_error() -> None:
    """The client does not follow redirects, and a 307 carries no JSON body.
    The old `>= 400` gate let it through the success path, where the failed
    JSON decode produced {"error": ""} - a failure with an EMPTY reason, no
    status and no url. A non-2xx answer is an HTTP failure whatever its class."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            307, headers={"Location": "https://api.test/api/v1/kalshi/events/"}, request=request
        )

    client = _client(handler)
    try:
        result = await client.get("/api/v1/kalshi/events")
    finally:
        await client.aclose()

    assert result["error"] == "HTTP 307"
    assert result["status_code"] == 307
    assert result["url"] == "https://api.test/api/v1/kalshi/events"
    assert isinstance(result["elapsed_ms"], int)


# ---- size enforcement stays the default for every non-gateway caller ----
#
# gateway.call_endpoint is the ONE caller that shapes its own response
# (a fields/limit projection) after getting it back from the client, so it
# is the one caller that needs to measure size AFTER that projection rather
# than on the client's raw body. Every other caller - the agent-plane tools
# (resolve_entity, get_snapshot, get_timeseries) and the entity lookup/screen
# tools - calls client.get/post directly and never shapes anything
# afterward: for them the raw body IS the whole response, so the cap has to
# apply right here, by default, exactly as it did before gateway's own
# opt-out was introduced.


async def test_default_get_still_enforces_the_size_cap_on_a_non_gateway_call() -> None:
    """A plain client.get with no explicit enforce_size (what every
    non-gateway caller does) must still refuse a body with nothing to
    shrink, not pass it through whole."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"blob": "x" * 200_000}, "meta": {}}, request=request)

    client = _client(handler)
    try:
        result = await client.get("/api/v1/some/big/payload")
    finally:
        await client.aclose()

    assert result.get("error") == "response_too_large"
    assert "estimated_tokens" in result


async def test_default_post_cuts_a_large_list_body_not_passing_it_through() -> None:
    """Same backstop over client.post (what the agent-plane tools and the
    entity screen/lookup tools call), over a shape the gate CAN shrink."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "data": [{"id": i, "name": f"item_{i}", "desc": "x" * 100} for i in range(2000)],
            "meta": {"source": "test"},
        }
        return httpx.Response(200, json=body, request=request)

    client = _client(handler)
    try:
        result = await client.post("/api/v1/some/big/list", json={})
    finally:
        await client.aclose()

    assert "error" not in result
    assert len(result["data"]) < 2000
    assert "truncated" in result["meta"]


async def test_enforce_size_false_opts_out_per_call_not_client_wide() -> None:
    """gateway.call_endpoint passes enforce_size=False so it can measure
    AFTER its own fields/limit projection - confirm the opt-out is a
    per-call keyword, not a client-wide switch that would also silence the
    backstop for every other caller."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"blob": "x" * 200_000}, "meta": {}}, request=request)

    client = _client(handler)
    try:
        raw = await client.get("/api/v1/some/big/payload", enforce_size=False)
    finally:
        await client.aclose()

    assert raw == {"data": {"blob": "x" * 200_000}, "meta": {}}
