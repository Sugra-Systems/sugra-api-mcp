"""Agent-hint heuristics (MCP-Imp-3).

Hints are computed from path/method, not stored in the bundled catalog, so
these tests construct Endpoint models directly: the classification must hold
for any endpoint shape, not just fixture content.
"""

from __future__ import annotations

from sugra_api_mcp.catalog.hints import hints_for
from sugra_api_mcp.catalog.models import Endpoint


def _endpoint(method: str = "GET", path: str = "/api/v1/quotes/{symbol}/price") -> Endpoint:
    return Endpoint(operation_id="op", method=method, path=path)


def test_default_endpoint_is_fast_with_default_concurrency() -> None:
    hints = hints_for(_endpoint())

    assert hints["duration_class"] == "fast"
    assert hints["max_concurrency"] == 4
    assert "bulk_cost" not in hints
    assert hints["duration_note"]


def test_network_family_is_slow_with_low_concurrency() -> None:
    hints = hints_for(_endpoint(path="/api/v1/network/asn/{asn}/peers"))

    assert hints["duration_class"] == "slow"
    assert hints["max_concurrency"] == 2
    assert "bulk_cost" not in hints


def test_post_bulk_endpoint_is_heavy_serial_and_billed_per_item() -> None:
    hints = hints_for(_endpoint(method="POST", path="/api/v1/network/bulk/ip"))

    assert hints["duration_class"] == "heavy"
    assert hints["max_concurrency"] == 1
    # Billing fact: the API sets X-RateLimit-Cost to the body item count on
    # POST bulk endpoints - the hint must state 1 credit per item.
    assert "1 request credit per item" in hints["bulk_cost"]
    assert "X-RateLimit-Cost" in hints["bulk_cost"]


def test_get_endpoint_with_bulk_suffix_is_not_per_item_billed() -> None:
    """GET /api/v2/quotes/resolve/bulk resolves in one upstream round and is
    NOT per-item billed - only POST + /bulk/ paths get the heavy class."""
    hints = hints_for(_endpoint(path="/api/v2/quotes/resolve/bulk"))

    assert hints["duration_class"] == "fast"
    assert "bulk_cost" not in hints


def test_v2_path_family_is_parsed_after_version_segment() -> None:
    hints = hints_for(_endpoint(path="/api/v2/market/batch-quotes"))

    assert hints["duration_class"] == "fast"


def test_non_api_path_does_not_crash() -> None:
    hints = hints_for(_endpoint(path="/health"))

    assert hints["duration_class"] == "fast"
    assert hints["max_concurrency"] == 4
