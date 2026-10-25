"""One tool per catalog operation, listed only to the ChatGPT client profile.

OpenAI's app submission guidelines ask for each action to be its own tool,
with a name that says what it does, minimal inputs, no raw location fields and
no logging metadata in the response. The gateway tools (call_endpoint and its
search and describe companions) are one generic executor, which those
guidelines do not accept, so ChatGPT is also listed the typed tools defined in
catalog/data/typed_tools.json: a name, a title and a description per catalog
operation. Their input schemas come from the bundled catalog, less location
parameters, and an entry may narrow its own: leave optional parameters out,
or make one that stays required, give it a smaller enum, its own description
or example (_narrowed). The catalog itself is never changed, so every other
client calls the operation as it was.

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
import logging
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

logger = logging.getLogger("sugra_mcp.typed")

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
    "pattern",
)

# A parameter description longer than this is cut at its last sentence end.
MAX_PARAMETER_DESCRIPTION = 300

# What an entry's "parameters" may change on one parameter the tool takes.
_OVERRIDE_KEYS = frozenset({"required", "enum", "description", "example"})

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
    "detail",
    "detail_truncated",
    "daily_limit",
    "plan",
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

# What it keeps of the data's provenance, from meta or, on an envelope-less
# payload, from _meta: when the data was observed and any notice its source
# requires. `source` is left out: it can name a commercial upstream, which a
# ChatGPT session is never shown by name; the tool's description names the
# source it may cite.
_PROVENANCE_KEYS = (
    "attribution",
    "data_age_days",
    "data_time",
    "period",
    "stale",
    "stale_since",
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
    # The catalog keeps an example on the parameter or, for some, only in its schema.
    examples = schema.get("examples")
    example = parameter.example
    if example is None and isinstance(examples, list) and examples:
        example = examples[0]
    if example is not None and "enum" not in out and example != out.get("default"):
        out["examples"] = [example]
    return out


def exposed_parameters(endpoint: Endpoint) -> list[EndpointParameter]:
    """The endpoint's path and query parameters, less location fields."""
    return [
        parameter
        for parameter in endpoint.parameters
        if parameter.location in ("path", "query")
        and parameter.name.lower() not in LOCATION_PARAMETERS
    ]


def _narrowed(entry: dict[str, Any], parameters: list[EndpointParameter]) -> list[EndpointParameter]:
    """The exposed parameters after the entry's own narrowing, in catalog order.

    omit_parameters leaves optional parameters out of the tool, and parameters
    changes one that stays (_overridden). A narrowing only ever takes inputs
    away, so a call that passes the narrowed schema also passes the catalog's.
    A narrowing the catalog does not allow raises a ValueError.
    """
    name = entry["name"]
    omitted = entry.get("omit_parameters", [])
    if not isinstance(omitted, list) or not all(isinstance(item, str) for item in omitted):
        raise ValueError(f"typed_tools.json: {name} omit_parameters is not a list of names")
    by_name = {p.name: p for p in parameters}
    for item in omitted:
        if item not in by_name:
            raise ValueError(f"typed_tools.json: {name} omits {item}, which it does not expose")
        if by_name[item].required:
            raise ValueError(f"typed_tools.json: {name} omits required parameter {item}")
    overrides = entry.get("parameters", {})
    if not isinstance(overrides, dict):
        raise ValueError(f"typed_tools.json: {name} parameters is not an object")
    unknown = sorted(set(overrides) - set(by_name))
    if unknown:
        raise ValueError(
            f"typed_tools.json: {name} changes {', '.join(unknown)}, which it does not take"
        )
    dropped = sorted(set(overrides) & set(omitted))
    if dropped:
        raise ValueError(f"typed_tools.json: {name} changes {', '.join(dropped)}, which it omits")
    kept = [p for p in parameters if p.name not in omitted]
    return [_overridden(name, p, overrides[p.name]) if p.name in overrides else p for p in kept]


