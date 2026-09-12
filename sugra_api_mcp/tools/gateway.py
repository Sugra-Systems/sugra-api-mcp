"""Gateway MCP tools backed by the bundled endpoint catalog."""

from __future__ import annotations

import time
from typing import Annotated, Any

from pydantic import Field

from ..catalog.hints import hints_for
from ..catalog.loader import load_catalog
from ..catalog.response import shape_response
from ..catalog.search import known_sources, known_toolsets, search_catalog
from ..catalog.toolsets import ordered_toolsets
from ..errors import is_error_payload
from ..observability import trace_mcp_tool
from ..server import get_client, mcp, read_only


def _resolve_path(path: str, params: dict[str, Any]) -> str:
    resolved = path
    for name, value in params.items():
        resolved = resolved.replace(f"{{{name}}}", str(value))
    return resolved


def _group_violation(endpoint, params: dict[str, Any]) -> str | None:
    """Group-contract verdict BEFORE any HTTP call (audit P1-8 MCP half).

    Returns "uncovered" when NO declared group is fully covered, and
    "multiple" when the endpoint declares its groups mutually exclusive
    and the params complete MORE than one - both would be upstream 4xxs,
    so the gateway refuses with the groups spelled out. None = dispatch.
    """
    groups = getattr(endpoint, "required_groups", ()) or ()
    if not groups:
        return None
    covered = sum(1 for group in groups if all(name in params for name in group))
    if covered == 0:
        return "uncovered"
    if getattr(endpoint, "groups_mutually_exclusive", False):
        # Exclusivity judges ACTIVE groups (any member supplied), not just
        # complete ones: a complete group mixed with a stray member of a
        # competing group is still a mixed-mode request the upstream will
        # reject (codex r2).
        active = sum(1 for group in groups if any(name in params for name in group))
        if active > 1:
            return "multiple"
    return None


def _missing_required(
    endpoint, params: dict[str, Any], body: dict[str, Any] | list[Any] | None
) -> list[str]:
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
    query: Annotated[
        str,
        Field(
            description=(
                "Natural-language search over the bundled catalog. Name the "
                "instrument, series, place, or task (examples: 'US CPI', "
                "'AAPL quote', 'North Sea AIS'). Returns ranked operation_id "
                "hits with required_parameters. Then call describe_endpoint "
                "on a hit before call_endpoint."
            ),
        ),
    ],
    toolset: Annotated[
        str | None,
        Field(
            description=(
                "Optional catalog group filter (markets, macro, news, "
                "network, ...). Call list_toolsets for the live names. An "
                "unknown value returns error unknown_toolset with known_toolsets "
                "rather than an empty hit list."
            ),
        ),
    ] = None,
    source: Annotated[
        str | None,
        Field(
            description=(
                "Optional source-family filter as listed by list_sources "
                "(macro, markets, ...). An unknown value returns error "
                "unknown_source with known_sources."
            ),
        ),
    ] = None,
    limit: Annotated[
        int,
        Field(
            description=(
                "Maximum ranked hits to return. Default 10. Does not call "
                "the Sugra API; this only bounds the catalog search list."
            ),
        ),
    ] = 10,
) -> dict[str, Any]:
    """Search the bundled Sugra endpoint catalog by natural-language query.

    Use this to pick an operation_id. It does not fetch data. Typical loop:
    1. search_endpoints(query) -> ranked hits with required_parameters
    2. describe_endpoint(operation_id) -> params, request_body_schema, agent_hints
    3. call_endpoint(operation_id, params=..., body=...) or fetch_data(query, params=...)

    Filter with toolset or source only after list_toolsets / list_sources;
    a misspelled filter is an error, not a silent empty result.

    Examples:
    - search_endpoints("US CPI inflation")
    - search_endpoints("AAPL price", toolset="markets")
    - search_endpoints("container ship AIS", toolset="network")
    """
    catalog = load_catalog()
    # An unknown filter value used to fall through the per-endpoint comparison and
    # return an empty result list - indistinguishable from "this catalog genuinely
    # has nothing for your query". A misspelling, or a client written against a
    # different catalog vintage (the toolset taxonomy is versioned WITH the
    # bundle), therefore surfaced as a silent zero instead of a diagnosable error.
    # Validate against the accept-set derived from the catalog and say what is
    # valid, so the caller can correct the filter in one step.
    # Activate validation on exactly the predicate the search filter uses
    # (truthiness, not `is not None`): an empty string has always meant "no
    # filter" - clients serialize unset optional strings that way - so validating
    # it would turn a working call into a bogus unknown_* error.
    if toolset:
        valid_toolsets = known_toolsets(catalog)
        if toolset not in valid_toolsets:
            return {
                "error": "unknown_toolset",
                "requested": toolset,
                "known_toolsets": sorted(valid_toolsets),
                "catalog_source": catalog.source,
            }
    if source:
        valid_sources = known_sources(catalog)
        if source not in valid_sources:
            return {
                "error": "unknown_source",
                "requested": source,
                "known_sources": sorted(valid_sources),
                "catalog_source": catalog.source,
            }
    results = search_catalog(catalog, query, toolset=toolset, source=source, limit=limit)
    return {"results": results, "total_matched": len(results), "catalog_source": catalog.source}


