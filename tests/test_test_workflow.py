"""The Test workflow only tests.

A merge to main runs the test matrix and nothing else: no job in test.yml
deploys, logs in to a cloud account or reads a repository secret. The job and
matrix names stay fixed because branch protection requires the checks they
produce (`test (3.12)`, `test (3.13)`, `test (3.14)`).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "test.yml"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _workflow() -> dict:
    return yaml.safe_load(_text())


def test_the_only_job_is_the_test_matrix() -> None:
    jobs = _workflow()["jobs"]

    assert list(jobs) == ["test"]
    # Any other matrix key, an include or exclude entry, or a job name would
    # rename the required checks.
    assert jobs["test"]["strategy"]["matrix"] == {"python-version": ["3.12", "3.13", "3.14"]}
    assert "name" not in jobs["test"]


def test_the_workflow_reads_the_repository_and_nothing_more() -> None:
    workflow = _workflow()

    assert workflow["permissions"] == {"contents": "read"}
    for name, job in workflow["jobs"].items():
        assert "permissions" not in job, name
        assert "environment" not in job, name


# Expression context names are case-insensitive, and the context is reached
# by dot or bracket access, or whole (toJSON(secrets), `secrets: inherit`).
SECRETS = re.compile(r"\bsecrets\b", re.IGNORECASE)
ID_TOKEN = re.compile(r"id-token", re.IGNORECASE)
FORBIDDEN_OWNERS = ("azure/", "appleboy/")


def _uses(workflow: dict) -> list[str]:
    """Every action or reusable workflow the jobs call, lowercased."""
    refs = []
    for job in workflow["jobs"].values():
        if "uses" in job:
            refs.append(str(job["uses"]))
        for step in job.get("steps", []):
            if "uses" in step:
                refs.append(str(step["uses"]))
    return [ref.strip().lower() for ref in refs]


def test_no_step_reaches_a_secret_or_a_server() -> None:
    text = _text()
    workflow = _workflow()

    assert not SECRETS.search(text)
    assert not ID_TOKEN.search(text)
    for uses in _uses(workflow):
        assert not uses.startswith(FORBIDDEN_OWNERS), uses
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            run = step.get("run", "")
            assert "ssh " not in run and "az " not in run, step.get("name")
