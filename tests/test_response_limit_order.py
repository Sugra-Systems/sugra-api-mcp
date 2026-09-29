"""Which end of a records list ``limit`` keeps.

A series sent oldest first used to lose its newest points to ``limit``,
because the bound always kept the first N records. When every record
carries one recognisable date or period key in one consistent format and
the list is ordered by it, ``limit`` keeps the newest end; otherwise it keeps
the first N exactly as before. ``meta.shaped`` reports ``order`` and
``kept_end`` whenever a limit was applied to a records list.
"""

from __future__ import annotations

from sugra_api_mcp.catalog.response import shape_response


def _series(key: str, values: list, **extra) -> list[dict]:
    return [{key: value, "value": index, **extra} for index, value in enumerate(values)]


def _enveloped(records: list) -> dict:
    return {"data": records, "meta": {"source": "fixture"}}


def _block(shaped: dict) -> dict:
    return shaped["meta"]["shaped"]


# Direction


def test_ascending_series_keeps_the_newest_records_in_original_order() -> None:
    records = _series("date", ["2025-09-01", "2025-10-01", "2026-06-01", "2026-07-01"])

    shaped = shape_response(_enveloped(records), limit=2)

    assert shaped["data"] == records[2:]
    assert _block(shaped)["order"] == "asc"
    assert _block(shaped)["kept_end"] == "newest"
    assert _block(shaped)["limit_applied"] is True
    assert _block(shaped)["records_path"] == "data"


def test_descending_series_keeps_the_first_records_and_reports_it() -> None:
    records = _series("date", ["2026-07-01", "2026-06-01", "2025-10-01", "2025-09-01"])

    shaped = shape_response(_enveloped(records), limit=2)

    assert shaped["data"] == records[:2]
    assert _block(shaped)["order"] == "desc"
    assert _block(shaped)["kept_end"] == "newest"


def test_bare_array_ascending_series_keeps_the_newest_records() -> None:
    records = _series("period", ["2026-01", "2026-02", "2026-03"])

    shaped = shape_response(records, limit=1)

    assert shaped["data"] == records[2:]
    assert _block(shaped)["order"] == "asc"


def test_records_inside_data_ascending_series_keeps_the_newest_records() -> None:
    observations = _series("date", ["2026-05-01", "2026-06-01", "2026-07-01"])
    payload = {"data": {"series_id": "X", "count": 3, "observations": observations}}

    shaped = shape_response(payload, limit=2)

    assert shaped["data"]["observations"] == observations[1:]
    assert shaped["data"]["count"] == 3
    assert _block(shaped)["records_path"] == "data.observations"
    assert _block(shaped)["order"] == "asc"
    assert _block(shaped)["kept_end"] == "newest"


def test_ties_inside_a_real_progression_still_establish_the_order() -> None:
    records = _series(
        "filing_date", ["2026-01-02", "2026-01-02", "2026-01-03", "2026-01-03", "2026-01-05"]
    )

    shaped = shape_response(_enveloped(records), limit=3)

    assert shaped["data"] == records[2:]
    assert _block(shaped)["order"] == "asc"


# Values that establish no order


def test_non_monotone_series_keeps_the_first_records() -> None:
    """Ordered at the two ends but not in between: the whole sequence is
    checked, so this stays on the first-N path."""
    records = _series("date", ["2026-01-01", "2026-03-01", "2026-02-01", "2026-04-01"])

    shaped = shape_response(_enveloped(records), limit=2)

    assert shaped["data"] == records[:2]
    assert _block(shaped)["order"] == "unknown"
    assert _block(shaped)["kept_end"] == "first"


def test_a_record_missing_the_date_key_means_unknown() -> None:
    records = _series("date", ["2026-01-01", "2026-02-01", "2026-03-01"])
    del records[1]["date"]

    shaped = shape_response(_enveloped(records), limit=1)

    assert shaped["data"] == records[:1]
    assert _block(shaped)["order"] == "unknown"


def test_a_null_date_value_means_unknown() -> None:
    records = _series("date", ["2026-01-01", None, "2026-03-01"])

    shaped = shape_response(_enveloped(records), limit=1)

    assert shaped["data"] == records[:1]
    assert _block(shaped)["order"] == "unknown"


