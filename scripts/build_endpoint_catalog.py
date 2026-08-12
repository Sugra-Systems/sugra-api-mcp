"""Build bundled Sugra MCP endpoint catalog from OpenAPI JSON.

Default source is the sibling API repo's static OpenAPI file
(prod-sugra-ai-API/static/openapi.json), located by walking up from this
repo so builds also work from per-session worktrees under
.claude/worktrees/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from datetime import UTC, datetime
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


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def read_spec(source: str) -> tuple[bytes, str]:
    """Return the spec bytes and a stable label for `source` (path or URL).

    Hashing the RAW bytes (not the parsed dict) is what makes the stamp
    comparable: a checker can hash a candidate spec and tell instantly whether
    the bundle was built from it, without reparsing or diffing.
    """
    if _is_url(source):
        with urllib.request.urlopen(source, timeout=60) as response:
            return response.read(), source
    path = Path(source)
    if not path.exists():
        raise SystemExit(f"OpenAPI source not found: {path}")
    label = os.path.relpath(path.resolve(), REPO_ROOT).replace("\\", "/")
    # Normalize away leading parent hops so the label does not encode the
    # checkout's depth (a worktree build would otherwise stamp the bundle
    # with ../../../../prod-sugra-ai-API/...).
    while label.startswith("../"):
        label = label[3:]
    return path.read_bytes(), label


def build(source: str, output: Path) -> None:
    raw, source_label = read_spec(source)
    openapi = json.loads(raw.decode("utf-8"))
    catalog = build_catalog_from_openapi(
        openapi,
        source=source_label,
        spec_sha256=hashlib.sha256(raw).hexdigest(),
        built_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(catalog.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"Wrote {output}")
    print(f"Source: {source}")
    print(f"Spec sha256: {catalog.spec_sha256}")
    print(f"Built at: {catalog.built_at}")
    print(f"Endpoints: {catalog.endpoint_count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Sugra MCP endpoint catalog.")
    parser.add_argument(
        "--source",
        default=str(DEFAULT_SOURCE),
        help="OpenAPI source: a file path, or an http(s) URL to build from the live spec.",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    build(str(args.source), args.output)


if __name__ == "__main__":
    main()
