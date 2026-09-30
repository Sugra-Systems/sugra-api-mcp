"""The sugra-api-skills skills as MCP resources, read from the pinned index."""

from __future__ import annotations

import re

import pytest

from sugra_api_mcp import skills_index
from sugra_api_mcp.tools.skills import SKILL_SPECS, SKILL_URIS, read_skill

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

FRONTMATTER_RE = re.compile(
    r"^---\nname: (?P<name>[^\n]+)\ndescription: (?P<description>[^\n]+)\n",
)

GATEWAY_LOOP_TOOLS = (
    "search_endpoints",
    "describe_endpoint",
    "call_endpoint",
)
HOSTED_ONLY_TOOLS = ("resolve_entity", "get_snapshot", "get_timeseries")

# The five URIs predate sugra-api-skills; clients may have them saved.
LEGACY_URIS = {
    "sugra://skills/explore-catalog",
    "sugra://skills/envelope-attribution",
    "sugra://skills/auth-limits",
    "sugra://skills/hosted-vs-gateway",
    "sugra://skills/cross-domain-briefing",
}


@pytest.fixture()
def registered_mcp(monkeypatch):
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    from sugra_api_mcp import tools  # noqa: F401
    from sugra_api_mcp.server import mcp

    return mcp


async def _read(mcp, uri: str):
    contents = list(await mcp.read_resource(uri))
    assert len(contents) == 1
    return contents[0]


def test_the_old_uris_keep_working() -> None:
    assert set(SKILL_URIS) == LEGACY_URIS


async def test_all_skill_uris_are_registered(registered_mcp) -> None:
    listed = {str(r.uri): r for r in await registered_mcp.list_resources()}
    descriptions = {s["name"]: s["description"] for s in skills_index.INDEX["skills"]}
    for (slug, name, title, skill), uri in zip(SKILL_SPECS, SKILL_URIS, strict=True):
        resource = listed[uri]
        assert resource.mimeType == "text/markdown", slug
        assert (resource.name, resource.title) == (name, title)
        assert resource.description == descriptions[skill]


async def test_each_uri_serves_its_skill_from_the_pinned_index(registered_mcp) -> None:
    for (_slug, _name, _title, skill), uri in zip(SKILL_SPECS, SKILL_URIS, strict=True):
        content = await _read(registered_mcp, uri)
        assert content.mime_type == "text/markdown"
        assert content.content == skills_index.SKILL_MD[skill]
        match = FRONTMATTER_RE.match(content.content)
        assert match, f"{skill}: missing YAML frontmatter name/description"
        assert match.group("name") == skill


def test_the_index_keeps_the_text_of_every_listed_skill() -> None:
    assert set(skills_index.SKILL_MD) == {s["name"] for s in skills_index.INDEX["skills"]}


def test_skill_copy_lint() -> None:
    for skill, text in skills_index.SKILL_MD.items():
        assert text.isascii(), f"{skill}: skill copy must be plain ASCII"
        lowered = text.lower()
        assert "real-time" not in lowered
        assert "realtime" not in lowered
        assert "financial intelligence" not in lowered
        assert "blackbox" not in lowered
        for fragment in TIER_C_NAME_FRAGMENTS:
            assert fragment not in lowered, f"{skill}: commercial name {fragment}"


def test_discover_and_call_teaches_the_search_describe_call_loop() -> None:
    text = read_skill("discover-and-call")
    for tool in GATEWAY_LOOP_TOOLS:
        assert tool in text
    assert "fetch_data" in text
    lowered = text.lower()
    assert "prompt" in lowered
    assert "catalog" in lowered


def test_connect_names_the_hosted_only_tools_and_the_bearer_header() -> None:
    text = read_skill("connect")
    for tool in HOSTED_ONLY_TOOLS:
        assert tool in text
    assert "Bearer" in text


def test_auth_skill_distinguishes_stdio_env_from_http_bearer() -> None:
    text = read_skill("auth-and-quota")
    assert "missing_bearer_token" in text
    assert "retry_after" in text
    assert "SUGRA_API_KEY" in text
