"""Response shaping helpers for gateway calls."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from ..client import MAX_RESPONSE_CHARS


def _filter_fields(value: Any, fields: list[str] | None) -> Any:
    if not fields:
        return value
    allowed = set(fields)
    if isinstance(value, dict):
        return {key: item for key, item in value.items() if key in allowed}
    if isinstance(value, list):
        return [_filter_fields(item, fields) for item in value]
    return value


def _apply_limit(value: Any, limit: int | None) -> Any:
    if limit is None:
        return value
    if isinstance(value, list):
        return value[: max(0, limit)]
    return value


def shape_response(
    payload: dict[str, Any],
    *,
    limit: int | None = None,
    fields: list[str] | None = None,
    include_raw: bool = False,
    max_raw_chars: int = MAX_RESPONSE_CHARS,
) -> dict[str, Any]:
    """Apply list limit, field projection, and optional raw payload inclusion."""
    original = deepcopy(payload)
    shaped = deepcopy(payload)
    if "data" in shaped:
        shaped["data"] = _filter_fields(_apply_limit(shaped["data"], limit), fields)

    if limit is not None or fields:
        meta = dict(shaped.get("meta") or {})
        meta["shaped"] = {
            "limit": limit,
            "fields": fields or [],
        }
        shaped["meta"] = meta

    if include_raw:
        raw_json = json.dumps(original)
        if len(raw_json) <= max_raw_chars:
            shaped["raw"] = original
        else:
            meta = dict(shaped.get("meta") or {})
            meta["raw_omitted"] = {
                "reason": "exceeds_raw_size_limit",
                "max_raw_chars": max_raw_chars,
                "actual_chars": len(raw_json),
            }
            shaped["meta"] = meta
    return shaped
