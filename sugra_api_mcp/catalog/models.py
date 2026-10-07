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


class MacroKey(BaseModel):
    """One curated series an operation serves under a fixed key.

    ``key`` is "<country>/<section>", the two path values that select the
    series; ``title`` is the series' own name, which search matches a query
    against; ``freq`` is how often it is observed ("monthly"), empty when the
    spec does not say.
    """

    model_config = ConfigDict(frozen=True)

    key: str
    title: str = ""
    freq: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MacroKey:
        return cls(
            key=str(data["key"]),
            title=str(data.get("title") or ""),
            freq=str(data.get("freq") or ""),
        )

    def to_dict(self) -> dict[str, str]:
        result = {"key": self.key, "title": self.title}
        if self.freq:
            result["freq"] = self.freq
        return result

    @property
    def params(self) -> dict[str, str]:
        country, _, section = self.key.partition("/")
        return {"country": country, "section": section}


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
    # Resolved application/json requestBody schema ($refs inlined at build
    # time); {} for endpoints without a JSON body. Lets describe_endpoint
    # show the exact body shape instead of clients guessing keys.
    request_body_schema: dict[str, Any] = Field(default_factory=dict)
    # The spec's deprecated flag, carried into the bundle
    # so search can penalize deprecated routes; replaced_by names the live
    # replacement operation when one exists (v1 path re-published under v2).
    deprecated: bool = False
    replaced_by: str | None = None
    # Conditionally-required parameter groups
    # from the spec's x-sugra-required-groups extension - at least one group
    # must be fully covered before the gateway dispatches.
    required_groups: tuple[tuple[str, ...], ...] = ()
    groups_mutually_exclusive: bool = False
    # Search vocabulary from the spec's x-sugra-keywords vendor extension -
    # lowercase synonyms/labels a query might use that the operation's own
    # path/summary/description never spell out (coffee, cocoa, sugar,
    # gasoline, ...). Empty for every operation the API does not annotate.
    keywords: list[str] = Field(default_factory=list)
    # The curated series behind a country/section operation, from the spec's
    # x-sugra-macro-keys extension: search reads a query's indicator words
    # against their titles and names the matching keys in the hit. Hundreds
    # of entries, so describe_endpoint leaves them out (see to_dict).
    macro_keys: list[MacroKey] = Field(default_factory=list)

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
            request_body_schema=dict(data.get("request_body_schema") or {}),
            deprecated=bool(data.get("deprecated", False)),
            replaced_by=(str(data["replaced_by"])
                         if data.get("replaced_by") else None),
            required_groups=tuple(
                tuple(str(name) for name in group)
                for group in (data.get("required_groups") or [])
            ),
            groups_mutually_exclusive=bool(data.get("groups_mutually_exclusive", False)),
            keywords=[str(word) for word in (data.get("keywords") or [])],
            macro_keys=[
                MacroKey.from_dict(item)
                for item in (data.get("macro_keys") or [])
                if isinstance(item, dict) and item.get("key")
            ],
        )

    def to_dict(self, *, include_macro_keys: bool = False) -> dict[str, Any]:
        """The endpoint as a dict; ``include_macro_keys`` adds the key index.

        The bundle and the parity check carry the index; describe_endpoint
        does not, since its hundreds of entries would crowd out the schema.
        """
        result: dict[str, Any] = {
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
        # Omit when empty: 1300+ GET endpoints would otherwise carry dead
        # keys in the bundled catalog and in describe_endpoint output.
        if self.request_body_schema:
            result["request_body_schema"] = self.request_body_schema
        # Omit-when-default keeps 1500+ live endpoints free of dead keys.
        if self.deprecated:
            result["deprecated"] = True
        if self.replaced_by:
            result["replaced_by"] = self.replaced_by
        if self.required_groups:
            result["required_groups"] = [list(group) for group in self.required_groups]
            if self.groups_mutually_exclusive:
                result["groups_mutually_exclusive"] = True
        if self.keywords:
            result["keywords"] = self.keywords
        if include_macro_keys and self.macro_keys:
            result["macro_keys"] = [item.to_dict() for item in self.macro_keys]
        return result


class Catalog(BaseModel):
    """Immutable endpoint catalog keyed by operation_id."""

    model_config = ConfigDict(frozen=True)

    source: str
    endpoints: list[Endpoint]
    # Machine-readable provenance. `source` alone is a free-text label, so a stale
    # bundle is only detectable by reading it - these let a check compare the
    # bundle against a spec mechanically (an unrebuilt bundle after API routes
    # land is exactly how the hosted surface silently drifted behind).
    # Optional so an older bundle without them still loads.
    spec_sha256: str | None = None
    built_at: str | None = None

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

    @property
    def operation_ids(self) -> set[str]:
        return {endpoint.operation_id for endpoint in self.endpoints}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Catalog:
        spec_sha256 = data.get("spec_sha256")
        built_at = data.get("built_at")
        return cls(
            source=str(data.get("source", "unknown")),
            spec_sha256=str(spec_sha256) if spec_sha256 else None,
            built_at=str(built_at) if built_at else None,
            endpoints=[
                Endpoint.from_dict(endpoint)
                for endpoint in data.get("endpoints", [])
                if isinstance(endpoint, dict)
            ],
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "endpoint_count": self.endpoint_count,
            "endpoints": [
                endpoint.to_dict(include_macro_keys=True) for endpoint in self.endpoints
            ],
        }
        # Omitted rather than emitted as null when absent, so an older bundle
        # round-trips byte-identically through load -> dump.
        if self.spec_sha256:
            payload["spec_sha256"] = self.spec_sha256
        if self.built_at:
            payload["built_at"] = self.built_at
        return payload
