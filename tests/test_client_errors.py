"""SugraClient transport-failure error contract (MCP-Imp-1).

Field-test defect D2: httpx exceptions propagated through FastMCP and
surfaced as "Error executing tool call_endpoint:" with an empty message,
making timeout vs connect-refused vs 5xx indistinguishable. The client must
catch every transport failure class and return a structured dict instead.
One test per failure class: timeout, connect error, mid-stream disconnect,
4xx, 5xx (+ Retry-After), plus the success path staying unmodified.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from sugra_api_mcp.client import (
    _DETAIL_CHARS,
    PLANS_PAGE_URL,
    SugraClient,
    _detail_fields,
    response_chars,
)
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


# ---- the API's own explanation reaches the caller, and never sells ----

_QUOTA_DETAIL = (
    "Daily limit of 50 requests reached. Current plan: free. "
    "Upgrade for a higher daily limit: https://app.sugra.ai/plans?from=api.ratelimit.upgrade"
)
_QUOTA_HEADERS = {
    "Retry-After": "3600",
    "X-RateLimit-Limit": "50",
    "X-RateLimit-Remaining": "0",
    "X-RateLimit-Reset": "2026-09-29T23:59:59Z",
    "X-Request-ID": "req_quota",
}


async def _answer(status: int, body: Any, headers: dict[str, str] | None = None) -> dict[str, Any]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, headers=headers or {}, request=request)

    client = _client(handler)
    try:
        return await client.get("/api/v1/quotes/AAPL/price")
    finally:
        await client.aclose()


async def test_the_daily_limit_refusal_is_plain_information_for_the_model() -> None:
    result = await _answer(429, {"detail": _QUOTA_DETAIL}, _QUOTA_HEADERS)

    assert result["error"] == (
        "The daily request limit of 50 for this Sugra account on the free plan has been "
        "reached. It resets at 00:00 UTC. Sugra plans and their daily limits are described "
        f"at {PLANS_PAGE_URL}."
    )
    assert result["reason"] == "daily_limit_reached"
    assert result["daily_limit"] == 50
    assert result["plan"] == "free"
    assert result["status_code"] == 429
    assert result["retry_after"] == 3600
    assert result["request_id"] == "req_quota"
    text = json.dumps(result).lower()
    assert "upgrade" not in text
    assert "app.sugra.ai" not in text


async def test_the_daily_limit_refusal_leaves_out_what_the_api_did_not_say() -> None:
    detail = _QUOTA_DETAIL.replace("Current plan: free.", "Current plan: unknown.")

    result = await _answer(429, {"detail": detail}, {"Retry-After": "3600"})

    assert result["error"] == (
        "The daily request limit for this Sugra account has been reached. It resets at "
        f"00:00 UTC. Sugra plans and their daily limits are described at {PLANS_PAGE_URL}."
    )
    assert result["reason"] == "daily_limit_reached"
    assert "daily_limit" not in result
    assert "plan" not in result


async def test_other_rate_limits_keep_their_own_text() -> None:
    """A provider's limit behind the API, or the API's reduced quota while
    its cache is down, is not the account's daily limit: saying so would
    send the caller to the plans page for nothing."""
    for detail in (
        "FRED rate limit exceeded",
        "Rate limit temporarily reduced (upstream cache unavailable). Try again shortly. "
        "Conservative per-worker quota: 12.",
    ):
        result = await _answer(429, {"detail": detail}, {"Retry-After": "60"})

        assert result["error"] == detail
        assert "reason" not in result
        assert result["retry_after"] == 60


async def test_a_string_detail_is_the_error_text() -> None:
    result = await _answer(404, {"detail": "Unknown series XYZ"})

    assert result["error"] == "Unknown series XYZ"
    assert result["status_code"] == 404


async def test_an_error_key_wins_over_detail() -> None:
    result = await _answer(401, {"error": "Unauthorized", "detail": "Missing x-api-key"})

    assert result["error"] == "Unauthorized"

    for empty in ("", None):
        result = await _answer(404, {"error": empty, "detail": "Unknown series XYZ"})

        assert result["error"] == "HTTP 404", empty


async def test_a_detail_that_is_not_a_sentence_keeps_the_status_text() -> None:
    for body in (
        {"detail": [{"loc": ["query", "symbol"], "msg": "field required"}]},
        {"detail": "   "},
    ):
        result = await _answer(422, body)

        assert result["error"] == "HTTP 422"


async def test_a_detail_that_sells_is_not_forwarded() -> None:
    for status, detail in (
        (403, "This endpoint needs another plan. Upgrade at https://sugra.systems/api/pricing"),
        (429, "Quota spent. Manage your plan at https://app.sugra.ai/billing"),
    ):
        result = await _answer(status, {"detail": detail})

        assert result["error"] == f"HTTP {status}"
        assert "reason" not in result


# ---- a detail that is an object or a validation list reaches the caller ----

_WINDOW_DETAIL = {
    "error": "invalid_window",
    "allowed": ["1m", "ytd", "1y", "3y", "5y", "10y"],
    "received": "2w",
}


def _fields_chars(error: str | None, detail: Any) -> int:
    """The measure of the fields an object detail adds: its code and itself."""
    fields = {"detail": detail} if error is None else {"error": error, "detail": detail}
    return response_chars(fields)


async def test_an_object_detail_names_the_failure_and_shows_what_is_allowed() -> None:
    """The API refuses window=2w with an object that names the failure and
    lists the windows it takes. Before, the model saw only "HTTP 400"."""
    result = await _answer(400, {"detail": _WINDOW_DETAIL}, {"X-Request-ID": "req_window"})

    assert result["error"] == "invalid_window"
    assert result["detail"] == _WINDOW_DETAIL
    assert result["status_code"] == 400
    assert result["request_id"] == "req_window"
    assert "detail_truncated" not in result


async def test_an_object_detail_without_a_code_keeps_the_status_text() -> None:
    for detail in (
        {"message": "No observations for series XYZ", "series_id": "XYZ"},
        {"error": "   ", "series_id": "XYZ"},
        {"error": 7, "series_id": "XYZ"},
    ):
        result = await _answer(404, {"detail": detail})

        assert result["error"] == "HTTP 404", detail
        assert result["detail"] == detail


async def test_an_object_detail_code_is_stripped_and_the_object_kept_as_sent() -> None:
    detail = {"error": " invalid_window\n", "allowed": ["1m"]}

    result = await _answer(400, {"detail": detail})

    assert result["error"] == "invalid_window"
    assert result["detail"] == detail


async def test_an_empty_object_detail_adds_nothing() -> None:
    result = await _answer(400, {"detail": {}})

    assert result["error"] == "HTTP 400"
    assert "detail" not in result
    assert "detail_truncated" not in result


async def test_an_object_detail_at_the_bound_is_carried_and_one_past_it_is_not() -> None:
    def detail_of(pad: int) -> dict[str, Any]:
        return {"error": "invalid_sub_industry", "allowed": "x" * pad}

    pad = _DETAIL_CHARS - _fields_chars("invalid_sub_industry", detail_of(0))
    assert _fields_chars("invalid_sub_industry", detail_of(pad)) == _DETAIL_CHARS

    result = await _answer(400, {"detail": detail_of(pad)})

    assert result["error"] == "invalid_sub_industry"
    assert result["detail"] == detail_of(pad)
    assert "detail_truncated" not in result

    result = await _answer(400, {"detail": detail_of(pad + 1)})

    assert result["error"] == "invalid_sub_industry"
    assert "detail" not in result
    assert result["detail_truncated"] is True


async def test_an_object_detail_over_the_bound_with_no_code_that_fits_keeps_the_status_text() -> None:
    for detail in (
        {"error": "x" * _DETAIL_CHARS},
        {"allowed": ["y" * 100] * 100},
    ):
        result = await _answer(400, {"detail": detail})

        assert result["error"] == "HTTP 400"
        assert "detail" not in result
        assert result["detail_truncated"] is True


async def test_an_object_detail_that_sells_anywhere_is_not_forwarded() -> None:
    for detail in (
        {"error": "plan_required", "message": "Upgrade to a higher plan"},
        {"error": "plan_required", "links": {"upgrade": "/plans"}},
        {"error": "plan_required", "see": [["https://app.sugra.ai/plans"]]},
    ):
        result = await _answer(403, {"detail": detail})

        assert result["error"] == "HTTP 403", detail
        assert "detail" not in result
        assert "detail_truncated" not in result
        text = json.dumps(result).lower()
        assert "upgrade" not in text
        assert "app.sugra.ai" not in text


async def test_an_error_key_wins_over_an_object_or_list_detail() -> None:
    for detail in (_WINDOW_DETAIL, [{"loc": ["query", "window"], "msg": "bad window"}]):
        result = await _answer(401, {"error": "Unauthorized", "detail": detail})

        assert result["error"] == "Unauthorized"
        assert "detail" not in result
        assert "detail_truncated" not in result


async def test_a_validation_list_says_where_and_what_without_the_value_sent() -> None:
    """FastAPI's 422 list carries the value the caller sent (`input`) and the
    rule it broke (`ctx`); the model needs only where each failure is and
    what it says."""
    body = {"detail": [
        {"type": "missing", "loc": ["query", "symbol"], "msg": "Field required", "input": None},
        {
            "type": "literal_error",
            "loc": ["query", "window"],
            "msg": "Input should be '1m', 'ytd' or '1y'",
            "input": "SENT-VALUE-2w",
            "ctx": {"expected": "'1m', 'ytd' or '1y'"},
        },
        {
            "type": "int_parsing",
            "loc": ["body", "items", 0, "qty"],
            "msg": "Input should be a valid integer",
            "input": "SENT-VALUE-x",
        },
    ]}

    result = await _answer(422, body)

    assert result["error"] == "HTTP 422"
    assert result["detail"] == [
        {"loc": "query.symbol", "msg": "Field required"},
        {"loc": "query.window", "msg": "Input should be '1m', 'ytd' or '1y'"},
        {"loc": "body.items.0.qty", "msg": "Input should be a valid integer"},
    ]
    assert "detail_truncated" not in result
    assert "SENT-VALUE" not in json.dumps(result)


async def test_a_validation_list_skips_what_says_nothing() -> None:
    body = {"detail": [
        {"msg": "Body is not valid JSON"},
        "stray",
        {"loc": ["query", "symbol"]},
        {"loc": ["query", "symbol"], "msg": 3},
        {"loc": [], "msg": "Empty location"},
        {"loc": ["query", {"name": "symbol"}], "msg": "Odd location"},
        {"loc": ["query", True], "msg": "Bool location"},
    ]}

    result = await _answer(422, body)

    assert result["detail"] == [
        {"msg": "Body is not valid JSON"},
        {"msg": "Empty location"},
        {"msg": "Odd location"},
        {"msg": "Bool location"},
    ]

    for nothing in ([], ["stray", 1, None]):
        result = await _answer(422, {"detail": nothing})

        assert result["error"] == "HTTP 422"
        assert "detail" not in result
        assert "detail_truncated" not in result


async def test_a_validation_list_at_the_bound_is_carried_whole_and_one_past_it_is_cut() -> None:
    """The truncation flag costs room only when something is left out: a list
    that fits whole is never cut to make room for it."""
    def items_of(pad: int) -> list[dict[str, Any]]:
        return [
            {"loc": ["query", "symbol"], "msg": "Field required"},
            {"loc": ["query", "window"], "msg": "w" * pad},
        ]

    def compact_of(pad: int) -> list[dict[str, str]]:
        return [
            {"loc": "query.symbol", "msg": "Field required"},
            {"loc": "query.window", "msg": "w" * pad},
        ]

    pad = _DETAIL_CHARS - response_chars({"detail": compact_of(0)})
    assert response_chars({"detail": compact_of(pad)}) == _DETAIL_CHARS
    assert response_chars({"detail": compact_of(pad), "detail_truncated": True}) > _DETAIL_CHARS

    result = await _answer(422, {"detail": items_of(pad)})

    assert result["detail"] == compact_of(pad)
    assert "detail_truncated" not in result

    result = await _answer(422, {"detail": items_of(pad + 1)})

    assert result["detail"] == compact_of(pad)[:1]
    assert result["detail_truncated"] is True


async def test_a_validation_list_over_the_bound_keeps_the_items_that_fit() -> None:
    items = [{"loc": ["query", f"p{index}"], "msg": "m" * 100} for index in range(200)]
    compact = [{"loc": f"query.p{index}", "msg": "m" * 100} for index in range(200)]

    result = await _answer(422, {"detail": items})

    kept = result["detail"]
    assert 0 < len(kept) < len(items)
    assert kept == compact[: len(kept)]
    assert result["detail_truncated"] is True
    assert response_chars({"detail": kept, "detail_truncated": True}) <= _DETAIL_CHARS
    one_more = {"detail": compact[: len(kept) + 1], "detail_truncated": True}
    assert response_chars(one_more) > _DETAIL_CHARS


async def test_a_validation_list_whose_first_item_does_not_fit_says_so() -> None:
    result = await _answer(422, {"detail": [{"loc": ["query", "q"], "msg": "m" * _DETAIL_CHARS}]})

    assert result["error"] == "HTTP 422"
    assert "detail" not in result
    assert result["detail_truncated"] is True


async def test_a_validation_list_that_sells_is_not_forwarded() -> None:
    """Selling text anywhere in the list leaves it out whole, in a part the
    model would never see too."""
    required = {"loc": ["query", "symbol"], "msg": "Field required"}
    for selling in (
        {"loc": ["query", "range"], "msg": "Longer ranges need an upgrade"},
        {"loc": ["query", "range"], "msg": "Too long", "ctx": {"see": "https://app.sugra.ai/plans"}},
        {"loc": ["query", "range"], "msg": "Too long", "type": "Upgrade to go further"},
        {"loc": ["query", "range"], "msg": "Too long", "upgrade": "/plans"},
        "Longer ranges need an upgrade",
    ):
        result = await _answer(422, {"detail": [required, selling]})

        assert result["error"] == "HTTP 422", selling
        assert "detail" not in result
        assert "detail_truncated" not in result
        text = json.dumps(result).lower()
        assert "upgrade" not in text
        assert "app.sugra.ai" not in text


def test_a_detail_nested_too_deep_to_measure_is_left_out_not_raised() -> None:
    """The client never raises: a detail whose measure would pass the
    recursion limit counts as over the bound, the selling-text walk keeps
    its own stack, and a location part that is not a name or an index is
    never turned into text. Each level adds two characters, so the measure
    meets the recursion limit long before it meets the bound."""
    deep: list[Any] = []
    node = deep
    for _ in range(50_000):
        child: list[Any] = []
        node.append(child)
        node = child

    assert _detail_fields({"detail": {"error": "too_deep", "n": deep}}) == {
        "error": "too_deep",
        "detail_truncated": True,
    }
    assert _detail_fields({"detail": [{"loc": ["query", deep], "msg": "m"}]}) == {
        "detail": [{"msg": "m"}],
    }
    node.append("upgrade")
    assert _detail_fields({"detail": {"error": "too_deep", "n": deep}}) == {}
    assert _detail_fields({"detail": [{"loc": ["query", deep], "msg": "m"}]}) == {}


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
