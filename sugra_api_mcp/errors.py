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
    """The structured error for work refused at a concurrency limit (MCP-26.1).

    `scope` names the bound that refused it: "tool_calls" for the in-flight cap
    on tool calls in the process, "caller_tool_calls" for one caller's share of
    it, "search" for the catalog search queue and "caller_search" for one
    caller's share of the queue. The scope also reaches the span as
    `mcp.busy.scope` (MCP-26.1.1), so observability._BUSY_SCOPES lists every
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
