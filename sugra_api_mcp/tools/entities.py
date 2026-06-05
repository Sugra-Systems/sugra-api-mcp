"""Sugra Entity MCP tools: name screening and composed KYB lookup.

Two read-only tools wrap the Sugra Entity API surface for agents:

- ``sugra_entity_screen`` - screen a name against the sanctions corpus.
- ``sugra_entity_lookup`` - resolve an entity by ``(anchor, value)`` and return
  the composed KYB envelope.

Both shape the heavy raw API envelope down to a COMPACT projection by default so
the agent's context budget is not flooded (Sugra Entity Spec 9.8 MCP-OUT). The
lookup tool opts INTO fuller per-slice detail via ``include``. Every result
carries the Sugra Entity disclaimer: a screening result is a SCREENING SIGNAL,
not a compliance determination, and PEP / adverse-media coverage is
supplementary and non-comprehensive.

These tools call the Sugra API BY PATH (not by catalog operation_id): the
bundled endpoint catalog predates the Sugra Entity endpoints, so they go through
``get_client()`` directly against the stable ``/api/v1/entity/*`` paths. When a
future catalog rebuild adds the entity operation_ids, these tools still work
unchanged - the path call is the contract.
"""

from __future__ import annotations

from typing import Any

from ..observability import trace_mcp_tool
from ..server import get_client, mcp, read_only

_ENTITY_BASE = "/api/v1/entity"

# Anchors this surface can resolve. lei -> GLEIF identity + ownership; vat -> EU
# VIES validation. Everything else fails closed before any API round-trip.
_SUPPORTED_ANCHORS = ("lei", "vat")

# The composed API exposes the adverse-media slice under the include token
# `adverse`, while the MCP tool (and its compact output key) use the clearer
# `adverse_media`. Map the friendlier MCP token to the API token on the way out.
_INCLUDE_TOKEN_ALIASES = {"adverse_media": "adverse"}

# Slices the `include` opt-in can request in fuller form. Maps the MCP-facing
# token to the key the API places in `data`.
_FULLER_SLICE_KEYS = {
    "ownership": "ownership",
    "adverse_media": "adverse_media",
    "profile": "entity",
    "screening": "screening",
}

# How many screening matches the compact lookup surfaces (scores + top hits).
_TOP_MATCHES = 3

# Fallback disclaimer used only if the API envelope omits meta.disclaimer. The
# agent must never treat a screening result as a determination, so a disclaimer
# is ALWAYS present in the tool output even when the upstream meta is sparse.
_FALLBACK_DISCLAIMER = (
    "Screening signal, not a compliance determination. PEP and adverse-media "
    "coverage is supplementary and non-comprehensive."
)


def _is_error(payload: Any) -> bool:
    """The client returns a flat {error, status_code, url} dict on >=400."""
    return isinstance(payload, dict) and "error" in payload and "data" not in payload


