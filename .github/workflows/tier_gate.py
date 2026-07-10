# tier-gate: mechanical risk-tier floor enforced as a required status check.
#
# A single organization identity means an approving review cannot gate a merge
# (there is no second identity to approve, so required_approving_review_count is
# 0). This check is the gate instead. Logic:
#   floor    = T3 if the PR touches any HIGH-RISK path (tier-floor-paths.txt), else T0
#   declared = the single `tier:T#` label on the PR (FAIL if 0 or >1)
#   FAIL if declared < floor ("high-risk paths require >= floor")
#   FAIL if floor == T3 and the `release:human-ok` label is absent
#   If the PR vendors a CHANGE.md, its `risk-tier:` must equal the declared label.
# Any script error exits non-zero = a RED, clearable check. The dangerous state
# is a check that never reports, not a red one - so path logic lives HERE, never
# in the workflow's `on.paths` (a path-filtered workflow would starve low-risk
# PRs of the report and block them forever once the check is required).
#
# Lives inside .github/workflows/: the publish workflow triggers only on version
# tags, so changes here never publish.
#
# Offline test mode: --files-list FILE --labels "a,b" (no gh, no network).
import argparse
import fnmatch
import json
import os
import re
import subprocess
import sys

RANK = {'T0': 0, 'T1': 1, 'T2': 2, 'T3': 3}
HERE = os.path.dirname(os.path.abspath(__file__))
PATHS_FILE = os.path.join(HERE, 'tier-floor-paths.txt')


def fail(msg):
    print(f'tier-gate: FAIL - {msg}')
    sys.exit(1)


def load_globs():
    globs = []
    try:
        for line in open(PATHS_FILE, encoding='utf-8'):
            line = line.strip()
            if line and not line.startswith('#'):
                globs.append(line)
    except OSError as e:
        fail(f'cannot read {PATHS_FILE}: {e}')  # config unreadable = red, never skip
    if not globs:
        fail(f'{PATHS_FILE} is empty - the floor would silently vanish')
    return globs


def gh_json(args):
    r = subprocess.run(['gh'] + args, capture_output=True, encoding='utf-8', errors='replace')
    if r.returncode != 0:
        fail(f'gh {" ".join(args[:3])}... failed: {r.stderr.strip()[:300]}')
    try:
        return json.loads(r.stdout)
    except ValueError as e:
        fail(f'gh output not JSON: {e}')


def pr_state(repo, pr):
    # --slurp wraps each page's array into one outer array ([[...],[...]]) so
    # multi-page output stays valid JSON. Bare --paginate CONCATENATES the page
    # arrays ("[...][...]"), json.loads fails, and every large PR would be stuck
    # red regardless of labels - a lockout, not a gate.
    pages = gh_json(['api', f'repos/{repo}/pulls/{pr}/files', '--paginate', '--slurp'])
    files = [f for page in pages for f in page]
    paths, statuses = [], {}
    for f in files:
        name = f.get('filename', '')
        paths.append(name)
        if name:
            statuses[name] = f.get('status', 'modified')
        if f.get('previous_filename'):  # renames: the OLD path counts too
            paths.append(f['previous_filename'])
            statuses[f['previous_filename']] = 'removed'  # old path is gone at head
    labels = [l['name'] for l in gh_json(['api', f'repos/{repo}/pulls/{pr}'])['labels']]
    return [p for p in paths if p], labels, statuses