@mcp.tool(annotations=read_only("Describe endpoint"))
@trace_mcp_tool("describe_endpoint")
async def describe_endpoint(
    operation_id: Annotated[
        str,
        Field(
            description=(
                "Catalog operation_id from search_endpoints (or from "
                "list_toolsets drill-down). Unknown ids return error "
                "unknown_operation_id."
            ),
        ),
    ],
) -> dict[str, Any]:
    """Describe one Sugra API endpoint by operation_id.

    Includes agent_hints (duration_class fast/slow/heavy, max_concurrency,
    bulk billing) so you can budget timeouts and parallelism before calling.
    POST endpoints with a JSON body also carry request_body_schema (the
    resolved JSON schema) - construct the `body` argument from it instead
    of guessing key names. Call this after search_endpoints and before
    call_endpoint when you need the exact parameter names and examples.
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
    params: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "Query and path parameters for this operation_id. Keys and types are "
                "operation-specific - call describe_endpoint(operation_id) first to get the "
                "exact parameter names, types, and examples. Omit if the operation takes none."
            ),
        ),
    ] = None,
    body: Annotated[
        dict[str, Any] | list[Any] | None,
        Field(
            description=(
                "JSON request body for a POST operation, matching the request_body_schema "
                "returned by describe_endpoint(operation_id): a JSON object for most "
                "operations, or a JSON array when that schema's top-level type is array. "
                "Omit for GET operations."
            ),
        ),
    ] = None,
    limit: Annotated[
        int | None,
        Field(
            description=(
                "Bounds ONLY the records list: the data list, a bare top-level "
                "array, or the list inside an object data when exactly one of "
                "these keys holds a list: data, entries, events, history, items, "
                "observations, points, records, results, rows, series, timeseries "
                "(for example data.items). No such list, or several, means the "
                "limit does not apply. Keys beside the list such as total and "
                "count are not rewritten, and lists nested inside records are "
                "never truncated. meta.shaped reports limit_applied and "
                "records_path."
            ),
        ),
    ] = None,
    fields: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional projection of keys to keep on each record of the "
                "records list: the data list, a bare top-level array, or the "
                "list inside an object data when exactly one of these keys "
                "holds a list: data, entries, events, history, items, "
                "observations, points, records, results, rows, series, "
                "timeseries (for example data.items). Keys beside that list "
                "such as total and count stay. If a field names a key of data "
                "itself, or of a payload without data, that object is "
                "projected instead; an object data without such a list is "
                "otherwise kept whole. Dotted paths (geo.city) walk nested "
                "objects. If no field matches, nothing is removed. meta.shaped "
                "reports fields_applied, fields_unmatched and records_path. "
                "Omit to keep every key."
            ),
        ),
    ] = None,
    include_raw: Annotated[
        bool,
        Field(
            description=(
                "If true, attach the original unshaped payload under raw "
                "when it fits the size cap; otherwise meta.raw_omitted "
                "explains why. Default false."
            ),
        ),
    ] = False,
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
            payload: dict[str, Any] = {
                "error": "missing_required_parameters",
                "operation_id": operation_id,
                "missing": missing,
            }
            # One diagnostic carries EVERYTHING the next call needs: hiding
            # the group constraint here would force a second failing round
            # trip (codex r3).
            if endpoint.required_groups:
                payload["required_groups"] = [list(g) for g in endpoint.required_groups]
                payload["groups_hint"] = (
                    "also supply every parameter of "
                    + ("EXACTLY one group" if endpoint.groups_mutually_exclusive
                       else "at least one group"))
            return payload
        violation = _group_violation(endpoint, clean_params)
        if violation:
            return {
                "error": "missing_required_parameter_groups",
                "operation_id": operation_id,
                "groups": [list(group) for group in endpoint.required_groups],
                "hint": ("supply every parameter of EXACTLY one group"
                         if violation == "multiple"
                         else "supply every parameter of at least one group"
                         + (" (groups are mutually exclusive)"
                            if endpoint.groups_mutually_exclusive else "")),
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

        if is_error_payload(payload):
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


def toolsets_payload() -> dict[str, Any]:
    """Toolset groups with endpoint counts from the bundled catalog.

    Shared by the list_toolsets tool and the sugra://catalog/domains
    resource so both surfaces always report identical data.
    """
    catalog = load_catalog()
    counts: dict[str, int] = {}
    for endpoint in catalog.endpoints:
        counts[endpoint.toolset] = counts.get(endpoint.toolset, 0) + 1
    return {"toolsets": ordered_toolsets(counts), "total_endpoints": catalog.endpoint_count}


@mcp.tool(annotations=read_only("List toolsets"))
@trace_mcp_tool("list_toolsets")
async def list_toolsets() -> dict[str, Any]:
    """List catalog groups with endpoint counts and short descriptions.

    Use the group names as the toolset filter on search_endpoints. This
    does not call the Sugra API; it reads the bundled catalog.
    """
    return toolsets_payload()


@mcp.tool(annotations=read_only("Fetch data"))
@trace_mcp_tool("fetch_data")
async def fetch_data(
    query: Annotated[
        str,
        Field(
            description=(
                "Natural-language request for data (examples: 'US CPI', "
                "'Bitcoin price', 'latest news'). The tool picks the top "
                "catalog match and calls it. If required params are missing "
                "it returns needs_params instead of guessing."
            ),
        ),
    ],
    params: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "Parameters for the auto-selected endpoint. If omitted and the best-match "
                "endpoint has required parameters, the tool returns that endpoint's "
                "required_parameters and examples so you can retry with them filled in."
            ),
        ),
    ] = None,
    body: Annotated[
        dict[str, Any] | list[Any] | None,
        Field(
            description=(
                "JSON body for an auto-selected POST operation; the tool returns the "
                "request_body_schema to fill when the match needs one. Pass a JSON "
                "object or a JSON array as that schema's top-level type dictates."
            ),
        ),
    ] = None,
    limit: Annotated[
        int | None,
        Field(
            description=(
                "Bounds ONLY the records list: the data list, a bare top-level "
                "array, or the list inside an object data when exactly one of "
                "these keys holds a list: data, entries, events, history, items, "
                "observations, points, records, results, rows, series, timeseries "
                "(for example data.items). No such list, or several, means the "
                "limit does not apply. Keys beside the list such as total and "
                "count are not rewritten, and lists nested inside records are "
                "never truncated. meta.shaped reports limit_applied and "
                "records_path."
            ),
        ),
    ] = None,
    fields: Annotated[
        list[str] | None,
        Field(
            description=(
                "Optional projection of keys to keep on each record of the "
                "records list: the data list, a bare top-level array, or the "
                "list inside an object data when exactly one of these keys "
                "holds a list: data, entries, events, history, items, "
                "observations, points, records, results, rows, series, "
                "timeseries (for example data.items). Keys beside that list "
                "such as total and count stay. If a field names a key of data "
                "itself, or of a payload without data, that object is "
                "projected instead; an object data without such a list is "
                "otherwise kept whole. Dotted paths (geo.city) walk nested "
                "objects. If no field matches, nothing is removed. meta.shaped "
                "reports fields_applied, fields_unmatched and records_path. "
                "Omit to keep every key."
            ),
        ),
    ] = None,
    include_raw: Annotated[
        bool,
        Field(
            description=(
                "If true, attach the original unshaped payload under raw "
                "when it fits the size cap; otherwise meta.raw_omitted "
                "explains why. Default false."
            ),
        ),
    ] = False,
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
                **({"required_groups": [list(g) for g in endpoint.required_groups],
                    "groups_mutually_exclusive": endpoint.groups_mutually_exclusive}
                   if endpoint.required_groups else {}),
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

        violation = _group_violation(endpoint, clean_params)
        if violation:
            return {
                "error": "missing_required_parameter_groups",
                "operation_id": operation_id,
                "groups": [list(group) for group in endpoint.required_groups],
                "hint": ("supply every parameter of EXACTLY one group"
                         if violation == "multiple"
                         else "supply every parameter of at least one group"
                         + (" (groups are mutually exclusive)"
                            if endpoint.groups_mutually_exclusive else "")),
                "candidate_endpoints": results,
            }

        # All required params satisfied - delegate to the same call path as
        # call_endpoint so behavior is identical (path resolution, query/body
        # routing, response shaping). operation_id goes by KEYWORD: the
        # delegate is the decorated tool, and its span reads the operation
        # from kwargs only (a positional first argument is a raw query on
        # other tools). Passed positionally, every delegated failure was a
        # call_endpoint span with no operation at all.
        return await call_endpoint(
            operation_id=operation_id,
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


def sources_payload() -> dict[str, Any]:
    """Source families with endpoint counts from the bundled catalog.

    Shared by the list_sources tool and the sugra://catalog/sources
    resource so both surfaces always report identical data.
    """
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


@mcp.tool(annotations=read_only("List sources"))
@trace_mcp_tool("list_sources")
async def list_sources() -> dict[str, Any]:
    """List source families in the bundled catalog with endpoint counts.

    Use the family names as the source filter on search_endpoints. This
    does not call the Sugra API.
    """
    return sources_payload()
