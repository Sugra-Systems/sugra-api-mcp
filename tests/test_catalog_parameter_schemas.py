"""Parameter schemas in the bundled catalog, and what the typed tools keep of them."""

from __future__ import annotations

from typing import Any

from sugra_api_mcp.catalog.builder import build_catalog_from_openapi
from sugra_api_mcp.catalog.loader import load_catalog
from sugra_api_mcp.tools import typed

# Catalog keywords a typed input does not take from the schema, each on purpose.
_LEFT_OUT = {
    "title": "a component or model field name, which tells the model nothing",
    "description": "taken as the input's own description, clipped",
    "examples": "taken as the input's examples",
    "anyOf": "collapsed to its one concrete option, or kept whole",
}


def _openapi(parameter_schema: dict[str, Any]) -> dict[str, Any]:
    return {
        "paths": {
            "/api/v2/things": {
                "get": {
                    "operationId": "list_things",
                    "parameters": [{"name": "color", "in": "query", "schema": parameter_schema}],
                }
            }
        },
        "components": {
            "schemas": {
                "Color": {"title": "Color", "type": "string", "enum": ["red", "green"]},
                "Loop": {"$ref": "#/components/schemas/Loop"},
            }
        },
    }


def _parameter_schema(openapi: dict[str, Any]) -> dict[str, Any]:
    endpoint = build_catalog_from_openapi(openapi).get("list_things")
    return endpoint.parameters[0].schema_


def test_a_parameter_ref_is_inlined_with_its_own_keys_on_top() -> None:
    schema = _parameter_schema(
        _openapi({"$ref": "#/components/schemas/Color", "description": "Which color.", "default": "red"})
    )
    assert schema == {
        "title": "Color",
        "type": "string",
        "enum": ["red", "green"],
        "description": "Which color.",
        "default": "red",
    }


def test_a_ref_inside_a_nullable_parameter_is_inlined() -> None:
    schema = _parameter_schema(
        _openapi({"anyOf": [{"$ref": "#/components/schemas/Color"}, {"type": "null"}]})
    )
    assert schema == {
        "anyOf": [{"title": "Color", "type": "string", "enum": ["red", "green"]}, {"type": "null"}]
    }


def test_an_unknown_or_cyclic_parameter_ref_stays_a_marker() -> None:
    assert _parameter_schema(_openapi({"$ref": "#/components/schemas/Nothing"})) == {
        "$ref": "#/components/schemas/Nothing"
    }
    assert _parameter_schema(_openapi({"$ref": "#/components/schemas/Loop"})) == {
        "$ref": "#/components/schemas/Loop"
    }


def _refs(node: Any) -> list[str]:
    if isinstance(node, dict):
        found = [node["$ref"]] if "$ref" in node else []
        return found + [ref for value in node.values() for ref in _refs(value)]
    if isinstance(node, list):
        return [ref for item in node for ref in _refs(item)]
    return []


def _kept_refs(endpoints: Any) -> dict[str, list[str]]:
    """Every ref left in a parameter or request body schema, by operation and place."""
    kept = {}
    for endpoint in endpoints:
        for parameter in endpoint.parameters:
            if _refs(parameter.schema_):
                kept[f"{endpoint.operation_id}.{parameter.name}"] = _refs(parameter.schema_)
        if _refs(endpoint.request_body_schema):
            kept[f"{endpoint.operation_id}.body"] = _refs(endpoint.request_body_schema)
    return kept


def test_no_bundled_schema_keeps_a_ref() -> None:
    assert _kept_refs(load_catalog().endpoints) == {}


def test_the_ref_scan_finds_a_marker_left_in_a_request_body() -> None:
    openapi = _openapi({"type": "string"})
    openapi["paths"]["/api/v2/things"]["post"] = {
        "operationId": "make_thing",
        "requestBody": {
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Loop"}}}
        },
    }
    catalog = build_catalog_from_openapi(openapi)
    assert _kept_refs(catalog.endpoints) == {"make_thing.body": ["#/components/schemas/Loop"]}


