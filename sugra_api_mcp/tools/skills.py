"""The Sugra API skills registered as MCP resources (sugra://skills/...).

The skills live in the sugra-api-skills repository; skills_index.json pins one
of its commits and keeps each SKILL.md text, so these resources read no file
and make no network call. The five URIs predate the move and keep working:
each serves the skill its old text was merged into. Discovery
(resources/list) is public; reading a resource requires auth, same boundary as
the catalog resources.

A ChatGPT session (server.client_profile) is not listed the generic catalog
tools (server.CHATGPT_UNLISTED_TOOLS), which the pinned texts name, so it reads
each skill with CHATGPT_PASSAGES put in: the same text with the passages that
name those tools reworded for the typed tools it is listed. Every other client
reads the pinned text unchanged.
"""

from __future__ import annotations

from collections.abc import Callable

from .. import skills_index
from ..server import CHATGPT_PROFILE, client_profile, mcp

# uri slug, resource name, title, skill in sugra-api-skills it serves
SKILL_SPECS: tuple[tuple[str, str, str, str], ...] = (
    ("explore-catalog", "skill_explore_catalog", "Discover and call", "discover-and-call"),
    (
        "envelope-attribution",
        "skill_envelope_attribution",
        "Envelope and attribution",
        "envelope-and-attribution",
    ),
    ("auth-limits", "skill_auth_limits", "Auth and quota", "auth-and-quota"),
    ("hosted-vs-gateway", "skill_hosted_vs_gateway", "Connect", "connect"),
    (
        "cross-domain-briefing",
        "skill_cross_domain_briefing",
        "Cross-domain briefing",
        "cross-domain-briefing",
    ),
)

SKILL_SLUGS = tuple(spec[0] for spec in SKILL_SPECS)
SKILL_URIS = tuple(f"sugra://skills/{slug}" for slug in SKILL_SLUGS)

_DESCRIPTIONS = {skill["name"]: skill["description"] for skill in skills_index.INDEX["skills"]}


