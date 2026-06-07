"""Catalog builder, loader, search, and response shaping tests."""

from __future__ import annotations

import json
from pathlib import Path

from sugra_api_mcp.catalog.builder import build_catalog_from_openapi
from sugra_api_mcp.catalog.loader import load_catalog
from sugra_api_mcp.catalog.response import shape_response
from sugra_api_mcp.catalog.search import search_catalog

FIXTURE = Path(__file__).parent / "fixtures" / "openapi_minimal.json"


def test_catalog_builder_from_fixture() -> None:
    catalog = build_catalog_from_openapi(json.loads(FIXTURE.read_text(encoding="utf-8")))

    assert catalog.endpoint_count == 6
    assert catalog.source == "fixture"
    assert catalog.get("cot_financial").path == "/api/v1/cot/financial"
    assert catalog.get("quotes_symbol_price").required_parameters == ["symbol"]
    assert catalog.get("cot_financial").source_family == "markets"
    assert catalog.get("bis_cb_rates").toolset == "central_banks"


def test_load_bundled_catalog_has_endpoints() -> None:
    catalog = load_catalog()

    assert catalog.endpoint_count > 0
    assert catalog.get("cot_financial").operation_id == "cot_financial"


def test_bundled_catalog_network_toolset_covers_net_atlas() -> None:
    """MCP-Imp-5: every /api/v1/network endpoint must live in the network
    toolset (they used to be invisible inside the catch-all core)."""
    catalog = load_catalog()

    network_paths = [
        endpoint
        for endpoint in catalog.endpoints
        if endpoint.path.startswith("/api/v1/network")
    ]
    assert len(network_paths) >= 50
    assert all(endpoint.toolset == "network" for endpoint in network_paths)
    # And nothing else claims the toolset.
    network_toolset = [e for e in catalog.endpoints if e.toolset == "network"]
    assert len(network_toolset) == len(network_paths)


def test_bundled_catalog_carries_bulk_request_body_schema() -> None:
    """MCP-Imp-6: the regenerated bundle must contain the resolved body
    schema for the per-item bulk endpoints (clients used to guess 'ips')."""
    catalog = load_catalog()

    bulk_ip = catalog.get("post_network_bulk_ip")
    assert bulk_ip.request_body_schema["required"] == ["ips"]
    assert bulk_ip.request_body_schema["properties"]["ips"]["items"] == {"type": "string"}

    bulk_asn = catalog.get("post_network_bulk_asn")
    assert bulk_asn.request_body_schema["properties"]["asns"]["items"] == {"type": "integer"}


def test_search_nasdaq_futures_returns_market_relevant_candidate() -> None:
    catalog = build_catalog_from_openapi(json.loads(FIXTURE.read_text(encoding="utf-8")))

    results = search_catalog(catalog, "NASDAQ futures", limit=3)

    assert results
    assert results[0]["operation_id"] == "cot_financial"
    assert results[0]["toolset"] == "markets"
    assert results[0]["why"]
    assert any(reason.startswith("alias:") for reason in results[0]["why"])
    assert "central_banks_bcra_cotizaciones" not in {
        result["operation_id"] for result in results
    }


def test_search_supports_toolset_and_source_family_filters() -> None:
    catalog = build_catalog_from_openapi(json.loads(FIXTURE.read_text(encoding="utf-8")))

    market_results = search_catalog(catalog, "NASDAQ futures", toolset="markets", limit=3)
    environment_results = search_catalog(catalog, "air quality", source="environment", limit=3)
    central_bank_results = search_catalog(
        catalog, "central bank rates", toolset="central_banks", limit=3
    )

    assert [result["operation_id"] for result in market_results] == ["cot_financial"]
    assert environment_results[0]["operation_id"] == "air_quality_current"
    assert environment_results[0]["source_family"] == "environment"
    assert central_bank_results[0]["operation_id"] == "bis_cb_rates"
    assert {result["toolset"] for result in central_bank_results} == {"central_banks"}