def _overridden(tool: str, parameter: EndpointParameter, override: Any) -> EndpointParameter:
    """A copy of parameter with an entry's changes: required, a smaller enum, a description, an example."""
    where = f"typed_tools.json: {tool} parameter {parameter.name}"
    if not isinstance(override, dict) or not override:
        raise ValueError(f"{where} has no changes")
    unknown = sorted(set(override) - _OVERRIDE_KEYS)
    if unknown:
        raise ValueError(f"{where} cannot change {', '.join(unknown)}")
    update: dict[str, Any] = {}
    if "required" in override:
        # A tool may need an input its operation leaves optional, never the
        # other way round: the operation would refuse the call.
        if override["required"] is not True:
            raise ValueError(f"{where} required can only be true")
        update["required"] = True
    if "enum" in override:
        enum = override["enum"]
        if (
            not isinstance(enum, list)
            or not enum
            or not all(isinstance(value, str) for value in enum)
            or len(set(enum)) != len(enum)
        ):
            raise ValueError(f"{where} enum is not a list of distinct strings")
        catalog = _parameter_schema(parameter)
        if catalog.get("type") != "string":
            raise ValueError(f"{where} enum needs a string parameter")
        if "enum" in catalog and not set(enum) <= set(catalog["enum"]):
            raise ValueError(f"{where} enum takes values the catalog does not")
        if "default" in catalog and catalog["default"] not in enum:
            raise ValueError(f"{where} enum leaves out the catalog default {catalog['default']}")
        # A listed value the input's own schema refuses (its pattern or length)
        # would fail every call that sends it.
        validator = Draft202012Validator(catalog)
        for value in enum:
            if not validator.is_valid(value):
                raise ValueError(f"{where} enum value {value} is outside its own schema")
        # A top-level key wins over the catalog's nullable wrapper (_parameter_schema).
        update["schema_"] = {**parameter.schema_, "enum": list(enum)}
    if "description" in override:
        description = override["description"]
        if not isinstance(description, str) or not description.strip():
            raise ValueError(f"{where} description is not a text")
        if len(" ".join(description.split())) > MAX_PARAMETER_DESCRIPTION:
            raise ValueError(
                f"{where} description is longer than {MAX_PARAMETER_DESCRIPTION} characters"
            )
        update["description"] = description
    if "example" in override:
        update["example"] = override["example"]
    narrowed = parameter.model_copy(update=update)
    if "example" in override:
        schema = _parameter_schema(narrowed)
        if "enum" in schema:
            # An enum lists its values, so no example is shown beside it.
            raise ValueError(f"{where} example would not be shown beside an enum")
        if not Draft202012Validator(schema).is_valid(override["example"]):
            raise ValueError(f"{where} example is outside its own schema")
    return narrowed


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
    parameters = _narrowed(entry, parameters)
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
    return _checked_spec(json.loads(raw))


def _checked_spec(spec: Any) -> dict[str, Any]:
    """The spec, or a ValueError when its tools are missing or not a list of at least one entry."""
    entries = spec.get("tools") if isinstance(spec, dict) else None
    if not isinstance(entries, list) or not entries:
        got = "an empty list" if entries == [] else "no tools" if entries is None else type(entries).__name__
        raise ValueError(f"typed_tools.json: tools is not a list of entries (got {got})")
    return spec


def load_spec() -> list[dict[str, Any]]:
    """The typed tool entries bundled with the package, in listing order."""
    return _load_raw()["tools"]


@lru_cache(maxsize=1)
def typed_tools() -> dict[str, TypedTool]:
    """Every typed tool by name.

    An entry that cannot be built (a duplicate name, an operation the catalog
    lacks or a typed tool cannot run, a missing field, a narrowing the catalog
    does not allow) is left out with an error log naming it, and the rest are
    built: one bad entry costs a ChatGPT session that one tool, not its whole
    list. A test builds the shipped spec whole, so a bad entry fails CI before
    it can ship. A spec whose tools are missing or not a list of at least one
    entry raises a ValueError on load (_checked_spec): nothing in it can be
    built.
    """
    catalog = load_catalog()
    spec = _load_raw()
    fallbacks = dict(spec.get("parameter_descriptions") or {})
    tools: dict[str, TypedTool] = {}
    for index, entry in enumerate(spec["tools"]):
        name = entry.get("name") if isinstance(entry, dict) else None
        try:
            if name in tools:
                raise ValueError(f"typed_tools.json: duplicate tool name {name}")
            tools[name] = _build(entry, catalog.get(entry["operation_id"]), fallbacks)
        except Exception as e:
            logger.error(
                "typed_tools.json: entry %d (%s) left out (%s: %s).",
                index,
                name,
                type(e).__name__,
                e,
            )
    return tools


def typed_tool_listing() -> list[MCPTool]:
    """The typed tools as tools/list declares them, before server-wide metadata."""
    return [typed.tool for typed in typed_tools().values()]


def is_typed_tool(name: str) -> bool:
    return name in typed_tools()


def minimize_response(result: Any) -> Any:
    """Drop request and logging metadata from a typed tool's result.

    A failure keeps only what went wrong and what to do next. A success loses
    its top-level request metadata and keeps in meta only data facts and the
    data's provenance, the latter from _meta when meta lacks it. The data
    itself is never touched: dates inside records are data, not metadata.
    """
    if not isinstance(result, dict):
        return result
    if is_error_payload(result):
        return {key: result[key] for key in _ERROR_KEYS if key in result}
    out = {key: value for key, value in result.items() if key not in _DROPPED_KEYS}
    meta = out.pop("meta", None)
    meta = meta if isinstance(meta, dict) else {}
    flat = result.get("_meta")
    flat = flat if isinstance(flat, dict) else {}
    kept = {key: meta[key] for key in _META_KEYS + _PROVENANCE_KEYS if key in meta}
    kept.update({key: flat[key] for key in _PROVENANCE_KEYS if key in flat and key not in meta})
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
    # call_endpoint's body without its span: the span is the typed tool's
    # own, so the call is not counted twice.
    result = await gateway.call_operation(operation_id=operation_id, params=dict(arguments))
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
