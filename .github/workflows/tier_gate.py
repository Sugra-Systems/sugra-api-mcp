# tier-gate : mechanical tier-floor check for a single-identity org.
#
# Enforces FLEET-PROTOCOL section 3 as a required STATUS CHECK (approvals cannot
# gate here: required_approving_review_count must stay 0 - no second identity
# exists to approve). Logic:
#   floor    = T3 if the PR touches any HIGH-RISK path (tier-floor-paths.txt), else T0
#   declared = the single `tier:T#` label on the PR (PENDING if 0 or >1)
#   PENDING if declared < floor ("high-risk paths require >= floor")
#   PENDING if floor == T3 and the `release:human-ok` label is absent
#            (human release is a label event the loop's code path never emits)
#   If the PR vendors a CHANGE.md, its `risk-tier:` must equal the declared label.
#
# those unmet states are PENDING, not FAILURE. Every one of them means
# "the author or a human has not acted yet" - the label is missing, the human-ok
# is awaited, a declared value needs adjusting. They block merge (the posted
# check-run is `in_progress`, which is not `success`) WITHOUT failing the workflow
# run, so they no longer email "Run failed" on every push. Only a genuine system
# or caller fault (config unreadable, gh failed, CHANGE.md unfetchable at head)
# fails the run and alerts. The dangerous state is still a check that never
# reports, not a blocking one - so path logic lives HERE, never in the workflow's
# `on.paths` (a path-filtered workflow would starve low-risk PRs of the report and
# block them forever once the check is required).
#
# Lives inside .github/workflows/ on purpose: it is the only path the deploy
# workflow's push paths-ignore exempts, so changes here never trigger a deploy.
# GitHub only parses *.yml/*.yaml in this dir as workflows.
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


# Exit codes . The
# workflow maps each to the `tier-gate` CHECK-RUN it posts on the PR head:
#   0 -> completed/success     the declared tier clears the floor.
#   2 -> in_progress (pending) a NORMAL in-progress state: the tier label is not
#              applied yet, release:human-ok is still awaited, or a declared value
#              needs adjusting. It BLOCKS merge (in_progress is not success) but it
#              is NOT a failure - it must not be emitted as a failed workflow run
#              that emails "Run failed" on every push while the author or a human
#              simply has not acted yet. This state was 181 failed runs across the
#              org, almost none of them broken code.
#   3 -> completed/failure     a genuine system/caller fault: config unreadable,
#              gh call failed, CHANGE.md unfetchable at head, bad invocation. This
#              is the ONLY state that alerts.
EXIT_PASS = 0
EXIT_PENDING = 2
EXIT_FAULT = 3


def check_run_plan(exit_code):
    """Single source of truth for exit-code -> check-run state, used by the unit
    tests AND by the workflow (via `--plan`), so the two cannot drift. Returns
    (status, conclusion); conclusion is None for pending. Anything that is not
    pass (0) or pending (2) maps to a completed FAILURE, never success."""
    try:
        code = int(str(exit_code).strip())
    except (TypeError, ValueError):
        code = EXIT_FAULT
    if code == EXIT_PASS:
        return ('completed', 'success')
    if code == EXIT_PENDING:
        return ('in_progress', None)
    return ('completed', 'failure')


def pending(msg):
    """A normal not-yet-satisfied state: blocks merge, does NOT alert.

    Classification tradeoff, named rather than implied (review feedback). Two of the five
    pending states - declaring BELOW the floor, and a vendored CHANGE.md whose
    risk-tier disagrees with the label - read less like "the human has not acted
    yet" and more like a mis-declaration, which in an agent fleet is the shape a
    tier-gaming attempt would take. They are pending anyway, deliberately: both
    still BLOCK the merge, both are stated in full on the PR's own check, and both
    are overwhelmingly ordinary author error in practice. Making them alert would
    reintroduce exactly the noise this card removes, for a signal already visible
    where the decision is made. If a real gaming attempt is ever observed, split
    these two into fault() - the machinery is one function call away."""
    print(f'tier-gate: PENDING - {msg}')
    print('::notice::tier-gate not yet satisfied - this blocks merge but is a '
          'normal in-progress state, not a failure')
    sys.exit(EXIT_PENDING)


def fault(msg):
    """A genuine system or caller fault: blocks merge AND alerts."""
    print(f'tier-gate: FAULT - {msg}')
    print('::error::tier-gate could not evaluate - a real failure, unlike a '
          'pending tier-gate')
    sys.exit(EXIT_FAULT)