def test_describe_known_operation() -> None:
    catalog = build_catalog_from_openapi(json.loads(FIXTURE.read_text(encoding="utf-8")))

    endpoint = catalog.get("quotes_symbol_price")

    assert endpoint.summary == "Quote price"
    assert endpoint.parameters[0].name == "symbol"
    assert endpoint.parameters[0].location == "path"


def test_response_shaping_limit_fields_and_include_raw() -> None:
    payload = {
        "data": [
            {"symbol": "AAPL", "price": 200, "volume": 10},
            {"symbol": "MSFT", "price": 300, "volume": 20},
        ],
        "meta": {"source": "fixture"},
    }

    shaped = shape_response(payload, limit=1, fields=["symbol", "price"], include_raw=True)

    assert shaped["data"] == [{"symbol": "AAPL", "price": 200}]
    assert shaped["meta"]["shaped"]["limit"] == 1
    assert shaped["meta"]["shaped"]["fields"] == ["symbol", "price"]
    assert shaped["raw"] == payload


def test_response_shaping_projects_envelope_less_flat_payload() -> None:
    """Net Atlas endpoints return flat dicts without a data envelope; fields
    used to be a silent no-op there (field test 2026-06-07) while meta.shaped
    echoed the requested fields as if they had been applied.
    """
    payload = {
        "ip": "8.8.8.8",
        "asn": 15169,
        "rdns": "dns.google",
        "geo": {"city": "Ashburn", "country": "US"},
        "_meta": {"atlas_built_at": "2026-06-01"},
    }

    shaped = shape_response(payload, fields=["ip", "asn"])

    assert shaped["ip"] == "8.8.8.8"
    assert shaped["asn"] == 15169
    assert "rdns" not in shaped
    assert "geo" not in shaped
    # Provenance keys survive projection.
    assert shaped["_meta"] == {"atlas_built_at": "2026-06-01"}
    assert shaped["meta"]["shaped"]["fields_applied"] == ["ip", "asn"]
    assert shaped["meta"]["shaped"]["fields_unmatched"] == []


def test_response_shaping_supports_dotted_paths() -> None:
    payload = {
        "ip": "8.8.8.8",
        "geo": {"city": "Ashburn", "country": "US"},
        "privacy": {"vpn": False},
    }

    shaped = shape_response(payload, fields=["geo.city"])

    assert shaped["geo"] == {"city": "Ashburn"}
    assert "privacy" not in shaped
    assert "ip" not in shaped
    assert shaped["meta"]["shaped"]["fields_applied"] == ["geo.city"]


def test_response_shaping_reports_unmatched_fields() -> None:
    payload = {"data": [{"symbol": "AAPL", "price": 200}], "meta": {}}

    shaped = shape_response(payload, fields=["symbol", "nonexistent"])

    assert shaped["data"] == [{"symbol": "AAPL"}]
    assert shaped["meta"]["shaped"]["fields_applied"] == ["symbol"]
    assert shaped["meta"]["shaped"]["fields_unmatched"] == ["nonexistent"]


def test_response_shaping_literal_dotted_key_wins_over_path() -> None:
    payload = {"data": {"a.b": 1, "a": {"b": 2}}, "meta": {}}

    shaped = shape_response(payload, fields=["a.b"])

    assert shaped["data"] == {"a.b": 1}


def test_response_shaping_dotted_path_inside_data_list_items() -> None:
    payload = {
        "data": [
            {"ip": "1.1.1.1", "geo": {"city": "X", "country": "A"}},
            {"ip": "2.2.2.2", "geo": {"city": "Y", "country": "B"}},
        ],
        "meta": {},
    }

    shaped = shape_response(payload, fields=["ip", "geo.city"])

    assert shaped["data"] == [
        {"ip": "1.1.1.1", "geo": {"city": "X"}},
        {"ip": "2.2.2.2", "geo": {"city": "Y"}},
    ]


