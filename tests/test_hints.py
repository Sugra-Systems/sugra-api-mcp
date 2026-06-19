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


def test_live_proxy_families_are_slow() -> None:
    """gleif/comtrade/wits/wto/gfw proxy live upstreams per request (verified:
    no blob/snapshot reads in their API routers). Labeling them fast would be
    an affirmatively wrong hint - worse than no hint (GLEIF was measured at
    24.5s avg in prod before its timeout fix)."""
    for family in ("gleif", "comtrade", "wits", "wto", "gfw"):
        hints = hints_for(_endpoint(path=f"/api/v1/{family}/lookup"))
        assert hints["duration_class"] == "slow", family
        assert hints["max_concurrency"] == 2, family


def test_mixed_families_stay_fast_with_hedged_note() -> None:
    """sec/gdelt/maritime are mostly snapshot-backed with a few live paths -
    family-level slow would over-tag them. The fast note must hedge (no
    unconditional "snapshot-backed" claim)."""
    for family in ("sec", "gdelt", "maritime"):
        hints = hints_for(_endpoint(path=f"/api/v1/{family}/something"))
        assert hints["duration_class"] == "fast", family
        assert "can occasionally take longer" in hints["duration_note"]


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


def test_slow_weather_paths_are_slow() -> None:
    """BUG-3.2: the weather family is mixed. flood (GloFAS heavy per-request
    compute), climate (CMIP6), and nws (api.weather.gov live) carry a 30s
    client budget in the API and can approach the gateway timeout. Labeling
    them "fast" made an agent fire parallel calls with a short budget and hit
    502s on cold cells. They must be "slow"."""
    for path in (
        "/api/v1/weather/flood",
        "/api/v1/weather/flood/ensemble",
        "/api/v1/weather/climate/projection",
        "/api/v1/weather/nws/forecast",
        "/api/v1/weather/nws/forecast/hourly",
    ):
        hints = hints_for(_endpoint(path=path))
        assert hints["duration_class"] == "slow", path
        assert hints["max_concurrency"] == 2, path


def test_slow_path_match_is_segment_bounded() -> None:
    """A slow key matches a whole segment, not a prefix of a longer one - a
    hypothetical /weather/floodplain path must not inherit flood's slow class."""
    hints = hints_for(_endpoint(path="/api/v1/weather/floodplain/maps"))
    assert hints["duration_class"] == "fast"
    assert hints["max_concurrency"] == 4


def test_fast_weather_paths_stay_fast() -> None:
    """The rest of the weather family reads fast upstreams (forecast 10s,
    marine 15s, us 10s, air-quality 15s client budget) - they must NOT be
    over-tagged slow by the flood/climate/nws rule."""
    for path in (
        "/api/v1/weather/forecast",
        "/api/v1/weather/marine/forecast",
        "/api/v1/weather/marine/hourly",
        "/api/v1/weather/us/forecast",
        "/api/v1/air-quality/forecast",
    ):
        hints = hints_for(_endpoint(path=path))
        assert hints["duration_class"] == "fast", path
        assert hints["max_concurrency"] == 4, path
