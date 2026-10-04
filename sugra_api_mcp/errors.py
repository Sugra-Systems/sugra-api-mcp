"""What makes a tool result a failure.

Tools report failures by RETURNING a structured payload rather than raising:
raising loses the payload entirely and, when the exception carries no message,
leaves the agent with an empty string. Returning keeps the explanation intact.

The cost of that choice is that "failed" is a property of the payload, so the
definition has to live in exactly one place. It previously existed twice, once
per consumer, which is one edit away from two different answers to the same
question.
"""

from __future__ import annotations

from typing import Any


def is_error_payload(payload: Any) -> bool:
    """Whether a tool result reports a failure instead of carrying data.

    BOTH clauses are load-bearing. An `error` key alone is not enough: a 200
    response can carry a partial-degradation note alongside real `data`, and
    that is a success the caller should still receive - the ABSENCE of `data` is
    what makes the payload a failure. And the dict check is not a formality:
    endpoints legitimately return bare arrays and scalars, where a membership
    test would either raise or, on a string, match the substring "error" inside
    ordinary prose.
    """
    return isinstance(payload, dict) and "error" in payload and "data" not in payload


def server_busy_error(scope: str, limit: int, elapsed_ms: int = 0) -> dict[str, Any]:
    """The structured error for work refused at a concurrency limit.

    `scope` names the bound that refused it: "tool_calls" for the in-flight cap
    on tool calls in the process, "caller_tool_calls" for one caller's share of
    it, "search" for the catalog search queue, "caller_search" for one caller's
    share of the queue, "shaping" for the response shaping pool and
    "caller_shaping" for one caller's share of the pool. The scope also reaches
    the span as `mcp.busy.scope`, so observability._BUSY_SCOPES lists every
    value used here. `elapsed_ms` is the time spent waiting for a slot before
    the refusal. Nothing about the refused call itself is echoed back.
    """
    return {
        "error": "server_busy",
        "scope": scope,
        "limit": limit,
        "elapsed_ms": elapsed_ms,
        "retry_hint": "The server is at its concurrency limit. Retry in a few seconds.",
    }


# One fixed hint per bound of a fields projection. A hint never repeats the
# request: the field text stays with the caller, only the numbers come back.
_PROJECTION_HINTS: dict[str, str] = {
    "fields": "Name no more fields than the limit value in one call, or omit fields to keep every key.",
    "path_chars": "The field at field_index is longer than the limit value in characters: shorten its path.",
    "path_parts": "The field at field_index has more dotted parts than the limit value: use a shorter path.",
    "rows": (
        "The response holds more list items than one projection visits: "
        "pass the limit parameter or narrow the request."
    ),
    "shaping_ms": "The projection ran past its time limit: pass the limit parameter or narrow the request.",
    "raw_chars": "The response is too large to project: narrow the request, or omit fields.",
}


def projection_too_large_error(
    kind: str, limit: int, actual: int, field_index: int | None, operation_id: str, elapsed_ms: int
) -> dict[str, Any]:
    """The structured error for a fields projection past one of its bounds.

    `limit_kind` names the bound: "fields" for how many paths, "path_chars"
    for the length of one path, "path_parts" for the dotted parts of one path,
    "rows" for the list items one projection visits, "shaping_ms" for the time
    one projection runs and "raw_chars" for the response size before
    projection. `limit` and `actual` are its numbers, and `field_index` is the
    position (from 0) of the path for "path_chars" and "path_parts", else None.
    The field text itself is never echoed back.
    """
    return {
        "error": "projection_too_large",
        "limit_kind": kind,
        "limit": limit,
        "actual": actual,
        "field_index": field_index,
        "operation_id": operation_id,
        "elapsed_ms": elapsed_ms,
        "hint": _PROJECTION_HINTS[kind],
    }