def test_response_shaping_limit_applied_flag() -> None:
    enveloped = shape_response({"data": [1, 2, 3], "meta": {}}, limit=2)
    assert enveloped["data"] == [1, 2]
    assert enveloped["meta"]["shaped"]["limit_applied"] is True

    # Limit on a non-list payload is a documented no-op and must say so.
    flat = shape_response({"ip": "8.8.8.8"}, limit=2)
    assert flat["ip"] == "8.8.8.8"
    assert flat["meta"]["shaped"]["limit_applied"] is False


def test_response_shaping_wraps_bare_list_payload_when_shaping_requested() -> None:
    payload = [{"a": 1, "b": 2}, {"a": 3, "b": 4}, {"a": 5, "b": 6}]

    shaped = shape_response(payload, limit=2, fields=["a"])

    assert shaped["data"] == [{"a": 1}, {"a": 3}]
    assert shaped["meta"]["shaped"]["limit_applied"] is True
    assert shaped["meta"]["shaped"]["fields_applied"] == ["a"]


def test_response_shaping_bare_list_without_shaping_params_is_untouched() -> None:
    assert shape_response([1, 2, 3]) == [1, 2, 3]


def test_response_shaping_tolerates_non_dict_meta_key() -> None:
    payload = {"ip": "8.8.8.8", "meta": "not-a-dict"}

    shaped = shape_response(payload, fields=["ip"])

    assert shaped["ip"] == "8.8.8.8"
    assert shaped["meta"]["shaped"]["fields_applied"] == ["ip"]


def test_response_shaping_omits_oversized_raw_payload() -> None:
    payload = {"data": [{"blob": "x" * 1000}], "meta": {}}

    shaped = shape_response(payload, include_raw=True, max_raw_chars=200)

    assert "raw" not in shaped
    assert shaped["meta"]["raw_omitted"]["reason"] == "exceeds_raw_size_limit"


def test_catalog_builder_extracts_request_body_schema_with_ref_resolution() -> None:
    """Field test 2026-06-07: describe_endpoint kept only the required flag
    and discarded the requestBody schema, so clients had to guess POST body
    keys ({"ips": [...]}). FastAPI emits $ref into components/schemas - the
    builder must inline them.
    """
    openapi = {
        "paths": {
            "/api/v1/network/bulk/ip": {
                "post": {
                    "operationId": "post_network_bulk_ip",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/BulkIpRequest"}
                            }
                        },
                    },
                }
            },
            "/api/v1/openfigi/mapping": {
                "post": {
                    "operationId": "openfigi_mapping",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "array",
                                    "items": {"$ref": "#/components/schemas/MappingJob"},
                                }
                            }
                        },
                    },
                }
            },
        },
        "components": {
            "schemas": {
                "BulkIpRequest": {
                    "type": "object",
                    "required": ["ips"],
                    "properties": {
                        "ips": {"type": "array", "items": {"type": "string"}, "maxItems": 100}
                    },
                },
                "MappingJob": {
                    "type": "object",
                    "properties": {"idType": {"type": "string"}},
                },
            }
        },
    }

    catalog = build_catalog_from_openapi(openapi)

    bulk = catalog.get("post_network_bulk_ip")
    assert bulk.request_body_required is True
    assert bulk.request_body_schema["type"] == "object"
    assert bulk.request_body_schema["required"] == ["ips"]
    assert bulk.request_body_schema["properties"]["ips"]["items"] == {"type": "string"}

    # Array-of-$ref shape (openfigi): nested refs resolve too.
    figi = catalog.get("openfigi_mapping")
    assert figi.request_body_required is False
    assert figi.request_body_schema["type"] == "array"
    assert figi.request_body_schema["items"]["properties"]["idType"] == {"type": "string"}


