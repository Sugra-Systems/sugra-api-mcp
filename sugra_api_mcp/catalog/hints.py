"""Computed agent-facing hints for catalog endpoints.

The bundled catalog carries no latency or billing metadata, so hints are
derived at describe time from path and method heuristics grounded in how
the Sugra API serves each family:

- Most endpoints read blob-backed or cached snapshots and respond well
  under 2 seconds ("fast").
- The network family (Sugra Net Atlas) proxies live registry upstreams
  per request; single lookups usually take 1-5 seconds but can approach
  the gateway timeout when the upstream computes a cold result, and
  parallel calls contend for a shared upstream budget ("slow").
- Bulk endpoints process one upstream round per item in the request body;
  large batches can exceed the gateway timeout entirely ("heavy"), and
  each item bills one request credit (the API reports the total in the
  X-RateLimit-Cost response header).

Hints are computed, not stored: they ship with the gateway code and evolve
without a catalog rebuild.
"""

from __future__ import annotations

from typing import Any

from .models import Endpoint

DURATION_FAST = "fast"
DURATION_SLOW = "slow"
DURATION_HEAVY = "heavy"

_DURATION_NOTES = {
    DURATION_FAST: "typically responds in under ~2s (snapshot or cache-backed read)",
    DURATION_SLOW: (
        "proxies live upstream services per request: usually 1-5s, "
        "occasionally 15s+ on a cold upstream computation"
    ),
    DURATION_HEAVY: (
        "processes each submitted item against live upstreams: large batches "
        "can exceed the gateway timeout, keep batches small"
    ),
}

# Path families (first segment after /api/vN/) that proxy live upstreams per
# request rather than reading snapshots.
_SLOW_FAMILIES = frozenset({"network"})

_DEFAULT_MAX_CONCURRENCY = 4
# Field-tested: 3 parallel network calls starve each other behind the shared
# upstream budget. Advertise a low ceiling until server-side fanout hardening
# ships (NetAtlas-Imp-9).
_SLOW_MAX_CONCURRENCY = 2
_HEAVY_MAX_CONCURRENCY = 1

_BULK_COST_NOTE = (
    "1 request credit per item in the request body (the API reports the total "
    "via the X-RateLimit-Cost response header). Prefer small batches: large "
    "batches can exceed the gateway timeout."
)


def _path_family(path: str) -> str:
    parts = [segment for segment in path.split("/") if segment]
    # Expected shape: api / v1 / family / ...
    if len(parts) >= 3 and parts[0] == "api":
        return parts[2]
    return parts[0] if parts else ""


def _is_per_item_bulk(endpoint: Endpoint) -> bool:
    """POST endpoints that fan out upstream work per body item and bill per item."""
    return endpoint.method == "POST" and "/bulk/" in endpoint.path


def hints_for(endpoint: Endpoint) -> dict[str, Any]:
    """Return computed agent hints for one endpoint.

    Keys:
        duration_class   - fast | slow | heavy (see _DURATION_NOTES)
        duration_note    - human-readable explanation of the class
        max_concurrency  - advisory ceiling for parallel calls to this
                           endpoint (and its family) from one agent session
        bulk_cost        - billing note, only on per-item bulk endpoints
    """
    if _is_per_item_bulk(endpoint):
        duration = DURATION_HEAVY
        max_concurrency = _HEAVY_MAX_CONCURRENCY
    elif _path_family(endpoint.path) in _SLOW_FAMILIES:
        duration = DURATION_SLOW
        max_concurrency = _SLOW_MAX_CONCURRENCY
    else:
        duration = DURATION_FAST
        max_concurrency = _DEFAULT_MAX_CONCURRENCY

    hints: dict[str, Any] = {
        "duration_class": duration,
        "duration_note": _DURATION_NOTES[duration],
        "max_concurrency": max_concurrency,
    }
    if _is_per_item_bulk(endpoint):
        hints["bulk_cost"] = _BULK_COST_NOTE
    return hints