def test_two_candidate_keys_on_every_record_are_ambiguous() -> None:
    records = [
        {"date": "2026-01-01", "period": "2026-01", "value": 1},
        {"date": "2026-02-01", "period": "2026-02", "value": 2},
        {"date": "2026-03-01", "period": "2026-03", "value": 3},
    ]

    shaped = shape_response(_enveloped(records), limit=1)

    assert shaped["data"] == records[:1]
    assert _block(shaped)["order"] == "unknown"


def test_a_candidate_key_on_only_some_records_does_not_count() -> None:
    """Only keys present and non-null on every record are candidates, so a
    sparse second key leaves the one complete key in charge."""
    records = _series("date", ["2026-01-01", "2026-02-01", "2026-03-01"])
    records[0]["period"] = "2026-01"

    shaped = shape_response(_enveloped(records), limit=1)

    assert shaped["data"] == records[2:]
    assert _block(shaped)["order"] == "asc"


def test_nested_date_keys_are_not_read() -> None:
    """Only top-level record keys are read; a date inside a nested object
    establishes nothing."""
    records = [
        {"registration": {"lastUpdateDate": f"2026-0{month}-01"}, "value": month}
        for month in (1, 2, 3)
    ]

    shaped = shape_response(_enveloped(records), limit=1)

    assert shaped["data"] == records[:1]
    assert _block(shaped)["order"] == "unknown"


def test_mismatched_string_formats_mean_unknown() -> None:
    records = _series("period", ["2024", "2024-01", "2024-02"])

    shaped = shape_response(_enveloped(records), limit=1)

    assert shaped["data"] == records[:1]
    assert _block(shaped)["order"] == "unknown"


def test_month_names_mean_unknown() -> None:
    records = _series("period", ["Dec 2025", "Jan 2026", "Feb 2026"])

    shaped = shape_response(_enveloped(records), limit=1)

    assert shaped["data"] == records[:1]
    assert _block(shaped)["order"] == "unknown"


def test_day_first_and_month_first_dates_mean_unknown() -> None:
    """A date that does not start with the year sorts wrongly across years:
    read as text, 12/15/2024 then 01/15/2025 looks descending."""
    for values in (
        ["12/15/2024", "01/15/2025", "02/15/2025"],
        ["15/12/2024", "15/01/2025", "15/02/2025"],
    ):
        records = _series("date", values)

        shaped = shape_response(_enveloped(records), limit=1)

        assert shaped["data"] == records[:1], values
        assert _block(shaped)["order"] == "unknown", values


def test_ordinal_period_codes_mean_unknown() -> None:
    for values in (["0m", "-1m", "-2m"], ["m0", "m1", "m2"], ["-2m", "-1m", "0m"]):
        records = _series("period", values)

        shaped = shape_response(_enveloped(records), limit=1)

        assert shaped["data"] == records[:1], values
        assert _block(shaped)["order"] == "unknown", values


def test_year_led_formats_with_letters_establish_the_order() -> None:
    """An identical template keeps its letters literal, so quarter and
    month codes after a year still order correctly across the year."""
    for values in (
        ["2024-Q3", "2024-Q4", "2025-Q1"],
        ["2024M11", "2024M12", "2025M01"],
        ["2026-07-30T10:00:00Z", "2026-07-30T11:00:00Z", "2026-07-31T09:00:00Z"],
        ["2024", "2025", "2026"],
    ):
        records = _series("period", values)

        shaped = shape_response(_enveloped(records), limit=1)

        assert shaped["data"] == records[2:], values
        assert _block(shaped)["order"] == "asc", values


def test_differing_utc_offsets_order_by_instant_not_text() -> None:
    """00:30 at +01:00 is 23:30 UTC the day before, so this pair ascends in
    time although it descends as text."""
    ascending = _series("time", ["2026-01-01T00:30:00+01:00", "2026-01-01T00:00:00+00:00"])
    descending = list(reversed(ascending))

    up = shape_response(_enveloped(ascending), limit=1)
    down = shape_response(_enveloped(descending), limit=1)

    assert up["data"] == ascending[1:]
    assert _block(up)["order"] == "asc"
    assert _block(up)["kept_end"] == "newest"
    assert down["data"] == descending[:1]
    assert _block(down)["order"] == "desc"


