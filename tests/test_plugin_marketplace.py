"""Plugin marketplace manifests point at the existing SKILL.md pack (MCP-15.7)."""

from __future__ import annotations

import json
from pathlib import Path

from sugra_api_mcp.tools.skills import SKILL_SLUGS

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = REPO_ROOT / "sugra_api_mcp" / "skills"
CLAUDE_MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"
GROK_MARKETPLACE = REPO_ROOT / ".grok-plugin" / "marketplace.json"
PLUGIN_MANIFEST = PLUGIN_ROOT / ".claude-plugin" / "plugin.json"
README = REPO_ROOT / "README.md"

MARKETPLACE_NAME = "sugra-api-mcp"
PLUGIN_NAME = "sugra-api"
PLUGIN_RELATIVE = "sugra_api_mcp/skills"

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


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _plugin_source_path(entry: dict) -> str:
    source = entry["source"]
    if isinstance(source, str):
        return source.replace("\\", "/").lstrip("./")
    if isinstance(source, dict):
        if "path" in source:
            return str(source["path"]).replace("\\", "/").lstrip("./")
        raise AssertionError(f"marketplace source object has no path: {source}")
    raise AssertionError(f"unexpected source type: {type(source)}")


def _copy_lint(text: str, label: str) -> None:
    assert text.isascii(), f"{label}: marketplace copy must be plain ASCII"
    lowered = text.lower()
    assert "real-time" not in lowered
    assert "realtime" not in lowered
    assert "financial intelligence" not in lowered
    assert "blackbox" not in lowered
    for fragment in TIER_C_NAME_FRAGMENTS:
        assert fragment not in lowered, f"{label}: commercial name {fragment}"
    assert "\u2014" not in text, f"{label}: em dash"
    assert "—" not in text, f"{label}: em dash"


def test_marketplace_files_exist() -> None:
    assert CLAUDE_MARKETPLACE.is_file()
    assert GROK_MARKETPLACE.is_file()
    assert PLUGIN_MANIFEST.is_file()


def test_plugin_root_is_the_existing_skill_pack() -> None:
    assert PLUGIN_ROOT.is_dir()
    assert not (PLUGIN_ROOT / "skills").is_dir(), (
        "do not nest a second skills/ under the pack; plugin.json sets skills to ./"
    )
    for slug in SKILL_SLUGS:
        skill_file = PLUGIN_ROOT / slug / "SKILL.md"
        assert skill_file.is_file(), f"missing {skill_file.relative_to(REPO_ROOT)}"


def test_plugin_manifest_points_skills_at_plugin_root() -> None:
    manifest = _load(PLUGIN_MANIFEST)
    assert manifest["name"] == PLUGIN_NAME
    assert manifest["skills"] == "./"
    assert "mcpServers" not in manifest
    _copy_lint(json.dumps(manifest), "plugin.json")


def test_claude_and_grok_catalogs_name_the_same_plugin() -> None:
    claude = _load(CLAUDE_MARKETPLACE)
    grok = _load(GROK_MARKETPLACE)
    assert claude["name"] == MARKETPLACE_NAME
    assert grok["name"] == MARKETPLACE_NAME
    assert len(claude["plugins"]) == 1
    assert len(grok["plugins"]) == 1
    claude_plugin = claude["plugins"][0]
    grok_plugin = grok["plugins"][0]
    assert claude_plugin["name"] == PLUGIN_NAME
    assert grok_plugin["name"] == PLUGIN_NAME
    assert _plugin_source_path(claude_plugin) == PLUGIN_RELATIVE
    assert _plugin_source_path(grok_plugin) == PLUGIN_RELATIVE
    assert (REPO_ROOT / _plugin_source_path(claude_plugin)).resolve() == PLUGIN_ROOT.resolve()
    assert claude_plugin.get("skills") == "./"
    _copy_lint(json.dumps(claude), "claude marketplace.json")
    _copy_lint(json.dumps(grok), "grok marketplace.json")


def test_readme_teaches_marketplace_install_not_only_copy() -> None:
    text = README.read_text(encoding="utf-8")
    assert "/plugin marketplace add Sugra-Systems/sugra-api-mcp" in text
    assert "/plugin install sugra-api@sugra-api-mcp" in text
    assert "grok plugin marketplace add Sugra-Systems/sugra-api-mcp" in text
    assert "grok plugin install sugra-api --trust" in text
    assert "sugra_api_mcp/skills/explore-catalog" in text
    assert ".cursor/skills/" in text
    assert "ChatGPT does not load SKILL.md from disk" in text
    assert "https://mcp.sugra.ai/mcp" in text
