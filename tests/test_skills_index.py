"""The skills index: the pinned data file and the script that builds it.

GitHub is not exercised here; build_index takes the tree and a reader.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from sugra_api_mcp import skills_index

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "build_skills_index", REPO_ROOT / "scripts" / "build_skills_index.py"
)
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

SHA = "0" * 40


def _skill_md(name: str, description: str = "Does a thing.") -> str:
    return f"---\nname: {name}\ndescription: {description}\nlicense: MIT\n---\n\n# {name}\n"


def test_build_index_lists_every_file_of_every_skill() -> None:
    blobs = [
        "README.md",
        "plugins/p/skills/beta/SKILL.md",
        "plugins/p/skills/alpha/SKILL.md",
        "plugins/p/skills/alpha/references/extra.md",
        "plugins/p/skills/alpha/agents/openai.yaml",
        "plugins/p/other/SKILL.md",
    ]
    texts = {
        "plugins/p/skills/alpha/SKILL.md": _skill_md("alpha", '"Quoted: yes."'),
        "plugins/p/skills/beta/SKILL.md": _skill_md("beta"),
    }
    index = build.build_index("Org/repo", SHA, "plugins/p/skills/", blobs, texts.__getitem__)
    assert index == {
        "source": {"repo": "Org/repo", "commit": SHA, "path": "plugins/p/skills"},
        "skills": [
            {
                "name": "alpha",
                "description": "Quoted: yes.",
                "files": ["SKILL.md", "agents/openai.yaml", "references/extra.md"],
            },
            {"name": "beta", "description": "Does a thing.", "files": ["SKILL.md"]},
        ],
    }


@pytest.mark.parametrize(
    ("blobs", "text", "message"),
    [
        (["s/Bad_Name/SKILL.md"], _skill_md("Bad_Name"), "not a valid skill name"),
        (["s/alpha/README.md"], "", "has no SKILL.md"),
        (["s/alpha/SKILL.md"], _skill_md("beta"), "declares name"),
        (["s/alpha/SKILL.md"], "---\nname: alpha\n---\n", "has no description"),
        (["s/alpha/SKILL.md"], "# no frontmatter\n", "no frontmatter"),
        (["s/alpha/SKILL.md"], "---\nname: alpha\n", "not closed"),
        (["other/alpha/SKILL.md"], "", "no skills under"),
    ],
)
def test_build_index_refuses_what_the_skills_cli_would_refuse(blobs, text, message) -> None:
    with pytest.raises(ValueError, match=message):
        build.build_index("Org/repo", SHA, "s", blobs, lambda _path: text)


def test_build_index_pins_a_full_sha() -> None:
    with pytest.raises(ValueError, match="full 40-character sha"):
        build.build_index("Org/repo", "main", "s", [], lambda _path: "")


def test_the_pinned_index_is_well_formed() -> None:
    data = json.loads((REPO_ROOT / "sugra_api_mcp" / "skills_index.json").read_text("utf-8"))
    assert data["source"]["repo"] == "Sugra-Systems/sugra-api-skills"
    assert data["source"]["path"] == "plugins/sugra-api/skills"
    assert re.fullmatch(r"[0-9a-f]{40}", data["source"]["commit"])
    names = [skill["name"] for skill in data["skills"]]
    assert names == sorted(set(names))
    for skill in data["skills"]:
        assert set(skill) == {"name", "description", "files"}
        assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", skill["name"]), skill["name"]
        assert 0 < len(skill["description"]) <= 1024, skill["name"]
        assert "SKILL.md" in skill["files"], skill["name"]
        for name in skill["files"]:
            assert not name.startswith(("/", "\\")) and ".." not in name, name
            assert re.fullmatch(r"[A-Za-z0-9._/-]+", name), name


def test_runtime_targets_are_the_pinned_raw_files() -> None:
    source = skills_index.SOURCE
    prefix = (
        f"https://raw.githubusercontent.com/{source['repo']}/{source['commit']}/{source['path']}/"
    )
    assert skills_index.FILE_TARGETS
    for path, target in skills_index.FILE_TARGETS.items():
        assert path.startswith("/.well-known/skills/")
        assert target == prefix + path.removeprefix("/.well-known/skills/")