def test_a_series_across_a_clock_change_orders_by_instant() -> None:
    """The hour repeated when clocks go back: text order runs 01:30, 01:15,
    01:45, time order 05:30, 06:15, 06:45 UTC."""
    records = _series(
        "timestamp",
        ["2026-11-01T01:30:00-04:00", "2026-11-01T01:15:00-05:00", "2026-11-01T01:45:00-05:00"],
    )

    shaped = shape_response(_enveloped(records), limit=1)

    assert shaped["data"] == records[2:]
    assert _block(shaped)["order"] == "asc"


def test_offset_values_that_name_one_instant_at_both_ends_mean_unknown() -> None:
    records = _series("time", ["2026-01-01T01:00:00+01:00", "2026-01-01T00:00:00+00:00"])

    shaped = shape_response(_enveloped(records), limit=1)

    assert shaped["data"] == records[:1]
    assert _block(shaped)["order"] == "unknown"


def test_offset_values_that_do_not_parse_mean_unknown() -> None:
    """Same shape, so text would read this pair as descending, but the
    offsets cannot be read, so neither can the order."""
    records = _series("time", ["2026-01-01T00:30:00+01:00 (local)", "2026-01-01T00:00:00+00:00 (local)"])

    shaped = shape_response(_enveloped(records), limit=1)

    assert shaped["data"] == records[:1]
    assert _block(shaped)["order"] == "unknown"


def test_integer_years_ascending_and_descending() -> None:
    ascending = _series("year", [2022, 2023, 2024, 2025])
    descending = list(reversed(ascending))

    up = shape_response(_enveloped(ascending), limit=2)
    down = shape_response(_enveloped(descending), limit=2)

    assert up["data"] == ascending[2:]
    assert _block(up)["order"] == "asc"
    assert down["data"] == descending[:2]
    assert _block(down)["order"] == "desc"


def test_epoch_seconds_ascending_keeps_the_newest_records() -> None:
    records = _series("timestamp", [1767225600, 1767312000.5, 1767398400])

    shaped = shape_response(_enveloped(records), limit=1)

    assert shaped["data"] == records[2:]
    assert _block(shaped)["order"] == "asc"


def test_booleans_are_never_numbers() -> None:
    records = _series("t", [False, True, True])

    shaped = shape_response(_enveloped(records), limit=1)

    assert shaped["data"] == records[:1]
    assert _block(shaped)["order"] == "unknown"


def test_one_boolean_among_numbers_means_unknown() -> None:
    records = _series("year", [0, 1, True, 3])

    shaped = shape_response(_enveloped(records), limit=1)

    assert shaped["data"] == records[:1]
    assert _block(shaped)["order"] == "unknown"


def test_numbers_and_strings_never_mix() -> None:
    records = _series("year", [2024, "2025", 2026])

    shaped = shape_response(_enveloped(records), limit=1)

    assert shaped["data"] == records[:1]
    assert _block(shaped)["order"] == "unknown"


def test_all_identical_dates_mean_unknown() -> None:
    records = _series("published", ["2026-09-12T08:00:00Z"] * 4)

    shaped = shape_response(_enveloped(records), limit=2)

    assert shaped["data"] == records[:2]
    assert _block(shaped)["order"] == "unknown"
    assert _block(shaped)["kept_end"] == "first"


def test_records_that_are_not_objects_mean_unknown() -> None:
    shaped = shape_response(_enveloped(["2026-01", "2026-02", "2026-03"]), limit=1)

    assert shaped["data"] == ["2026-01"]
    assert _block(shaped)["order"] == "unknown"
    assert _block(shaped)["kept_end"] == "first"


# Sizes


def test_single_record_list_means_unknown() -> None:
    records = _series("date", ["2026-01-01"])

    shaped = shape_response(_enveloped(records), limit=5)

    assert shaped["data"] == records
    assert _block(shaped)["order"] == "unknown"
    assert _block(shaped)["kept_end"] == "first"


def test_empty_list_means_unknown() -> None:
    shaped = shape_response(_enveloped([]), limit=5)

    assert shaped["data"] == []
    assert _block(shaped)["order"] == "unknown"
    assert _block(shaped)["kept_end"] == "first"


