"""Build the bundled endpoint catalog from OpenAPI."""

from __future__ import annotations

from typing import Any

from .models import Catalog, Endpoint, EndpointParameter
from .toolsets import toolset_for_tags

SUPPORTED_METHODS = {"get", "post"}

_SCHEMA_REF_PREFIX = "#/components/schemas/"


def _resolve_schema_refs(node: Any, schemas: dict[str, Any], seen: frozenset[str] = frozenset()) -> Any:
    """Inline #/components/schemas/* references into a self-contained schema.

    FastAPI emits requestBody schemas as $ref pointers; the bundled catalog
    must be usable without the source OpenAPI document, so refs are resolved
    at build time. Cyclic or unknown references stay as {"$ref": ...} markers,
    which keeps resolution terminating and the output valid JSON.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith(_SCHEMA_REF_PREFIX):
            name = ref[len(_SCHEMA_REF_PREFIX):]
            target = schemas.get(name)
            if name in seen or not isinstance(target, dict):
                return {"$ref": ref}
            resolved = _resolve_schema_refs(target, schemas, seen | {name})
            # Keep sibling keys (description overrides etc.) on top of the
            # inlined target.
            extras = {key: value for key, value in node.items() if key != "$ref"}
            if extras and isinstance(resolved, dict):
                return {**resolved, **extras}
            return resolved
        return {key: _resolve_schema_refs(value, schemas, seen) for key, value in node.items()}
    if isinstance(node, list):
        return [_resolve_schema_refs(item, schemas, seen) for item in node]
    return node


def _request_body_schema(operation: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    """Extract the resolved application/json requestBody schema, or {}."""
    request_body = operation.get("requestBody")
    if not isinstance(request_body, dict):
        return {}
    content = request_body.get("content")
    json_content = content.get("application/json") if isinstance(content, dict) else None
    raw_schema = json_content.get("schema") if isinstance(json_content, dict) else None
    if not isinstance(raw_schema, dict) or not raw_schema:
        return {}
    resolved = _resolve_schema_refs(raw_schema, schemas)
    return resolved if isinstance(resolved, dict) else {}


def _parameter_from_openapi(data: dict[str, Any]) -> EndpointParameter:
    return EndpointParameter(
        name=str(data["name"]),
        location=str(data.get("in", "query")),
        required=bool(data.get("required", False)),
        description=str(data.get("description", "")),
        schema=dict(data.get("schema") or {}),
        example=data.get("example"),
    )


def build_catalog_from_openapi(openapi: dict[str, Any], *, source: str = "fixture") -> Catalog:
    """Distill an OpenAPI document into a compact operation_id catalog."""
    endpoints: list[Endpoint] = []
    paths = openapi.get("paths", {})
    if not isinstance(paths, dict):
        raise ValueError("OpenAPI document has no paths object")
    components = openapi.get("components")
    schemas = components.get("schemas", {}) if isinstance(components, dict) else {}
    if not isinstance(schemas, dict):
        schemas = {}

    for path, methods in sorted(paths.items()):
        if not isinstance(methods, dict):
            continue
        for method, operation in sorted(methods.items()):
            if method.lower() not in SUPPORTED_METHODS or not isinstance(operation, dict):
                continue
            operation_id = str(operation.get("operationId", "")).strip()
            if not operation_id:
                raise ValueError(f"{method.upper()} {path} missing operationId")

            tags = [str(tag) for tag in operation.get("tags", [])]
            toolset = toolset_for_tags(tags)
            parameters = [
                _parameter_from_openapi(param)
                for param in operation.get("parameters", [])
                if isinstance(param, dict) and param.get("name")
            ]
            request_body = operation.get("requestBody")
            request_body_required = (
                isinstance(request_body, dict) and bool(request_body.get("required", False))
            )

            endpoints.append(
                Endpoint(
                    operation_id=operation_id,
                    method=method.upper(),
                    path=str(path),
                    summary=str(operation.get("summary", "")),
                    description=str(operation.get("description", "")),
                    tags=tags,
                    toolset=toolset,
                    source_family=toolset,
                    sources=[toolset],
                    parameters=parameters,
                    request_body_required=request_body_required,
                    request_body_schema=_request_body_schema(operation, schemas),
                )
            )

    return Catalog(source=source, endpoints=endpoints)
