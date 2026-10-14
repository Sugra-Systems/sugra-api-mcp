"""One tool per catalog operation, listed only to the ChatGPT client profile.

OpenAI's app submission guidelines ask for each action to be its own tool,
with a name that says what it does, minimal inputs, no raw location fields and
no logging metadata in the response. The gateway tools (call_endpoint and its
search and describe companions) are one generic executor, which those
guidelines do not accept, so ChatGPT is also listed the typed tools defined in
catalog/data/typed_tools.json: a name, a title and a description per catalog
operation. Their input schemas come from the bundled catalog, less location
parameters.

The typed tools are not registered with FastMCP's tool manager:
SugraFastMCP.list_tools adds them for the ChatGPT profile only, so every other
client keeps today's list byte for byte, and _call_tool_in_budget routes a
typed name here. A call runs the same code path as call_endpoint, under a span
named after the typed tool, and its response is reduced by minimize_response.
Before any request, a call's arguments are held to the input schema the tool
is listed with: names, required values, types, enums and bounds alike.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache, lru_cache
from importlib import resources
from typing import Any

from jsonschema import Draft202012Validator
from mcp.types import Tool as MCPTool

from ..catalog.loader import load_catalog
from ..catalog.models import Endpoint, EndpointParameter
from ..errors import is_error_payload
from ..observability import trace_mcp_tool
from ..server import read_only
from . import gateway

SPEC_PACKAGE = "sugra_api_mcp.catalog.data"
SPEC_FILE = "typed_tools.json"

# Never exposed in a typed tool's input schema. OpenAI: "Avoid requesting raw
# location fields (for example, city or coordinates) in your input schema."
# Country, state and region codes are not raw locations and stay.
LOCATION_PARAMETERS = frozenset({
    "address",
    "bbox",
    "city",
    "coordinates",
    "lat",
    "latitude",
    "lng",
    "lon",
    "longitude",
    "max_lat",
    "max_lon",
    "min_lat",
    "min_lon",
    "place",
    "postal_code",
    "postcode",
    "radius",
    "radius_km",
    "zip",
    "zipcode",
})

# JSON Schema keywords a typed parameter keeps from the catalog schema.
_SCHEMA_KEYS = (
    "type",
    "enum",
    "default",
    "items",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "format",
)

# A parameter description longer than this is cut at its last sentence end.
MAX_PARAMETER_DESCRIPTION = 300

# Keys a failure keeps: what went wrong and what to do next. The rest of an
# error dict (url, elapsed_ms, request_id, exception_type, timeout_s) is
# request and logging metadata.
_ERROR_KEYS = (
    "error",
    "reason",
    "message",
    "hint",
    "status_code",
    "retry_hint",
    "retry_after",
    "missing",
    "unknown",
    "accepted",
    "did_you_mean",
    "invalid",
)

# A refusal names at most this many schema violations, each message clipped.
MAX_REPORTED_VIOLATIONS = 5
MAX_VIOLATION_CHARS = 200

# Top-level keys of a successful payload that are request or logging metadata.
_DROPPED_KEYS = frozenset({
    "_meta",
    "cached",
    "elapsed_ms",
    "exception_type",
    "fetched_at",
    "generated_at",
    "raw",
    "request_id",
    "retrieved_at",
    "session_id",
    "timeout_s",
    "trace_id",
    "url",
})

# What a successful payload's meta keeps: facts about the data itself.
_META_KEYS = (
    "count",
    "currency",
    "frequency",
    "granularity",
    "has_more",
    "interval",
    "next_offset",
    "note",
    "notes",
    "retry_hint",
    "total",
    "truncated",
    "unit",
    "units",
    "warning",
    "warnings",
)


@dataclass(frozen=True)
class TypedTool:
    """One typed tool: its listing and the catalog operation it calls."""

    name: str
    operation_id: str
    tool: MCPTool
    accepted: frozenset[str]
    required: tuple[str, ...]
    validator: Draft202012Validator


def _clip(text: str, limit: int = MAX_PARAMETER_DESCRIPTION) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if cut.endswith("."):
        return cut
    end = cut.rfind(". ")
    return cut[: end + 1] if end > 0 else cut.rstrip() + "..."


def _parameter_schema(parameter: EndpointParameter, fallback: str = "") -> dict[str, Any]:
    """The JSON Schema of one exposed parameter, without nullable wrappers or titles.

    fallback describes a parameter the catalog leaves undescribed.
    """
    schema = dict(parameter.schema_)
    options = schema.pop("anyOf", None)
    if isinstance(options, list):
        concrete = [o for o in options if isinstance(o, dict) and o.get("type") != "null"]
        if len(concrete) == 1:
            schema = {**concrete[0], **schema}
        elif concrete:
            schema["anyOf"] = concrete
    out: dict[str, Any] = {key: schema[key] for key in _SCHEMA_KEYS if key in schema}
    if "anyOf" in schema:
        out["anyOf"] = schema["anyOf"]
    if "type" not in out and "anyOf" not in out and "enum" not in out:
        out["type"] = "string"
    description = parameter.description or str(schema.get("description") or "") or fallback
    if description:
        out["description"] = _clip(description)
    if parameter.example is not None and "enum" not in out and parameter.example != out.get("default"):
        out["examples"] = [parameter.example]
    return out


def exposed_parameters(endpoint: Endpoint) -> list[EndpointParameter]:
    """The endpoint's path and query parameters, less location fields."""
    return [
        parameter
        for parameter in endpoint.parameters
        if parameter.location in ("path", "query")
        and parameter.name.lower() not in LOCATION_PARAMETERS
    ]


