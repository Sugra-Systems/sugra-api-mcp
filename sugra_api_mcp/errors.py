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
