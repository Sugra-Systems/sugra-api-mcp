"""Official SKILL.md pack as MCP resources (MCP-15.4)."""

from __future__ import annotations

import re

import pytest

from sugra_api_mcp.tools.skills import (
    HOSTED_ONLY_TOOLS,
    SKILL_SLUGS,
    SKILL_URIS,
    STDIO_ONLY_SKILL_SLUGS,
    read_skill,
)

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
    r"^---\nname: (?P<name>[^\n]+)\ndescription: (?P<description>.+)\n---\n",
    re.DOTALL,
)

GATEWAY_LOOP_TOOLS = (
    "search_endpoints",
    "describe_endpoint",
    "call_endpoint",
)


@pytest.fixture()
def registered_mcp(monkeypatch):
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    from sugra_api_mcp import tools  # noqa: F401
    from sugra_api_mcp.server import mcp

    return mcp


async def _read(mcp, uri: str) -> str:
    contents = list(await mcp.read_resource(uri))
    assert len(contents) == 1
    return contents[0]


async def test_all_skill_uris_are_registered(registered_mcp) -> None:
    listed = {str(resource.uri) for resource in await registered_mcp.list_resources()}
    assert set(SKILL_URIS) <= listed
    for uri in SKILL_URIS:
        listed_mime = next(
            resource.mimeType
            for resource in await registered_mcp.list_resources()
            if str(resource.uri) == uri
        )
        assert listed_mime == "text/markdown"


async def test_skill_resource_body_matches_packaged_file(registered_mcp) -> None:
    for slug, uri in zip(SKILL_SLUGS, SKILL_URIS, strict=True):
        content = await _read(registered_mcp, uri)
        assert content.mime_type == "text/markdown"
        assert content.content == read_skill(slug)


def test_each_skill_is_a_claude_codex_drop_in() -> None:
    for slug in SKILL_SLUGS:
        text = read_skill(slug)
        match = FRONTMATTER_RE.match(text)
        assert match, f"{slug}: missing YAML frontmatter name/description"
        assert match.group("name") == slug
        assert len(match.group("description").strip()) > 20


def test_skill_copy_lint() -> None:
    for slug in SKILL_SLUGS:
        text = read_skill(slug)
        assert text.isascii(), f"{slug}: skill copy must be plain ASCII"
        lowered = text.lower()
        assert "real-time" not in lowered
        assert "realtime" not in lowered
        assert "financial intelligence" not in lowered
        assert "blackbox" not in lowered
        for fragment in TIER_C_NAME_FRAGMENTS:
            assert fragment not in lowered, f"{slug}: commercial name {fragment}"


def test_stdio_skills_do_not_name_hosted_only_tools() -> None:
    for slug in STDIO_ONLY_SKILL_SLUGS:
        text = read_skill(slug)
        for tool in HOSTED_ONLY_TOOLS:
            assert tool not in text, f"{slug} names hosted-only tool {tool}"


def test_hosted_skill_names_hosted_only_and_the_eight() -> None:
    text = read_skill("hosted-vs-gateway")
    for tool in HOSTED_ONLY_TOOLS:
        assert tool in text
    for tool in GATEWAY_LOOP_TOOLS:
        assert tool in text


def test_auth_skill_distinguishes_stdio_env_from_http_bearer() -> None:
    text = read_skill("auth-limits")
    assert "missing_bearer_token" in text
    assert "retry_after" in text
    assert "SUGRA_API_KEY" in text
    hosted = read_skill("hosted-vs-gateway")
    assert "Bearer" in hosted
    assert "fallback" in hosted.lower()


def test_explore_catalog_teaches_the_search_describe_call_loop() -> None:
    text = read_skill("explore-catalog")
    for tool in GATEWAY_LOOP_TOOLS:
        assert tool in text
    assert "fetch_data" in text
    lowered = text.lower()
    assert "prompt" in lowered
    assert "catalog" in lowered
