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