def test_limit_at_or_above_the_length_keeps_everything_in_both_directions() -> None:
    ascending = _series("date", ["2026-01-01", "2026-02-01", "2026-03-01"])
    descending = list(reversed(ascending))

    for records, order in ((ascending, "asc"), (descending, "desc")):
        for limit in (3, 10):
            shaped = shape_response(_enveloped(records), limit=limit)

            assert shaped["data"] == records, (order, limit)
            assert _block(shaped)["order"] == order, (order, limit)
            assert _block(shaped)["kept_end"] == "newest", (order, limit)


def test_limit_zero_returns_an_empty_list_on_every_path() -> None:
    ascending = _series("date", ["2026-01-01", "2026-02-01", "2026-03-01"])
    descending = list(reversed(ascending))
    unordered = _series("date", ["2026-02-01", "2026-01-01", "2026-03-01"])

    for records, order in ((ascending, "asc"), (descending, "desc"), (unordered, "unknown")):
        shaped = shape_response(_enveloped(records), limit=0)

        assert shaped["data"] == [], order
        assert _block(shaped)["order"] == order, order


def test_negative_limit_returns_an_empty_list() -> None:
    records = _series("date", ["2026-01-01", "2026-02-01", "2026-03-01"])

    shaped = shape_response(_enveloped(records), limit=-2)

    assert shaped["data"] == []


# Sibling sub-series


def test_each_sibling_sub_series_is_judged_on_its_own() -> None:
    payload = {
        "data": {
            "annual_change": {
                "unit": "percent",
                "observations": _series("period", ["2025-09", "2025-10", "2026-06", "2026-07"]),
            },
            "monthly_change": {
                "unit": "percent",
                "observations": _series("date", ["2026-07-01", "2026-06-01", "2025-10-01"]),
            },
            "index": {
                "unit": "index",
                "observations": _series("period", ["2026-07"]),
            },
        },
        "meta": {},
    }

    shaped = shape_response(payload, limit=2)

    data = shaped["data"]
    annual = payload["data"]["annual_change"]["observations"]
    monthly = payload["data"]["monthly_change"]["observations"]
    assert data["annual_change"]["observations"] == annual[2:]
    assert data["monthly_change"]["observations"] == monthly[:2]
    assert data["index"]["observations"] == payload["data"]["index"]["observations"]
    assert data["annual_change"]["unit"] == "percent"
    block = _block(shaped)
    assert block["records_path"] == "data.*.observations"
    assert block["order"] == {
        "annual_change": "asc",
        "monthly_change": "desc",
        "index": "unknown",
    }
    assert block["kept_end"] == {
        "annual_change": "newest",
        "monthly_change": "newest",
        "index": "first",
    }


# What order detection never touches


def test_no_limit_adds_no_order_keys() -> None:
    records = _series("date", ["2026-01-01", "2026-02-01", "2026-03-01"])

    shaped = shape_response(_enveloped(records), fields=["value"])

    assert shaped["data"] == [{"value": 0}, {"value": 1}, {"value": 2}]
    assert "order" not in _block(shaped)
    assert "kept_end" not in _block(shaped)


def test_limit_without_a_records_list_adds_no_order_keys() -> None:
    shaped = shape_response({"data": {"symbol": "X", "price": 1}}, limit=1)

    assert _block(shaped)["limit_applied"] is False
    assert "order" not in _block(shaped)
    assert "kept_end" not in _block(shaped)


def test_order_is_read_before_fields_projection() -> None:
    """Projecting the date key away does not change which end is kept, and
    the never-empty rule still holds for an unmatched field."""
    records = _series("date", ["2026-01-01", "2026-02-01", "2026-03-01"])

    projected = shape_response(_enveloped(records), limit=2, fields=["value"])
    unmatched = shape_response(_enveloped(records), limit=2, fields=["missing"])

    assert projected["data"] == [{"value": 1}, {"value": 2}]
    assert _block(projected)["order"] == "asc"
    assert _block(projected)["fields_applied"] == ["value"]
    assert unmatched["data"] == records[1:]
    assert _block(unmatched)["fields_unmatched"] == ["missing"]
    assert _block(unmatched)["order"] == "asc"


def test_payload_is_not_mutated() -> None:
    records = _series("date", ["2026-01-01", "2026-02-01", "2026-03-01"])
    payload = _enveloped(records)
    before = [dict(record) for record in records]

    shape_response(payload, limit=1)

    assert payload["data"] == before
