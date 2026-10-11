"""A failure the client returns is held to _ERROR_CHARS, every key kept.

Every way the client fails is driven with one value of 200,000 characters (a
URL with about 60,000, under the 65,536 past which httpx refuses to build one).
The failure keeps its code, its status and its timings as they were, cuts only
the long value, marks it `<key>_truncated`, and stays within _ERROR_CHARS, so
the client's own size gate and call_endpoint pass it on as it is instead of
refusing it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from sugra_api_mcp.client import (
    _CUT_MARK,
    _DETAIL_CHARS,
    _ERROR_CHARS,
    MAX_RESPONSE_CHARS,
    SugraClient,
    _bounded_error,
    response_chars,
)
from sugra_api_mcp.config import Config

LONG = 200_000
# A query this long makes a URL of about 60,000 characters, under the 65,536
# past which httpx refuses to build one.
LONG_QUERY = 60_000
URL = "https://api.test/api/v1/thing"


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> SugraClient:
    config = Config(api_base="https://api.test", api_key="test-key", timeout=0.25)
    return SugraClient(config, transport=httpx.MockTransport(handler))


async def _get(
    handler: Callable[[httpx.Request], httpx.Response],
    params: dict[str, Any] | None = None,
    *,
    enforce_size: bool = True,
) -> dict[str, Any]:
    client = _client(handler)
    try:
        return await client.get("/api/v1/thing", params=params, enforce_size=enforce_size)
    finally:
        await client.aclose()


def _answer(
    status: int, body: Any, headers: dict[str, str] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, headers=headers or {}, request=request)

    return handler


def _assert_held(result: dict[str, Any], *cut: str) -> None:
    """result fits _ERROR_CHARS, and exactly the keys in cut are cut: text
    that ends in the mark, with a head of real length, marked beside it."""
    assert response_chars(result) <= _ERROR_CHARS
    assert {key for key in result if key.endswith("_truncated")} == {
        f"{key}_truncated" for key in cut
    }
    for key in cut:
        assert result[f"{key}_truncated"] is True
        assert result[key].endswith(_CUT_MARK)
        assert response_chars(result[key]) > 1_000


def test_the_bound_leaves_room_under_the_cap_and_the_readme_names_it() -> None:
    assert _DETAIL_CHARS * 2 < _ERROR_CHARS < MAX_RESPONSE_CHARS
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    assert f"held to {_ERROR_CHARS:,} characters" in readme


# ---- transport failures ----


@pytest.mark.parametrize(
    ("raised", "code"),
    [
        (httpx.ReadTimeout, "upstream_timeout"),
        (httpx.ConnectError, "upstream_connect_error"),
        (httpx.RemoteProtocolError, "upstream_transport_error"),
    ],
)
async def test_a_transport_failure_with_a_long_message_keeps_all_but_the_message(
    raised: type[httpx.TransportError], code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise raised("m" * LONG, request=request)

    result = await _get(handler)

    _assert_held(result, "reason")
    assert result["error"] == code
    assert result["reason"].startswith(f"{raised.__name__}: mmm")
    assert result["status_code"] is None
    assert isinstance(result["elapsed_ms"], int)
    assert result["url"] == URL
    assert result["retry_hint"]
    assert result.get("timeout_s") == (0.25 if code == "upstream_timeout" else None)


async def test_a_transport_failure_on_a_long_url_keeps_all_but_the_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    result = await _get(handler, {"q": "q" * LONG_QUERY})

    _assert_held(result, "url")
    assert result["error"] == "upstream_connect_error"
    assert result["reason"] == "ConnectError: refused"
    assert result["url"].startswith(f"{URL}?q=qqq")


# ---- HTTP failures ----


async def test_an_http_failure_on_a_long_url_keeps_all_but_the_url() -> None:
    result = await _get(_answer(404, {"error": "not found"}), {"q": "q" * LONG_QUERY})

    _assert_held(result, "url")
    assert result["error"] == "not found"
    assert result["status_code"] == 404
    assert result["url"].startswith(f"{URL}?q=qqq")


@pytest.mark.parametrize("enforce_size", [True, False])
async def test_a_long_api_error_text_is_cut_and_the_status_kept(enforce_size: bool) -> None:
    result = await _get(_answer(502, {"error": "e" * LONG}), enforce_size=enforce_size)

    _assert_held(result, "error")
    assert result["error"].startswith("eee")
    assert result["status_code"] == 502
    assert result["url"] == URL
    assert isinstance(result["elapsed_ms"], int)


async def test_a_long_detail_sentence_is_cut_as_the_error_text() -> None:
    result = await _get(_answer(400, {"detail": "d" * LONG}))

    _assert_held(result, "error")
    assert result["error"].startswith("ddd")
    assert result["status_code"] == 400


@pytest.mark.parametrize(
    "error",
    [{"code": "bad", "items": ["i" * 1_000] * 300}, [{"value": "v" * LONG}]],
    ids=["object", "list"],
)
async def test_an_error_that_is_not_text_is_cut_as_the_head_of_its_json(error: Any) -> None:
    result = await _get(_answer(400, {"error": error}))

    _assert_held(result, "error")
    assert result["error"].startswith(json.dumps(error)[:20])
    assert result["status_code"] == 400


@pytest.mark.parametrize("char", [chr(0xE9), chr(0x10000)], ids=["accented", "astral"])
async def test_a_cut_counts_the_escapes_of_text_that_is_not_ascii(char: str) -> None:
    result = await _get(_answer(404, {"error": char * LONG}))

    _assert_held(result, "error")
    assert set(result["error"].removesuffix(_CUT_MARK)) == {char}


async def test_a_long_request_id_is_cut_and_the_api_error_kept() -> None:
    result = await _get(_answer(404, {"error": "not found"}, {"X-Request-ID": "r" * LONG}))

    _assert_held(result, "request_id")
    assert result["error"] == "not found"
    assert result["request_id"].startswith("rrr")


@pytest.mark.parametrize("header", ["w" * LONG, "9" * LONG], ids=["text", "digits"])
async def test_a_long_retry_after_is_cut_and_never_raises(header: str) -> None:
    result = await _get(_answer(503, {"error": "busy"}, {"Retry-After": header}))

    _assert_held(result, "retry_after")
    assert result["retry_after"].startswith(header[:3])
    assert result["error"] == "busy"
    assert result["status_code"] == 503


async def test_a_retry_after_of_15_digits_is_a_number_and_of_16_is_text() -> None:
    at_bound = await _get(_answer(503, {"error": "busy"}, {"Retry-After": "9" * 15}))
    past_it = await _get(_answer(503, {"error": "busy"}, {"Retry-After": "9" * 16}))

    assert at_bound["retry_after"] == int("9" * 15)
    assert past_it["retry_after"] == "9" * 16


async def test_a_spent_quota_with_a_long_plan_name_keeps_its_reason_and_limit() -> None:
    detail = (
        f"Daily limit of 50 requests reached. Current plan: {'p' * LONG}. "
        "Upgrade for a higher daily limit: https://app.sugra.ai/plans?from=api.ratelimit.upgrade"
    )

    result = await _get(_answer(429, {"detail": detail}, {"X-RateLimit-Limit": "50"}))

    _assert_held(result, "error", "plan")
    assert result["error"].startswith(
        "The daily request limit of 50 for this Sugra account on the ppp"
    )
    assert result["plan"].startswith("ppp")
    assert result["reason"] == "daily_limit_reached"
    assert result["daily_limit"] == 50
    assert result["status_code"] == 429


async def test_a_detail_at_its_own_bound_stays_whole_beside_a_long_value() -> None:
    detail = {"allowed": "a" * (_DETAIL_CHARS - response_chars({"detail": {"allowed": ""}}))}
    assert response_chars({"detail": detail}) == _DETAIL_CHARS
    headers = {"Retry-After": "30", "X-Request-ID": "r" * LONG}

    result = await _get(_answer(422, {"detail": detail}, headers))

    _assert_held(result, "request_id")
    assert result["detail"] == detail
    assert result["error"] == "HTTP 422"
    assert result["retry_after"] == 30


async def test_every_value_long_at_once_still_fits_and_keeps_the_status() -> None:
    headers = {"Retry-After": "w" * LONG, "X-Request-ID": "r" * LONG}

    result = await _get(_answer(503, {"error": "e" * LONG}, headers), {"q": "q" * LONG_QUERY})

    _assert_held(result, "error", "url", "retry_after", "request_id")
    assert result["status_code"] == 503
    assert isinstance(result["elapsed_ms"], int)


# ---- the bound itself ----


def test_a_failure_that_fits_is_returned_as_it_is() -> None:
    failure = {"error": "not found", "status_code": 404, "url": URL, "elapsed_ms": 3}

    assert _bounded_error(failure) is failure


def test_a_failure_at_the_bound_is_whole_and_one_past_it_is_cut() -> None:
    room = _ERROR_CHARS - response_chars({"error": "", "status_code": 500})
    at_bound = {"error": "e" * room, "status_code": 500}
    past_it = {"error": "e" * (room + 1), "status_code": 500}

    assert response_chars(at_bound) == _ERROR_CHARS
    assert _bounded_error(at_bound) is at_bound
    bounded = _bounded_error(past_it)
    _assert_held(bounded, "error")
    assert bounded["status_code"] == 500


def test_a_value_too_deep_to_measure_is_cut_as_text_not_raised() -> None:
    value: list[Any] = []
    for _ in range(5_000):
        value = [value]

    bounded = _bounded_error({"error": value, "status_code": 400})

    assert bounded["status_code"] == 400
    assert bounded["error_truncated"] is True
    assert bounded["error"].startswith("[[[")
    assert bounded["error"].endswith(_CUT_MARK)
    assert response_chars(bounded) <= _ERROR_CHARS