def load_globs():
    globs = []
    try:
        for line in open(PATHS_FILE, encoding='utf-8'):
            line = line.strip()
            if line and not line.startswith('#'):
                globs.append(line)
    except OSError as e:
        fault(f'cannot read {PATHS_FILE}: {e}')  # config unreadable = red, never skip
    if not globs:
        fault(f'{PATHS_FILE} is empty - the floor would silently vanish')
    return globs


def gh_json(args):
    r = subprocess.run(['gh'] + args, capture_output=True, encoding='utf-8', errors='replace')
    if r.returncode != 0:
        fault(f'gh {" ".join(args[:3])}... failed: {r.stderr.strip()[:300]}')
    try:
        return json.loads(r.stdout)
    except ValueError as e:
        fault(f'gh output not JSON: {e}')


def pr_state(repo, pr):
    # --slurp wraps each page's array into one outer array ([[...],[...]]) so
    # multi-page output stays valid JSON. Bare --paginate CONCATENATES the page
    # arrays ("[...][...]"), json.loads fails, and every large PR would be stuck
    # red regardless of labels (review feedback - a lockout, not a gate).
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
    """Review feedback: a bare directory entry (`dir/`) never matches children under
    fnmatch - normalize it to `dir/**` so config editors cannot silently no-op."""
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
        fault('need --pr/--repo or --files-list/--labels')

    globs = [normalize_glob(g) for g in load_globs()]
    # case-insensitive matching (review feedback, belt-and-suspenders): no legit
    # case-collisions exist in these repos, so lowering both sides only closes
    # the odd-casing path and can never widen a hole.
    hits = sorted({p for p in paths for g in globs if fnmatch.fnmatch(p.lower(), g.lower())})
    floor = 'T3' if hits else 'T0'

    # Any label in the tier: namespace counts toward "exactly one" - a malformed
    # tier:T4 next to a valid tier:T3 must FAIL, not be silently ignored (review feedback).
    tierish = [l for l in labels if l.lower().startswith('tier:')]
    tier_labels = [l for l in tierish if re.fullmatch(r'tier:T[0-3]', l)]
    if len(tierish) != 1 or len(tier_labels) != 1:
        pending(f'exactly one valid tier:T0..T3 label required, found {tierish or "none"} '
             f'(gh pr edit <n> --add-label tier:T#)')
    declared = tier_labels[0].split(':')[1]

    # CHANGE.md cross-validation (vendored ticket must agree with the label).
    # Review feedback: (a) content comes from the PR HEAD via the contents API, never
    # local disk - under pull_request_target the checkout is the BASE ref, so a
    # disk read would silently see nothing; (b) FAIL-LOUD - a vendored CHANGE.md
    # whose risk-tier is missing/unparseable is a red check, not a silent skip.
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
                # Review feedback: only a REMOVED file may 404; for an added/modified
                # CHANGE.md a 404 means our fetch is wrong - fail loud, never skip.
                fault(f'cannot fetch {p} (status {statuses.get(p, "unknown")!r}) at head: '
                     f'{r.stderr.strip()[:200]}')
            content = r.stdout
        else:
            fault(f'{p} present in the PR but no --head-sha to fetch it (fail closed)')
        m = re.search(r'^\s*-?\s*risk-tier:\s*(T[0-3])\b', content, re.M)
        if not m:
            pending(f'{p} is vendored but carries no parseable "risk-tier: T#" line - '
                 f'fail loud, never silently skip the cross-validation')
        if m.group(1) != declared:
            pending(f'{p} risk-tier {m.group(1)} != declared label {declared}')

    if RANK[declared] < RANK[floor]:
        pending(f'high-risk paths touched ({", ".join(hits[:5])}) -> floor {floor}; '
             f'declared {declared} is below it. Re-declare the tier (a Reviewer may '
             f'raise, never silently lower - FLEET-PROTOCOL section 3).')

    if floor == 'T3' and 'release:human-ok' not in labels:
        pending('floor T3 requires the release:human-ok label - an interactive human '
             'applies it after review convergence; the loop never does.')

    print(f'tier-gate: PASS - declared {declared} >= floor {floor}'
          + (f' (high-risk: {", ".join(hits[:5])})' if hits else ' (no high-risk paths)'))
    sys.exit(EXIT_PASS)


if __name__ == '__main__':
    # `--plan <rc>` prints "<status> <conclusion>" for the workflow's Report step,
    # so the exit-code -> check-run mapping has exactly one definition.
    if len(sys.argv) >= 3 and sys.argv[1] == '--plan':
        _status, _conclusion = check_run_plan(sys.argv[2])
        print(f'{_status} {_conclusion or ""}')
        sys.exit(0)
    main()
