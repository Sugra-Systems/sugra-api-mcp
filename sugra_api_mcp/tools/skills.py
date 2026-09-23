"""Official skill pack registered as MCP resources (sugra://skills/...).

Each SKILL.md is a Claude/Codex drop-in (YAML frontmatter + markdown). The
same files are served as text/markdown resources. Discovery (resources/list)
is public; reading a resource requires auth, same boundary as the catalog
resources.
"""

from __future__ import annotations

from importlib import resources

from ..server import mcp

SKILLS_PACKAGE = "sugra_api_mcp.skills"

# slug, resource name, title, one-line description (also in SKILL.md frontmatter)
SKILL_SPECS: tuple[tuple[str, str, str, str], ...] = (
    (
        "explore-catalog",
        "skill_explore_catalog",
        "Explore the catalog",
        (
            "Find and call the right Sugra API endpoint through the bundled "
            "catalog (search, describe, call)."
        ),
    ),
    (
        "envelope-attribution",
        "skill_envelope_attribution",
        "Envelope and attribution",
        (
            "Parse Sugra API payloads, keep source attribution, and tell "
            "observation time from request time."
        ),
    ),
    (
        "auth-limits",
        "skill_auth_limits",
        "Auth and rate limits",
        "Authenticate to Sugra API MCP and stay inside the daily request quota.",
    ),
    (
        "hosted-vs-gateway",
        "skill_hosted_vs_gateway",
        "Hosted vs gateway",
        (
            "Choose hosted Sugra MCP versus the local gateway package, and "
            "which tools exist on each."
        ),
    ),
    (
        "cross-domain-briefing",
        "skill_cross_domain_briefing",
        "Cross-domain briefing",
        (
            "Compose one briefing from two or three Sugra domains using only "
            "the eight gateway tools."
        ),
    ),
)

SKILL_SLUGS = tuple(spec[0] for spec in SKILL_SPECS)
SKILL_URIS = tuple(f"sugra://skills/{slug}" for slug in SKILL_SLUGS)

# Hosted-only tool names. Only the hosted-vs-gateway skill may mention them.
HOSTED_ONLY_TOOLS = ("resolve_entity", "get_snapshot", "get_timeseries")

STDIO_ONLY_SKILL_SLUGS = tuple(
    slug for slug in SKILL_SLUGS if slug != "hosted-vs-gateway"
)


def read_skill(slug: str) -> str:
    """Return the SKILL.md text for a slug. Raises FileNotFoundError if missing."""
    path = resources.files(SKILLS_PACKAGE).joinpath(slug, "SKILL.md")
    return path.read_text(encoding="utf-8")


def _skill_uri(slug: str) -> str:
    return f"sugra://skills/{slug}"


@mcp.resource(
    _skill_uri("explore-catalog"),
    name="skill_explore_catalog",
    title="Explore the catalog",
    description=(
        "Find and call the right Sugra API endpoint through the bundled "
        "catalog (search, describe, call)."
    ),
    mime_type="text/markdown",
)
def skill_explore_catalog() -> str:
    return read_skill("explore-catalog")


@mcp.resource(
    _skill_uri("envelope-attribution"),
    name="skill_envelope_attribution",
    title="Envelope and attribution",
    description=(
        "Parse Sugra API payloads, keep source attribution, and tell "
        "observation time from request time."
    ),
    mime_type="text/markdown",
)
def skill_envelope_attribution() -> str:
    return read_skill("envelope-attribution")


@mcp.resource(
    _skill_uri("auth-limits"),
    name="skill_auth_limits",
    title="Auth and rate limits",
    description="Authenticate to Sugra API MCP and stay inside the daily request quota.",
    mime_type="text/markdown",
)
def skill_auth_limits() -> str:
    return read_skill("auth-limits")


@mcp.resource(
    _skill_uri("hosted-vs-gateway"),
    name="skill_hosted_vs_gateway",
    title="Hosted vs gateway",
    description=(
        "Choose hosted Sugra MCP versus the local gateway package, and "
        "which tools exist on each."
    ),
    mime_type="text/markdown",
)
def skill_hosted_vs_gateway() -> str:
    return read_skill("hosted-vs-gateway")


@mcp.resource(
    _skill_uri("cross-domain-briefing"),
    name="skill_cross_domain_briefing",
    title="Cross-domain briefing",
    description=(
        "Compose one briefing from two or three Sugra domains using only "
        "the eight gateway tools."
    ),
    mime_type="text/markdown",
)
def skill_cross_domain_briefing() -> str:
    return read_skill("cross-domain-briefing")
