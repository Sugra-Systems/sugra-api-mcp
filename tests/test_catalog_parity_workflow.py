"""Catalog drift on main ends in a resync PR and a green run.

catalog-parity.yml opened or updated the resync PR on drift and then failed the
run anyway, so every spec change turned the run red although nothing was
broken. The checker now reports drift as a step output (--drift-output), and
the run is red only when the check could not run or the resync failed. These
tests pin both paths: the checker's output contract and the workflow wiring.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from sugra_api_mcp.catalog.builder import build_catalog_from_openapi

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "openapi_minimal.json"
CHECKER = REPO_ROOT / "scripts" / "check_catalog_parity.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "catalog-parity.yml"

# GITHUB_OUTPUT already exists and may hold earlier outputs when the step runs.
EARLIER = "earlier=kept\n"


def _fixture_spec() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _output_file(tmp_path: Path) -> Path:
    output = tmp_path / "github_output"
    output.write_text(EARLIER, encoding="utf-8")
    return output


def _run_checker(spec: Path, bundle: Path, output: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), "--spec", str(spec), "--bundle", str(bundle),
         "--drift-output", str(output)],
        capture_output=True, text=True, timeout=300,
    )


def test_drift_is_reported_as_an_output_and_the_step_stays_green(tmp_path) -> None:
    bundle = build_catalog_from_openapi(_fixture_spec(), source="unit").to_dict()
    dropped = bundle["endpoints"].pop(0)["operation_id"]
    bundle["endpoint_count"] = len(bundle["endpoints"])
    output = _output_file(tmp_path)

    result = _run_checker(_write(tmp_path / "spec.json", _fixture_spec()),
                          _write(tmp_path / "endpoints.json", bundle), output)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRIFT" in result.stdout
    assert dropped in result.stdout
    assert output.read_text(encoding="utf-8") == EARLIER + "drift=true\n"


def test_a_matching_bundle_reports_no_drift(tmp_path) -> None:
    bundle = build_catalog_from_openapi(_fixture_spec(), source="unit").to_dict()
    output = _output_file(tmp_path)

    result = _run_checker(_write(tmp_path / "spec.json", _fixture_spec()),
                          _write(tmp_path / "endpoints.json", bundle), output)

    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text(encoding="utf-8") == EARLIER + "drift=false\n"


def test_a_check_that_cannot_run_stays_red_and_reports_nothing(tmp_path) -> None:
    """A wrong spec path is a broken gate, not drift: the step fails and the
    resync job, which needs drift=true, does not run."""
    bundle = build_catalog_from_openapi(_fixture_spec(), source="unit").to_dict()
    output = _output_file(tmp_path)

    result = _run_checker(tmp_path / "definitely_not_here.json",
                          _write(tmp_path / "endpoints.json", bundle), output)

    assert result.returncode == 1, result.stdout
    assert "FAIL" in result.stdout
    assert output.read_text(encoding="utf-8") == EARLIER


def test_a_crashed_check_stays_red_and_reports_nothing(tmp_path) -> None:
    bundle = tmp_path / "endpoints.json"
    bundle.write_text("{not json", encoding="utf-8")
    output = _output_file(tmp_path)

    result = _run_checker(_write(tmp_path / "spec.json", _fixture_spec()), bundle, output)

    assert result.returncode != 0
    assert "Traceback" in result.stderr
    assert output.read_text(encoding="utf-8") == EARLIER


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def test_the_parity_job_takes_drift_from_the_checker_output() -> None:
    parity = _jobs()["parity"]
    step = next(s for s in parity["steps"] if s.get("id") == "parity")
    outside_main = next(s for s in parity["steps"]
                        if s.get("name") == "Fail the job on drift outside main")

    assert step["run"] == 'python scripts/check_catalog_parity.py --drift-output "$GITHUB_OUTPUT"'
    # Nothing may swallow a failed step: a check that cannot run fails the job.
    assert not any("continue-on-error" in s for s in parity["steps"])
    assert parity["outputs"]["drift"] == "${{ steps.parity.outputs.drift == 'true' }}"
    # Outside main no PR is opened, so drift still fails the run there.
    assert outside_main["if"] == (
        "steps.parity.outputs.drift == 'true' && github.ref != 'refs/heads/main'")
    assert "exit 1" in outside_main["run"]


def test_the_resync_job_ends_green_with_a_notice_after_the_pr() -> None:
    resync = _jobs()["resync"]
    steps = resync["steps"]
    names = [s.get("name") for s in steps]

    assert resync["if"] == (
        "needs.parity.outputs.drift == 'true' && github.ref == 'refs/heads/main'")
    assert not any("exit 1" in s.get("run", "") for s in steps)
    # A failed token fetch or PR step fails the job: nothing swallows it.
    assert not any("continue-on-error" in s for s in steps)
    # The notice is the last step, after the PR step, with no `if`: it runs
    # only when every earlier step succeeded.
    assert names.index("Open or update a catalog resync PR") < len(steps) - 1
    assert steps[-1]["run"].startswith('echo "::notice title=Catalog drift::')
    assert "if" not in steps[-1]
