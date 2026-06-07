"""Gateway MCP tools backed by the bundled endpoint catalog."""

from __future__ import annotations

import time
from typing import Any

from ..catalog.hints import hints_for
from ..catalog.loader import load_catalog
from ..catalog.response import shape_response
from ..catalog.search import search_catalog
from ..catalog.toolsets import ordered_toolsets
from ..observability import trace_mcp_tool
from ..server import get_client, mcp, read_only


def _resolve_path(path: str, params: dict[str, Any]) -> str:
    resolved = path
    for name, value in params.items():
        resolved = resolved.replace(f"{{{name}}}", str(value))
    return resolved


def _missing_required(endpoint, params: dict[str, Any], body: dict[str, Any] | None) -> list[str]:
    missing = [
        parameter.name
        for parameter in endpoint.parameters
        if parameter.required and parameter.name not in params
    ]
    if endpoint.request_body_required and body is None:
        missing.append("body")
    return missing


@mcp.tool(annotations=read_only("Search endpoints"))
@trace_mcp_tool("search_endpoints")
async def search_endpoints(
    query: str,
    toolset: str | None = None,
    source: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Search the bundled Sugra endpoint catalog by natural-language query."""
    catalog = load_catalog()
    results = search_catalog(catalog, query, toolset=toolset, source=source, limit=limit)
    return {"results": results, "total_matched": len(results), "catalog_source": catalog.source}


@mcp.tool(annotations=read_only("Describe endpoint"))
@trace_mcp_tool("describe_endpoint")
async def describe_endpoint(operation_id: str) -> dict[str, Any]:
    """Describe one Sugra API endpoint by operation_id.

    Includes agent_hints (duration_class fast/slow/heavy, max_concurrency,
    bulk billing) so you can budget timeouts and parallelism before calling.
    POST endpoints with a JSON body also carry request_body_schema (the
    resolved JSON schema) - construct the `body` argument from it instead
    of guessing key names.
    """
    catalog = load_catalog()
    try:
        endpoint = catalog.get(operation_id)
    except KeyError:
        return {"error": "unknown_operation_id", "operation_id": operation_id}
    described = endpoint.to_dict()
    described["agent_hints"] = hints_for(endpoint)
    return described


@mcp.tool(annotations=read_only("Call endpoint"))
@trace_mcp_tool("call_endpoint")
async def call_endpoint(
    operation_id: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    limit: int | None = None,
    fields: list[str] | None = None,
    include_raw: bool = False,
) -> dict[str, Any]:
    """Call a Sugra API endpoint by operation_id from the bundled catalog.

    Plan calls with describe_endpoint's agent_hints: duration_class "fast"
    usually responds in under ~2s, "slow" usually 1-5s and occasionally 15s+
    on a cold upstream, "heavy" can exceed the gateway timeout - keep parallel
    calls within max_concurrency and prefer small batches. Bulk endpoints bill
    1 request credit per body item. Failures return structured errors {error,
    reason, status_code, elapsed_ms, retry_hint}; after "upstream_timeout" a
    single retry often succeeds because the aborted attempt warms upstream
    caches.
    """
    # The whole body sits in one safety net: a raised exception surfaces to
    # MCP clients as "Error executing tool call_endpoint: <message>" where
    # the message can be EMPTY (field-test defect D2). Returning a structured
    # dict keeps the error contract intact for any unexpected failure class,
    # including catalog-load and parameter-resolution failures.
    start = time.perf_counter()
    try:
        catalog = load_catalog()
        try:
            endpoint = catalog.get(operation_id)
        except KeyError:
            return {"error": "unknown_operation_id", "operation_id": operation_id}

        clean_params = {key: value for key, value in (params or {}).items() if value is not None}
        missing = _missing_required(endpoint, clean_params, body)
        if missing:
            return {
                "error": "missing_required_parameters",
                "operation_id": operation_id,
                "missing": missing,
            }

        path_param_names = {
            parameter.name for parameter in endpoint.parameters if parameter.location == "path"
        }
        query_param_names = {
            parameter.name for parameter in endpoint.parameters if parameter.location == "query"
        }
        path = _resolve_path(
            endpoint.path,
            {key: value for key, value in clean_params.items() if key in path_param_names},
        )
        if "{" in path:
            return {
                "error": "unresolved_path_parameters",
                "operation_id": operation_id,
                "path": path,
            }

        query_params = {
            key: value
            for key, value in clean_params.items()
            if key in query_param_names or key not in path_param_names
        }

        client = get_client()
        if endpoint.method == "GET":
            payload = await client.get(path, params=query_params)
        elif endpoint.method == "POST":
            payload = await client.request(endpoint.method, path, params=query_params, json=body)
        else:
            return {
                "error": "unsupported_method",
                "operation_id": operation_id,
                "method": endpoint.method,
            }

        if isinstance(payload, dict) and "error" in payload and "data" not in payload:
            # Structured error contract from SugraClient (transport failure,
            # HTTP 4xx/5xx, or size-limit refusal). Return it untouched:
            # shaping an error dict would only decorate it with misleading
            # meta while the agent needs the raw {error, reason, elapsed_ms}.
            # The "no data key" guard mirrors entities._is_error: a success
            # envelope always carries data, so a hypothetical 200 partial
            # payload with both keys still gets shaped normally.
            return payload

        return shape_response(payload, limit=limit, fields=fields, include_raw=include_raw)
    except Exception as exc:
        return {
            "error": "tool_execution_failed",
            "operation_id": operation_id,
            "exception_type": type(exc).__name__,
            "reason": str(exc)[:300].strip() or type(exc).__name__,
            "elapsed_ms": int((time.perf_counter() - start) * 1000),
        }


@mcp.tool(annotations=read_only("List toolsets"))
@trace_mcp_tool("list_toolsets")
async def list_toolsets() -> dict[str, Any]:
    """List endpoint groups available in the bundled catalog."""
    catalog = load_catalog()
    counts: dict[str, int] = {}
    for endpoint in catalog.endpoints:
        counts[endpoint.toolset] = counts.get(endpoint.toolset, 0) + 1
    return {"toolsets": ordered_toolsets(counts), "total_endpoints": catalog.endpoint_count}


@mcp.tool(annotations=read_only("Fetch data"))
@trace_mcp_tool("fetch_data")
async def fetch_data(
    query: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    limit: int | None = None,
    fields: list[str] | None = None,
    include_raw: bool = False,
) -> dict[str, Any]:
    """One-step fetch: find the best Sugra endpoint for the query and call it.

    Combines search_endpoints + call_endpoint into a single round trip. Use
    this when you want data without manually picking an operation_id. The
    full search_endpoints + describe_endpoint + call_endpoint dance is still
    available when you need explicit control, but for most natural-language
    queries this tool is enough.

    Behavior:
    1. Search the bundled catalog for the query. Top match wins.
    2. If the matched endpoint has required parameters and they are all
       provided in `params`, call it and return the response.
    3. If required parameters are missing, return the candidate endpoints
       and the missing-params list so the LLM can retry with the correct
       `params` dict on the next call.

    Examples:
    - `fetch_data("US CPI inflation", params={"series_id": "CPIAUCSL"})`
      → calls /api/v1/fred/series/CPIAUCSL, returns observations.
    - `fetch_data("Bitcoin price", params={"coin_id": "bitcoin"})`
      → calls /api/v1/crypto/bitcoin/price.
    - `fetch_data("Latest financial news")`
      → news_latest has no required params, returns latest news directly.
    """
    # Same whole-body safety net as call_endpoint (defect D2): the search and
    # selection path must never raise through FastMCP as an empty message.
    start = time.perf_counter()
    try:
        catalog = load_catalog()
        results = search_catalog(catalog, query, limit=3)

        if not results:
            return {
                "error": "no_endpoint_found",
                "query": query,
                "hint": "Try a more specific query or use search_endpoints + describe_endpoint to explore the catalog manually.",
            }

        top = results[0]
        operation_id = top["operation_id"]

        try:
            endpoint = catalog.get(operation_id)
        except KeyError:
            # Should never happen — search returned an op_id that load_catalog
            # doesn't recognise. Surface as a clear error rather than crashing.
            return {
                "error": "stale_search_result",
                "operation_id": operation_id,
                "candidate_endpoints": results,
            }

        clean_params = {key: value for key, value in (params or {}).items() if value is not None}
        missing = _missing_required(endpoint, clean_params, body)

        if missing:
            # LLM didn't supply enough — return both the selected endpoint's
            # schema and the alternative candidates so the next call can either
            # fill the gap or pick a different endpoint.
            selected: dict[str, Any] = {
                "operation_id": operation_id,
                "method": endpoint.method,
                "path": endpoint.path,
                "summary": endpoint.summary,
                "agent_hints": hints_for(endpoint),
                "required_parameters": endpoint.required_parameters,
                "parameter_examples": [
                    {
                        "name": p.name,
                        "description": p.description,
                        "example": p.example,
                        "required": p.required,
                    }
                    for p in endpoint.parameters
                    if p.required
                ],
            }
            if endpoint.request_body_schema:
                # "body" in missing means the agent must construct a JSON
                # body - hand it the exact schema instead of letting it guess.
                selected["request_body_schema"] = endpoint.request_body_schema
            return {
                "needs_params": missing,
                "selected_endpoint": selected,
                "candidate_endpoints": results,
                "hint": (
                    f"The top match `{operation_id}` requires {missing}. "
                    f"Retry as fetch_data(query, params={{...}}) with those keys filled in, "
                    f"or call describe_endpoint(operation_id) for full schema."
                ),
            }

        # All required params satisfied — delegate to the same call path as
        # call_endpoint so behavior is identical (path resolution, query/body
        # routing, response shaping).
        return await call_endpoint(
            operation_id,
            params=clean_params,
            body=body,
            limit=limit,
            fields=fields,
            include_raw=include_raw,
        )
    except Exception as exc:
        return {
            "error": "tool_execution_failed",
            "exception_type": type(exc).__name__,
            "reason": str(exc)[:300].strip() or type(exc).__name__,
            "elapsed_ms": int((time.perf_counter() - start) * 1000),
        }


@mcp.tool(annotations=read_only("List sources"))
@trace_mcp_tool("list_sources")
async def list_sources() -> dict[str, Any]:
    """List endpoint source families derived from catalog metadata."""
    catalog = load_catalog()
    counts: dict[str, int] = {}
    for endpoint in catalog.endpoints:
        family = endpoint.source_family
        counts[family] = counts.get(family, 0) + 1
    return {
        "source_families": ordered_toolsets(counts),
        "endpoint_count": catalog.endpoint_count,
        "catalog_source": catalog.source,
    }
