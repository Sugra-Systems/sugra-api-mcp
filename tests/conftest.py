"""Checks that run around every test."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from sugra_api_mcp import observability

# The variables setup_observability writes into os.environ when telemetry is
# configured. A value one test left behind would reach every later test in the
# process and every server a test starts as a child process.
_TELEMETRY_VARIABLES = (
    "OTEL_RESOURCE_ATTRIBUTES",
    "OTEL_SERVICE_NAME",
    *observability._TRACE_OVERRIDE_VARS,
)


@pytest.fixture(autouse=True)
def _telemetry_environment_restored(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fail a test that leaves a telemetry variable changed, after putting it back.

    An autouse fixture of this conftest is set up before the test's own fixtures
    and torn down after them, so the check sees the environment once monkeypatch
    has undone what it recorded.
    """
    before = {name: os.environ.get(name) for name in _TELEMETRY_VARIABLES}
    yield
    changed = [name for name, value in before.items() if os.environ.get(name) != value]
    for name in changed:
        value = before[name]
        if value is None:
            del os.environ[name]
        else:
            os.environ[name] = value
    if changed:
        pytest.fail(f"{request.node.nodeid} left {', '.join(changed)} changed in os.environ")
