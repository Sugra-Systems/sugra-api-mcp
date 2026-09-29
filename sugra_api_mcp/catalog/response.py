"""Response shaping helpers for gateway calls.

Shaping works with both payload shapes the Sugra API serves:

- envelope payloads ``{"data": ..., "meta": ...}`` - limit and fields apply
  to ``data``;
- envelope-less payloads (e.g. Sugra Net Atlas returns flat dicts like
  ``{"ip": ..., "asn": ..., "_meta": ...}``) - fields apply to the payload's
  own top-level keys, while the ``meta`` / ``_meta`` provenance keys always
  survive projection.

``limit`` bounds the records list and ``fields`` projects it record by
record. The records list is the ``data`` list, a bare top-level array, or
the single record list inside an object ``data`` (news_latest sends
``data: {total, count, items: [...]}``). Keys beside that list, such as
``total`` and ``count``, stay exactly as the API sent them. When a requested
field names one of an object ``data``'s own keys, that object is projected
instead, so single-record payloads such as a quote keep their behaviour.

A third shape is sibling sub-series: an object ``data`` whose values are all
dicts, each holding exactly one list under ``observations`` one level down
(``data: {annual_change: {..., observations: [...]}, monthly_change: {...},
index: {...}}``). ``limit`` bounds every sub-series independently instead of
picking one; a payload that only partly matches (a scalar sibling, a sibling
without an ``observations`` list) keeps today's behaviour exactly.

``limit`` keeps the newest end of a records list whose order it can read:
every record carries exactly one of the known date or period keys at its
top level, all values share one format, and the whole list runs one way by
them (ISO 8601 date-times by the moment they name, so differing UTC
offsets compare correctly; another format only when it is a year alone or
a year and one part, such as a month or a quarter). An ascending list
keeps its last N records, a descending one its first N. Anything else
keeps the first N records, as limit always did. When a
limit was applied to a records list, ``meta.shaped`` reports ``order``
(``asc``, ``desc`` or ``unknown``) and ``kept_end`` (``newest`` or
``first``), as maps keyed by sibling name for sibling sub-series.

Shaping never empties a response: when no requested field matches, the
target is returned unprojected and every field is reported unmatched.

``fields`` entries support dotted paths into nested dicts (``geo.city``); a
literal key containing a dot wins over path traversal. ``meta.shaped``
reports what was ACTUALLY applied (``fields_applied`` / ``fields_unmatched``,
``limit_applied`` and ``records_path``), never just an echo of the request.
Two live defects shaped these rules: fields were a silent no-op on
envelope-less payloads (2026-06-07), and fields on news_latest returned
``data: {}`` while limit left all 50 items in place (2026-09-12).
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime
from itertools import pairwise
from typing import Any

from ..client import MAX_RESPONSE_CHARS

# Provenance keys preserved on envelope-less payloads even when a fields
# projection does not request them.
_PRESERVED_KEYS = ("meta", "_meta")

# Keys under which the Sugra API puts the record list inside an object
# ``data``. Taken from a census of the API's OpenAPI response models (typed
# from live samples) and its stored response samples on 2026-09-12: each name
# holds a list of objects wherever it is typed, and no operation or sample
# carries two of them as lists. Arrays that are not records stay out on
# purpose; the indicators ``time_period`` array (108 operations) lists the
# integer look-back periods, not observations. A payload whose record list
# sits under a name missing here keeps its ``data`` whole, and meta.shaped
# says so (limit_applied false, records_path null).
_RECORD_LIST_KEYS = frozenset(
    {
        "data",
        "entries",
        "events",
        "history",
        "items",
        "observations",
        "points",
        "records",
        "results",
        "rows",
        "series",
        "timeseries",
    }
)

# Top-level record keys that carry a date, time or period in the API's
# response samples. A name match alone decides nothing: the values must also
# share one format and run one way through the whole list, so a key that is
# present but not the sort key leaves the list on the first-N path. "data"
# is here as a record key (a date in some sources), never the envelope's own.
_DATE_KEY_CANDIDATES = frozenset(
    {
        "AUC_BOND_AUCTION_DATE",
        "AUC_TBILL_AUCTION_DATE",
        "Reference",
        "Start Date",
        "TIME_PERIOD",
        "TimeDim",
        "TimePeriod",
        "announcement_date",
        "asOfDate",
        "as_of",
        "auction_date",
        "claimDate",
        "created_time",
        "d",
        "data",
        "data_time",
        "date",
        "date_added",
        "date_announced",
        "date_published",
        "date_reported",
        "datum_primjene",
        "end",
        "ex_dividend_date",
        "execution_date",
        "expiration_date",
        "fecha",
        "filing_date",
        "fiscal_year",
        "from_date",
        "issueDate",
        "last_update",
        "last_updated",
        "latest_transaction_date",
        "metadata_modified",
        "month",
        "period",
        "period_current",
        "period_end",
        "periodo",
        "position_date",
        "price_date",
        "pubDate",
        "published",
        "quarter",
        "record_date",
        "release_date",
        "report_date",
        "report_date_as_yyyy_mm_dd",
        "settlement_date",
        "snapshot_date",
        "source_event_time",
        "start",
        "start_date",
        "start_time",
        "survey_date",
        "t",
        "time",
        "timeLabel",
        "timePeriodStart",
        "time_period",
        "time_tag",
        "timestamp",
        "updated_time",
        "valid_for",
        "valid_time",
        "value_date",
        "week_ending",
        "year",
    }
)

_ASCII_DIGITS = frozenset("0123456789")

# A year alone, or a year and ONE part after it (digits read as "#"): an
# optional separator, an optional capital letter, one to three digits, as in
# 2024, 2024-07, 202407, 2024-Q3, 2024M11, 2024H1 or 2024-189. Whatever that
# part means, a list of one such shape runs year first and then the part, so
# text order is date order, and there is no time of day to carry an offset.
# Two or more parts after the year could run month then day or day then
# month, which a shape cannot tell. Only consulted for values that do not
# parse as ISO 8601, where the standard names every part.
_YEAR_AND_ONE_PART_SHAPE = re.compile(r"####(?:[-/.]?[A-Z]?#{1,3})?")

ORDER_ASC = "asc"
ORDER_DESC = "desc"
ORDER_UNKNOWN = "unknown"
KEPT_NEWEST = "newest"
KEPT_FIRST = "first"


def _split_field_path(field: str) -> list[str]:
    return [segment for segment in field.split(".") if segment]


def _project_dict(value: dict[str, Any], fields: list[str], matched: set[str]) -> dict[str, Any]:
    """Project one dict by the requested fields.

    A literal key match (including keys that contain dots) takes precedence;
    otherwise the field is treated as a dotted path through nested dicts.
    Matched field names are recorded so the caller can report what actually
    applied.
    """
    result: dict[str, Any] = {}
    for field in fields:
        if field in value:
            result[field] = value[field]
            matched.add(field)
            continue
        segments = _split_field_path(field)
        if len(segments) < 2:
            continue
        node: Any = value
        for segment in segments[:-1]:
            node = node.get(segment) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict) and segments[-1] in node:
            cursor = result
            for segment in segments[:-1]:
                existing = cursor.get(segment)
                if not isinstance(existing, dict):
                    existing = {}
                    cursor[segment] = existing
                cursor = existing
            cursor[segments[-1]] = node[segments[-1]]
            matched.add(field)
    return result


def _project_value(value: Any, fields: list[str], matched: set[str]) -> Any:
    if isinstance(value, dict):
        return _project_dict(value, fields, matched)
    if isinstance(value, list):
        return [_project_value(item, fields, matched) for item in value]
    return value


def _project_or_keep(value: Any, fields: list[str], matched: set[str]) -> tuple[Any, bool]:
    """Project ``value``, or return it whole when no field matches anything.

    This is the never-empty rule: a projection that matched nothing would
    hand back ``{}`` or a list of ``{}`` and read as a successful empty
    result. The flag says whether any field matched.
    """
    hits: set[str] = set()
    projected = _project_value(value, fields, hits)
    if not hits:
        return value, False
    matched.update(hits)
    return projected, True


def _records_key(data: Any) -> str | None:
    """Name the single record list inside an object ``data``.

    Exactly one allowlisted key must hold a list. None, or two or more, means
    there is no records list: picking one of two lists could bound or
    project the wrong one.
    """
    if not isinstance(data, dict):
        return None
    keys = [
        key for key, value in data.items()
        if key in _RECORD_LIST_KEYS and isinstance(value, list)
    ]
    return keys[0] if len(keys) == 1 else None


def _nested_series_keys(data: Any) -> list[str] | None:
    """Name the sibling sub-series inside an object ``data`` whose values are
    all dicts, each holding exactly one list, under ``observations`` one
    level down (three named sub-series each carrying its own observation
    list, rather than one list at the top).

    None when ``data`` is empty, carries a non-dict value, or any sub-dict's
    lists are not exactly a single list named ``observations`` - any of
    which keeps today's behaviour (no records list found).
    """
    if not isinstance(data, dict) or not data:
        return None
    keys: list[str] = []
    for key, value in data.items():
        if not isinstance(value, dict):
            return None
        list_keys = [sub_key for sub_key, sub_value in value.items() if isinstance(sub_value, list)]
        if list_keys != ["observations"]:
            return None
        keys.append(key)
    return keys


def _shape_nested_series(
    data: dict[str, Any],
    nested_keys: list[str],
    *,
    limit: int | None,
    fields: list[str] | None,
    matched: set[str],
) -> tuple[Any, bool, str | None, dict[str, Any] | None]:
    """Bound each sibling sub-series independently.

    Each sub-series list is cut on its own by the single-list rule
    (``_limit_records``), its order read from its own records, so siblings
    sorted in different directions each keep their newest end. The order
    report is ``{"order": {name: ...}, "kept_end": {name: ...}}``.
    ``fields`` only matches ``data``'s own keys here, same as any object
    without a records list, so a field this shape does not understand never
    empties a sub-series.
    """
    shaped = dict(data)
    limit_applied = False
    order_report: dict[str, Any] | None = None
    if limit is not None:
        orders: dict[str, str] = {}
        kept_ends: dict[str, str] = {}
        for key in nested_keys:
            sub_series = dict(shaped[key])
            sub_series["observations"], orders[key], kept_ends[key] = _limit_records(
                sub_series["observations"], limit
            )
            shaped[key] = sub_series
        limit_applied = True
        order_report = {"order": orders, "kept_end": kept_ends}
    records_path = "data.*.observations" if limit_applied else None
    if fields:
        projected, own_match = _project_or_keep(shaped, fields, matched)
        if own_match:
            shaped = projected
    return shaped, limit_applied, records_path, order_report


def _is_number(value: Any) -> bool:
    # bool subclasses int, but True and False are never dates.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _string_template(value: str) -> str:
    return "".join("#" if char in _ASCII_DIGITS else char for char in value)


def _order_keys(keys: list[Any]) -> list[Any] | None:
    """The keys in a form whose natural order is date order, or None.

    Numbers compare numerically (years, epoch seconds). Strings must all
    have the same shape, with each ASCII digit read as a placeholder and
    every other character kept literally, and that shape must start with a
    four-digit year. Day-first and month-first dates, month names and
    ordinal codes such as ``-1m`` fail here. Any boolean, or numbers mixed
    with strings, fails.

    Values that parse as ISO 8601 compare as the moments they name, so a
    UTC offset that varies still orders correctly; they must all parse and
    all carry an offset or all lack one. A value that does not parse
    compares as text only when it is a year alone or a year and one part
    after it (``_YEAR_AND_ONE_PART_SHAPE``), where text order is date order
    whatever the part means. Any other shape needs a convention the shape
    cannot show (month then day or day then month, a UTC offset), and fails
    the whole list.
    """
    if all(_is_number(key) for key in keys):
        return keys
    if not all(isinstance(key, str) for key in keys):
        return None
    template = _string_template(keys[0])
    if not template.startswith("####"):
        return None
    if not all(_string_template(key) == template for key in keys[1:]):
        return None
    if _iso_moment(keys[0]) is not None:
        return _iso_moments(keys)
    if _YEAR_AND_ONE_PART_SHAPE.fullmatch(template) is None:
        return None
    return keys


def _iso_moment(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _iso_moments(keys: list[str]) -> list[datetime] | None:
    moments = []
    for key in keys:
        moment = _iso_moment(key)
        if moment is None:
            return None
        moments.append(moment)
    # Aware and naive values never compare with each other.
    if len({moment.utcoffset() is None for moment in moments}) != 1:
        return None
    return moments


def _records_order(records: list[Any]) -> str:
    """Read the order of a records list from its one date or period key.

    ``asc`` or ``desc`` only when exactly one known key is present and
    non-null on every record, its values are comparable, the first and last
    differ, and the whole list runs one way (ties allowed). Otherwise
    ``unknown``.
    """
    if len(records) <= 1 or not all(isinstance(record, dict) for record in records):
        return ORDER_UNKNOWN
    present = [
        key for key in _DATE_KEY_CANDIDATES
        if all(record.get(key) is not None for record in records)
    ]
    if len(present) != 1:
        return ORDER_UNKNOWN
    keys = _order_keys([record[present[0]] for record in records])
    if keys is None or keys[0] == keys[-1]:
        return ORDER_UNKNOWN
    pairs = list(pairwise(keys))
    if all(earlier <= later for earlier, later in pairs):
        return ORDER_ASC
    if all(earlier >= later for earlier, later in pairs):
        return ORDER_DESC
    return ORDER_UNKNOWN


def _limit_records(records: list[Any], limit: int) -> tuple[list[Any], str, str]:
    """Bound a records list, keeping its newest end when the order is known.

    Returns ``(records, order, kept_end)``. A descending list keeps its first
    ``limit`` records and an ascending one its last ``limit``, in their
    original order. Without an established order the first ``limit`` records
    are kept, exactly as before order was read.
    """
    count = max(0, limit)
    order = _records_order(records)
    if order == ORDER_ASC:
        if count == 0:
            # records[-0:] would be the whole list.
            return [], order, KEPT_NEWEST
        return records[max(0, len(records) - count):], order, KEPT_NEWEST
    if order == ORDER_DESC:
        return records[:count], order, KEPT_NEWEST
    return records[:count], order, KEPT_FIRST


def _shape_data(
    data: Any,
    *,
    limit: int | None,
    fields: list[str] | None,
    matched: set[str],
) -> tuple[Any, bool, str | None, dict[str, Any] | None]:
    """Shape an envelope's ``data`` value.

    Returns ``(data, limit_applied, records_path, order_report)``.
    ``records_path`` names the records list that limit bounded or that
    fields were matched against, and is None when shaping used no records
    list. ``order_report`` holds the ``order`` and ``kept_end`` of the
    bounded records, and is None when no limit was applied to a records
    list. Order is read before any projection.
    """
    if isinstance(data, list):
        limited = data
        order_report: dict[str, Any] | None = None
        if limit is not None:
            limited, order, kept_end = _limit_records(data, limit)
            order_report = {"order": order, "kept_end": kept_end}
        if fields:
            limited, _ = _project_or_keep(limited, fields, matched)
        return limited, limit is not None, "data", order_report

    key = _records_key(data)
    if key is None:
        nested_keys = _nested_series_keys(data)
        if nested_keys is not None:
            return _shape_nested_series(
                data, nested_keys, limit=limit, fields=fields, matched=matched
            )
        # Scalar data, or an object without a single record list: limit
        # never applies, and fields project the object's own keys or leave
        # it whole.
        if fields:
            data, _ = _project_or_keep(data, fields, matched)
        return data, False, None, None

    shaped = dict(data)
    records_path: str | None = None
    order_report = None
    if limit is not None:
        shaped[key], order, kept_end = _limit_records(shaped[key], limit)
        order_report = {"order": order, "kept_end": kept_end}
        records_path = f"data.{key}"
    if fields:
        projected, own_match = _project_or_keep(shaped, fields, matched)
        if own_match:
            # A field names one of data's own keys: project the object as a
            # single record, exactly as before records lists were known.
            shaped = projected
        else:
            shaped[key], _ = _project_or_keep(shaped[key], fields, matched)
            records_path = f"data.{key}"
    return shaped, limit is not None, records_path, order_report


def _shaped_meta_block(
    *,
    limit: int | None,
    limit_applied: bool,
    fields: list[str] | None,
    matched: set[str],
    records_path: str | None,
    order_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    requested = list(fields or [])
    block = {
        "limit": limit,
        "limit_applied": limit_applied,
        "fields": requested,
        "fields_applied": [field for field in requested if field in matched],
        "fields_unmatched": [field for field in requested if field not in matched],
        "records_path": records_path,
    }
    if order_report is not None:
        block.update(order_report)
    return block


def _attach_meta(shaped: dict[str, Any], block: dict[str, Any]) -> None:
    existing = shaped.get("meta")
    meta = dict(existing) if isinstance(existing, dict) else {}
    meta["shaped"] = block
    shaped["meta"] = meta


def shape_response(
    payload: Any,
    *,
    limit: int | None = None,
    fields: list[str] | None = None,
    include_raw: bool = False,
    max_raw_chars: int = MAX_RESPONSE_CHARS,
) -> Any:
    """Apply list limit, field projection, and optional raw payload inclusion."""
    original = deepcopy(payload)
    shaping_requested = limit is not None or bool(fields)
    matched: set[str] = set()

    if isinstance(payload, list):
        # Bare-array payload. Always wrap in {data: ...}: FastMCP validates
        # call_endpoint/fetch_data against dict[str, Any], and MCP
        # CallToolResult.structuredContent is dict-only. Returning the list
        # unchanged made a successful call arrive as isError with a
        # pydantic dict_type message and no rows.
        data, limit_applied, records_path, order_report = _shape_data(
            payload, limit=limit, fields=fields, matched=matched
        )
        shaped: dict[str, Any] = {"data": data}
        if shaping_requested:
            _attach_meta(
                shaped,
                _shaped_meta_block(
                    limit=limit, limit_applied=limit_applied,
                    fields=fields, matched=matched, records_path=records_path,
                    order_report=order_report,
                ),
            )
        return _maybe_include_raw(shaped, original, include_raw, max_raw_chars)

    if not isinstance(payload, dict):
        # Scalar JSON. Same dict contract as a bare array: wrap so the MCP
        # tool result is never a non-object. If the caller asked for
        # shaping, report the no-op (limit never applies to a scalar;
        # fields never match) instead of silently dropping meta.shaped.
        shaped = {"data": payload}
        if shaping_requested:
            _attach_meta(
                shaped,
                _shaped_meta_block(
                    limit=limit, limit_applied=False,
                    fields=fields, matched=matched, records_path=None,
                ),
            )
        return _maybe_include_raw(shaped, original, include_raw, max_raw_chars)

    shaped = deepcopy(payload)
    limit_applied = False
    records_path: str | None = None
    order_report: dict[str, Any] | None = None

    if "data" in shaped:
        shaped["data"], limit_applied, records_path, order_report = _shape_data(
            shaped["data"], limit=limit, fields=fields, matched=matched
        )
    elif fields:
        # Envelope-less payload: project the payload's own keys, then make
        # sure provenance keys survive even when not requested. When no
        # field matches, the payload stays whole.
        projected, any_match = _project_or_keep(shaped, fields, matched)
        if any_match:
            for key in _PRESERVED_KEYS:
                if key in shaped:
                    projected.setdefault(key, shaped[key])
            shaped = projected

    if shaping_requested:
        _attach_meta(
            shaped,
            _shaped_meta_block(
                limit=limit, limit_applied=limit_applied,
                fields=fields, matched=matched, records_path=records_path,
                order_report=order_report,
            ),
        )

    return _maybe_include_raw(shaped, original, include_raw, max_raw_chars)


def _maybe_include_raw(
    shaped: dict[str, Any],
    original: Any,
    include_raw: bool,
    max_raw_chars: int,
) -> dict[str, Any]:
    if not include_raw:
        return shaped
    raw_json = json.dumps(original)
    if len(raw_json) <= max_raw_chars:
        shaped["raw"] = original
    else:
        _attach_raw_omitted(shaped, max_raw_chars, len(raw_json))
    return shaped


def _attach_raw_omitted(shaped: dict[str, Any], max_raw_chars: int, actual_chars: int) -> None:
    existing = shaped.get("meta")
    meta = dict(existing) if isinstance(existing, dict) else {}
    meta["raw_omitted"] = {
        "reason": "exceeds_raw_size_limit",
        "max_raw_chars": max_raw_chars,
        "actual_chars": actual_chars,
    }
    shaped["meta"] = meta
