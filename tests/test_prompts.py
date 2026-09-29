"""Workflow prompt tests: registration, rendering, copy rules, auth boundary.

The six prompts are public copy shipped in the package, so beyond behavior we
lint the module source for the copy rules (no em dash, no emoji, no
"real-time", no commercial source names) and pin the auth asymmetry:
prompts/list is public discovery while prompts/get stays behind auth.
"""

from __future__ import annotations

import asyncio
import inspect
import re

import pytest

from tests.test_metadata_sync import EXPECTED_TOOL_COUNT

EXPECTED_PROMPT_COUNT = 6

EXPECTED_PROMPT_NAMES = {
    "market_snapshot",
    "macro_briefing",
    "sanctions_screening",
    "sector_compare",
    "earth_conditions",
    "source_overview",
}

# Tools that exist on every transport (the package surface). Each prompt must
# name at least one of these so the agent knows what to call.
PACKAGE_TOOL_NAMES = (
    "fetch_data",
    "search_endpoints",
    "describe_endpoint",
    "call_endpoint",
    "list_toolsets",
    "list_sources",
    "sugra_entity_screen",
    "sugra_entity_lookup",
)

# Hosted-only tools are absent from the pip-installed package, so prompt text
# must never send an agent to them.
HOSTED_ONLY_TOOL_NAMES = ("resolve_entity", "get_snapshot", "get_timeseries")

SAMPLE_ARGUMENTS = {
    "market_snapshot": {"symbol": "AAPL"},
    "macro_briefing": {"country": "Germany"},
    "sanctions_screening": {"name": "Example Holdings"},
    "sector_compare": {"sector_a": "technology", "sector_b": "energy"},
    "earth_conditions": {"lat": "51.5", "lon": "-0.1"},
    "source_overview": {"domain": "macro"},
}


def _registered_prompts(monkeypatch):
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    import sugra_api_mcp.tools  # noqa: F401  (import registers tools + prompts)
    from sugra_api_mcp.server import mcp

    return mcp, asyncio.run(mcp.list_prompts())


def _render_text(mcp, name: str) -> str:
    result = asyncio.run(mcp.get_prompt(name, SAMPLE_ARGUMENTS[name]))
    texts = [
        message.content.text
        for message in result.messages
        if getattr(message.content, "text", None)
    ]
    return "\n".join(texts)


def test_exactly_six_prompts_registered(monkeypatch):
    _, prompts = _registered_prompts(monkeypatch)
    assert len(prompts) == EXPECTED_PROMPT_COUNT
    assert {p.name for p in prompts} == EXPECTED_PROMPT_NAMES


def test_every_prompt_has_title_and_description(monkeypatch):
    _, prompts = _registered_prompts(monkeypatch)
    for prompt in prompts:
        assert prompt.title, f"{prompt.name} is missing a title"
        assert prompt.description, f"{prompt.name} is missing a description"


@pytest.mark.parametrize("prompt_name", sorted(EXPECTED_PROMPT_NAMES))
def test_prompt_renders_nonempty_and_names_a_real_tool(monkeypatch, prompt_name):
    mcp, _ = _registered_prompts(monkeypatch)
    text = _render_text(mcp, prompt_name)
    assert text.strip(), f"{prompt_name} rendered empty content"
    assert any(tool in text for tool in PACKAGE_TOOL_NAMES), (
        f"{prompt_name} does not mention any package tool name"
    )


@pytest.mark.parametrize("prompt_name", sorted(EXPECTED_PROMPT_NAMES))
def test_prompt_operation_ids_exist_in_bundled_catalog(monkeypatch, prompt_name):
    from sugra_api_mcp.catalog.loader import load_catalog

    mcp, _ = _registered_prompts(monkeypatch)
    text = _render_text(mcp, prompt_name)
    catalog = load_catalog()
    for operation_id in re.findall(r'operation_id "([a-z0-9_]+)"', text):
        try:
            catalog.get(operation_id)
        except KeyError:
            pytest.fail(f"{prompt_name} references unknown operation_id {operation_id}")


def test_prompt_module_source_follows_copy_rules():
    from sugra_api_mcp.tools import prompts as prompts_module

    source = inspect.getsource(prompts_module)

    assert "\u2014" not in source, "em dash found in prompt module"
    assert "\u2013" not in source, "en dash found in prompt module"
    assert source.isascii(), "non-ASCII character (emoji or dash) in prompt module"

    lowered = source.lower()
    for banned in ("real-time", "realtime", "real time"):
        assert banned not in lowered, f"banned phrase {banned!r} in prompt module"

    # Tier C commercial source names must never appear in public prompt copy.
    for banned in ("yahoo", "finnhub", "coingecko", "tomorrow.io"):
        assert banned not in lowered, f"commercial source name {banned!r} in prompt module"

    for hosted_only in HOSTED_ONLY_TOOL_NAMES:
        assert hosted_only not in source, (
            f"prompt module references hosted-only tool {hosted_only}"
        )


def test_prompts_list_is_public_but_prompts_get_is_authed():
    """Boundary pin: prompt discovery is public, prompt content needs auth."""
    from sugra_api_mcp.auth import PUBLIC_MCP_METHODS

    assert "prompts/list" in PUBLIC_MCP_METHODS
    assert "prompts/get" not in PUBLIC_MCP_METHODS
    assert "resources/read" not in PUBLIC_MCP_METHODS


def test_stdio_tool_surface_unchanged_by_prompts(monkeypatch):
    mcp, _ = _registered_prompts(monkeypatch)
    tool_list = asyncio.run(mcp.list_tools())
    assert len(tool_list) == EXPECTED_TOOL_COUNT
