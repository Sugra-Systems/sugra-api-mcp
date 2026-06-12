"""Agent Context Layer tools - HOSTED-ONLY wrappers over /internal/agent/v1.

Three thin read-only tools expose the M1 internal agent plane (API PR #224,
design: sugra-internal-docs/docs/AGENT_CONTEXT_LAYER.md) to MCP agents:

- ``resolve_entity``    - free text -> canonical market/macro entity ids.
- ``get_snapshot``      - entity + recipe -> composed current view.
- ``get_timeseries``    - entity + metric -> bounded series.

Unlike every other tool module, this one does NOT register at import. The
plane requires the ``X-Internal-Token`` infrastructure credential
(``SUGRA_AGENT_INTERNAL_TOKEN``) which exists ONLY on the hosted deployment
(app.sugra.ai/mcp) and can never ship inside the public PyPI package, so:

1. ``__main__`` calls :func:`register_agent_tools` from the streamable-http
   branch only - a stdio process never registers these tools even if the env
   var leaks into its environment (transport-aware gate).
2. ``register_agent_tools`` itself refuses to register without a non-empty
   token (env gate) - a misconfigured hosted deployment serves the classic
   surface and logs loudly instead of exposing tools that can only 403.

Naming note: ``resolve_entity`` resolves MARKET/MACRO entities (tickers,
companies, macro series, coins, currency pairs) into the agent plane's
``{namespace, ids}`` shape for use with ``get_snapshot``/``get_timeseries``.
For compliance KYB lookups (LEI/VAT anchors, sanctions screening) use the
separate ``sugra_entity_lookup`` / ``sugra_entity_screen`` tools - a
different product surface over the public /api/v1/entity/* endpoints.

Envelopes pass through as-is: the plane's response contract (schema_version 1:
freshness / provenance / coverage / billing) is already agent-compact by
design - reshaping it here would desync the MCP surface from the documented
contract.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..observability import trace_mcp_tool
from ..server import get_client, mcp, read_only

_PLANE_BASE = "/internal/agent/v1"
_TOKEN_ENV = "SUGRA_AGENT_INTERNAL_TOKEN"

# Idempotence latch for the GLOBAL mcp instance only. Explicit instances
# (tests) are the caller's responsibility - they are never latched so a test
# can exercise registration repeatedly against fresh FastMCP objects.
_registered_global = False

# Envelope statuses allowed onto telemetry spans. Anything outside this set is
# dropped so free-text upstream values can never reach App Insights.
_KNOWN_STATUSES = frozenset({"full", "partial", "resolved", "ambiguous", "none"})

logger = logging.getLogger("sugra_mcp.agent")


def _internal_headers() -> dict[str, str]:
    """Per-request plane credential. Read at CALL time, not import time, so a
    token rotation (env update + service restart) needs no code path change."""
    return {"X-Internal-Token": os.environ.get(_TOKEN_ENV, "").strip()}


def _map_plane_error(result: Any) -> Any:
    """Give the infra-level 403 its own error code.

    The plane returns 403 when the INTERNAL token is wrong or unset - an
    infrastructure credential the calling agent cannot see or fix. The generic
    HTTP-error dict would invite pointless retries, so it is remapped to a
    distinct structured code (also allowlisted in observability). 401 (the
    user's API key) intentionally stays generic - that one IS caller-fixable.
    """
    if isinstance(result, dict) and result.get("status_code") == 403:
        return {
            "error": "agent_plane_unavailable",
            "reason": "The internal agent plane rejected the gateway's infrastructure credential.",
            "status_code": 403,
            "url": result.get("url"),
            "elapsed_ms": result.get("elapsed_ms"),
            "retry_hint": (
                "Hosted gateway infrastructure issue (internal plane token), NOT your "
                "API key. Retrying will not help. Use the classic gateway tools "
                "(fetch_data / call_endpoint) for raw endpoints, and report this to "
                "support@sugra.systems."
            ),
        }
    return result


def _agent_result_attrs(result: Any) -> dict[str, Any]:
    """Span attributes from RESPONSE envelope metadata only (privacy-by-design).

    Extracts the bounded, non-identifying fields the MCP-2.3 card names:
    recipe_version, units (billing rate_limit_cost), downstream_calls, status
    class, stale flag. Request values (query, entity ids) NEVER reach spans.
    """
    attrs: dict[str, Any] = {}
    if not isinstance(result, dict):
        return attrs
    recipe_version = result.get("recipe_version")
    if isinstance(recipe_version, str):
        attrs["mcp.agent.recipe_version"] = recipe_version
    status = result.get("status")
    if isinstance(status, str) and status in _KNOWN_STATUSES:
        attrs["mcp.agent.status"] = status
    billing = result.get("billing")
    if isinstance(billing, dict):
        units = billing.get("rate_limit_cost")
        if isinstance(units, int):
            attrs["mcp.agent.units"] = units
        downstream = billing.get("downstream_calls")
        if isinstance(downstream, int):
            attrs["mcp.agent.downstream_calls"] = downstream
    freshness = result.get("freshness")
    if isinstance(freshness, dict) and isinstance(freshness.get("stale"), bool):
        attrs["mcp.agent.stale"] = freshness["stale"]
    return attrs


@trace_mcp_tool("resolve_entity", result_attrs=_agent_result_attrs)
async def resolve_entity(query: str, type_hint: str | None = None) -> dict[str, Any]:
    """Resolve free text to a canonical market or macro entity.

    Turns a ticker, company name, macro indicator, coin, or currency pair into
    the agent plane's ``{namespace, ids}`` entity for use with get_snapshot and
    get_timeseries. A cross-namespace collision (e.g. a ticker that is both an
    equity and a coin) returns status "ambiguous" with ranked candidates and
    NEVER silently picks one; pass type_hint (e.g. "equity", "etf", "coin") to
    narrow the universe. For compliance KYB lookups by LEI/VAT or sanctions
    screening use sugra_entity_lookup / sugra_entity_screen instead - this tool
    is for market-data entities.

    Args:
        query: Free-form text - ticker, company, indicator, coin, or pair.
        type_hint: Optional namespace hint narrowing resolution.
    """
    result = await get_client().post(
        f"{_PLANE_BASE}/resolve",
        json={"query": query, "type_hint": type_hint},
        headers=_internal_headers(),
    )
    return _map_plane_error(result)


@trace_mcp_tool("get_snapshot", result_attrs=_agent_result_attrs)
async def get_snapshot(recipe: str, entity: dict[str, Any]) -> dict[str, Any]:
    """Composed current view of an entity via a named recipe.

    Executes a fixed server-side recipe (company_snapshot, etf_snapshot,
    quote_snapshot, macro_indicator_snapshot, macro_calendar,
    earnings_snapshot, debt_snapshot) and returns one envelope with freshness,
    provenance, per-component coverage, and billing. Composed calls charge the
    recipe's fixed cost (1-2 units) from the daily quota. status "partial"
    means an optional component was unavailable - the present components are
    still trustworthy; honor the freshness block (stale=true means the data
    aged past its budget).

    Args:
        recipe: Recipe name from the fixed manifest.
        entity: Entity dict from resolve_entity ({"namespace": ..., "ids": ...}).
    """
    result = await get_client().post(
        f"{_PLANE_BASE}/snapshot",
        json={"recipe": recipe, "entity": entity},
        headers=_internal_headers(),
    )
    return _map_plane_error(result)


@trace_mcp_tool("get_timeseries", result_attrs=_agent_result_attrs)
async def get_timeseries(
    metric: str,
    entity: dict[str, Any],
    granularity: str = "1d",
    max_points: int = 500,
) -> dict[str, Any]:
    """Bounded timeseries for an entity: price, macro_series, or etf_flows.

    Returns points oldest-first with an explicit downsampling flag when the
    raw series exceeded max_points. etf_flows is filing-cadence (one point per
    SEC filing refresh), NOT per calendar day, so even a wide window yields a
    handful of points. Times are UTC. Costs 1 unit per call.

    Args:
        metric: One of price / macro_series / etf_flows.
        entity: Entity dict from resolve_entity ({"namespace": ..., "ids": ...}).
        granularity: Requested point granularity (default "1d").
        max_points: Hard cap on returned points (default 500).
    """
    result = await get_client().post(
        f"{_PLANE_BASE}/timeseries",
        json={
            "metric": metric,
            "entity": entity,
            "granularity": granularity,
            "max_points": max_points,
        },
        headers=_internal_headers(),
    )
    return _map_plane_error(result)


_AGENT_TOOLS: tuple[tuple[Any, str], ...] = (
    (resolve_entity, "Resolve entity"),
    (get_snapshot, "Get snapshot"),
    (get_timeseries, "Get timeseries"),
)


def register_agent_tools(instance: Any | None = None) -> bool:
    """Register the hosted-only agent tools. Returns True when registered.

    Called from the streamable-http branch of ``__main__`` only (transport
    gate). Refuses without a non-empty ``SUGRA_AGENT_INTERNAL_TOKEN`` (env
    gate): the tools could only ever 403, so a misconfigured hosted deployment
    keeps the classic 8-tool surface and logs a loud warning instead.

    Idempotent for the global instance; explicit ``instance`` arguments (tests)
    are never latched and never touch the global.
    """
    global _registered_global
    target = mcp if instance is None else instance
    if instance is None and _registered_global:
        return True
    if not os.environ.get(_TOKEN_ENV, "").strip():
        logger.warning(
            "%s is not set - agent tools (resolve_entity / get_snapshot / "
            "get_timeseries) NOT registered. If this is the hosted deployment, "
            "the service env is misconfigured.",
            _TOKEN_ENV,
        )
        return False
    for func, title in _AGENT_TOOLS:
        target.tool(annotations=read_only(title))(func)
    if instance is None:
        _registered_global = True
    return True
