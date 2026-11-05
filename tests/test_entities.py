"""Sugra Entity MCP tool tests.

These tools wrap the Sugra Entity API surface (screening + composed KYB) with
compact output shaping so an agent's context budget is not flooded by the heavy
raw envelope. The tools call the API BY PATH (the bundled endpoint catalog
predates the entity endpoints), so the tests mock the shared client's GET / POST
methods rather than the catalog.
"""

from __future__ import annotations

from typing import Any

from sugra_api_mcp.tools import entities

# ---------------------------------------------------------------------------
# Fakes: capture the path + payload the tool sends and return a realistic,
# heavy raw envelope the compact projection must trim down.
# ---------------------------------------------------------------------------


_SCREEN_RAW = {
    "data": {
        "screening": {
            "status": "hit",
            "matches": [
                {
                    "list": "ofac_sdn",
                    "program": "UKRAINE-EO13662",
                    "restriction_type": "block",
                    "matched_name": "PUTIN, Vladimir Vladimirovich",
                    "score": 1.0,
                    "match_type": "name",
                    # Heavy / rare fields the compact shape must DROP.
                    "rationale": ["token overlap", "phonetic match", "alias hit"],
                    "source_id": "ofac-12345",
                    "list_published": "2026-06-01",
                },
                {
                    "list": "eu_consolidated",
                    "program": None,
                    "restriction_type": "block",
                    "matched_name": "PUTIN Vladimir",
                    "score": 0.93,
                    "match_type": "alias",
                    "rationale": ["alias overlap"],
                    "source_id": "eu-99",
                    "list_published": "2026-05-30",
                },
            ],
        }
    },
    "meta": {
        "product": "Sugra Entity",
        "disclaimer": (
            "Screening signal, not a compliance determination. Sugra is a "
            "technology provider, not a consumer reporting agency or sanctions "
            "authority. PEP and adverse-media coverage is supplementary and "
            "non-comprehensive."
        ),
        "list_freshness": {"ofac_sdn": "2026-06-01"},
        "stale_screening": False,
        "source": "sugra-entity",
        "request_id": "abc-123",
    },
}


_LOOKUP_RAW = {
    "data": {
        "entity": {
            "id": "sugra-ent:lei:5493001KJTIIGC8Y1R12",
            "anchor": {"lei": "5493001KJTIIGC8Y1R12"},
            "name": "Acme PLC",
            "country": "GB",
            "type": "company",
            "status": "ACTIVE",
            "field_provenance": {"name": "gleif", "country": "gleif"},
        },
        "screening": {
            "status": "review",
            "matches": [
                {"list": "ofac_sdn", "matched_name": "ACME PLC", "score": 0.88,
                 "match_type": "name", "rationale": ["x"], "source_id": "s1"},
                {"list": "eu", "matched_name": "Acme", "score": 0.81,
                 "match_type": "alias", "rationale": ["y"], "source_id": "s2"},
                {"list": "uk_ofsi", "matched_name": "ACME", "score": 0.75,
                 "match_type": "alias", "rationale": ["z"], "source_id": "s3"},
                {"list": "un", "matched_name": "Acme Ltd", "score": 0.71,
                 "match_type": "alias", "rationale": ["w"], "source_id": "s4"},
            ],
            "fifty_percent_rule": {
                "triggered": False, "ambiguous": False, "aggregate_pct": None,
                "sanctioned_owners": [], "evidence": "no sanctioned owners",
            },
        },
        "ownership": {
            "direct_parent": {"lei": "PARENT123", "name": "Acme Holdings"},
            "ultimate_parent": {"lei": "ULT999", "name": "Acme Global"},
            "psc": [],
            "truncated": False,
            "cycle_detected": False,
        },
        "adverse_media": {
            "precision": "low",
            "coverage_note": "supplementary, non-comprehensive, keyword-based",
            "articles": [{"title": "Acme probe", "url": "http://x"}],
        },
    },
    "meta": {
        "product": "Sugra Entity",
        "disclaimer": "Screening signal, not a compliance determination. ...",
        "partial": False,
        "stale_screening": False,
        "screening_id": "uuid-1",
        "match_engine_version": "1.0.0",
        "source": "sugra-entity",
    },
}


