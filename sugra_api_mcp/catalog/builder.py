"""Build the bundled endpoint catalog from OpenAPI."""

from __future__ import annotations

from typing import Any

from .models import Catalog, Endpoint, EndpointParameter
from .toolsets import toolset_for_tags

SUPPORTED_METHODS = {"get", "post"}


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
                )
            )

    return Catalog(source=source, endpoints=endpoints)