def _typed_inputs() -> list[tuple[str, str, dict[str, Any], dict[str, Any]]]:
    """(tool, input, catalog schema, typed schema) for every typed tool input.

    An input its spec entry leaves out (omit_parameters) is not one.
    """
    catalog = load_catalog()
    entries = {entry["name"]: entry for entry in typed.load_spec()}
    inputs = []
    for typed_tool in typed.typed_tools().values():
        properties = typed_tool.tool.inputSchema["properties"]
        omitted = entries[typed_tool.name].get("omit_parameters", [])
        for parameter in typed.exposed_parameters(catalog.get(typed_tool.operation_id)):
            if parameter.name in omitted:
                continue
            inputs.append((typed_tool.name, parameter.name, parameter.schema_, properties[parameter.name]))
    return inputs


def _override(tool: str, name: str) -> dict[str, Any]:
    """What the spec entry of tool changes on its input name, if anything."""
    entry = next(entry for entry in typed.load_spec() if entry["name"] == tool)
    return entry.get("parameters", {}).get(name, {})


def _concrete(schema: dict[str, Any]) -> dict[str, Any]:
    """The schema with a nullable anyOf collapsed to its one concrete option."""
    options = [o for o in schema.get("anyOf") or [] if o.get("type") != "null"]
    if len(options) == 1:
        return {**options[0], **{k: v for k, v in schema.items() if k != "anyOf"}}
    return schema


def test_every_typed_input_lists_the_enum_of_its_catalog_schema() -> None:
    with_enum = {}
    for tool, name, catalog_schema, typed_schema in _typed_inputs():
        enum = _concrete(catalog_schema).get("enum")
        if enum is not None:
            # An entry may list fewer of the values (typed._overridden refuses more).
            with_enum[f"{tool}.{name}"] = typed_schema.get("enum") == _override(tool, name).get("enum", enum)
    assert with_enum and all(with_enum.values()), with_enum
    # The five inputs that were listed as free strings while their schema was a bare ref.
    for key in (
        "get_power_grid_data.metric",
        "get_power_grid_data.frequency",
        "get_power_generation_mix.frequency",
        "get_retail_electricity_prices.sector",
        "get_retail_electricity_prices.frequency",
    ):
        assert with_enum.get(key) is True, key


def test_every_catalog_keyword_of_a_typed_input_is_kept_or_left_out_on_purpose() -> None:
    known = set(typed._SCHEMA_KEYS) | set(_LEFT_OUT)
    unknown = {}
    for tool, name, catalog_schema, typed_schema in _typed_inputs():
        options = [catalog_schema, *(o for o in catalog_schema.get("anyOf") or [] if isinstance(o, dict))]
        keywords = {key for option in options for key in option}
        if keywords - known:
            unknown[f"{tool}.{name}"] = sorted(keywords - known)
        for option in options:
            if "pattern" in option:
                assert isinstance(option["pattern"], str), (tool, name)
        if "pattern" in _concrete(catalog_schema):
            assert typed_schema.get("pattern") == _concrete(catalog_schema)["pattern"], (tool, name)
    assert unknown == {}


def test_every_left_out_keyword_is_handled_as_its_reason_says() -> None:
    for tool, name, catalog_schema, typed_schema in _typed_inputs():
        assert "title" not in typed_schema, (tool, name)
        description = typed_schema.get("description")
        if description is not None:
            assert description == " ".join(description.split()), (tool, name)
            assert len(description) <= typed.MAX_PARAMETER_DESCRIPTION + len("..."), (tool, name)
        examples = typed_schema.get("examples")
        if examples is not None:
            assert isinstance(examples, list) and len(examples) == 1, (tool, name)
        concrete = [
            o for o in catalog_schema.get("anyOf") or [] if isinstance(o, dict) and o.get("type") != "null"
        ]
        if len(concrete) > 1:
            assert typed_schema.get("anyOf") == concrete, (tool, name)
        else:
            assert "anyOf" not in typed_schema, (tool, name)
