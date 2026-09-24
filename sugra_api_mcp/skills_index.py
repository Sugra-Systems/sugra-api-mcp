"""The skills index behind `npx skills add https://mcp.sugra.ai`.

The skills CLI reads /.well-known/skills/index.json (name, description and
file list per skill) and then fetches each listed file under
/.well-known/skills/<name>/. The skill text lives in the sugra-api-skills
repository: skills_index.json pins one of its commits
(scripts/build_skills_index.py writes it), and every listed file redirects to
that commit's raw copy. Only the exact paths below are public.
"""

from __future__ import annotations

import json
from pathlib import Path

INDEX_PATH = "/.well-known/skills/index.json"
_BASE = "/.well-known/skills/"

_DATA = json.loads((Path(__file__).parent / "skills_index.json").read_text(encoding="utf-8"))
SOURCE: dict[str, str] = _DATA["source"]

# The index format without $schema: {"skills": [{name, description, files}]}.
INDEX: dict[str, list[dict]] = {"skills": _DATA["skills"]}

RAW_BASE = f"https://raw.githubusercontent.com/{SOURCE['repo']}/{SOURCE['commit']}/{SOURCE['path']}"

# Public path of every listed file -> its raw copy at the pinned commit.
FILE_TARGETS: dict[str, str] = {
    f"{_BASE}{skill['name']}/{name}": f"{RAW_BASE}/{skill['name']}/{name}"
    for skill in INDEX["skills"]
    for name in skill["files"]
}

PUBLIC_PATHS: frozenset[str] = frozenset({INDEX_PATH, *FILE_TARGETS})
