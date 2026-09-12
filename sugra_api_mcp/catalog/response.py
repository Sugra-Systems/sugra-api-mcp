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
from copy import deepcopy
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


def _apply_limit(value: Any, limit: int | None) -> Any:
    if limit is None:
        return value
    if isinstance(value, list):
        return value[: max(0, limit)]
    return value


def _shape_data(
    data: Any,
    *,
    limit: int | None,
    fields: list[str] | None,
    matched: set[str],
) -> tuple[Any, bool, str | None]:
    """Shape an envelope's ``data`` value.

    Returns ``(data, limit_applied, records_path)``. ``records_path`` names
    the records list that limit bounded or that fields were matched against,
    and is None when shaping used no records list.
    """
    if isinstance(data, list):
        limited = _apply_limit(data, limit)
        if fields:
            limited, _ = _project_or_keep(limited, fields, matched)
        return limited, limit is not None, "data"

    key = _records_key(data)
    if key is None:
        # Scalar data, or an object without a single record list: limit
        # never applies, and fields project the object's own keys or leave
        # it whole.
        if fields:
            data, _ = _project_or_keep(data, fields, matched)
        return data, False, None

    shaped = dict(data)
    records_path: str | None = None
    if limit is not None:
        shaped[key] = _apply_limit(shaped[key], limit)
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
    return shaped, limit is not None, records_path


def _shaped_meta_block(
    *,
    limit: int | None,
    limit_applied: bool,
    fields: list[str] | None,
    matched: set[str],
    records_path: str | None,
) -> dict[str, Any]:
    requested = list(fields or [])
    return {
        "limit": limit,
        "limit_applied": limit_applied,
        "fields": requested,
        "fields_applied": [field for field in requested if field in matched],
        "fields_unmatched": [field for field in requested if field not in matched],
        "records_path": records_path,
    }


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
        # pydantic dict_type message and no rows (MCP-7.1).
        data, limit_applied, records_path = _shape_data(
            payload, limit=limit, fields=fields, matched=matched
        )
        shaped: dict[str, Any] = {"data": data}
        if shaping_requested:
            _attach_meta(
                shaped,
                _shaped_meta_block(
                    limit=limit, limit_applied=limit_applied,
                    fields=fields, matched=matched, records_path=records_path,
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

    if "data" in shaped:
        shaped["data"], limit_applied, records_path = _shape_data(
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
