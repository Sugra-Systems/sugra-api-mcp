"""Endpoint catalog data models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class EndpointParameter(BaseModel):
    """OpenAPI parameter distilled for MCP gateway use."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    name: str
    location: str
    required: bool = False
    description: str = ""
    schema_: dict[str, Any] = Field(default_factory=dict, alias="schema")
    example: Any = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> EndpointParameter:
        return cls(
            name=str(data["name"]),
            location=str(data["location"]),
            required=bool(data.get("required", False)),
            description=str(data.get("description", "")),
            schema=dict(data.get("schema") or {}),
            example=data.get("example"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "location": self.location,
            "required": self.required,
            "description": self.description,
            "schema": self.schema_,
        }
        if self.example is not None:
            result["example"] = self.example
        return result


class Endpoint(BaseModel):
    """Single callable Sugra API operation."""

    model_config = ConfigDict(frozen=True)

    operation_id: str
    method: str
    path: str
    summary: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    toolset: str = "other"
    source_family: str = "core"
    sources: list[str] = Field(default_factory=list)
    parameters: list[EndpointParameter] = Field(default_factory=list)
    request_body_required: bool = False

    @property
    def required_parameters(self) -> list[str]:
        required = [p.name for p in self.parameters if p.required]
        if self.request_body_required:
            required.append("body")
        return required

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Endpoint:
        return cls(
            operation_id=str(data["operation_id"]),
            method=str(data["method"]).upper(),
            path=str(data["path"]),
            summary=str(data.get("summary", "")),
            description=str(data.get("description", "")),
            tags=[str(tag) for tag in data.get("tags", [])],
            toolset=str(data.get("toolset", "other")),
            source_family=str(data.get("source_family", data.get("toolset", "core"))),
            sources=[str(source) for source in data.get("sources", [])],
            parameters=[
                EndpointParameter.from_dict(param)
                for param in data.get("parameters", [])
                if isinstance(param, dict)
            ],
            request_body_required=bool(data.get("request_body_required", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "method": self.method,
            "path": self.path,
            "summary": self.summary,
            "description": self.description,
            "tags": self.tags,
            "toolset": self.toolset,
            "source_family": self.source_family,
            "sources": self.sources or [self.source_family],
            "parameters": [param.to_dict() for param in self.parameters],
            "required_parameters": self.required_parameters,
            "request_body_required": self.request_body_required,
        }


class Catalog(BaseModel):
    """Immutable endpoint catalog keyed by operation_id."""

    model_config = ConfigDict(frozen=True)

    source: str
    endpoints: list[Endpoint]

    def model_post_init(self, __context: Any) -> None:
        ids = [endpoint.operation_id for endpoint in self.endpoints]
        if len(ids) != len(set(ids)):
            raise ValueError("Catalog contains duplicate operationId values")

    @property
    def endpoint_count(self) -> int:
        return len(self.endpoints)

    @property
    def by_operation_id(self) -> dict[str, Endpoint]:
        return {endpoint.operation_id: endpoint for endpoint in self.endpoints}

    def get(self, operation_id: str) -> Endpoint:
        try:
            return self.by_operation_id[operation_id]
        except KeyError as exc:
            raise KeyError(f"Unknown operation_id: {operation_id}") from exc

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Catalog:
        return cls(
            source=str(data.get("source", "unknown")),
            endpoints=[
                Endpoint.from_dict(endpoint)
                for endpoint in data.get("endpoints", [])
                if isinstance(endpoint, dict)
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "endpoint_count": self.endpoint_count,
            "endpoints": [endpoint.to_dict() for endpoint in self.endpoints],
        }