# What a ChatGPT session reads in place of a passage of a pinned skill text:
# (start, end, replacement) per skill, where the passage runs from the first
# character of start up to, not including, end. Each start and end is taken
# from the pinned text and occurs in it once, after one another; a test holds
# them to that, so a re-pin that rewords one fails CI instead of leaving the
# old wording in place.
CHATGPT_PASSAGES: dict[str, tuple[tuple[str, str, str], ...]] = {
    "auth-and-quota": (
        (
            "Before a bulk call read MCP `describe_endpoint`",
            "Over HTTPS, `X-RateLimit-Remaining`",
            "",
        ),
        (
            "Stdio catalog tools (`search_endpoints`",
            "\n\nMCP tool JSON does not forward",
            "Stdio catalog tools (`list_toolsets`, `list_sources`) work without a key. The data"
            " tools and the entity tools return `missing_api_key` until `SUGRA_API_KEY` is set.",
        ),
        (
            "against `describe_endpoint`)",
            ", fix it, then call again",
            "against the tool's input schema)",
        ),
    ),
    "connect": (
        (
            "Hosted tools: the gateway tools every transport has (",
            " plus composed tools that register only on the hosted server:",
            "Hosted tools in ChatGPT: one typed tool per supported operation, named for what it"
            " returns (for example `get_stock_quotes`, `get_economic_series`), the catalog map"
            " (`list_toolsets`, `list_sources`) and the entity tools (`sugra_entity_screen`,"
            " `sugra_entity_lookup`),",
        ),
        (
            "`call_endpoint` / `fetch_data` / entity tools",
            " return `missing_api_key` until it is set.",
            "the data and entity tools",
        ),
    ),
    "envelope-and-attribution": (
        (
            "MCP: `call_endpoint` / `fetch_data` return the same payload",
            "\n\n## Time",
            "MCP: a typed tool returns the payload with request and logging metadata left out."
            " `meta` keeps facts about the data, such as `count`, `total`, `unit` and `notes`,"
            " and its provenance when the API sent it: `data_time`, `period`, `data_age_days`,"
            " `stale`, `stale_since` and `attribution`, taken from `_meta` on an envelope-less"
            " payload. `endpoint`, `response_time`, `provider`, `source` and `cached` are left"
            " out.",
        ),
        (
            "`data_time` in `meta` (or `_meta` on a flat payload)",
            " is the observation or publication clock",
            "`data_time` in `meta`",
        ),
        (
            "Read them from `meta` / `_meta`: name the source,",
            " Live list:",
            "Read the as-of from `meta`: give `data_time` or `period`, and when `attribution` is"
            " present repeat it verbatim. Name the source the tool's description names; when it"
            " names none, name the Sugra wrapper of its direction.",
        ),
    ),
    "discover-and-call": (
        (
            "Two complete loops, same API.",
            "## Check the answer before quoting it",
            "In ChatGPT the API is reached only through the tools listed in this session,"
            " never by a request of its own.\n\n"
            "## MCP loop\n\n"
            "In ChatGPT the hosted server lists one typed tool per supported operation, named"
            " for what it returns (for example `get_stock_quotes`, `get_economic_series`,"
            " `get_country_macro_profile`), each with its own input schema. `list_toolsets` and"
            " `list_sources` read the catalog bundled with the server and do not call"
            " https://sugra.ai; the typed tools and the entity tools do.\n\n"
            "1. Pick the tool by its description, not by its name alone: the closest name is"
            " not always the measure asked for.\n"
            "2. Read its input schema: each input with its description, the required ones, and"
            " the allowed values. A search tool (for example `search_tickers`,"
            " `search_economic_series`) finds the identifier another tool takes.\n"
            "3. Call it. Set every input the question names. An input the tool does not take"
            " returns `unknown_parameters` with `accepted`; a value outside the schema returns"
            " `invalid_arguments` naming the input.\n\n"
            "`list_toolsets` and `list_sources` (resources `sugra://catalog/domains`,"
            " `sugra://catalog/sources`) are the map, not the query. The typed tools cover part"
            " of the catalog: when none covers the question, say so.\n\n"
            "Hosted only: `resolve_entity`, `get_snapshot`, `get_timeseries`. LEI/VAT and"
            " sanctions on every transport: `sugra_entity_lookup`, `sugra_entity_screen`.\n\n"
            "The MCP prompts (`market_snapshot`, `macro_briefing`, `sanctions_screening`,"
            " `sector_compare`, `earth_conditions`, `source_overview`) are numbered recipes over"
            " the tools listed in this session. They are not the catalog and do not cover every"
            " direction.\n\n",
        ),
        (
            "1. `meta.endpoint` is the operation you meant.",
            "\n2. The place",
            "1. The tool you called is the one whose description matches the question.",
        ),
        (
            "Drop a bad MCP `toolset`/`source` filter.",
            "\n\nAn error message usually names the fix",
            "When no tool listed in this session covers it, say so: this session reaches the"
            " API only through its tools.",
        ),
        (
            "Do not add per-endpoint MCP tools to skip this loop.",
            "\n\n## Timeouts",
            "Do not guess an input the tool's input schema does not list.",
        ),
    ),
    "cross-domain-briefing": (
        (
            "4. Quote each figure with its source and `data_time` from `meta`",
            "\n5. Close",
            "4. Quote each figure with `data_time` from `meta` (and `period` when present), its"
            " row-level `as_of` when present, any `attribution` notice word for word, and the"
            " source the tool's description names (see `envelope-and-attribution`).",
        ),
        (
            "Do not add per-endpoint MCP tools. Do not treat screening",
            " Do not frame the output",
            "Do not treat screening as a briefing source unless the question is about a named"
            " party. In ChatGPT, use the tools listed in this session.",
        ),
    ),
}


def read_skill(skill: str) -> str:
    """Return the pinned SKILL.md text of a sugra-api-skills skill. KeyError if absent."""
    return skills_index.SKILL_MD[skill]


def read_chatgpt_skill(skill: str) -> str:
    """The pinned SKILL.md text of a skill with its CHATGPT_PASSAGES put in.

    A passage whose start or end the text no longer holds is left as pinned;
    the tests fail on it before it can ship.
    """
    text = read_skill(skill)
    for start, end, replacement in CHATGPT_PASSAGES.get(skill, ()):
        begin = text.find(start)
        stop = text.find(end, begin + len(start)) if begin >= 0 else -1
        if stop >= 0:
            text = text[:begin] + replacement + text[stop:]
    return text


def _reader(skill: str) -> Callable[[], str]:
    def read() -> str:
        if client_profile() == CHATGPT_PROFILE:
            return read_chatgpt_skill(skill)
        return read_skill(skill)

    return read


for _slug, _name, _title, _skill in SKILL_SPECS:
    mcp.resource(
        f"sugra://skills/{_slug}",
        name=_name,
        title=_title,
        description=_DESCRIPTIONS[_skill],
        mime_type="text/markdown",
    )(_reader(_skill))