def _clean_error(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize a client error payload into a clean {error, detail} dict.

    Never raises and never leaks the entity envelope shape so an agent can branch
    cleanly on `result.get("error")`.
    """
    error = payload.get("error") or "request_failed"
    detail_bits: list[str] = []
    status = payload.get("status_code")
    if status is not None:
        detail_bits.append(f"HTTP {status}")
    url = payload.get("url")
    if url:
        detail_bits.append(str(url))
    detail = " ".join(detail_bits) if detail_bits else str(error)
    return {"error": str(error), "detail": detail}


def _disclaimer_of(meta: dict[str, Any]) -> str:
    value = meta.get("disclaimer")
    return value if isinstance(value, str) and value else _FALLBACK_DISCLAIMER


def _compact_match(match: dict[str, Any]) -> dict[str, Any]:
    """Project one screening match to {name, score, list, type}.

    Drops the heavy / rare fields (rationale arrays, source_id, list_published,
    restriction_type, program) that bloat the agent context (Spec 9.8 MCP-OUT).
    """
    return {
        "name": match.get("matched_name"),
        "score": match.get("score"),
        "list": match.get("list"),
        "type": match.get("match_type"),
    }


def _project_screen(payload: dict[str, Any]) -> dict[str, Any]:
    """Compact projection for POST /entity/screen.

    {status, matches:[{name, score, list, type}], disclaimer}
    """
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    screening = data.get("screening") or {}
    matches = screening.get("matches") or []
    return {
        "status": screening.get("status"),
        "matches": [_compact_match(m) for m in matches if isinstance(m, dict)],
        "disclaimer": _disclaimer_of(meta),
    }


def _project_lookup(
    payload: dict[str, Any], anchor: str, value: str, include: set[str]
) -> dict[str, Any]:
    """Compact-by-default projection for GET /entity/{anchor}/{value}.

    Default core: {entity, screening (status + top matches + hit_count), ids,
    disclaimer}. `include` opts into fuller per-slice detail (ownership,
    adverse_media) appended in full form. Always carries the disclaimer; any
    pep / adverse content keeps its non-comprehensive coverage note.
    """
    data = payload.get("data") or {}
    meta = payload.get("meta") or {}
    entity = data.get("entity") or {}
    screening = data.get("screening") or {}
    matches = [m for m in (screening.get("matches") or []) if isinstance(m, dict)]

    anchor_block = entity.get("anchor") or {}
    out: dict[str, Any] = {
        "entity": {
            "name": entity.get("name"),
            "anchor": anchor,
            "value": anchor_block.get(anchor, value),
            "status": entity.get("status"),
            "country": entity.get("country"),
        },
        "screening": {
            "status": screening.get("status"),
            "top_matches": [
                {"name": m.get("matched_name"), "score": m.get("score"), "list": m.get("list")}
                for m in matches[:_TOP_MATCHES]
            ],
            "hit_count": len(matches),
        },
        "ids": _ids_block(entity),
        "disclaimer": _disclaimer_of(meta),
    }

    # `include` opts INTO fuller slices. profile/screening are already in the
    # compact core, so only ownership + adverse_media are appended in full form.
    if "ownership" in include and "ownership" in data:
        out["ownership"] = data.get("ownership")
    if "adverse_media" in include and "adverse_media" in data:
        out["adverse_media"] = data.get("adverse_media")

    # Surface envelope-level degradation honestly so a partial compose is not
    # read as a clean result.
    if meta.get("partial"):
        out["partial"] = True
    return out


def _ids_block(entity: dict[str, Any]) -> dict[str, Any]:
    """Collect the entity identifiers into a small ids block.

    Carries the Sugra entity id, the anchor identifiers (lei / vat), and any
    linked identifiers the resolver attached. Absent keys are skipped.
    """
    ids: dict[str, Any] = {}
    if entity.get("id"):
        ids["id"] = entity["id"]
    anchor_block = entity.get("anchor")
    if isinstance(anchor_block, dict):
        ids.update({k: v for k, v in anchor_block.items() if v is not None})
    # Pass through any linked identifiers if present (lei records may carry isin,
    # bic, etc. under `identifiers`); keep it small and skip empties.
    linked = entity.get("identifiers")
    if isinstance(linked, dict):
        ids.update({k: v for k, v in linked.items() if v is not None})
    return ids


def _resolve_include(include: list[str] | None) -> tuple[set[str], list[str]]:
    """Split the requested include tokens into (mcp_tokens, api_tokens).

    `mcp_tokens` drive which fuller slices the projection appends;
    `api_tokens` are the comma-list the API understands (adverse_media ->
    adverse). Unknown tokens are dropped silently rather than erroring so a
    typo never blocks the call.
    """
    if not include:
        return set(), []
    mcp_tokens = {tok.strip() for tok in include if isinstance(tok, str) and tok.strip()}
    api_tokens: list[str] = []
    # Always include the default core slices the API needs to compose the
    # compact view (profile + screening), plus any opted-in fuller slices.
    for tok in ("profile", "screening", *sorted(mcp_tokens)):
        api_tok = _INCLUDE_TOKEN_ALIASES.get(tok, tok)
        if api_tok not in api_tokens:
            api_tokens.append(api_tok)
    return mcp_tokens, api_tokens


@mcp.tool(annotations=read_only("Sugra Entity screen"))
@trace_mcp_tool("sugra_entity_screen")
async def sugra_entity_screen(
    name: str,
    country: str | None = None,
    dob: str | None = None,
    nationality: str | None = None,
) -> dict[str, Any]:
    """Screen a person or organization name against the Sugra sanctions corpus.

    Returns a SCREENING SIGNAL, not a compliance determination. Sugra is a
    technology provider, not a sanctions authority or consumer reporting agency.
    PEP and adverse-media coverage is supplementary and non-comprehensive - a
    `clear` result is not proof of absence, and a `hit` is a candidate match to
    review, not a finding.

    Output is COMPACT to protect the agent context budget:
    `{status, matches:[{name, score, list, type}], disclaimer}`. The verdict
    `status` is one of `clear`, `review`, or `hit`. The heavy raw fields
    (match rationale, source ids, publish dates) are dropped; use the Sugra API
    directly when the full screening envelope is needed.

    Args:
        name: The person or organization name to screen (required).
        country: Optional ISO 3166-1 alpha-2 country to narrow the match.
        dob: Optional date of birth (YYYY-MM-DD) for a person.
        nationality: Optional nationality to narrow the match.
    """
    body: dict[str, Any] = {"name": name}
    for key, val in (("country", country), ("dob", dob), ("nationality", nationality)):
        if val is not None:
            body[key] = val

    client = get_client()
    payload = await client.post(f"{_ENTITY_BASE}/screen", json=body)
    if _is_error(payload):
        return _clean_error(payload)
    return _project_screen(payload)


@mcp.tool(annotations=read_only("Sugra Entity lookup"))
@trace_mcp_tool("sugra_entity_lookup")
async def sugra_entity_lookup(
    anchor: str,
    value: str,
    include: list[str] | None = None,
) -> dict[str, Any]:
    """Resolve an entity by identifier and return its composed KYB envelope.

    `anchor` is `lei` (Legal Entity Identifier, resolved via the GLEIF registry)
    or `vat` (EU VAT number, validated via the EU VIES service). The result
    weaves identity, a sanctions screening signal, and - on request - ownership
    and adverse-media slices.

    The screening verdict is a SCREENING SIGNAL, not a compliance determination,
    and any PEP / adverse-media content is supplementary and non-comprehensive.
    The `disclaimer` field carries this and is always present.

    Output is COMPACT by default to protect the agent context budget:
    `{entity:{name, anchor, value, status, country}, screening:{status,
    top_matches:[...3], hit_count}, ids:{...}, disclaimer}`. Pass `include` to
    opt INTO fuller per-slice detail, e.g.
    `include=["ownership","adverse_media"]` adds those slices in full form.

    On a bad anchor or an API error this returns a clean `{error, detail}` dict
    rather than raising, so the agent can branch on `result.get("error")`.

    Args:
        anchor: Identifier type, one of `lei` or `vat`.
        value: The identifier value (the 20-char LEI code or the VAT number).
        include: Optional list of fuller slices to add, e.g.
            `["ownership", "adverse_media"]`. Omit for the compact default.
    """
    anchor_norm = (anchor or "").strip().lower()
    if anchor_norm not in _SUPPORTED_ANCHORS:
        return {
            "error": "invalid_anchor",
            "detail": (
                f"anchor must be one of {list(_SUPPORTED_ANCHORS)} "
                f"(lei via GLEIF, vat via EU VIES); got {anchor!r}"
            ),
        }

    mcp_tokens, api_tokens = _resolve_include(include)
    params: dict[str, Any] = {}
    if api_tokens:
        params["include"] = ",".join(api_tokens)

    client = get_client()
    payload = await client.get(f"{_ENTITY_BASE}/{anchor_norm}/{value}", params=params or None)
    if _is_error(payload):
        return _clean_error(payload)
    return _project_lookup(payload, anchor_norm, value, mcp_tokens)
