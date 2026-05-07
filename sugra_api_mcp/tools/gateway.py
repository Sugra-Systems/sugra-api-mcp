"""Gateway MCP tools backed by the bundled endpoint catalog."""

from __future__ import annotations

from typing import Any

from ..catalog.loader import load_catalog
from ..catalog.response import shape_response
from ..catalog.search import search_catalog
from ..catalog.toolsets import ordered_toolsets
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
async def describe_endpoint(operation_id: str) -> dict[str, Any]:
    """Describe one Sugra API endpoint by operation_id."""
    catalog = load_catalog()
    try:
        endpoint = catalog.get(operation_id)
    except KeyError:
        return {"error": "unknown_operation_id", "operation_id": operation_id}
    return endpoint.to_dict()


@mcp.tool(annotations=read_only("Call endpoint"))
async def call_endpoint(
    operation_id: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    limit: int | None = None,
    fields: list[str] | None = None,
    include_raw: bool = False,
) -> dict[str, Any]:
    """Call a Sugra API endpoint by operation_id from the bundled catalog."""
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

    path_param_names = {parameter.name for parameter in endpoint.parameters if parameter.location == "path"}
    query_param_names = {
        parameter.name for parameter in endpoint.parameters if parameter.location == "query"
    }
    path = _resolve_path(
        endpoint.path,
        {key: value for key, value in clean_params.items() if key in path_param_names},
    )
    if "{" in path:
        return {"error": "unresolved_path_parameters", "operation_id": operation_id, "path": path}

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
        return {"error": "unsupported_method", "operation_id": operation_id, "method": endpoint.method}

    return shape_response(payload, limit=limit, fields=fields, include_raw=include_raw)


@mcp.tool(annotations=read_only("List toolsets"))
async def list_toolsets() -> dict[str, Any]:
    """List endpoint groups available in the bundled catalog."""
    catalog = load_catalog()
    counts: dict[str, int] = {}
    for endpoint in catalog.endpoints:
        counts[endpoint.toolset] = counts.get(endpoint.toolset, 0) + 1
    return {"toolsets": ordered_toolsets(counts), "total_endpoints": catalog.endpoint_count}


@mcp.tool(annotations=read_only("List sources"))
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