def normalize_glob(g):
    """A bare directory entry (`dir/`) never matches children under fnmatch -
    normalize it to `dir/**` so config editors cannot silently no-op."""
    return g + '**' if g.endswith('/') else g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pr')
    ap.add_argument('--repo')
    ap.add_argument('--head-sha', help='PR head sha - CHANGE.md is fetched from THIS ref '
                    'via the contents API (never from local disk: under '
                    'pull_request_target the checkout is the trusted BASE ref)')
    ap.add_argument('--files-list', help='offline test: file with one changed path per line')
    ap.add_argument('--labels', help='offline test: comma-separated label names')
    ap.add_argument('--change-md', help='offline test: local file standing in for a vendored CHANGE.md')
    a = ap.parse_args()

    if a.files_list is not None or a.labels is not None:
        paths = [l.strip() for l in open(a.files_list, encoding='utf-8') if l.strip()] if a.files_list else []
        labels = [l.strip() for l in (a.labels or '').split(',') if l.strip()]
        statuses = {}  # offline: unknown -> treated as modified (fail-closed on fetch)
    elif a.pr and a.repo:
        paths, labels, statuses = pr_state(a.repo, a.pr)
    else:
        fail('need --pr/--repo or --files-list/--labels')

    globs = [normalize_glob(g) for g in load_globs()]
    # case-insensitive matching (belt-and-suspenders): no legit case-collisions
    # exist here, so lowering both sides only closes the odd-casing path and can
    # never widen a hole.
    hits = sorted({p for p in paths for g in globs if fnmatch.fnmatch(p.lower(), g.lower())})
    floor = 'T3' if hits else 'T0'

    # Any label in the tier: namespace counts toward "exactly one" - a malformed
    # tier:T4 next to a valid tier:T3 must FAIL, not be silently ignored.
    tierish = [l for l in labels if l.lower().startswith('tier:')]
    tier_labels = [l for l in tierish if re.fullmatch(r'tier:T[0-3]', l)]
    if len(tierish) != 1 or len(tier_labels) != 1:
        fail(f'exactly one valid tier:T0..T3 label required, found {tierish or "none"} '
             f'(gh pr edit <n> --add-label tier:T#)')
    declared = tier_labels[0].split(':')[1]

    # CHANGE.md cross-validation (vendored ticket must agree with the label):
    # (a) content comes from the PR HEAD via the contents API, never local disk -
    # under pull_request_target the checkout is the BASE ref, so a disk read would
    # silently see nothing; (b) FAIL-LOUD - a vendored CHANGE.md whose risk-tier
    # is missing/unparseable is a red check, not a silent skip.
    for p in paths:
        if os.path.basename(p) != 'CHANGE.md':
            continue
        if statuses.get(p) == 'removed':
            continue  # deleted at head - nothing to cross-validate
        if a.change_md is not None:  # offline test path
            content = open(a.change_md, encoding='utf-8', errors='replace').read()
        elif a.repo and a.head_sha:
            from urllib.parse import quote
            r = subprocess.run(['gh', 'api',
                                f'repos/{a.repo}/contents/{quote(p, safe="/")}?ref={a.head_sha}',
                                '-H', 'Accept: application/vnd.github.raw+json'],
                               capture_output=True, encoding='utf-8', errors='replace')
            if r.returncode != 0:
                # only a REMOVED file may 404; for an added/modified CHANGE.md a
                # 404 means our fetch is wrong - fail loud, never skip.
                fail(f'cannot fetch {p} (status {statuses.get(p, "unknown")!r}) at head: '
                     f'{r.stderr.strip()[:200]}')
            content = r.stdout
        else:
            fail(f'{p} present in the PR but no --head-sha to fetch it (fail closed)')
        m = re.search(r'^\s*-?\s*risk-tier:\s*(T[0-3])\b', content, re.M)
        if not m:
            fail(f'{p} is vendored but carries no parseable "risk-tier: T#" line - '
                 f'fail loud, never silently skip the cross-validation')
        if m.group(1) != declared:
            fail(f'{p} risk-tier {m.group(1)} != declared label {declared}')

    if RANK[declared] < RANK[floor]:
        fail(f'high-risk paths touched ({", ".join(hits[:5])}) -> floor {floor}; '
             f'declared {declared} is below it. Re-declare the tier (a reviewer may '
             f'raise, never silently lower).')

    if floor == 'T3' and 'release:human-ok' not in labels:
        fail('floor T3 requires the release:human-ok label - an interactive human '
             'applies it after review; automation never does.')

    print(f'tier-gate: PASS - declared {declared} >= floor {floor}'
          + (f' (high-risk: {", ".join(hits[:5])})' if hits else ' (no high-risk paths)'))
    sys.exit(0)


if __name__ == '__main__':
    main()
