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
