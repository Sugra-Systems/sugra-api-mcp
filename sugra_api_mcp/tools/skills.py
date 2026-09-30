"""The Sugra API skills registered as MCP resources (sugra://skills/...).

The skills live in the sugra-api-skills repository; skills_index.json pins one
of its commits and keeps each SKILL.md text, so these resources read no file
and make no network call. The five URIs predate the move and keep working:
each serves the skill its old text was merged into. Discovery
(resources/list) is public; reading a resource requires auth, same boundary as
the catalog resources.
"""

from __future__ import annotations

from collections.abc import Callable

from .. import skills_index
from ..server import mcp

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


def read_skill(skill: str) -> str:
    """Return the pinned SKILL.md text of a sugra-api-skills skill. KeyError if absent."""
    return skills_index.SKILL_MD[skill]


def _reader(skill: str) -> Callable[[], str]:
    def read() -> str:
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
