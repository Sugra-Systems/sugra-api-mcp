# -*- coding: utf-8 -*-
"""Tests for the tier-gate three-state contract (OPS-5.2).

The regression being locked down: every unmet tier-gate state used to exit 1 and
fail the whole workflow run, which emailed "Run failed" for what is ordinarily
just work in progress - a missing tier:T# label, or a T3 floor waiting for a human
to apply release:human-ok. That was 181 failed runs across the org, almost none of
them broken code. The states must now be distinguishable:

  PASS    (0) -> completed/success   merge allowed
  PENDING (2) -> in_progress         blocks merge, NOT a failure, no alert
  FAULT   (3) -> completed/failure   blocks merge AND alerts

Run: python -m unittest test_tier_gate -v
"""
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tier_gate import (check_run_plan, EXIT_PASS,  # noqa: E402
                       EXIT_PENDING, EXIT_FAULT)

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'tier_gate.py')

# This file is VENDORED byte-identically into every repo that carries the gate,
# so nothing in it may assume one repo's layout. Both the low-risk sample path and
# the CI workflow filename are therefore DERIVED from the repo it runs in - the
# first version hardcoded a Laravel path and `deploy.yml`, and broke the moment it
# was copied to a Python repo whose CI file is named after its Azure app.
HIGH_RISK = '.github/workflows/anything.yml'  # every repo floors this path

# ONE definition of "this workflow wires the gate suite": the anchored step-name
# line. Used both to FIND the CI workflow and to extract the step from it, so the
# two can never disagree about what counts as wiring.
STEP_NAME_RE = re.compile(r'^      - name:.*CI-gate contract tests.*$', re.M)


def _samples():
    """Read the per-repo DECLARED samples.

    Not derived (review feedback): exhausting a fixed candidate list never proved a repo has
    no low-risk path, so ordinary floor patterns could silently skip real
    assertions. Declaring them removes the guesswork, and SampleDeclaration below
    validates each against the REAL gate - a wrong declaration FAILS, never skips.
    """
    path = os.path.join(HERE, 'gate-test-samples.txt')
    values = {}
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            values[k.strip()] = v.strip()
    missing = [k for k in ('low_risk', 'change_md') if not values.get(k)]
    if missing:
        raise AssertionError(
            'gate-test-samples.txt is missing ' + str(missing) + '. Every repo '
            'carrying this gate declares its own samples; see that file.')
    return values['low_risk'], values['change_md']


LOW_RISK, CHANGE_MD = _samples()


def run_gate(paths, labels):
    """Run the REAL script in offline mode -> (rc, stdout)."""
    with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False,
                                     encoding='utf-8') as f:
        f.write('\n'.join(paths) + '\n')
        listfile = f.name
    try:
        p = subprocess.run([sys.executable, SCRIPT, '--files-list', listfile,
                            '--labels', labels],
                           capture_output=True, text=True)
        return p.returncode, p.stdout + p.stderr
    finally:
        os.unlink(listfile)