class FakeClient:
    def __init__(self, get_payload: dict[str, Any] | None = None,
                 post_payload: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None, dict[str, Any] | None]] = []
        self._get_payload = get_payload or {}
        self._post_payload = post_payload or {}

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("GET", path, params, None))
        return self._get_payload

    async def post(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(("POST", path, None, json))
        return self._post_payload

    async def request(self, method: str, path: str,
                      params: dict[str, Any] | None = None,
                      json: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append((method, path, params, json))
        return self._post_payload if method.upper() == "POST" else self._get_payload


# ---------------------------------------------------------------------------
# sugra_entity_screen
# ---------------------------------------------------------------------------


async def test_screen_posts_to_entity_screen_path(monkeypatch) -> None:
    fake = FakeClient(post_payload=_SCREEN_RAW)
    monkeypatch.setattr(entities, "get_client", lambda: fake)

    await entities.sugra_entity_screen(name="Vladimir Putin", country="RU")

    assert len(fake.calls) == 1
    method, path, _params, body = fake.calls[0]
    assert method == "POST"
    assert path == "/api/v1/entity/screen"
    # Optional fields with None are dropped; provided ones are sent.
    assert body == {"name": "Vladimir Putin", "country": "RU"}


async def test_screen_returns_compact_shape_and_drops_heavy_fields(monkeypatch) -> None:
    fake = FakeClient(post_payload=_SCREEN_RAW)
    monkeypatch.setattr(entities, "get_client", lambda: fake)

    result = await entities.sugra_entity_screen(name="Vladimir Putin")

    assert result["status"] == "hit"
    assert result["disclaimer"]  # screening-signal-not-determination carried through
    # Compact matches: ONLY name, score, list, type.
    assert result["matches"] == [
        {"name": "PUTIN, Vladimir Vladimirovich", "score": 1.0, "list": "ofac_sdn", "type": "name"},
        {"name": "PUTIN Vladimir", "score": 0.93, "list": "eu_consolidated", "type": "alias"},
    ]
    # Heavy fields gone everywhere in the projection.
    serialized = repr(result)
    for heavy in ("rationale", "source_id", "list_published", "restriction_type", "program"):
        assert heavy not in serialized


async def test_screen_omits_none_optional_params(monkeypatch) -> None:
    fake = FakeClient(post_payload=_SCREEN_RAW)
    monkeypatch.setattr(entities, "get_client", lambda: fake)

    await entities.sugra_entity_screen(
        name="Jane Doe", country=None, dob="1980-01-01", nationality=None
    )

    _method, _path, _params, body = fake.calls[0]
    assert body == {"name": "Jane Doe", "dob": "1980-01-01"}


# The API answer when the request sent dob, country or nationality: `score` is
# the adjusted score, `name_score` the name-only score before the fields moved
# it, and `secondary_fields` the outcome per field sent. With none sent the API
# still returns `name_score` (equal to `score`) and `secondary_fields: null`.
_SCREEN_RAW_SECONDARY = {
    "data": {
        "screening": {
            "status": "hit",
            "matches": [
                {
                    "list": "ofac_sdn",
                    "matched_name": "DOE, Jane",
                    "score": 1.0,
                    "name_score": 0.95,
                    "secondary_fields": {"dob": "match", "country": "match"},
                    "match_type": "name_fuzzy",
                    "rationale": ["dob 1980-01-01 matches the record"],
                    "source_id": "ofac-1",
                },
                {
                    "list": "eu_consolidated",
                    "matched_name": "DOE Janet",
                    "score": 0.8,
                    "name_score": 0.9,
                    "secondary_fields": {"dob": "contradict", "country": "unknown"},
                    "match_type": "name_fuzzy",
                    "rationale": ["dob 1980-01-01 contradicts the record"],
                    "source_id": "eu-2",
                },
            ],
        }
    },
    "meta": {"disclaimer": "Screening signal, not a compliance determination."},
}

_SCREEN_RAW_NAME_ONLY = {
    "data": {
        "screening": {
            "status": "review",
            "matches": [
                {
                    "list": "ofac_sdn",
                    "matched_name": "DOE, Jane",
                    "score": 0.95,
                    "name_score": 0.95,
                    "secondary_fields": None,
                    "match_type": "name_fuzzy",
                },
            ],
        }
    },
    "meta": {"disclaimer": "Screening signal, not a compliance determination."},
}


async def test_screen_with_dob_carries_secondary_outcomes_per_match(monkeypatch) -> None:
    fake = FakeClient(post_payload=_SCREEN_RAW_SECONDARY)
    monkeypatch.setattr(entities, "get_client", lambda: fake)

    result = await entities.sugra_entity_screen(name="Jane Doe", dob="1980-01-01")

    assert result["matches"] == [
        {"name": "DOE, Jane", "score": 1.0, "list": "ofac_sdn", "type": "name_fuzzy",
         "name_score": 0.95, "secondary_fields": {"dob": "match", "country": "match"}},
        {"name": "DOE Janet", "score": 0.8, "list": "eu_consolidated", "type": "name_fuzzy",
         "name_score": 0.9,
         "secondary_fields": {"dob": "contradict", "country": "unknown"}},
    ]
    # The heavy fields stay dropped in this mode too.
    assert "rationale" not in repr(result) and "source_id" not in repr(result)


async def test_screen_with_country_or_nationality_alone_carries_outcomes(monkeypatch) -> None:
    for kwargs in ({"country": "US"}, {"nationality": "US"}, {"dob": "1980"}):
        fake = FakeClient(post_payload=_SCREEN_RAW_SECONDARY)
        monkeypatch.setattr(entities, "get_client", lambda fake=fake: fake)

        result = await entities.sugra_entity_screen(name="Jane Doe", **kwargs)

        # The field reaches the request body and turns the outcome on: both
        # come from the one place the fields are named.
        assert fake.calls[0][3] == {"name": "Jane Doe", **kwargs}, kwargs
        assert [m["secondary_fields"] for m in result["matches"]] == [
            {"dob": "match", "country": "match"},
            {"dob": "contradict", "country": "unknown"},
        ], kwargs


async def test_screen_sends_all_three_secondary_fields_together(monkeypatch) -> None:
    fake = FakeClient(post_payload=_SCREEN_RAW_SECONDARY)
    monkeypatch.setattr(entities, "get_client", lambda: fake)

    result = await entities.sugra_entity_screen(
        name="Jane Doe", country="US", dob="1980-01-01", nationality="GB")

    assert fake.calls[0][3] == {"name": "Jane Doe", "country": "US",
                                "dob": "1980-01-01", "nationality": "GB"}
    assert all("secondary_fields" in m for m in result["matches"])


async def test_screen_with_dob_leaves_out_a_null_secondary_fields(monkeypatch) -> None:
    # One rule for both added keys: a key the API sent as null, or did not
    # send, is left out, never passed through as null.
    raw = {
        "data": {"screening": {"status": "review", "matches": [
            {"list": "ofac_sdn", "matched_name": "DOE, Jane", "score": 0.9,
             "match_type": "name_fuzzy", "name_score": 0.9,
             "secondary_fields": None},
            {"list": "ofac_sdn", "matched_name": "DOE, J", "score": 0.8,
             "match_type": "name_fuzzy", "name_score": None,
             "secondary_fields": None},
            {"list": "ofac_sdn", "matched_name": "DOE", "score": 0.75,
             "match_type": "name_fuzzy"},
        ]}},
        "meta": {},
    }
    fake = FakeClient(post_payload=raw)
    monkeypatch.setattr(entities, "get_client", lambda: fake)

    result = await entities.sugra_entity_screen(name="Jane Doe", dob="1980-01-01")

    assert result["matches"] == [
        {"name": "DOE, Jane", "score": 0.9, "list": "ofac_sdn", "type": "name_fuzzy",
         "name_score": 0.9},
        {"name": "DOE, J", "score": 0.8, "list": "ofac_sdn", "type": "name_fuzzy"},
        {"name": "DOE", "score": 0.75, "list": "ofac_sdn", "type": "name_fuzzy"},
    ]
    assert None not in [v for m in result["matches"] for v in m.values()]


async def test_screen_without_secondary_fields_keeps_the_four_key_match(monkeypatch) -> None:
    # The API always returns name_score; with no dob, country or nationality
    # sent the compact match stays exactly {name, score, list, type}.
    fake = FakeClient(post_payload=_SCREEN_RAW_NAME_ONLY)
    monkeypatch.setattr(entities, "get_client", lambda: fake)

    result = await entities.sugra_entity_screen(name="Jane Doe")

    assert result["matches"] == [
        {"name": "DOE, Jane", "score": 0.95, "list": "ofac_sdn", "type": "name_fuzzy"},
    ]


async def test_screen_with_dob_leaves_out_a_name_score_the_api_did_not_send(monkeypatch) -> None:
    raw = {
        "data": {"screening": {"status": "review", "matches": [
            {"list": "ofac_sdn", "matched_name": "DOE, Jane", "score": 0.9,
             "match_type": "name_fuzzy", "secondary_fields": {"dob": "unknown"}},
            {"list": "ofac_sdn", "matched_name": "DOE, J", "score": 0.8,
             "match_type": "name_fuzzy", "name_score": None,
             "secondary_fields": {"dob": "unrecognized"}},
        ]}},
        "meta": {},
    }
    fake = FakeClient(post_payload=raw)
    monkeypatch.setattr(entities, "get_client", lambda: fake)

    result = await entities.sugra_entity_screen(name="Jane Doe", dob="1980-01-01")

    assert result["matches"] == [
        {"name": "DOE, Jane", "score": 0.9, "list": "ofac_sdn", "type": "name_fuzzy",
         "secondary_fields": {"dob": "unknown"}},
        {"name": "DOE, J", "score": 0.8, "list": "ofac_sdn", "type": "name_fuzzy",
         "secondary_fields": {"dob": "unrecognized"}},
    ]


def _screen_tool_schema() -> dict[str, Any]:
    import asyncio

    from sugra_api_mcp import tools  # noqa: F401  (import registers all tools)
    from sugra_api_mcp.server import mcp

    tool = next(t for t in asyncio.run(mcp.list_tools()) if t.name == "sugra_entity_screen")
    return {"description": tool.description, "properties": tool.inputSchema["properties"]}


def _flat(text: str) -> str:
    return " ".join(text.split())


def test_screen_texts_wording_is_pinned(monkeypatch) -> None:
    # Only the wording is pinned here: the tool forwards dob, country and
    # nationality and projects the answer, and the scoring behind it is the
    # API's to test, not this repository's.
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    schema = _screen_tool_schema()
    props = schema["properties"]
    description = _flat(schema["description"])

    forwarded = "Forwarded to the API, which re-scores and re-orders the name candidates."
    assert props["country"]["description"] == "Optional country code. " + forwarded
    assert props["dob"]["description"] == (
        "Optional date of birth for a person, a date string. " + forwarded)
    assert props["nationality"]["description"] == "Optional nationality. " + forwarded
    assert ("Optional `dob`, `country` and `nationality` are forwarded to the "
            "Sugra API, which re-scores and re-orders the candidates the name "
            "screen found; it never adds or removes one.") in description
    # The shape is stated once, with the two conditional keys inline.
    assert ("`{status, matches:[{name, score, list, type, name_score?, "
            "secondary_fields?}], disclaimer}`") in description
    assert "{name, score, list, type}]" not in description
    assert ("A match carries `name_score` and `secondary_fields` only when the "
            "call sends `dob`, `country` or `nationality` and the API returns "
            "them.") in description

    # Claims about the API's scoring this repository cannot show stay out.
    texts = [description, *(p["description"] for p in props.values())]
    for text in texts:
        for absent in ("two or more years", "within one year", "alpha-3",
                       "English name", "count once", "weighs less",
                       "never demoted", "narrow", "turn `clear`", "adjusted score",
                       "`near`", "`contradict`", "`unrecognized`"):
            assert absent.lower() not in text.lower(), (absent, text)
        assert chr(0x2014) not in text  # no em dash in public copy


async def test_screen_propagates_api_error_as_clean_dict(monkeypatch) -> None:
    fake = FakeClient(post_payload={"error": "auth_failed", "status_code": 401,
                                    "url": "https://sugra.ai/api/v1/entity/screen"})
    monkeypatch.setattr(entities, "get_client", lambda: fake)

    result = await entities.sugra_entity_screen(name="x")

    assert result["error"] == "auth_failed"
    assert "detail" in result


# ---------------------------------------------------------------------------
# sugra_entity_lookup
# ---------------------------------------------------------------------------


async def test_lookup_gets_composed_path(monkeypatch) -> None:
    fake = FakeClient(get_payload=_LOOKUP_RAW)
    monkeypatch.setattr(entities, "get_client", lambda: fake)

    await entities.sugra_entity_lookup(anchor="lei", value="5493001KJTIIGC8Y1R12")

    assert len(fake.calls) == 1
    method, path, _params, _body = fake.calls[0]
    assert method == "GET"
    assert path == "/api/v1/entity/lei/5493001KJTIIGC8Y1R12"


async def test_lookup_compact_default_shape(monkeypatch) -> None:
    fake = FakeClient(get_payload=_LOOKUP_RAW)
    monkeypatch.setattr(entities, "get_client", lambda: fake)

    result = await entities.sugra_entity_lookup(anchor="lei", value="5493001KJTIIGC8Y1R12")

    # entity: name, anchor, value, status, country
    assert result["entity"] == {
        "name": "Acme PLC",
        "anchor": "lei",
        "value": "5493001KJTIIGC8Y1R12",
        "status": "ACTIVE",
        "country": "GB",
    }
    # screening: status, top 3 matches (name+score+list), hit_count
    assert result["screening"]["status"] == "review"
    assert result["screening"]["hit_count"] == 4
    assert len(result["screening"]["top_matches"]) == 3
    assert result["screening"]["top_matches"][0] == {
        "name": "ACME PLC", "score": 0.88, "list": "ofac_sdn"
    }
    # ids block present
    assert result["ids"]["id"] == "sugra-ent:lei:5493001KJTIIGC8Y1R12"
    assert result["ids"]["lei"] == "5493001KJTIIGC8Y1R12"
    # disclaimer always present
    assert result["disclaimer"]
    # Default mode does NOT carry full ownership / adverse slices.
    assert "ownership" not in result
    assert "adverse_media" not in result


async def test_lookup_include_opts_into_fuller_slices(monkeypatch) -> None:
    fake = FakeClient(get_payload=_LOOKUP_RAW)
    monkeypatch.setattr(entities, "get_client", lambda: fake)

    result = await entities.sugra_entity_lookup(
        anchor="lei", value="5493001KJTIIGC8Y1R12",
        include=["ownership", "adverse_media"],
    )

    # Compact core is still there.
    assert result["entity"]["name"] == "Acme PLC"
    # Opted-in slices appear in fuller form.
    assert result["ownership"]["direct_parent"]["name"] == "Acme Holdings"
    assert result["ownership"]["ultimate_parent"]["lei"] == "ULT999"
    assert result["adverse_media"]["precision"] == "low"
    # Adverse content carries the non-comprehensive coverage note.
    assert result["adverse_media"]["coverage_note"]


async def test_lookup_include_passes_through_to_api_query(monkeypatch) -> None:
    fake = FakeClient(get_payload=_LOOKUP_RAW)
    monkeypatch.setattr(entities, "get_client", lambda: fake)

    await entities.sugra_entity_lookup(
        anchor="lei", value="L1", include=["ownership", "adverse_media"]
    )

    _method, _path, params, _body = fake.calls[0]
    # The API uses `adverse` (not `adverse_media`) as the include token.
    assert params is not None
    assert "include" in params
    assert "ownership" in params["include"]
    assert "adverse" in params["include"]


async def test_lookup_rejects_bad_anchor_without_calling_api(monkeypatch) -> None:
    fake = FakeClient(get_payload=_LOOKUP_RAW)
    monkeypatch.setattr(entities, "get_client", lambda: fake)

    result = await entities.sugra_entity_lookup(anchor="ssn", value="123")

    assert result["error"] == "invalid_anchor"
    assert "lei" in result["detail"] and "vat" in result["detail"]
    assert fake.calls == []


async def test_lookup_propagates_api_error_as_clean_dict(monkeypatch) -> None:
    fake = FakeClient(get_payload={"error": "entity not found for lei:L1",
                                   "status_code": 404,
                                   "url": "https://sugra.ai/api/v1/entity/lei/L1"})
    monkeypatch.setattr(entities, "get_client", lambda: fake)

    result = await entities.sugra_entity_lookup(anchor="lei", value="L1")

    assert result["error"]
    assert "detail" in result
    # Must NOT raise and must NOT pretend the entity exists.
    assert "entity" not in result


async def test_lookup_vat_anchor_uses_vat_path(monkeypatch) -> None:
    vat_raw = {
        "data": {
            "entity": {
                "id": "sugra-ent:vat:DE123456789",
                "anchor": {"vat": "DE123456789"},
                "name": "Beispiel GmbH",
                "country": "DE",
                "type": "company",
                "status": "valid",
            },
            "screening": {"status": "clear", "matches": [], "fifty_percent_rule": {}},
        },
        "meta": {"disclaimer": "Screening signal, not a compliance determination."},
    }
    fake = FakeClient(get_payload=vat_raw)
    monkeypatch.setattr(entities, "get_client", lambda: fake)

    result = await entities.sugra_entity_lookup(anchor="vat", value="DE123456789")

    _method, path, _params, _body = fake.calls[0]
    assert path == "/api/v1/entity/vat/DE123456789"
    assert result["entity"]["anchor"] == "vat"
    assert result["screening"]["hit_count"] == 0


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_entity_tools_are_registered(monkeypatch) -> None:
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    import asyncio

    from sugra_api_mcp import tools  # noqa: F401  (import registers all tools)
    from sugra_api_mcp.server import mcp

    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert "sugra_entity_screen" in names
    assert "sugra_entity_lookup" in names


# ---------------------------------------------------------------------------
# Error contract parity with call_endpoint (MCP-Imp-1)
# ---------------------------------------------------------------------------


def test_clean_error_passes_through_transport_fields() -> None:
    """The client's structured transport errors carry retry guidance and
    timing; _clean_error must forward them, not strip them, so the entity
    tools keep contract parity with call_endpoint."""
    cleaned = entities._clean_error(
        {
            "error": "upstream_timeout",
            "reason": "ReadTimeout",
            "status_code": None,
            "elapsed_ms": 30012,
            "url": "https://sugra.ai/api/v1/entity/lei/X/screen",
            "retry_hint": "Retry once.",
            "timeout_s": 30.0,
        }
    )

    assert cleaned["error"] == "upstream_timeout"
    assert cleaned["reason"] == "ReadTimeout"
    assert cleaned["retry_hint"] == "Retry once."
    assert cleaned["elapsed_ms"] == 30012
    assert cleaned["timeout_s"] == 30.0


def test_clean_error_stays_compact_for_plain_http_errors() -> None:
    """Plain HTTP errors (no transport fields) keep the original compact
    {error, detail} shape plus retry_after when the API sent one."""
    cleaned = entities._clean_error(
        {
            "error": "Rate limit exceeded",
            "status_code": 429,
            "url": "https://sugra.ai/api/v1/entity/lei/X/screen",
            "elapsed_ms": 120,
            "retry_after": 12,
        }
    )

    assert cleaned["error"] == "Rate limit exceeded"
    assert "HTTP 429" in cleaned["detail"]
    assert cleaned["retry_after"] == 12
    assert "reason" not in cleaned
