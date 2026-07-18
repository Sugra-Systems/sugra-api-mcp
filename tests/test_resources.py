"""Tests for the read-only catalog resources (sugra:// URIs)."""

from __future__ import annotations

import json

import pytest

DOMAINS_URI = "sugra://catalog/domains"
SOURCES_URI = "sugra://catalog/sources"
ATTRIBUTION_URI = "sugra://attribution"

EXPECTED_URIS = {DOMAINS_URI, SOURCES_URI, ATTRIBUTION_URI}

EXPECTED_MIME_TYPES = {
    DOMAINS_URI: "application/json",
    SOURCES_URI: "application/json",
    ATTRIBUTION_URI: "text/markdown",
}

# Commercial upstream names that must never appear in public copy. Lowercase
# substrings checked against the lowercased attribution text.
TIER_C_NAME_FRAGMENTS = [
    "yahoo",
    "finnhub",
    "coingecko",
    "tomorrow.io",
    "alpha vantage",
    "polygon",
    "tiingo",
    "cboe",
]


@pytest.fixture()
def registered_mcp(monkeypatch):
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    from sugra_api_mcp import tools  # noqa: F401  (import registers resources)
    from sugra_api_mcp.server import mcp

    return mcp


async def _read(mcp, uri: str):
    contents = list(await mcp.read_resource(uri))
    assert len(contents) == 1
    return contents[0]


async def test_exactly_three_resources_registered(registered_mcp) -> None:
    resource_list = await registered_mcp.list_resources()
    assert len(resource_list) == 3
    assert {str(resource.uri) for resource in resource_list} == EXPECTED_URIS


async def test_listed_resources_carry_expected_mime_types(registered_mcp) -> None:
    resource_list = await registered_mcp.list_resources()
    listed = {str(resource.uri): resource.mimeType for resource in resource_list}
    assert listed == EXPECTED_MIME_TYPES


async def test_domains_resource_agrees_with_catalog_loader(registered_mcp) -> None:
    from sugra_api_mcp.catalog.loader import load_catalog

    content = await _read(registered_mcp, DOMAINS_URI)
    assert content.mime_type == "application/json"
    payload = json.loads(content.content)

    catalog = load_catalog()
    distinct_toolsets = {endpoint.toolset for endpoint in catalog.endpoints}
    assert len(payload["toolsets"]) == len(distinct_toolsets)
    assert payload["total_endpoints"] == catalog.endpoint_count

    counts: dict[str, int] = {}
    for endpoint in catalog.endpoints:
        counts[endpoint.toolset] = counts.get(endpoint.toolset, 0) + 1
    assert {entry["name"]: entry["endpoint_count"] for entry in payload["toolsets"]} == counts


async def test_domains_resource_matches_list_toolsets_tool(registered_mcp) -> None:
    from sugra_api_mcp.tools.gateway import list_toolsets

    content = await _read(registered_mcp, DOMAINS_URI)
    assert json.loads(content.content) == await list_toolsets()


async def test_sources_resource_agrees_with_catalog_loader(registered_mcp) -> None:
    from sugra_api_mcp.catalog.loader import load_catalog

    content = await _read(registered_mcp, SOURCES_URI)
    assert content.mime_type == "application/json"
    payload = json.loads(content.content)

    catalog = load_catalog()
    distinct_families = {endpoint.source_family for endpoint in catalog.endpoints}
    assert len(payload["source_families"]) == len(distinct_families)
    assert payload["endpoint_count"] == catalog.endpoint_count
    assert payload["catalog_source"] == catalog.source


async def test_sources_resource_matches_list_sources_tool(registered_mcp) -> None:
    from sugra_api_mcp.tools.gateway import list_sources

    content = await _read(registered_mcp, SOURCES_URI)
    assert json.loads(content.content) == await list_sources()


async def test_attribution_resource_reads_as_markdown(registered_mcp) -> None:
    content = await _read(registered_mcp, ATTRIBUTION_URI)
    assert content.mime_type == "text/markdown"
    text = content.content
    assert isinstance(text, str)
    assert text.strip()
    assert text.lstrip().startswith("#")
    assert "https://sugra.ai/sources" in text


def test_resources_list_is_public_but_read_is_authed() -> None:
    """Boundary pin: discovery is public, reading requires auth (by design)."""
    from sugra_api_mcp.auth import PUBLIC_MCP_METHODS

    assert "resources/list" in PUBLIC_MCP_METHODS
    assert "resources/read" not in PUBLIC_MCP_METHODS


def test_attribution_copy_lint() -> None:
    """The attribution text is public copy: ASCII only (no emoji, no em dash),
    no "real-time" phrasing, and no commercial upstream names."""
    from sugra_api_mcp.tools.resources import ATTRIBUTION_MARKDOWN

    assert ATTRIBUTION_MARKDOWN.isascii(), "attribution copy must be plain ASCII"
    lowered = ATTRIBUTION_MARKDOWN.lower()
    assert "real-time" not in lowered
    assert "realtime" not in lowered
    for fragment in TIER_C_NAME_FRAGMENTS:
        assert fragment not in lowered, f"commercial upstream name in public copy: {fragment}"
