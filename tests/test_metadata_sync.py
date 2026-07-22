"""Guard cross-file metadata consistency: package version and tool-surface wording.

These checks would have caught the historical drift where server.json lagged at
0.5.2 while the package shipped 0.6.3, and where the "five tools" wording
survived after a sixth tool (fetch_data) was added.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import sugra_api_mcp

REPO_ROOT = Path(__file__).resolve().parent.parent
EXPECTED_TOOL_COUNT = 8
# Hosted surface = classic tools + 3 Agent Context Layer tools (resolve_entity
# / get_snapshot / get_timeseries), registered only by the streamable-http
# branch of __main__ when SUGRA_AGENT_INTERNAL_TOKEN is present.
EXPECTED_HOSTED_TOOL_COUNT = EXPECTED_TOOL_COUNT + 3
NUMBER_WORDS = {
    1: "one",
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
}

# Registration order: tools/__init__ imports entities first, then gateway;
# the SDK ToolManager stores tools in an insertion-ordered dict, so
# list_tools() returns exactly this order on every call.
EXPECTED_TOOL_NAME_ORDER = [
    "sugra_entity_screen",
    "sugra_entity_lookup",
    "search_endpoints",
    "describe_endpoint",
    "call_endpoint",
    "list_toolsets",
    "fetch_data",
    "list_sources",
]


def _pyproject_version():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _server_json():
    return json.loads((REPO_ROOT / "server.json").read_text(encoding="utf-8"))


def test_init_version_matches_pyproject():
    assert sugra_api_mcp.__version__ == _pyproject_version()


def test_server_json_versions_match_pyproject():
    version = _pyproject_version()
    server = _server_json()
    assert server["version"] == version, "server.json top-level version drifted from pyproject"
    for package in server["packages"]:
        assert package["version"] == version, "server.json package version drifted from pyproject"


def test_server_info_version_matches_package():
    """serverInfo.version in the initialize response must be OUR version.

    FastMCP never forwards a version to the lowlevel server, which then falls
    back to the MCP SDK version. SugraFastMCP.__init__ pins it; both the
    attribute and the initialization options built from it must match.
    """
    from sugra_api_mcp.server import mcp

    assert mcp._mcp_server.version == sugra_api_mcp.__version__
    init_options = mcp._mcp_server.create_initialization_options()
    assert init_options.server_version == sugra_api_mcp.__version__


def test_server_website_url_and_icons():
    from sugra_api_mcp.server import SERVER_ICONS, WEBSITE_URL, mcp

    low = mcp._mcp_server
    assert WEBSITE_URL == "https://sugra.ai"
    assert low.website_url == WEBSITE_URL

    assert low.icons == SERVER_ICONS
    assert [icon.sizes for icon in low.icons] == [["192x192"], ["512x512"]]
    for icon in low.icons:
        assert icon.mimeType == "image/png"
        assert icon.src.startswith("https://app.sugra.ai/images/brand/")

    # The lowlevel server forwards both fields into the initialize response.
    init_options = mcp._mcp_server.create_initialization_options()
    assert init_options.website_url == WEBSITE_URL
    assert init_options.icons == SERVER_ICONS


def test_registered_tool_count_matches_expectation(monkeypatch):
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    import asyncio

    from sugra_api_mcp import tools  # noqa: F401  (import registers the tools)
    from sugra_api_mcp.server import mcp

    tool_list = asyncio.run(mcp.list_tools())
    assert len(tool_list) == EXPECTED_TOOL_COUNT


def test_tool_list_order_is_pinned(monkeypatch):
    """list_tools() must be deterministic AND stay in registration order."""
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    import asyncio

    from sugra_api_mcp import tools  # noqa: F401  (import registers the tools)
    from sugra_api_mcp.server import mcp

    first = [tool.name for tool in asyncio.run(mcp.list_tools())]
    second = [tool.name for tool in asyncio.run(mcp.list_tools())]
    assert first == second, "consecutive list_tools calls disagree on order"
    assert first == EXPECTED_TOOL_NAME_ORDER
    assert len(EXPECTED_TOOL_NAME_ORDER) == EXPECTED_TOOL_COUNT


def test_hosted_tool_count_with_internal_token():
    """Hosted surface = classic tools + agent tools. Runs in a SUBPROCESS so
    the global mcp singleton in this test process is never mutated - the
    classic tests above assert exactly EXPECTED_TOOL_COUNT on it (Codex
    plan-review P1)."""
    import subprocess
    import sys

    code = (
        "import asyncio, os\n"
        "os.environ['SUGRA_AGENT_INTERNAL_TOKEN'] = 'probe-token'\n"
        "import sugra_api_mcp.tools\n"
        "from sugra_api_mcp.server import mcp\n"
        "from sugra_api_mcp.tools.agent import register_agent_tools\n"
        "assert register_agent_tools() is True\n"
        "assert register_agent_tools() is True  # idempotent latch\n"
        "tools = asyncio.run(mcp.list_tools())\n"
        "assert all(t.model_dump(by_alias=True, exclude_none=True).get('_meta', {})"
        ".get('securitySchemes') for t in tools), 'OAuth metadata missing'\n"
        "print(len(tools))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=True,
    )
    assert int(out.stdout.strip()) == EXPECTED_HOSTED_TOOL_COUNT


def test_public_tool_count_wording_is_not_stale():
    expected_word = NUMBER_WORDS[EXPECTED_TOOL_COUNT]

    server_description = _server_json()["description"].lower()
    assert f"{expected_word} tools" in server_description, (
        f"server.json description should advertise '{expected_word} tools'"
    )

    assert sugra_api_mcp.__doc__ is not None
    assert f"{expected_word} gateway tools" in sugra_api_mcp.__doc__.lower(), (
        f"package docstring should advertise '{expected_word} gateway tools'"
    )


# Operator-only secrets must not appear in the root README: public MCP
# directories (Glama and similar) scrape README env tables into Try/sandbox
# forms. Keep INTERNAL_API_TOKEN and OAuth wiring in docs/self-hosting.md only.
_OPERATOR_ENV_NAMES = (
    "INTERNAL_API_TOKEN",
    "SUGRA_APP_URL",
    "SUGRA_JWKS_URL",
    "SUGRA_MCP_ALLOWED_HOSTS",
    "SUGRA_MCP_ALLOWED_ORIGINS",
)


def test_server_json_exposes_only_user_facing_api_key_env():
    """MCP registry / directory installers should only require SUGRA_API_KEY."""
    packages = _server_json()["packages"]
    assert packages, "server.json must declare at least one package"
    env_vars = packages[0].get("environmentVariables") or []
    names = [item["name"] for item in env_vars]
    assert names == ["SUGRA_API_KEY"], (
        "server.json environmentVariables must list only SUGRA_API_KEY "
        f"(user-facing); got {names}"
    )
    key = env_vars[0]
    assert key.get("isRequired") is True
    assert key.get("isSecret") is True


def test_readme_does_not_list_operator_env_in_root_docs():
    """Root README must not document operator secrets in env tables.

    Directory scrapers treat README tables as the public env schema for Try
    forms. Operator vars live in docs/self-hosting.md instead.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    for name in _OPERATOR_ENV_NAMES:
        assert name not in readme, (
            f"{name} must not appear in README.md (move to docs/self-hosting.md "
            "so public directory sandboxes do not request operator secrets)"
        )
    assert "docs/self-hosting.md" in readme


def test_self_hosting_doc_covers_operator_env():
    path = REPO_ROOT / "docs" / "self-hosting.md"
    assert path.is_file(), "docs/self-hosting.md is required for operator env docs"
    body = path.read_text(encoding="utf-8")
    for name in _OPERATOR_ENV_NAMES:
        assert name in body, f"docs/self-hosting.md must document {name}"
    assert "Never commit" in body or "never put" in body.lower()
