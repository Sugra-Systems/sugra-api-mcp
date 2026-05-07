"""CLI tests."""

from __future__ import annotations

import subprocess
import sys


def test_cli_search_outputs_operation_id() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "sugra_api_mcp", "search", "NASDAQ futures"],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "operation_id" in result.stdout
    assert "cot_financial" in result.stdout
