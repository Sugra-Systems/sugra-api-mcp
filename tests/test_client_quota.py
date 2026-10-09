"""The account's daily quota on a successful answer.

The API reports the quota on every response it counts, in the
X-RateLimit-Limit, X-RateLimit-Remaining and X-RateLimit-Reset headers. The
client copies them into ``meta.quota`` so a model sees what is left before the
limit is reached, not only the refusal once it is. A response without the
three headers, or with any one of them malformed, is left as the API sent it,
and reading them never raises.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from sugra_api_mcp.catalog.models import Catalog, Endpoint
from sugra_api_mcp.client import MAX_RESPONSE_CHARS, SugraClient
from sugra_api_mcp.config import Config
from sugra_api_mcp.tools import gateway

_HEADERS = {
    "X-RateLimit-Limit": "1000",
    "X-RateLimit-Remaining": "990",
    "X-RateLimit-Reset": "2026-10-08T23:59:59Z",
}
_QUOTA = {"limit": 1000, "remaining": 990, "resets_at": "2026-10-08T23:59:59Z"}
_OPERATION_ID = "v1_test_series"


def _response(
    request: httpx.Request, status: int, body: Any, headers: dict[str, str] | list[tuple[str, str]]
) -> httpx.Response:
    # Values go out as UTF-8 bytes, the way a proxy could send them: httpx
    # refuses a str header value that is not ASCII.
    pairs = headers.items() if isinstance(headers, dict) else headers
    raw = [(name.encode("ascii"), value.encode("utf-8")) for name, value in pairs]
    return httpx.Response(status, json=body, headers=raw, request=request)


async def _get(
    body: Any,
    headers: dict[str, str] | list[tuple[str, str]] | None = None,
    *,
    status: int = 200,
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(request, status, body, _HEADERS if headers is None else headers)

    config = Config(api_base="https://api.test", api_key="test-key", timeout=5.0)
    client = SugraClient(config, transport=httpx.MockTransport(handler))
    try:
        return await client.get("/api/v1/test/series")
    finally:
        await client.aclose()


def _with(**changes: str) -> dict[str, str]:
    """The valid headers with some values replaced."""
    names = {
        "limit": "X-RateLimit-Limit",
        "remaining": "X-RateLimit-Remaining",
        "reset": "X-RateLimit-Reset",
    }
    return {**_HEADERS, **{names[key]: value for key, value in changes.items()}}


# ---- a counted answer carries the quota ----


async def test_a_counted_answer_carries_the_quota_beside_the_apis_own_meta() -> None:
    body = {"data": [{"v": 1}], "meta": {"source": "fixture", "cached": False}}

    result = await _get(body)

    assert result == {
        "data": [{"v": 1}],
        "meta": {"source": "fixture", "cached": False, "quota": _QUOTA},
    }


async def test_the_last_request_of_the_day_reports_none_left() -> None:
    result = await _get({"data": [], "meta": {}}, _with(remaining="0"))

    assert result["meta"]["quota"] == {**_QUOTA, "remaining": 0}


@pytest.mark.parametrize(
    "reset",
    [
        pytest.param("2026-10-08T23:59:59+00:00", id="numeric-offset"),
        pytest.param("2026-10-08T18:59:59-05:00", id="negative-offset"),
        pytest.param("2026-10-08T23:59:59.999999Z", id="fraction-of-a-second"),
    ],
)
async def test_a_reset_time_in_another_iso_form_is_kept_as_sent(reset: str) -> None:
    result = await _get({"data": [], "meta": {}}, _with(reset=reset))

    assert result["meta"]["quota"]["resets_at"] == reset


# ---- no quota unless the API reported one ----


async def test_an_answer_without_the_headers_is_left_as_sent() -> None:
    body = {"data": [{"v": 1}], "meta": {"source": "fixture"}}

    assert await _get(body, {}) == body


@pytest.mark.parametrize("missing", sorted(_HEADERS))
async def test_an_answer_missing_one_header_is_left_as_sent(missing: str) -> None:
    body = {"data": [{"v": 1}], "meta": {}}
    headers = {name: value for name, value in _HEADERS.items() if name != missing}

    assert await _get(body, headers) == body


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"limit": "abc"}, id="limit-not-a-number"),
        pytest.param({"limit": "0"}, id="limit-zero"),
        pytest.param({"limit": "-5"}, id="limit-negative"),
        pytest.param({"limit": "1000.0"}, id="limit-decimal"),
        pytest.param({"limit": "1e3"}, id="limit-exponent"),
        # isdigit() is true for both, and int() refuses the second.
        pytest.param({"limit": chr(0xFF11) + chr(0xFF10) * 3}, id="limit-fullwidth-digits"),
        pytest.param({"limit": chr(0x00B2)}, id="limit-superscript-two"),
        pytest.param({"limit": "9" * 16}, id="limit-sixteen-digits"),
        pytest.param({"remaining": "1001"}, id="remaining-above-limit"),
        pytest.param({"remaining": "-1"}, id="remaining-negative"),
        pytest.param({"remaining": ""}, id="remaining-empty"),
        # Past the bound int() itself puts on digit strings.
        pytest.param({"remaining": "9" * 5000}, id="remaining-5000-digits"),
        pytest.param({"reset": "tomorrow"}, id="reset-not-a-time"),
        pytest.param({"reset": "2026-10-08T23:59:59"}, id="reset-without-offset"),
        pytest.param({"reset": "2026-10-08"}, id="reset-date-only"),
        pytest.param({"reset": "2026-10-08T23:59:59+24:00"}, id="reset-offset-out-of-range"),
        pytest.param({"reset": "2026-02-30T00:00:00Z"}, id="reset-no-such-day"),
        pytest.param(
            {"reset": "2026-10-08T23:59:59." + "1" * 30 + "Z"}, id="reset-longer-than-a-timestamp"
        ),
        pytest.param({"reset": "2026-10-08T23:59:59" + chr(0xFF3A)}, id="reset-fullwidth-z"),
        # fromisoformat takes any one character between the date and the time.
        pytest.param({"reset": "2026-10-08Q23:59:59+00:00"}, id="reset-letter-separator"),
        pytest.param({"reset": "2026-10-08 23:59:59Z"}, id="reset-space-separator"),
        pytest.param({"reset": "2026-10-08t23:59:59Z"}, id="reset-lowercase-separator"),
        pytest.param({"reset": "2026-10-08\t23:59:59Z"}, id="reset-tab-separator"),
        pytest.param({"reset": "2026-10-08\x1f23:59:59Z"}, id="reset-control-separator"),
        # Python 3.14 reads 24:00 as midnight of the next day; earlier ones refuse it.
        pytest.param({"reset": "2026-10-08T24:00:00Z"}, id="reset-hour-24"),
        pytest.param({"reset": "2026-10-08T23:59:59+0000"}, id="reset-offset-without-colon"),
        pytest.param({"reset": "2026-10-08T23:59:59+00:60"}, id="reset-offset-minute-60"),
    ],
)
async def test_a_malformed_header_carries_no_quota_and_never_raises(
    changes: dict[str, str],
) -> None:
    body = {"data": [{"v": 1}], "meta": {}}

    assert await _get(body, _with(**changes)) == body


async def test_a_repeated_header_carries_no_quota() -> None:
    """httpx joins the values of a repeated header with a comma, so two
    limits read as neither."""
    body = {"data": [{"v": 1}], "meta": {}}
    headers = [*_HEADERS.items(), ("X-RateLimit-Limit", "1000")]

    assert await _get(body, headers) == body


# ---- what the payload can carry ----


async def test_an_envelope_less_answer_gets_a_meta_of_its_own() -> None:
    result = await _get({"ip": "8.8.8.8", "asn": 15169})

    assert result == {"ip": "8.8.8.8", "asn": 15169, "meta": {"quota": _QUOTA}}


async def test_a_null_meta_is_replaced_by_the_quota() -> None:
    result = await _get({"data": [1], "meta": None})

    assert result == {"data": [1], "meta": {"quota": _QUOTA}}


@pytest.mark.parametrize(
    "body",
    [
        {"data": [1], "meta": "fixture"},
        {"data": [1], "meta": ["fixture"]},
        {"data": [1], "meta": {"quota": {"limit": 50}}},  # the API's own quota wins
        [{"v": 1}, {"v": 2}],
        42,
        "text",
    ],
    ids=repr,
)
async def test_a_payload_that_cannot_carry_the_quota_is_left_as_sent(body: Any) -> None:
    assert await _get(body) == body


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (404, {"detail": "Not Found"}),
        (422, {"detail": [{"loc": ["query", "start"], "msg": "bad date"}]}),
        (503, {"error": "upstream_unavailable"}),
        (
            429,
            {"detail": "Daily limit of 1000 requests reached. Current plan: personal."},
        ),
    ],
)
async def test_a_failure_carries_no_quota(status: int, body: Any) -> None:
    result = await _get(body, status=status)

    assert result["status_code"] == status
    assert "meta" not in result
    assert "quota" not in json.dumps(result)


# ---- through the tools: shaping, the size cut and fetch_data keep it ----


def _catalog() -> Catalog:
    return Catalog(
        source="test-series",
        endpoints=[
            Endpoint(
                operation_id=_OPERATION_ID,
                method="GET",
                path="/api/v1/test/series",
                summary="A long daily series",
                toolset="markets",
                source_family="markets",
                sources=["markets"],
                parameters=[],
            )
        ],
    )


def _serve(monkeypatch: pytest.MonkeyPatch, body: Any) -> SugraClient:
    """The gateway's tools on the one test operation, answered with body and
    the quota headers."""

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(request, 200, body, _HEADERS)

    config = Config(api_base="https://api.test", api_key="test-key", timeout=5.0)
    client = SugraClient(config, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(gateway, "load_catalog", _catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: client)
    return client


async def _call(monkeypatch: pytest.MonkeyPatch, body: Any, **kwargs: Any) -> dict[str, Any]:
    client = _serve(monkeypatch, body)
    try:
        return await gateway.call_endpoint(_OPERATION_ID, **kwargs)
    finally:
        await client.aclose()


def _series(count: int) -> list[dict[str, Any]]:
    return [
        {"date": f"2020-01-{index % 28 + 1:02d}", "value": index, "note": "x" * 100}
        for index in range(count)
    ]


async def test_the_quota_survives_limit_fields_and_include_raw(monkeypatch) -> None:
    body = {"data": _series(5), "meta": {"source": "fixture"}}

    result = await _call(monkeypatch, body, fields=["value"], limit=2, include_raw=True)

    assert result["meta"]["quota"] == _QUOTA
    assert result["meta"]["source"] == "fixture"
    assert result["meta"]["shaped"]["fields_applied"] == ["value"]
    assert all(set(record) == {"value"} for record in result["data"])


async def test_the_quota_survives_a_projection_of_an_envelope_less_answer(monkeypatch) -> None:
    result = await _call(monkeypatch, {"ip": "8.8.8.8", "asn": 15169}, fields=["ip"])

    assert result["ip"] == "8.8.8.8"
    assert "asn" not in result
    assert result["meta"]["quota"] == _QUOTA


async def test_the_quota_survives_the_size_cut(monkeypatch) -> None:
    body = {"data": _series(2000), "meta": {"source": "fixture"}}

    result = await _call(monkeypatch, body)

    assert "error" not in result
    assert result["meta"]["truncated"]["original_count"] == 2000
    assert result["meta"]["quota"] == _QUOTA
    assert len(json.dumps(result)) <= MAX_RESPONSE_CHARS


async def test_fetch_data_carries_the_quota_beside_its_selection(monkeypatch) -> None:
    client = _serve(monkeypatch, {"data": _series(3), "meta": {"source": "fixture"}})
    try:
        result = await gateway.fetch_data(query="long daily series")
    finally:
        await client.aclose()

    assert result["meta"]["fetch_data"]["operation_id"] == _OPERATION_ID
    assert result["meta"]["quota"] == _QUOTA
    assert result["meta"]["source"] == "fixture"


# ---- the spent-quota refusal reads its limit the same way ----


@pytest.mark.parametrize(
    "limit",
    [
        pytest.param("9" * 16, id="sixteen-digits"),
        # Past the bound int() itself puts on digit strings.
        pytest.param("9" * 5000, id="5000-digits"),
        pytest.param(chr(0xFF11) + chr(0xFF10) * 3, id="fullwidth-digits"),
    ],
)
async def test_the_spent_quota_refusal_names_no_malformed_limit(limit: str) -> None:
    body = {"detail": "Daily limit of 1000 requests reached. Current plan: personal."}

    result = await _get(body, _with(limit=limit, remaining="0"), status=429)

    assert result["reason"] == "daily_limit_reached"
    assert result["plan"] == "personal"
    assert "daily_limit" not in result