class SampleDeclaration(unittest.TestCase):
    """The derived sample must be low risk ACCORDING TO THE GATE ITSELF - a
    derivation that disagrees with tier_gate.py would make every test below it
    assert the wrong thing. a reviewer caught exactly that: the tracked-file fallback
    returned subdirectory-relative names, so a floored workflow file read as low
    risk."""

    def test_the_declared_low_risk_path_is_low_risk_to_the_real_gate(self):
        rc, out = run_gate([LOW_RISK], 'tier:T0')
        self.assertEqual(rc, EXIT_PASS,
                         f'{LOW_RISK!r} was derived as low risk but the gate '
                         f'disagrees: {out.strip().splitlines()[0] if out.strip() else ""}')
        self.assertIn('no high-risk paths', out)

    def test_the_declared_change_md_is_usable_for_cross_validation(self):
        """Review feedback: low_risk had a validator and change_md did not - the same layer
        must pin both, or a stale change_md is caught only indirectly.

        Runs the gate WITH a ticket body: a CHANGE.md in the file list and no
        content to read is a FAULT by design, so validating the declaration means
        exercising it the way the cross-validation tests do.
        """
        self.assertEqual(os.path.basename(CHANGE_MD), 'CHANGE.md',
                         'change_md must end in CHANGE.md - the gate selects these '
                         'files by BASENAME, so anything else is never '
                         'cross-validated at all')
        with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False,
                                         encoding='utf-8') as lf:
            lf.write(CHANGE_MD + '\n')
            listfile = lf.name
        with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False,
                                         encoding='utf-8') as cf:
            cf.write('risk-tier: T0\n')
            changefile = cf.name
        try:
            p = subprocess.run([sys.executable, SCRIPT, '--files-list', listfile,
                                '--labels', 'tier:T0', '--change-md', changefile],
                               capture_output=True, text=True)
        finally:
            os.unlink(listfile)
            os.unlink(changefile)
        out = p.stdout + p.stderr
        self.assertEqual(p.returncode, EXIT_PASS,
                         f'{CHANGE_MD!r} is declared in gate-test-samples.txt but '
                         f'this repo floors it, so it raises the floor by its own '
                         f'presence and the agreeing-tier case cannot pass: '
                         f'{out.strip().splitlines()[0] if out.strip() else ""}')


class ExitCodeContract(unittest.TestCase):
    """tier-gate.yml maps these EXACT codes to check-run states; changing a value
    here silently breaks the workflow, so pin them."""

    def test_the_three_codes_are_stable(self):
        self.assertEqual((EXIT_PASS, EXIT_PENDING, EXIT_FAULT), (0, 2, 3))


class CheckRunPlan(unittest.TestCase):
    """The single source of truth the workflow fetches via --plan."""

    def test_pass_is_a_completed_success(self):
        self.assertEqual(check_run_plan(EXIT_PASS), ('completed', 'success'))

    def test_pending_is_in_progress_with_no_conclusion(self):
        self.assertEqual(check_run_plan(EXIT_PENDING), ('in_progress', None))

    def test_fault_is_a_completed_failure(self):
        self.assertEqual(check_run_plan(EXIT_FAULT), ('completed', 'failure'))

    def test_an_unexpected_code_maps_to_failure_never_success(self):
        for code in (1, 7, -1, '', None, 'x'):
            self.assertEqual(check_run_plan(code), ('completed', 'failure'))

    def test_the_plan_cli_prints_what_the_workflow_parses(self):
        for rc, expected in (('0', 'completed success'), ('2', 'in_progress'),
                             ('3', 'completed failure'), ('1', 'completed failure')):
            out = subprocess.run([sys.executable, SCRIPT, '--plan', rc],
                                 capture_output=True, text=True).stdout.strip()
            self.assertTrue(out.startswith(expected),
                            f'--plan {rc} -> {out!r}, expected to start {expected!r}')


class UnmetStatesArePending(unittest.TestCase):
    """The whole point: an unmet gate blocks, it does not alert. Each of these was
    a red workflow run and an email before OPS-5.2."""

    def test_a_missing_tier_label_is_pending(self):
        # the single biggest source: 181 failed runs org-wide were mostly this.
        rc, out = run_gate([LOW_RISK], '')
        self.assertEqual(rc, EXIT_PENDING)
        self.assertIn('PENDING', out)

    def test_two_tier_labels_are_pending(self):
        rc, out = run_gate([LOW_RISK], 'tier:T0,tier:T2')
        self.assertEqual(rc, EXIT_PENDING)

    def test_a_malformed_tier_label_is_pending_not_ignored(self):
        rc, _ = run_gate([LOW_RISK], 'tier:T9')
        self.assertEqual(rc, EXIT_PENDING)

    def test_declaring_below_the_floor_is_pending(self):
        rc, out = run_gate([HIGH_RISK], 'tier:T0')
        self.assertEqual(rc, EXIT_PENDING)
        self.assertIn('floor T3', out)

    def test_a_T3_floor_awaiting_human_ok_is_pending(self):
        # "waiting for a human" is the clearest case of a normal in-progress state.
        rc, out = run_gate([HIGH_RISK], 'tier:T3')
        self.assertEqual(rc, EXIT_PENDING)
        self.assertIn('release:human-ok', out)

    def test_pending_emits_a_notice_never_an_error(self):
        _, out = run_gate([LOW_RISK], '')
        self.assertIn('::notice::', out)
        self.assertNotIn('::error::', out)