def _build(entry: dict[str, Any], endpoint: Endpoint, fallbacks: dict[str, str]) -> TypedTool:
    if endpoint.method != "GET":
        # A typed tool is annotated read-only, which only a GET operation is.
        raise ValueError(f"typed_tools.json: {entry['name']} runs a {endpoint.method} operation")
    parameters = exposed_parameters(endpoint)
    exposed = {p.name for p in parameters}
    dropped = [p.name for p in endpoint.parameters if p.required and p.name not in exposed]
    if dropped:
        # An operation that cannot run without an input the tool does not take
        # (a location, a header, a cookie) has no typed tool.
        raise ValueError(
            f"typed_tools.json: {entry['name']} drops required parameters {', '.join(dropped)}"
        )
    required = tuple(p.name for p in parameters if p.required)
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {p.name: _parameter_schema(p, fallbacks.get(p.name, "")) for p in parameters},
        "additionalProperties": False,
    }
    if required:
        input_schema["required"] = list(required)
    Draft202012Validator.check_schema(input_schema)
    tool = MCPTool(
        name=entry["name"],
        title=entry["title"],
        description=entry["description"],
        inputSchema=input_schema,
        annotations=read_only(entry["title"]),
    )
    return TypedTool(
        name=entry["name"],
        operation_id=endpoint.operation_id,
        tool=tool,
        accepted=frozenset(p.name for p in parameters),
        required=required,
        validator=Draft202012Validator(input_schema),
    )


def _load_raw() -> dict[str, Any]:
    raw = resources.files(SPEC_PACKAGE).joinpath(SPEC_FILE).read_text(encoding="utf-8")
    spec = json.loads(raw)
    if not isinstance(spec.get("tools"), list):
        raise ValueError("typed_tools.json: tools must be a list")
    return spec


def load_spec() -> list[dict[str, Any]]:
    """The typed tool entries bundled with the package, in listing order."""
    return _load_raw()["tools"]


@lru_cache(maxsize=1)
def typed_tools() -> dict[str, TypedTool]:
    """Every typed tool by name. Fails at first use on a name or operation the catalog lacks."""
    catalog = load_catalog()
    spec = _load_raw()
    fallbacks = dict(spec.get("parameter_descriptions") or {})
    tools: dict[str, TypedTool] = {}
    for entry in spec["tools"]:
        name = entry["name"]
        if name in tools:
            raise ValueError(f"typed_tools.json: duplicate tool name {name}")
        tools[name] = _build(entry, catalog.get(entry["operation_id"]), fallbacks)
    return tools


def typed_tool_listing() -> list[MCPTool]:
    """The typed tools as tools/list declares them, before server-wide metadata."""
    return [typed.tool for typed in typed_tools().values()]


def is_typed_tool(name: str) -> bool:
    return name in typed_tools()


def minimize_response(result: Any) -> Any:
    """Drop request and logging metadata from a typed tool's result.

    A failure keeps only what went wrong and what to do next. A success loses
    its top-level request metadata and keeps only data facts in meta. The data
    itself is never touched: dates inside records are data, not metadata.
    """
    if not isinstance(result, dict):
        return result
    if is_error_payload(result):
        return {key: result[key] for key in _ERROR_KEYS if key in result}
    out = {key: value for key, value in result.items() if key not in _DROPPED_KEYS}
    meta = out.pop("meta", None)
    if isinstance(meta, dict):
        kept = {key: meta[key] for key in _META_KEYS if key in meta}
        if kept:
            out["meta"] = kept
    return out


async def _run(typed: TypedTool, *, operation_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
    unknown = sorted(set(arguments) - typed.accepted)
    if unknown:
        return {
            "error": "unknown_parameters",
            "reason": f"{typed.name} does not take: {', '.join(unknown)}",
            "unknown": unknown,
            "accepted": sorted(typed.accepted),
        }
    missing = [name for name in typed.required if arguments.get(name) in (None, "")]
    if missing:
        return {
            "error": "missing_required_parameters",
            "reason": f"{typed.name} needs: {', '.join(missing)}",
            "missing": missing,
        }
    violations = sorted(
        typed.validator.iter_errors(arguments), key=lambda error: list(error.absolute_path)
    )
    if violations:
        invalid = [
            {
                "parameter": ".".join(str(part) for part in error.absolute_path) or None,
                "problem": _clip(error.message, MAX_VIOLATION_CHARS),
            }
            for error in violations[:MAX_REPORTED_VIOLATIONS]
        ]
        return {
            "error": "invalid_arguments",
            "reason": f"{typed.name} got arguments outside its input schema",
            "invalid": invalid,
        }
    # The undecorated call_endpoint: the span is the typed tool's own, so
    # the call is not counted twice.
    result = await gateway.call_endpoint.__wrapped__(
        operation_id=operation_id, params=dict(arguments)
    )
    return minimize_response(result)


@cache
def _runner(name: str) -> Any:
    typed = typed_tools()[name]

    @trace_mcp_tool(name)
    async def run(*, operation_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await _run(typed, operation_id=operation_id, arguments=arguments)

    return run


async def call_typed_tool(name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
    """Call the typed tool `name` with the caller's arguments."""
    typed = typed_tools()[name]
    return await _runner(name)(operation_id=typed.operation_id, arguments=dict(arguments or {}))
