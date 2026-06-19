"""Computed agent-facing hints for catalog endpoints.

The bundled catalog carries no latency or billing metadata, so hints are
derived at describe time from path and method heuristics grounded in how
the Sugra API serves each family:

- Most endpoints read blob-backed or cached snapshots and respond well
  under 2 seconds ("fast").
- Families that proxy live upstreams per request (network/Sugra Net Atlas,
  gleif, comtrade, wits, wto, gfw - verified against the API routers: no
  snapshot backing) usually take 1-5 seconds but can approach the gateway
  timeout when the upstream computes a cold result, and parallel calls
  contend for a shared upstream budget ("slow"). Mixed families (e.g. sec,
  gdelt, maritime - mostly snapshot-backed with a few live paths) stay
  "fast" at family level; the fast note is hedged accordingly.
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
    DURATION_FAST: (
        "typically responds in under ~2s; endpoints that fetch from the "
        "source on a cache miss can occasionally take longer"
    ),
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
# request rather than reading snapshots - verified against the API routers
# (no blob/snapshot reads). Mixed families (sec, gdelt, maritime) are NOT
# listed: most of their endpoints are snapshot-backed and a family-level
# "slow" would over-tag them.
_SLOW_FAMILIES = frozenset({"comtrade", "gfw", "gleif", "network", "wits", "wto"})

# Individually slow paths inside otherwise-fast families. The weather family is
# mixed: forecast/marine/us/air-quality read fast upstreams (10-15s client
# budget in the API), but flood (GloFAS heavy per-request compute), climate
# (CMIP6), and nws (api.weather.gov live) carry a 30s client budget and can
# approach the gateway timeout. Verified against prod-sugra-ai-API/helpers
# client _TIMEOUTs (flood/climate/noaa_nws = 30s). BUG-3.2: flood was labeled
# "fast", so an agent fired parallel calls with a short budget and hit 502s on
# cold cells. A family-level slow would over-tag the fast weather paths, so this
# is a per-path list. Re-verify the backing client _TIMEOUT before adding a path.
_SLOW_PATHS = (
    "/weather/flood",
    "/weather/climate",
    "/weather/nws",
)

_DEFAULT_MAX_CONCURRENCY = 4
# Field-tested on network: 3 parallel calls starve each other behind the
# shared upstream budget (the other live-proxy families share the same
# per-request upstream exposure). Conservative ceiling until server-side
# fanout hardening ships (NetAtlas-Imp-9).
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


def _is_slow_path(path: str) -> bool:
    """An individually slow path inside an otherwise-fast family (see _SLOW_PATHS).

    Segment-bounded: a key matches a whole `/weather/flood` segment (and any
    sub-path under it) but not a longer segment like `/weather/floodplain`.
    """
    bounded = path if path.endswith("/") else path + "/"
    return any((slow + "/") in bounded for slow in _SLOW_PATHS)


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
    elif _path_family(endpoint.path) in _SLOW_FAMILIES or _is_slow_path(endpoint.path):
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