class SatisfiedStatesPass(unittest.TestCase):
    def test_a_low_risk_pr_with_a_tier_label_passes(self):
        rc, out = run_gate([LOW_RISK], 'tier:T0')
        self.assertEqual(rc, EXIT_PASS)
        self.assertIn('PASS', out)

    def test_a_high_risk_pr_with_T3_and_human_ok_passes(self):
        rc, _ = run_gate([HIGH_RISK], 'tier:T3,release:human-ok')
        self.assertEqual(rc, EXIT_PASS)

    def test_the_gate_still_blocks_what_it_always_blocked(self):
        """Strength is unchanged: everything that used to be non-zero still is.
        Only the MEANING of the non-zero code changed."""
        for paths, labels in (([LOW_RISK], ''), ([HIGH_RISK], 'tier:T0'),
                              ([HIGH_RISK], 'tier:T3'), ([LOW_RISK], 'tier:T0,tier:T1')):
            rc, _ = run_gate(paths, labels)
            self.assertNotEqual(rc, EXIT_PASS,
                                f'{paths} {labels!r} must not pass')


class VendoredChangeMdCrossValidation(unittest.TestCase):
    """The vendored-ticket cross-validation. Each case asserts the REASON in the
    output, not only the exit code (review feedback): a below-floor result also yields
    PENDING, so a code-only assertion would pass even when the cross-validation
    never ran at all."""

    def _run(self, body, labels='tier:T0', path=None):
        path = path or CHANGE_MD
        with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False,
                                         encoding='utf-8') as lf:
            lf.write(path + '\n')
            listfile = lf.name
        with tempfile.NamedTemporaryFile('w', suffix='.md', delete=False,
                                         encoding='utf-8') as cf:
            cf.write(body)
            changefile = cf.name
        try:
            p = subprocess.run([sys.executable, SCRIPT, '--files-list', listfile,
                                '--labels', labels, '--change-md', changefile],
                               capture_output=True, text=True)
            return p.returncode, p.stdout + p.stderr
        finally:
            os.unlink(listfile)
            os.unlink(changefile)

    def test_an_agreeing_risk_tier_passes(self):
        rc, out = self._run('risk-tier: T0\n')
        self.assertEqual(rc, EXIT_PASS, out)

    def test_a_disagreeing_risk_tier_is_pending_for_that_reason(self):
        rc, out = self._run('risk-tier: T2\n')
        self.assertEqual(rc, EXIT_PENDING, out)
        self.assertIn('risk-tier T2 != declared label T0', out,
                      'PENDING alone is not enough - a below-floor result gives '
                      'the same code, so the reason must name the mismatch')

    def test_an_unparseable_risk_tier_is_pending_and_says_so(self):
        rc, out = self._run('no tier line here\n')
        self.assertEqual(rc, EXIT_PENDING, out)
        self.assertIn('no parseable', out)

    def test_the_cross_validation_runs_on_a_nested_change_md(self):
        """The gate keys on the BASENAME, so a nested path must cross-validate
        too - and the assertion must prove it did, not merely that something
        blocked (review feedback)."""
        rc, out = self._run('risk-tier: T2\n', path='ops/changes/CHANGE.md')
        self.assertEqual(rc, EXIT_PENDING, out)
        self.assertIn('risk-tier T2 != declared label T0', out)


class FaultsStillAlert(unittest.TestCase):
    def test_a_broken_invocation_is_a_fault_not_pending(self):
        # no --pr/--repo and no offline args: the caller is broken, which is a
        # genuine fault and must alert, unlike an unmet gate.
        p = subprocess.run([sys.executable, SCRIPT], capture_output=True, text=True)
        self.assertEqual(p.returncode, EXIT_FAULT)
        self.assertIn('::error::', p.stdout + p.stderr)