def test_catalog_builder_request_body_schema_survives_ref_cycles() -> None:
    """Self-referential schemas must terminate: the inner cyclic $ref stays
    as a marker instead of recursing forever."""
    openapi = {
        "paths": {
            "/api/v1/tree": {
                "post": {
                    "operationId": "tree_op",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Node"}
                            }
                        },
                    },
                }
            }
        },
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "children": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/Node"},
                        },
                    },
                }
            }
        },
    }

    catalog = build_catalog_from_openapi(openapi)

    schema = catalog.get("tree_op").request_body_schema
    assert schema["properties"]["name"] == {"type": "string"}
    # The cycle stops at a $ref marker rather than infinite inlining.
    assert schema["properties"]["children"]["items"] == {"$ref": "#/components/schemas/Node"}


def test_catalog_builder_unknown_ref_stays_as_marker() -> None:
    openapi = {
        "paths": {
            "/api/v1/x": {
                "post": {
                    "operationId": "x_op",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Missing"}
                            }
                        },
                    },
                }
            }
        }
    }

    catalog = build_catalog_from_openapi(openapi)

    assert catalog.get("x_op").request_body_schema == {"$ref": "#/components/schemas/Missing"}


def test_endpoint_dict_round_trip_with_request_body_schema() -> None:
    catalog = build_catalog_from_openapi(
        json.loads(FIXTURE.read_text(encoding="utf-8"))
    )

    figi = catalog.get("openfigi_map")
    # Fixture maps requestBody through components/schemas refs.
    assert figi.request_body_schema["properties"]["jobs"]["items"]["properties"][
        "idType"
    ] == {"type": "string"}

    as_dict = figi.to_dict()
    assert as_dict["request_body_schema"] == figi.request_body_schema

    from sugra_api_mcp.catalog.models import Endpoint

    round_tripped = Endpoint.from_dict(as_dict)
    assert round_tripped.request_body_schema == figi.request_body_schema

    # GET endpoints without a body keep their serialized form lean.
    quote = catalog.get("quotes_symbol_price")
    assert quote.request_body_schema == {}
    assert "request_body_schema" not in quote.to_dict()


def test_toolset_for_tags_maps_sugra_net_atlas_to_network() -> None:
    """The 53 Net Atlas endpoints used to fall into the catch-all core
    toolset (478 endpoints) because their tag was missing from the map -
    network engineers could not discover them via list_toolsets."""
    from sugra_api_mcp.catalog.toolsets import BROAD_TOOLSETS, toolset_for_tags

    assert toolset_for_tags(["Sugra Net Atlas"]) == "network"
    assert "network" in BROAD_TOOLSETS


def test_ordered_toolsets_carries_descriptions() -> None:
    from sugra_api_mcp.catalog.toolsets import ordered_toolsets

    out = ordered_toolsets({"network": 53, "digital_infra": 56, "core": 425})
    by_name = {entry["name"]: entry for entry in out}

    assert "Net Atlas" in by_name["network"]["description"]
    # digital_infra must be explicit that it is blockchain, not internet
    # infrastructure - it misled network engineers in the field test.
    assert "blockchain" in by_name["digital_infra"]["description"].lower()
    assert "network" in by_name["digital_infra"]["description"].lower()


def test_every_broad_toolset_has_a_description() -> None:
    from sugra_api_mcp.catalog.toolsets import BROAD_TOOLSETS, TOOLSET_DESCRIPTIONS

    missing = [name for name in BROAD_TOOLSETS if not TOOLSET_DESCRIPTIONS.get(name)]
    assert not missing, f"toolsets without descriptions: {missing}"


def test_catalog_builder_fails_on_missing_operation_id() -> None:
    openapi = {
        "paths": {
            "/api/v1/missing": {
                "get": {
                    "summary": "Missing operation",
                    "parameters": [],
                }
            }
        }
    }

    import pytest

    with pytest.raises(ValueError, match="missing operationId"):
        build_catalog_from_openapi(openapi)


def test_catalog_builder_fails_on_duplicate_operation_id() -> None:
    openapi = {
        "paths": {
            "/api/v1/one": {"get": {"operationId": "duplicate_op"}},
            "/api/v1/two": {"get": {"operationId": "duplicate_op"}},
        }
    }

    import pytest

    with pytest.raises(ValueError, match="duplicate operationId"):
        build_catalog_from_openapi(openapi)
