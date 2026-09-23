"""list_plans: the paid plans, their prices and limits, and a checkout link each.

The tool is bundled data: no network call, no API key, read-only and
closed-world. These tests pin the numbers, the link shape, the annotations,
and that it answers on a keyless process whose every outbound path fails.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re

import httpx
import pytest

from sugra_api_mcp import server
from sugra_api_mcp.errors import is_error_payload
from sugra_api_mcp.tools import plans

EXPECTED = {
    ("dev", "monthly"): (25, None, 5_000),
    ("dev", "annual"): (250, None, 5_000),
    ("pro", "monthly"): (59, None, 50_000),
    ("pro", "annual"): (499, 588, 50_000),
}

# The plan link route in the web app accepts exactly these values.
ROUTE_PLANS = {"dev", "pro"}
ROUTE_CADENCES = {"monthly", "annual"}


def _by_key(payload):
    return {(p["plan"], p["cadence"]): p for p in payload["plans"]}


def test_four_plans_with_prices_and_limits() -> None:
    payload = plans.plans_payload()
    by_key = _by_key(payload)
    assert len(payload["plans"]) == 4
    assert set(by_key) == set(EXPECTED)
    for key, (price, standard, limit) in EXPECTED.items():
        entry = by_key[key]
        assert entry["price_usd"] == price, key
        assert entry.get("standard_price_usd") == standard, key
        assert entry["daily_request_limit"] == limit, key
        assert entry["all_endpoints"] is True, key
        assert entry["auto_renews"] is False, key
    assert payload["currency"] == "USD"


def test_only_pro_annual_carries_a_standard_price() -> None:
    carrying = [
        key for key, entry in _by_key(plans.plans_payload()).items()
        if "standard_price_usd" in entry
    ]
    assert carrying == [("pro", "annual")]


def test_terms_follow_the_cadence() -> None:
    for (_plan, cadence), entry in _by_key(plans.plans_payload()).items():
        assert entry["term"] == ("one month" if cadence == "monthly" else "one year")


def test_checkout_links_use_the_agent_channel_on_the_plan_route() -> None:
    for (plan, cadence), entry in _by_key(plans.plans_payload()).items():
        assert plan in ROUTE_PLANS and cadence in ROUTE_CADENCES
        assert entry["checkout_url"] == (
            f"https://app.sugra.ai/subscribe/{plan}/{cadence}?channel=agent"
        )


def test_checkout_links_come_from_the_one_base_constant(monkeypatch) -> None:
    monkeypatch.setattr(plans, "CHECKOUT_BASE_URL", "https://example.test/buy")
    for (plan, cadence), entry in _by_key(plans.plans_payload()).items():
        assert entry["checkout_url"] == (
            f"https://example.test/buy/{plan}/{cadence}?channel=agent"
        )
    source = inspect.getsource(plans)
    assert source.count("https://app.sugra.ai") == 1


def test_instructions_cover_the_purchase_rules() -> None:
    text = " ".join(plans.plans_payload()["how_to_buy"]).lower()
    for phrase in (
        "give its checkout_url to your human",
        "sign up or sign in",
        "stripe checkout",
        "do not auto-renew",
        "one fixed year",
        "buy it again after the current month ends",
        "cannot buy again until that subscription ends",
        "us dollars",
        "every endpoint",
    ):
        assert phrase in text, phrase


def test_payload_is_not_an_error_and_is_json() -> None:
    payload = plans.plans_payload()
    assert not is_error_payload(payload)
    assert json.loads(json.dumps(payload)) == payload


def test_payload_is_a_fresh_copy_each_call() -> None:
    first = plans.plans_payload()
    first["plans"][0]["price_usd"] = 0
    first["how_to_buy"].clear()
    second = plans.plans_payload()
    assert second["plans"][0]["price_usd"] == 25
    assert second["how_to_buy"]


def test_copy_rules_in_module_source() -> None:
    # Code points built with chr() so this file itself carries neither.
    source = inspect.getsource(plans)
    assert chr(0x2014) not in source
    emoji = re.compile(
        "[" + chr(0x1F300) + "-" + chr(0x1FAFF) + chr(0x2600) + "-" + chr(0x27BF) + "]"
    )
    assert not emoji.search(source)


def _global_tool(name: str):
    import sugra_api_mcp.tools  # noqa: F401  (registers the tools)
    from sugra_api_mcp.server import mcp

    return next(t for t in asyncio.run(mcp.list_tools()) if t.name == name)


def test_registered_read_only_and_closed_world(monkeypatch) -> None:
    monkeypatch.setenv("SUGRA_API_KEY", "dummy")
    tool = _global_tool("list_plans")
    annotations = tool.annotations
    assert annotations is not None
    assert annotations.readOnlyHint is True
    assert annotations.destructiveHint is False
    assert annotations.idempotentHint is True
    assert annotations.openWorldHint is False
    assert annotations.title == "List plans"
    assert (tool.inputSchema or {}).get("properties", {}) == {}


def test_answers_keyless_with_every_outbound_path_failing(monkeypatch) -> None:
    """No key in the environment, and any attempt to reach the network or to
    build an API client fails the test: the tool must still answer."""
    from mcp.shared.memory import create_connected_server_and_client_session

    import sugra_api_mcp.tools  # noqa: F401
    from sugra_api_mcp.server import mcp

    monkeypatch.delenv("SUGRA_API_KEY", raising=False)
    monkeypatch.setattr(server, "_shared_client", None)

    def _no_client():
        raise AssertionError("list_plans must not build an API client")

    async def _no_network(*args, **kwargs):
        raise AssertionError("list_plans must not make a network call")

    # A from-import would bind the real accessor before the patch below, so
    # the module must not import it at all.
    assert "get_client" not in inspect.getsource(plans)
    monkeypatch.setattr(server, "get_client", _no_client)
    monkeypatch.setattr(httpx.AsyncClient, "send", _no_network)

    async def _call():
        async with create_connected_server_and_client_session(mcp) as session:
            return await session.call_tool("list_plans", {})

    result = asyncio.run(_call())
    assert result.isError is False
    assert result.structuredContent == plans.plans_payload()


def test_package_import_registers_it_on_a_clean_process() -> None:
    """The tools package import is what every transport runs at startup. A
    SUBPROCESS, because this test module imports plans directly, which would
    register the tool here even if the package import no longer did."""
    import os
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import asyncio\n"
        "import sugra_api_mcp.tools\n"
        "from sugra_api_mcp.server import mcp\n"
        "print(','.join(t.name for t in asyncio.run(mcp.list_tools())))\n"
    )
    env = dict(os.environ)
    env.pop("SUGRA_API_KEY", None)
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        check=True,
    )
    assert "list_plans" in out.stdout.strip().split(",")


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_direct_call_matches_payload(key) -> None:
    result = asyncio.run(plans.list_plans())
    assert _by_key(result)[key] == _by_key(plans.plans_payload())[key]