class WorkflowContract(unittest.TestCase):
    """Text tripwires on tier-gate.yml: the Python contract is only half the gate,
    and a broken rc->state mapping in the yml passes every test above."""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(HERE, 'tier-gate.yml'), encoding='utf-8') as f:
            cls.yml = f.read()

    def test_the_job_is_renamed_so_its_auto_check_is_not_the_context(self):
        import re
        self.assertRegex(self.yml, r'\n  tier-gate-eval:\s*\n')
        self.assertNotRegex(self.yml, r'\n  tier-gate:\s*\n',
                            'a job named tier-gate would BE the required context '
                            'and go green on exit 0, opening the gate')

    def test_the_required_context_is_named_tier_gate(self):
        self.assertIn("-f name='tier-gate'", self.yml)

    def test_the_report_step_uses_the_single_source_mapping(self):
        self.assertIn('tier_gate.py --plan "${rc:-3}"', self.yml)
        self.assertRegex(self.yml,
                         r'\[ "\$\{CONCLUSION:-\}" = "success" \] \|\| CONCLUSION=\'failure\'')
        self.assertRegex(self.yml, r"\*\)\s*STATUS='completed';\s*CONCLUSION='failure'")

    def test_a_pending_baseline_is_posted_before_evaluating(self):
        self.assertRegex(self.yml, r"in_progress'[\s\S]*Evaluate the tier floor",
                         'the in_progress baseline must precede the evaluation')

    def test_the_report_step_runs_even_when_evaluation_aborts(self):
        self.assertIn('!cancelled()', self.yml)

    def test_superseded_runs_are_not_cancelled(self):
        self.assertRegex(self.yml, r'cancel-in-progress:\s*false')

    def test_only_an_unexpected_or_fault_code_fails_the_run(self):
        self.assertIn("env.rc != '0' && env.rc != '2'", self.yml)

    def test_pull_request_target_safety_is_preserved(self):
        self.assertIn('pull_request_target', self.yml)
        self.assertNotIn('ref: ${{ github.event.pull_request.head', self.yml)


