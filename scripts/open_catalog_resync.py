"""Open or update the single catalog-resync PR when the bundle has drifted.

The bundled catalog is a build artifact of the live Sugra OpenAPI document.
catalog-parity.yml compares the two and, on DRIFT, runs this script so the
fix lands as a pull request instead of waiting for a human rebuild.

Public-repo rules (load-bearing):

- Branch name is the stable neutral ref `ci/catalog-resync` (force-with-lease
  from origin/main on every run, so there is exactly one resync PR).
- Commit author and committer are Arman Obosyan. github-actions[bot] cannot
  author this: a bot push does not trigger workflows, so Test would never
  run on the PR.
- The PR body must not close a board card. This is a catalog bump, not the
  continuity mechanism (MCP-15.3).

Requires env MCP_CATALOG_TOKEN (fine-grained PAT: contents + pull requests
on sugra-api-mcp). GITHUB_TOKEN is refused.

  python scripts/open_catalog_resync.py            # rebuild, commit, push, PR
  python scripts/open_catalog_resync.py --no-git   # rebuild + FACTS stamp only
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SPEC = "https://sugra.ai/openapi.json"
RESYNC_BRANCH = "ci/catalog-resync"
GH_REPO = "Sugra-Systems/sugra-api-mcp"
AUTHOR_NAME = "Arman Obosyan"
AUTHOR_EMAIL = "arman@sugra.systems"
BUNDLE_REL = Path("sugra_api_mcp") / "catalog" / "data" / "endpoints.json"
FACTS_REL = Path("FACTS.md")
PR_TITLE = "chore: resync bundled catalog from live OpenAPI"
LIST_CAP = 20

_BUNDLE_LINE = re.compile(
    r"^(- Bundled MCP catalog \(built_at )([^)]+)(\): )([0-9,]+)"
    r"( GET/POST operations from live OpenAPI)"
    r"(?: \(spec_sha256 [^)]+\))?(\..*)$",
    re.MULTILINE,
)


def require_catalog_token(env: Mapping[str, str]) -> str:
    """Return MCP_CATALOG_TOKEN or exit. GITHUB_TOKEN is not a substitute."""
    token = (env.get("MCP_CATALOG_TOKEN") or "").strip()
    if not token:
        raise SystemExit(
            "FAIL: MCP_CATALOG_TOKEN is not set. GITHUB_TOKEN cannot be used: "
            "a github-actions[bot] push does not trigger workflows, and the "
            "resync PR must be authored as Arman Obosyan so Test runs on it."
        )
    return token


def drift_lists(old: dict, new: dict) -> tuple[list[str], list[str], list[str]]:
    """Return (missing_from_old, extra_in_old, same_id_changed_contract)."""
    old_by = {e["operation_id"]: e for e in old["endpoints"]}
    new_by = {e["operation_id"]: e for e in new["endpoints"]}
    missing = sorted(set(new_by) - set(old_by))
    extra = sorted(set(old_by) - set(new_by))
    changed = sorted(
        op_id for op_id in old_by.keys() & new_by.keys() if old_by[op_id] != new_by[op_id]
    )
    return missing, extra, changed


def update_facts(
    text: str,
    *,
    operation_count: int,
    spec_sha256: str,
    built_at: str,
) -> str:
    """Replace the bundled-catalog stamp line in FACTS.md. Fail loud if absent."""

    def repl(match: re.Match[str]) -> str:
        return (
            f"{match.group(1)}{built_at}{match.group(3)}{operation_count:,}"
            f"{match.group(5)} (spec_sha256 {spec_sha256[:12]}...){match.group(6)}"
        )

    updated, n = _BUNDLE_LINE.subn(repl, text, count=1)
    if n != 1:
        raise SystemExit(
            "FAIL: FACTS.md has no bundled catalog stamp line to update. "
            "Expected a line starting with '- Bundled MCP catalog (built_at '."
        )
    return updated


def _fmt_ops(ids: list[str]) -> str:
    if not ids:
        return "none"
    head = ", ".join(ids[:LIST_CAP])
    if len(ids) > LIST_CAP:
        head += f" ... and {len(ids) - LIST_CAP} more"
    return head


def pr_body(
    *,
    missing: list[str],
    extra: list[str],
    changed: list[str],
    spec_url: str,
    operation_count: int,
    spec_sha256: str,
    built_at: str,
) -> str:
    """Human PR body. Must never close a board card and must name no agent."""
    return (
        f"The bundled OpenAPI catalog on main drifted from {spec_url}.\n"
        "\n"
        f"- Operations in the rebuilt bundle: {operation_count:,}\n"
        f"- spec_sha256: `{spec_sha256}`\n"
        f"- built_at: {built_at}\n"
        "\n"
        "### Drift\n"
        "\n"
        f"- missing from the bundle ({len(missing)}): {_fmt_ops(missing)}\n"
        f"- not in the spec ({len(extra)}): {_fmt_ops(extra)}\n"
        f"- same id, changed contract ({len(changed)}): {_fmt_ops(changed)}\n"
        "\n"
        "This PR rebuilds `sugra_api_mcp/catalog/data/endpoints.json` from the "
        "live spec and stamps FACTS.md.\n"
        "\n"
        "This is an automated catalog bump. It does not close a board card.\n"
        "\n"
        "Rebuild: `python scripts/build_endpoint_catalog.py "
        f"--source {spec_url}`\n"
    )


def _git(
    args: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )
    if check and result.returncode != 0:
        raise SystemExit(
            f"FAIL: git {' '.join(args)} exited {result.returncode}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


def git_https_extraheader(token: str) -> str:
    """GitHub Git-over-HTTPS wants Basic x-access-token, not REST Bearer.

    actions/checkout and git itself speak Basic. A Bearer extraheader authenticates
    the REST API and is ignored (or refused) by the Git HTTPS endpoint, so the
    resync push would never land.
    """
    blob = base64.b64encode(f"x-access-token:{token}".encode("ascii")).decode("ascii")
    return f"AUTHORIZATION: basic {blob}"


def _redact(text: str, token: str) -> str:
    if not token:
        return text
    out = text.replace(token, "***")
    blob = base64.b64encode(f"x-access-token:{token}".encode("ascii")).decode("ascii")
    return out.replace(blob, "***")


def _author_env(base: Mapping[str, str]) -> dict[str, str]:
    env = dict(base)
    env["GIT_AUTHOR_NAME"] = AUTHOR_NAME
    env["GIT_AUTHOR_EMAIL"] = AUTHOR_EMAIL
    env["GIT_COMMITTER_NAME"] = AUTHOR_NAME
    env["GIT_COMMITTER_EMAIL"] = AUTHOR_EMAIL
    return env


def rebuild_bundle(repo_root: Path, spec: str) -> dict:
    """Run the catalog builder and return the new bundle dict."""
    builder = repo_root / "scripts" / "build_endpoint_catalog.py"
    output = repo_root / BUNDLE_REL
    result = subprocess.run(
        [
            sys.executable,
            str(builder),
            "--source",
            spec,
            "--output",
            str(output),
        ],
        cwd=repo_root,
        check=False,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise SystemExit(
            f"FAIL: catalog rebuild exited {result.returncode}\n"
            f"{result.stdout}\n{result.stderr}"
        )
    print(result.stdout, end="")
    return json.loads(output.read_text(encoding="utf-8"))


def stamp_facts(repo_root: Path, bundle: dict) -> None:
    facts_path = repo_root / FACTS_REL
    updated = update_facts(
        facts_path.read_text(encoding="utf-8"),
        operation_count=int(bundle["endpoint_count"]),
        spec_sha256=str(bundle["spec_sha256"]),
        built_at=str(bundle["built_at"]),
    )
    facts_path.write_text(updated, encoding="utf-8", newline="\n")


def land_resync_pr(
    *,
    repo_root: Path,
    spec: str,
    token: str,
    env: Mapping[str, str],
) -> int:
    bundle_path = repo_root / BUNDLE_REL
    git_env = _author_env(env)
    git_env["GH_TOKEN"] = token
    git_env["GITHUB_TOKEN"] = token

    _git(["fetch", "origin", "main"], cwd=repo_root, env=git_env)
    _git(["fetch", "origin", RESYNC_BRANCH], cwd=repo_root, env=git_env, check=False)
    _git(["checkout", "-B", RESYNC_BRANCH, "origin/main"], cwd=repo_root, env=git_env)

    old = json.loads(bundle_path.read_text(encoding="utf-8"))
    new = rebuild_bundle(repo_root, spec)
    stamp_facts(repo_root, new)
    missing, extra, changed = drift_lists(old, new)
    body = pr_body(
        missing=missing,
        extra=extra,
        changed=changed,
        spec_url=spec,
        operation_count=int(new["endpoint_count"]),
        spec_sha256=str(new["spec_sha256"]),
        built_at=str(new["built_at"]),
    )
    if "Closes board item" in body:
        raise SystemExit("FAIL: resync PR body must not close a board card.")

    _git(["add", "--", str(BUNDLE_REL), str(FACTS_REL)], cwd=repo_root, env=git_env)
    cached = _git(["diff", "--cached", "--quiet"], cwd=repo_root, env=git_env, check=False)
    if cached.returncode == 0:
        raise SystemExit(
            "FAIL: rebuild produced no file changes, but the parity checker "
            "reported drift. The builder and the checker disagree."
        )
    _git(
        [
            "commit",
            "-m",
            "chore: resync bundled catalog from live OpenAPI\n\n"
            "Rebuild endpoints.json and stamp FACTS.md so search_endpoints "
            "matches the production spec.",
        ],
        cwd=repo_root,
        env=git_env,
    )

    remote_has_branch = bool(
        _git(
            ["ls-remote", "--heads", "origin", RESYNC_BRANCH],
            cwd=repo_root,
            env=git_env,
        ).stdout.strip()
    )
    push = [
        "-c",
        f"http.extraheader={git_https_extraheader(token)}",
        "push",
    ]
    if remote_has_branch:
        push.append(f"--force-with-lease={RESYNC_BRANCH}")
    push.extend(["origin", f"HEAD:{RESYNC_BRANCH}"])
    pushed = _git(push, cwd=repo_root, env=git_env, check=False)
    if pushed.returncode != 0:
        raise SystemExit(
            "FAIL: git push of "
            f"{RESYNC_BRANCH} exited {pushed.returncode}\n"
            f"{_redact(pushed.stdout, token)}\n{_redact(pushed.stderr, token)}"
        )

    listed = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            GH_REPO,
            "--head",
            RESYNC_BRANCH,
            "--state",
            "open",
            "--json",
            "number,url",
        ],
        cwd=repo_root,
        env=git_env,
        check=False,
        text=True,
        capture_output=True,
    )
    if listed.returncode != 0:
        raise SystemExit(
            f"FAIL: gh pr list exited {listed.returncode}\n"
            f"{_redact(listed.stdout, token)}\n{_redact(listed.stderr, token)}"
        )
    open_prs = json.loads(listed.stdout or "[]")

    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", newline="\n", suffix=".md", delete=False
    ) as handle:
        handle.write(body)
        body_file = handle.name
    try:
        if open_prs:
            number = str(open_prs[0]["number"])
            edited = subprocess.run(
                [
                    "gh",
                    "pr",
                    "edit",
                    number,
                    "--repo",
                    GH_REPO,
                    "--title",
                    PR_TITLE,
                    "--body-file",
                    body_file,
                ],
                cwd=repo_root,
                env=git_env,
                check=False,
                text=True,
                capture_output=True,
            )
            if edited.returncode != 0:
                raise SystemExit(
                    f"FAIL: gh pr edit exited {edited.returncode}\n"
                    f"{_redact(edited.stdout, token)}\n{_redact(edited.stderr, token)}"
                )
            print(f"Updated resync PR {open_prs[0]['url']}")
        else:
            created = subprocess.run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--repo",
                    GH_REPO,
                    "--base",
                    "main",
                    "--head",
                    RESYNC_BRANCH,
                    "--title",
                    PR_TITLE,
                    "--body-file",
                    body_file,
                ],
                cwd=repo_root,
                env=git_env,
                check=False,
                text=True,
                capture_output=True,
            )
            if created.returncode != 0:
                raise SystemExit(
                    f"FAIL: gh pr create exited {created.returncode}\n"
                    f"{_redact(created.stdout, token)}\n{_redact(created.stderr, token)}"
                )
            print(f"Opened resync PR {created.stdout.strip()}")
    finally:
        os.unlink(body_file)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Open or update the catalog resync PR.")
    parser.add_argument("--spec", default=DEFAULT_SPEC)
    parser.add_argument("--repo-root", type=Path, default=REPO_ROOT)
    parser.add_argument(
        "--no-git",
        action="store_true",
        help="Rebuild the bundle and stamp FACTS.md without committing or opening a PR.",
    )
    args = parser.parse_args(argv)
    repo_root = args.repo_root.resolve()

    if args.no_git:
        old = json.loads((repo_root / BUNDLE_REL).read_text(encoding="utf-8"))
        new = rebuild_bundle(repo_root, args.spec)
        stamp_facts(repo_root, new)
        missing, extra, changed = drift_lists(old, new)
        print(
            f"Drift: missing={len(missing)} extra={len(extra)} changed={len(changed)}"
        )
        return 0

    token = require_catalog_token(os.environ)
    return land_resync_pr(
        repo_root=repo_root,
        spec=args.spec,
        token=token,
        env=os.environ,
    )


if __name__ == "__main__":
    raise SystemExit(main())
