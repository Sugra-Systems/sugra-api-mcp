"""Which end of a data list the response size gate keeps.

The gate used to cut an oversized ``data`` list to its first records, so a
series sent oldest first lost its newest points. It now keeps the newest
end whenever the order of the list can be read, by the same rule ``limit``
uses, and keeps the first records otherwise. ``meta.truncated`` reports
``order`` and ``kept_end`` like ``meta.shaped``.

When call_endpoint has already shaped the response, the gate reads the
order from the records as the API sent them, the same list and the same
read ``meta.shaped`` reports. Shaping only cuts and projects that list, so
the two never disagree about the end kept, even when ``fields`` projected
the date key away.
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta

import httpx

from sugra_api_mcp.catalog.models import Catalog, Endpoint
from sugra_api_mcp.client import MAX_RESPONSE_CHARS, SugraClient, _enforce_size_limit
from sugra_api_mcp.config import Config
from sugra_api_mcp.tools import gateway

_COUNT = 2000
_OPERATION_ID = "v1_test_series"
_START = date(2020, 1, 1)


def _dates(count: int = _COUNT) -> list[str]:
    return [(_START + timedelta(days=index)).isoformat() for index in range(count)]


def _records(dates: list[str]) -> list[dict]:
    # About 140 characters a record: 2000 of them are well over the cap.
    return [
        {"date": value, "value": index, "note": "x" * 100}
        for index, value in enumerate(dates)
    ]


def _ascending() -> list[dict]:
    return _records(_dates())


def _descending() -> list[dict]:
    return _records(list(reversed(_dates())))


def _enveloped(records: list) -> dict:
    return {"data": records, "meta": {"source": "fixture"}}


def _notice(result: dict) -> dict:
    return result["meta"]["truncated"]


def _fits(result: dict) -> bool:
    return len(json.dumps(result)) <= MAX_RESPONSE_CHARS


# The gate on its own


def test_ascending_list_keeps_its_newest_records_in_original_order() -> None:
    records = _ascending()

    result = _enforce_size_limit(_enveloped(records), "test://url")

    kept = _notice(result)["kept_count"]
    assert 1 <= kept < _COUNT
    assert result["data"] == records[_COUNT - kept:]
    assert result["data"][-1] == records[-1]
    assert _notice(result)["order"] == "asc"
    assert _notice(result)["kept_end"] == "newest"
    assert _notice(result)["original_count"] == _COUNT
    assert _fits(result)


def test_descending_list_keeps_its_first_records_which_are_the_newest() -> None:
    records = _descending()

    result = _enforce_size_limit(_enveloped(records), "test://url")

    kept = _notice(result)["kept_count"]
    assert 1 <= kept < _COUNT
    assert result["data"] == records[:kept]
    assert _notice(result)["order"] == "desc"
    assert _notice(result)["kept_end"] == "newest"
    assert _fits(result)


def test_list_without_a_date_key_keeps_the_first_records() -> None:
    records = [{"id": index, "name": f"item_{index}", "desc": "x" * 100} for index in range(_COUNT)]

    result = _enforce_size_limit(_enveloped(records), "test://url")

    kept = _notice(result)["kept_count"]
    assert result["data"] == records[:kept]
    assert _notice(result)["order"] == "unknown"
    assert _notice(result)["kept_end"] == "first"
    assert _fits(result)


def test_list_that_does_not_run_one_way_keeps_the_first_records() -> None:
    dates = _dates()
    random.Random(7).shuffle(dates)
    records = _records(dates)

    result = _enforce_size_limit(_enveloped(records), "test://url")

    kept = _notice(result)["kept_count"]
    assert result["data"] == records[:kept]
    assert _notice(result)["order"] == "unknown"
    assert _notice(result)["kept_end"] == "first"


def test_records_at_the_kept_end_larger_than_the_average_still_fit() -> None:
    """The count is estimated from the average record; when the end that is
    kept holds larger records than the rest, the gate keeps fewer of them
    rather than returning more than the cap."""
    ascending = _records(_dates())
    for record in ascending[_COUNT // 2:]:
        record["note"] = "y" * 400
    unordered = [{"id": index, "desc": ("x" * 400 if index < _COUNT // 2 else "x")} for index in range(_COUNT)]

    for records, kept_end in ((ascending, "newest"), (unordered, "first")):
        result = _enforce_size_limit(_enveloped(records), "test://url")

        kept = _notice(result)["kept_count"]
        assert _notice(result)["kept_end"] == kept_end
        expected = records[_COUNT - kept:] if kept_end == "newest" else records[:kept]
        assert result["data"] == expected
        assert _fits(result)


def test_a_list_under_the_cap_is_returned_without_a_notice() -> None:
    payload = _enveloped(_ascending()[:10])

    assert _enforce_size_limit(payload, "test://url") == payload


def test_unshaped_payload_decides_the_order_over_the_shaped_list() -> None:
    """The records as sent run no one way, but their first part does: the
    order read from the unshaped payload wins, so the gate keeps the first
    records instead of the end of a part it happens to find sorted."""
    dates = _dates()
    records = _records(dates[1:] + dates[:1])
    shaped = _enveloped(records[: _COUNT - 1])

    result = _enforce_size_limit(shaped, "test://url", unshaped=_enveloped(records))

    kept = _notice(result)["kept_count"]
    assert result["data"] == records[:kept]
    assert _notice(result)["order"] == "unknown"
    assert _notice(result)["kept_end"] == "first"


def test_unshaped_bare_array_is_read_as_the_records_list() -> None:
    """A bare array as sent, projected without its date key: only the
    unshaped array can tell the order."""
    records = _ascending()
    projected = [{"value": record["value"], "note": record["note"]} for record in records]

    result = _enforce_size_limit({"data": projected}, "test://url", unshaped=records)

    assert result["data"][-1] == projected[-1]
    assert _notice(result)["order"] == "asc"
    assert _notice(result)["kept_end"] == "newest"


def test_payload_is_not_mutated() -> None:
    records = _ascending()
    payload = _enveloped(records)
    before = json.dumps(payload)

    _enforce_size_limit(payload, "test://url")

    assert json.dumps(payload) == before


# When no trim fits, the gate answers with its structured error, never an
# over-cap payload


def _too_large(result: dict) -> bool:
    return result.get("error") == "response_too_large" and "data" not in result and _fits(result)


def test_one_record_larger_than_the_cap_returns_the_structured_error() -> None:
    payload = _enveloped([{"date": "2020-01-01", "note": "x" * MAX_RESPONSE_CHARS}])

    assert _too_large(_enforce_size_limit(payload, "test://url"))


def test_newest_record_larger_than_the_cap_returns_the_structured_error() -> None:
    # The newest end is one record that alone exceeds the cap: the shrink
    # loop stops at one record, which still does not fit.
    records = _ascending()
    records[-1] = {**records[-1], "note": "x" * MAX_RESPONSE_CHARS}

    assert _too_large(_enforce_size_limit(_enveloped(records), "test://url"))


def test_envelope_too_large_for_any_record_returns_the_structured_error() -> None:
    payload = {"data": _ascending()[:10], "meta": {"blob": "y" * MAX_RESPONSE_CHARS}}

    assert _too_large(_enforce_size_limit(payload, "test://url"))


# The default client path, which every non-gateway tool takes


async def test_default_client_path_keeps_the_newest_records() -> None:
    records = _ascending()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_enveloped(records), request=request)

    config = Config(api_base="https://api.test", api_key="test-key", timeout=5.0)
    client = SugraClient(config, transport=httpx.MockTransport(handler))
    try:
        result = await client.get("/api/v1/test/series")
    finally:
        await client.aclose()

    assert result["data"][-1] == records[-1]
    assert _notice(result)["kept_end"] == "newest"


# call_endpoint: shaping runs first, then the gate


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


async def _call(monkeypatch, body, **kwargs) -> dict:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body, request=request)

    config = Config(api_base="https://api.test", api_key="test-key", timeout=5.0)
    client = SugraClient(config, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(gateway, "load_catalog", _catalog)
    monkeypatch.setattr(gateway, "get_client", lambda: client)
    try:
        return await gateway.call_endpoint(_OPERATION_ID, **kwargs)
    finally:
        await client.aclose()


async def test_limit_then_gate_both_keep_the_newest_end_of_an_ascending_series(monkeypatch) -> None:
    records = _ascending()

    result = await _call(monkeypatch, _enveloped(records), limit=1500)

    assert "error" not in result
    kept = _notice(result)["kept_count"]
    assert _notice(result)["original_count"] == 1500
    assert result["data"] == records[_COUNT - kept:]
    assert result["meta"]["shaped"]["kept_end"] == "newest"
    assert _notice(result)["order"] == result["meta"]["shaped"]["order"] == "asc"
    assert _notice(result)["kept_end"] == "newest"
    assert _fits(result)


async def test_limit_then_gate_both_keep_the_newest_end_of_a_descending_series(monkeypatch) -> None:
    records = _descending()

    result = await _call(monkeypatch, _enveloped(records), limit=1500)

    kept = _notice(result)["kept_count"]
    assert result["data"] == records[:kept]
    assert _notice(result)["order"] == result["meta"]["shaped"]["order"] == "desc"
    assert _notice(result)["kept_end"] == result["meta"]["shaped"]["kept_end"] == "newest"


async def test_limit_on_an_unordered_list_then_gate_both_keep_the_first_records(monkeypatch) -> None:
    """The first 1500 records run one way and the last one does not, so the
    list as sent has no order: limit keeps the first 1500 and the gate must
    keep the first of those too, not the end of a part that looks sorted."""
    dates = _dates()
    records = _records(dates[1:] + dates[:1])

    result = await _call(monkeypatch, _enveloped(records), limit=1500)

    kept = _notice(result)["kept_count"]
    assert result["data"] == records[:kept]
    assert _notice(result)["order"] == result["meta"]["shaped"]["order"] == "unknown"
    assert _notice(result)["kept_end"] == result["meta"]["shaped"]["kept_end"] == "first"


async def test_fields_that_drop_the_date_key_still_keep_the_newest_end(monkeypatch) -> None:
    records = _ascending()

    result = await _call(monkeypatch, _enveloped(records), limit=1500, fields=["value", "note"])

    assert "date" not in result["data"][0]
    assert result["data"][-1]["value"] == _COUNT - 1
    assert _notice(result)["order"] == result["meta"]["shaped"]["order"] == "asc"
    assert _notice(result)["kept_end"] == result["meta"]["shaped"]["kept_end"] == "newest"


async def test_fields_without_limit_keep_the_newest_end(monkeypatch) -> None:
    records = _ascending()

    result = await _call(monkeypatch, _enveloped(records), fields=["value", "note"])

    assert result["data"][-1]["value"] == _COUNT - 1
    assert _notice(result)["kept_end"] == "newest"
    assert "order" not in result["meta"]["shaped"]


async def test_bare_array_response_keeps_the_newest_end(monkeypatch) -> None:
    records = _ascending()

    result = await _call(monkeypatch, records)

    assert result["data"][-1] == records[-1]
    assert _notice(result)["order"] == "asc"
    assert _notice(result)["kept_end"] == "newest"


async def test_bare_array_with_fields_that_drop_the_date_key_keeps_the_newest_end(monkeypatch) -> None:
    records = _ascending()

    result = await _call(monkeypatch, records, fields=["value", "note"])

    assert "date" not in result["data"][0]
    assert result["data"][-1]["value"] == _COUNT - 1
    assert _notice(result)["kept_end"] == "newest"


async def test_oversized_error_envelope_keeps_the_structured_error(monkeypatch) -> None:
    # An error payload carries no data key by definition (is_error_payload),
    # so the order-aware trim never reaches the error path: an oversized
    # error still becomes response_too_large, as before this change.
    body = {"error": "upstream_error", "reason": "r" * MAX_RESPONSE_CHARS}

    result = await _call(monkeypatch, body)

    assert _too_large(result)
