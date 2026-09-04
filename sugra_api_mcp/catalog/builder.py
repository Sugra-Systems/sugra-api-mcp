"""Build the bundled endpoint catalog from OpenAPI."""

from __future__ import annotations

from typing import Any

from .models import Catalog, Endpoint, EndpointParameter
from .toolsets import toolset_for_tags

# MCP-9 (codex review): the v1->v2 rewrite alone resolved only 2 of 9
# deprecated twins. Family renames are enumerated - each rewrite is tried in
# order and the first that lands on a live operation wins. Deprecated ops with
# genuinely no single successor are allowlisted for the bundle test.
_DEPRECATED_PATH_REWRITES: tuple[tuple[str, str], ...] = (
    ("/api/v1/maritime/history/", "/api/v2/transport/vessels/history/"),
    ("/api/v1/maritime/vessels/", "/api/v2/transport/vessels/"),
    ("/api/v1/weather/current", "/api/v2/weather"),
    ("/api/v1/", "/api/v2/"),
)

# Deprecated operations whose successor is not another OPERATION, so the
# path-rewrite table above can never name one.
#
#   weather_geocode          - its successor is a PARAMETER PATTERN (the v2
#                              city params), not an endpoint.
#   environment_osm_pipelines - deprecated for SPEED, not removal: the route
#                              still works and is still served, but a
#                              country-wide pipeline query takes 23-49 seconds
#                              upstream when it succeeds at all. Its own
#                              description points at /environment/osm/bbox
#                              with tag_key=man_made, and that is an
#                              alternative rather than a twin - it takes a
#                              bounded region instead of a country, so a
#                              caller cannot simply switch operation ids.
DEPRECATED_WITHOUT_REPLACEMENT: frozenset[str] = frozenset({
    "weather_geocode", "environment_osm_pipelines"})

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


def build_catalog_from_openapi(
    openapi: dict[str, Any],
    *,
    source: str = "fixture",
    spec_sha256: str | None = None,
    built_at: str | None = None,
) -> Catalog:
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
        # OpenAPI allows `parameters` on the Path Item itself, where they apply
        # to EVERY operation under that path. Reading only the operation's own
        # list silently drops them: the catalog would then advertise an
        # incomplete signature and a client calling it would be rejected for a
        # parameter it was never told about.
        shared_parameters = [
            param
            for param in methods.get("parameters", [])
            if isinstance(param, dict) and param.get("name")
        ]
        for method, operation in sorted(methods.items()):
            if method.lower() not in SUPPORTED_METHODS or not isinstance(operation, dict):
                continue
            operation_id = str(operation.get("operationId", "")).strip()
            if not operation_id:
                raise ValueError(f"{method.upper()} {path} missing operationId")

            tags = [str(tag) for tag in operation.get("tags", [])]
            toolset = toolset_for_tags(tags)
            # Path-item parameters first, then the operation's own: an operation
            # parameter with the same (name, in) overrides the shared one, per
            # OpenAPI. Keyed insertion keeps the order stable, and a spec that
            # declares nothing at the path level (the common case) produces
            # exactly the previous list.
            merged_parameters: dict[tuple[str, str], dict[str, Any]] = {}
            for param in shared_parameters + [
                param
                for param in operation.get("parameters", [])
                if isinstance(param, dict) and param.get("name")
            ]:
                merged_parameters[(str(param["name"]), str(param.get("in", "")))] = param
            parameters = [
                _parameter_from_openapi(param) for param in merged_parameters.values()
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
                    deprecated=bool(operation.get("deprecated", False)),
                    required_groups=tuple(
                        tuple(str(name) for name in group)
                        for group in ((operation.get("x-sugra-required-groups") or {})
                                      .get("groups") or [])
                    ),
                    groups_mutually_exclusive=bool(
                        (operation.get("x-sugra-required-groups") or {})
                        .get("mutually_exclusive", False)),
                )
            )

    # MCP-9: resolve replaced_by for deprecated operations. The platform's
    # deprecation pattern is a v1 path re-published under /v2/ with the same
    # trailing path; when exactly that live twin exists, name it so search can
    # keep the deprecated route strictly below its replacement.
    by_path = {(e.method, e.path): e.operation_id for e in endpoints}
    for i, e in enumerate(endpoints):
        if not e.deprecated or "/v1/" not in e.path:
            continue
        twin = None
        for old, new in _DEPRECATED_PATH_REWRITES:
            if old in e.path:
                twin = by_path.get((e.method, e.path.replace(old, new, 1)))
                if twin:
                    break
        if twin:
            endpoints[i] = e.model_copy(update={"replaced_by": twin})

    return Catalog(
        source=source,
        endpoints=endpoints,
        spec_sha256=spec_sha256,
        built_at=built_at,
    )