class CiWiring(unittest.TestCase):
    """a reviewer S1: the gate suite is only worth something if the REQUIRED test job
    actually runs it - and the first attempt would have done the opposite, failing
    that job for every PR. A CI job may set defaults.run.working-directory (WEB's
    sets sugra-webSITE), and then any path relative to the repo root is wrong -
    which is how this nearly shipped a CI outage. The step must be explicit."""

    @classmethod
    def setUpClass(cls):
        # Find the CI workflow BY ITS CONTENT, not by filename: this file is
        # vendored into repos whose CI workflow is called deploy.yml,
        # main_api-sugra-ingest.yml, and other names. Hardcoding one name made
        # the suite error out on the first repo it was copied to.
        # Collect EVERY workflow carrying the marker, not the first alphabetically
        # (review feedback): a repo with two matching files - a template, a backup, a second
        # pipeline - would otherwise validate the wrong one and leave the real
        # gate wiring unchecked. Exactly one is the contract; anything else is
        # reported as such rather than silently resolved.
        # Match the anchored STEP-NAME line, not the bare phrase (review feedback): a
        # workflow that merely MENTIONS the marker in a comment would otherwise
        # count as a wiring, producing false ambiguity and a cascade of confusing
        # assertion failures. Same regex the step extraction uses below, so the
        # two cannot disagree about what counts as wiring.
        matches = []
        for name in sorted(os.listdir(HERE)):
            if not name.endswith(('.yml', '.yaml')):
                continue
            with open(os.path.join(HERE, name), encoding='utf-8') as f:
                text = f.read()
            if STEP_NAME_RE.search(text):
                matches.append((name, text))
        cls.matches = [n for n, _ in matches]
        # Populate from the first match even when there are SEVERAL (review feedback): blanking
        # it made every other assertion fail with "no CI workflow runs the gate
        # tests", which is misleading - the workflow exists, there are simply two of
        # them. The dedicated ambiguity test below reports the real problem, and the
        # remaining assertions still say something useful about the first match.
        cls.yml = matches[0][1] if matches else ''
        cls.ci_file = matches[0][0] if matches else None
        # Extraction is deliberately defensive: a tripwire that fires on a
        # harmless edit fails the REQUIRED test job, which is the same class of
        # damage this card exists to remove. Three brittleness modes a reviewer found and
        # each fix (text, not a YAML parse, so the suite needs no PyYAML on the
        # runner):
        #   - anchor on the step's NAME LINE, not a bare phrase: a comment
        #     elsewhere mentioning the phrase would otherwise split first and
        #     yield an empty extraction that fails every assertion;
        #   - bound at ANY next step (`- ` at step indent), not only a named one,
        #     so a bare `uses:` step is not gobbled in with its code;
        #   - strip INLINE comments too, not just whole-line ones, so prose
        #     quoting a defective form cannot trip an assertNotIn.
        m = STEP_NAME_RE.search(cls.yml)
        cls.has_step = m is not None
        if cls.has_step:
            rest = cls.yml[m.end():]
            nxt = re.search(r'\n      - |\n  \w[\w-]*:', rest)
            cls.step = rest[:nxt.start()] if nxt else rest
        else:
            cls.step = ''
        cls.code = '\n'.join(
            stripped for stripped in
            (l.split('#', 1)[0].rstrip() for l in cls.step.splitlines())
            if stripped)

    def test_exactly_one_workflow_wires_the_gate_suite(self):
        """Ambiguity is a defect in its own right: with two matching workflows the
        suite cannot know which one actually gates merges."""
        self.assertEqual(len(self.matches), 1,
                         f'expected exactly one CI workflow carrying the gate-test '
                         f'step, found {self.matches or "none"}. A repo that vendors '
                         f'this gate must also wire it - and wire it once.')

    def test_the_required_test_job_runs_the_gate_suite(self):
        self.assertTrue(self.has_step,
                        'a CI workflow must run the gate tests inside the required '
                        'test job, or a broken gate merges with a green CI')

    def test_the_extraction_itself_is_not_silently_empty(self):
        """Review feedback: an extraction that returns '' makes every other assertion in this
        class fail for a reason that has nothing to do with the defect they guard.
        Fail HERE, with a message naming the real cause, instead."""
        self.assertTrue(self.step.strip(),
                        'the step extraction is empty - the anchor or the bound '
                        'no longer matches the CI workflow, fix the extraction')
        self.assertTrue(self.code.strip(),
                        'the step has no executable lines after comment stripping')

    def test_the_step_does_not_inherit_the_job_working_directory(self):
        # The exact defect a reviewer caught: `cd .github/workflows` under a job whose
        # job sets a default cwd resolves to a path that does not exist.
        head = self.code
        self.assertIn('working-directory: ${{ github.workspace }}/.github/workflows',
                      head,
                      'the step must set an absolute working-directory that '
                      'overrides any job-level working-directory default')
        # Check the COMMAND POSITION, not a substring anywhere (review feedback): comment
        # stripping is a naive split on '#' and would mangle a '#' inside a
        # string, so do not depend on it for the security-relevant assertion. A
        # real `cd` is the first token of its line; a mention inside prose or a
        # trailing comment never is.
        offenders = [l for l in self.step.splitlines()
                     if l.strip().startswith('cd ')
                     and '.github/workflows' in l]
        self.assertEqual(offenders, [],
                         'a relative cd resolves against the job default and would '
                         f'fail the required test job for every PR: {offenders}')

    def test_the_step_discovers_rather_than_naming_modules(self):
        self.assertIn("unittest discover", self.code)

    def test_the_step_asserts_a_floor_so_it_cannot_pass_vacuously(self):
        self.assertRegex(self.code, r'-lt\s+\d+')


if __name__ == '__main__':
    unittest.main()
