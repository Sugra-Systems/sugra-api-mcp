"""Build the skills index served at https://mcp.sugra.ai/.well-known/skills/.

The skill text lives in one repository, Sugra-Systems/sugra-api-skills. This
script reads one commit of it and writes only the index: each skill's name,
its description and the list of its files. The server redirects every listed
file to that commit's raw copy, so the text is never copied here.

Rebuild after the skills change:

    python scripts/build_skills_index.py --commit <sha or branch>

A branch name is resolved to its current commit, and the full sha is what gets
pinned.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from collections.abc import Callable, Iterable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPO = "Sugra-Systems/sugra-api-skills"
DEFAULT_PATH = "plugins/sugra-api/skills"
DEFAULT_OUTPUT = REPO_ROOT / "sugra_api_mcp" / "skills_index.json"

# The skills CLI refuses any other name (lowercase letters, digits, single hyphens).
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "sugra-api-mcp-build"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def frontmatter(text: str) -> dict[str, str]:
    """The single-line `key: value` pairs of a SKILL.md frontmatter block."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md has no frontmatter")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        key, sep, value = line.partition(":")
        if sep and key and not key[0].isspace():
            fields[key.strip()] = value.strip().strip("\"'")
    raise ValueError("SKILL.md frontmatter is not closed")


def build_index(
    repo: str,
    commit: str,
    path: str,
    blobs: Iterable[str],
    read: Callable[[str], str],
) -> dict:
    """The index for every skill directory directly under `path`.

    `blobs` are the file paths of the commit's tree; `read` returns a file's
    text at that commit.
    """
    if not _SHA.match(commit):
        raise ValueError(f"commit must be a full 40-character sha, got {commit!r}")
    prefix = path.rstrip("/") + "/"
    files: dict[str, list[str]] = {}
    for blob in blobs:
        if not blob.startswith(prefix):
            continue
        name, sep, rest = blob[len(prefix) :].partition("/")
        if sep and rest:
            files.setdefault(name, []).append(rest)
    skills = []
    for name in sorted(files):
        if not _NAME.match(name) or len(name) > 64:
            raise ValueError(f"skill directory {name!r} is not a valid skill name")
        if "SKILL.md" not in files[name]:
            raise ValueError(f"skill {name!r} has no SKILL.md")
        meta = frontmatter(read(f"{prefix}{name}/SKILL.md"))
        if meta.get("name") != name:
            raise ValueError(f"skill {name!r} declares name {meta.get('name')!r}")
        if not meta.get("description"):
            raise ValueError(f"skill {name!r} has no description")
        skills.append(
            {"name": name, "description": meta["description"], "files": sorted(files[name])}
        )
    if not skills:
        raise ValueError(f"no skills under {path!r} at {commit}")
    return {"source": {"repo": repo, "commit": commit, "path": path.rstrip("/")}, "skills": skills}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--commit", required=True, help="sha or branch of the skills repo")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    api = f"https://api.github.com/repos/{args.repo}"
    commit = json.loads(_get(f"{api}/commits/{args.commit}"))["sha"]
    tree = json.loads(_get(f"{api}/git/trees/{commit}?recursive=1"))
    if tree.get("truncated"):
        raise SystemExit("the tree listing is truncated; the index would be incomplete")
    blobs = [entry["path"] for entry in tree["tree"] if entry["type"] == "blob"]

    def read(file_path: str) -> str:
        raw = f"https://raw.githubusercontent.com/{args.repo}/{commit}/{file_path}"
        return _get(raw).decode("utf-8")

    index = build_index(args.repo, commit, args.path, blobs, read)
    args.output.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"{args.output}: {len(index['skills'])} skills at {args.repo}@{commit}")


if __name__ == "__main__":
    main()
