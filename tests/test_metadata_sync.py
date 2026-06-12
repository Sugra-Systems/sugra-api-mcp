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


def test_registered_tool_count_matches_expectation(monkeypatch):
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    import asyncio

    from sugra_api_mcp import tools  # noqa: F401  (import registers the tools)
    from sugra_api_mcp.server import mcp

    tool_list = asyncio.run(mcp.list_tools())
    assert len(tool_list) == EXPECTED_TOOL_COUNT


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
        capture_output=True, text=True, cwd=REPO_ROOT, check=True,
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
