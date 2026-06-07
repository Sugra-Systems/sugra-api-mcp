"""Build bundled Sugra MCP endpoint catalog from OpenAPI JSON.

Default source is the sibling API repo's static OpenAPI file
(prod-sugra-ai-API/static/openapi.json), located by walking up from this
repo so builds also work from per-session worktrees under
.claude/worktrees/.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
# The checkout being built must win over any pip-installed copy of the
# package - otherwise a worktree build silently bundles stale builder code.
sys.path.insert(0, str(REPO_ROOT))

from sugra_api_mcp.catalog.builder import build_catalog_from_openapi  # noqa: E402


def _default_source() -> Path:
    for ancestor in REPO_ROOT.parents:
        candidate = ancestor / "prod-sugra-ai-API" / "static" / "openapi.json"
        if candidate.exists():
            return candidate
    return REPO_ROOT.parent / "prod-sugra-ai-API" / "static" / "openapi.json"


DEFAULT_SOURCE = _default_source()
DEFAULT_OUTPUT = REPO_ROOT / "sugra_api_mcp" / "catalog" / "data" / "endpoints.json"


def build(source: Path, output: Path) -> None:
    openapi = json.loads(source.read_text(encoding="utf-8"))
    source_label = os.path.relpath(source.resolve(), REPO_ROOT).replace("\\", "/")
    # Normalize away leading parent hops so the label does not encode the
    # checkout's depth (a worktree build would otherwise stamp the bundle
    # with ../../../../prod-sugra-ai-API/...).
    while source_label.startswith("../"):
        source_label = source_label[3:]
    catalog = build_catalog_from_openapi(openapi, source=source_label)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(catalog.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"Wrote {output}")
    print(f"Source: {source}")
    print(f"Endpoints: {catalog.endpoint_count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Sugra MCP endpoint catalog.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    if not args.source.exists():
        raise SystemExit(f"OpenAPI source not found: {args.source}")
    build(args.source, args.output)


if __name__ == "__main__":
    main()
