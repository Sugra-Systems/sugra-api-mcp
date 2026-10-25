"""The prompts and skill resources a ChatGPT session reads name only tools it is listed."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from mcp.shared.memory import create_connected_server_and_client_session
from mcp.types import Implementation

from sugra_api_mcp import skills_index
from sugra_api_mcp.server import CHATGPT_UNLISTED_TOOLS, CHATGPT_UNLISTED_TYPED_TOOLS, mcp
from sugra_api_mcp.tools import prompts, typed  # the package import registers them all
from sugra_api_mcp.tools.skills import (
    CHATGPT_PASSAGES,
    SKILL_SPECS,
    SKILL_URIS,
    read_chatgpt_skill,
    read_skill,
)
from tests.test_prompts import SAMPLE_ARGUMENTS
from tests.test_skills import TIER_C_NAME_FRAGMENTS

CHATGPT = Implementation(name="openai-mcp", version="1.0.0")
CLAUDE = Implementation(name="claude-ai", version="0.1.0")

# A snake_case word; the ones that are tool names are picked out below.
_WORD = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")
# A JSON object in a prompt's text: the arguments of a call.
_ARGUMENTS = re.compile(r"\{[^{}]*\}")


@pytest.fixture(autouse=True)
def _keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")


def _every_tool() -> set[str]:
    return {tool.name for tool in mcp._tool_manager.list_tools()} | set(typed.typed_tools())


async def _read(client_info: Implementation) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """Every prompt as rendered, every skill resource as read, and the tools listed."""
    async with create_connected_server_and_client_session(mcp, client_info=client_info) as session:
        listed = {tool.name for tool in (await session.list_tools()).tools}
        rendered: dict[str, str] = {}
        for prompt in (await session.list_prompts()).prompts:
            result = await session.get_prompt(prompt.name, SAMPLE_ARGUMENTS[prompt.name])
            rendered[prompt.name] = "\n".join(message.content.text for message in result.messages)
            rendered[f"{prompt.name} (description)"] = prompt.description or ""
        skills: dict[str, str] = {}
        resources = {str(resource.uri): resource for resource in (await session.list_resources()).resources}
        for uri in SKILL_URIS:
            (content,) = (await session.read_resource(uri)).contents
            skills[uri] = content.text
            skills[f"{uri} (description)"] = resources[uri].description or ""
    return rendered, skills, listed


def _named_tools(text: str, every_tool: set[str]) -> set[str]:
    return {word for word in _WORD.findall(text) if word in every_tool}


async def test_no_prompt_or_skill_a_chatgpt_session_reads_names_a_tool_it_is_not_listed() -> None:
    rendered, skills, listed = await _read(CHATGPT)
    assert set(skills) >= set(SKILL_URIS)
    unlisted = CHATGPT_UNLISTED_TOOLS | CHATGPT_UNLISTED_TYPED_TOOLS
    assert not listed & unlisted
    every_tool = _every_tool()
    for where, text in {**rendered, **skills}.items():
        assert text.strip() or where.endswith("(description)"), where
        named = {name for name in unlisted if re.search(rf"\b{re.escape(name)}\b", text)}
        assert not named, (where, sorted(named))
        # Not only the four: every tool a text names is one this session is listed.
        assert _named_tools(text, every_tool) <= listed, (where, _named_tools(text, every_tool) - listed)
    # Each prompt still tells the session what to call.
    for name, text in rendered.items():
        if not name.endswith("(description)"):
            assert _named_tools(text, every_tool), name


async def test_every_other_client_reads_the_texts_unchanged() -> None:
    rendered, skills, _ = await _read(CLAUDE)
    for (_slug, _name, _title, skill), uri in zip(SKILL_SPECS, SKILL_URIS, strict=True):
        assert skills[uri] == skills_index.SKILL_MD[skill], uri
    for name, arguments in SAMPLE_ARGUMENTS.items():
        # Outside a request the profile is the default one: the text as registered.
        assert rendered[name] == getattr(prompts, name)(**arguments), name
    # The default texts still teach the generic catalog tools.
    assert "call_endpoint" in rendered["market_snapshot"]
    assert "describe_endpoint" in skills["sugra://skills/explore-catalog"]


async def test_every_call_a_chatgpt_prompt_writes_out_fits_the_tools_input_schema() -> None:
    rendered, _, listed = await _read(CHATGPT)
    tools = typed.typed_tools()
    calls = 0
    for name, text in rendered.items():
        for line in text.splitlines():
            for match in _ARGUMENTS.finditer(line):
                before = [word for word in _WORD.findall(line[: match.start()]) if word in listed]
                assert before, (name, line)
                tool = before[-1]
                arguments: Any = json.loads(match.group())
                assert tool in tools, (name, tool)
                errors = [error.message for error in tools[tool].validator.iter_errors(arguments)]
                assert not errors, (name, tool, arguments, errors)
                calls += 1
    assert calls >= 15


def test_every_chatgpt_passage_is_found_once_in_its_pinned_skill() -> None:
    served = {skill for *_rest, skill in SKILL_SPECS}
    assert set(CHATGPT_PASSAGES) <= served
    for skill, passages in CHATGPT_PASSAGES.items():
        pinned = read_skill(skill)
        expected = pinned
        for start, end, replacement in passages:
            # Found once each and in order, so a re-pin that rewords either fails here.
            assert pinned.count(start) == 1, (skill, start)
            assert pinned.count(end) == 1, (skill, end)
            begin = expected.index(start)
            stop = expected.index(end, begin + len(start))
            expected = expected[:begin] + replacement + expected[stop:]
        assert read_chatgpt_skill(skill) == expected != pinned, skill


def test_the_chatgpt_discover_and_call_text_sends_the_session_to_no_http_request() -> None:
    # A ChatGPT session has no HTTP tool: the steps it reads go through its listed tools only.
    text = read_chatgpt_skill("discover-and-call")
    for step in ("## HTTP loop", "HTTP-call", "openapi.json", "x-api-key", "GET https://"):
        assert step not in text, step
    assert "## MCP loop" in text
    # Every other client keeps the HTTP loop.
    assert "## HTTP loop" in read_skill("discover-and-call")


def test_a_typed_result_drops_a_notice_naming_any_name_the_copy_rules_ban() -> None:
    assert set(typed._COMMERCIAL_NAME_FRAGMENTS) == set(TIER_C_NAME_FRAGMENTS)


def test_the_chatgpt_texts_ask_only_for_the_provenance_a_typed_result_keeps() -> None:
    # The typed result keeps these in meta (typed.minimize_response) and the envelope text names each.
    envelope = read_chatgpt_skill("envelope-and-attribution")
    for key in typed._PROVENANCE_KEYS:
        assert f"`{key}`" in envelope, key
    # Nothing a ChatGPT session reads sends it to a key the result leaves out, or to the gateway tools.
    for skill in ("envelope-and-attribution", "discover-and-call", "cross-domain-briefing"):
        text = read_chatgpt_skill(skill)
        for gone in ("meta.endpoint", "Read them from `meta` / `_meta`", "or `_meta` on a flat payload",
                     "per-endpoint MCP tools", "stay on the gateway tools"):
            assert gone not in text, (skill, gone)


def test_the_chatgpt_texts_follow_the_copy_rules() -> None:
    texts = [read_chatgpt_skill(skill) for *_rest, skill in SKILL_SPECS]
    texts += [replacement for passages in CHATGPT_PASSAGES.values() for *_ends, replacement in passages]
    for text in texts:
        assert text.isascii()
        lowered = text.lower()
        for banned in ("real-time", "realtime", "real time", "financial intelligence", "blackbox"):
            assert banned not in lowered, banned
        for fragment in TIER_C_NAME_FRAGMENTS:
            assert fragment not in lowered, fragment
