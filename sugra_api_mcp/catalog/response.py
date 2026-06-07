"""Response shaping helpers for gateway calls.

Shaping works with both payload shapes the Sugra API serves:

- envelope payloads ``{"data": ..., "meta": ...}`` - limit and fields apply
  to ``data``;
- envelope-less payloads (e.g. Sugra Net Atlas returns flat dicts like
  ``{"ip": ..., "asn": ..., "_meta": ...}``) - fields apply to the payload's
  own top-level keys, while the ``meta`` / ``_meta`` provenance keys always
  survive projection.

``fields`` entries support dotted paths into nested dicts (``geo.city``); a
literal key containing a dot wins over path traversal. ``meta.shaped``
reports what was ACTUALLY applied (``fields_applied`` / ``fields_unmatched``
and ``limit_applied``), never just an echo of the request - a silent
fields no-op on envelope-less payloads was a live field-test defect
(2026-06-07).
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from ..client import MAX_RESPONSE_CHARS

# Provenance keys preserved on envelope-less payloads even when a fields
# projection does not request them.
_PRESERVED_KEYS = ("meta", "_meta")


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


def _apply_limit(value: Any, limit: int | None) -> Any:
    if limit is None:
        return value
    if isinstance(value, list):
        return value[: max(0, limit)]
    return value


def _shaped_meta_block(
    *,
    limit: int | None,
    limit_applied: bool,
    fields: list[str] | None,
    matched: set[str],
) -> dict[str, Any]:
    requested = list(fields or [])
    return {
        "limit": limit,
        "limit_applied": limit_applied,
        "fields": requested,
        "fields_applied": [field for field in requested if field in matched],
        "fields_unmatched": [field for field in requested if field not in matched],
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
        # Bare-array payload (no envelope at all). Untouched unless shaping
        # was requested; shaping wraps it so meta.shaped has a place to live.
        if not shaping_requested and not include_raw:
            return payload
        limited = _apply_limit(payload, limit)
        projected = _project_value(limited, fields, matched) if fields else limited
        shaped: dict[str, Any] = {"data": projected}
        if shaping_requested:
            _attach_meta(
                shaped,
                _shaped_meta_block(
                    limit=limit, limit_applied=limit is not None,
                    fields=fields, matched=matched,
                ),
            )
        return _maybe_include_raw(shaped, original, include_raw, max_raw_chars)

    if not isinstance(payload, dict):
        # Scalar JSON payload - nothing to shape.
        return payload

    shaped = deepcopy(payload)
    limit_applied = False

    if "data" in shaped:
        target = _apply_limit(shaped["data"], limit)
        limit_applied = limit is not None and isinstance(shaped["data"], list)
        if fields:
            target = _project_value(target, fields, matched)
        shaped["data"] = target
    elif fields:
        # Envelope-less payload: project the payload's own keys, then make
        # sure provenance keys survive even when not requested.
        projected = _project_dict(shaped, fields, matched)
        for key in _PRESERVED_KEYS:
            if key in shaped:
                projected.setdefault(key, shaped[key])
        shaped = projected

    if shaping_requested:
        _attach_meta(
            shaped,
            _shaped_meta_block(
                limit=limit, limit_applied=limit_applied,
                fields=fields, matched=matched,
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
