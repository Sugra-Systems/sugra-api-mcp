"""Plan list for agents: the paid Sugra API plans and a checkout link for each.

``list_plans`` is static data shipped in the package. It makes no network call
and needs no API key, so it answers on every transport, keyless stdio
included. It only returns links; opening a link and paying is the human's
step, in their own browser.
"""

from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations

from ..observability import trace_mcp_tool
from ..server import mcp

# The one place the checkout address lives. Every link below is built from it.
CHECKOUT_BASE_URL = "https://app.sugra.ai/subscribe"

# Marks a checkout as bought through an agent: agent prices, no auto-renewal.
AGENT_CHANNEL = "agent"

CURRENCY = "USD"

# plan id, display name, daily request limit
_PLANS: tuple[tuple[str, str, int], ...] = (
    ("dev", "Dev", 5_000),
    ("pro", "Pro", 50_000),
)

# (plan id, cadence) -> price in USD through the agent link, and the standard
# price where the agent price differs from it.
_PRICES: dict[tuple[str, str], tuple[int, int | None]] = {
    ("dev", "monthly"): (25, None),
    ("dev", "annual"): (250, None),
    ("pro", "monthly"): (59, None),
    ("pro", "annual"): (499, 588),
}

_TERMS = {
    "monthly": "one month",
    "annual": "one year",
}

HOW_TO_BUY: tuple[str, ...] = (
    "Pick a plan and give its checkout_url to your human. The account and "
    "the payment belong to them.",
    "They open the link, sign up or sign in to Sugra (email or Google), and "
    "pay in Stripe Checkout.",
    "Purchases through these links do not auto-renew.",
    "Annual is one payment for one fixed year.",
    "Monthly is one payment for one month. To keep the plan, buy it again "
    "after the current month ends. An account with a live subscription "
    "cannot buy again until that subscription ends.",
    "Prices are in US dollars.",
    "Every plan includes every endpoint. Plans differ only in the daily "
    "request limit.",
)


def checkout_url(plan: str, cadence: str) -> str:
    """The agent checkout link for one plan and cadence."""
    return f"{CHECKOUT_BASE_URL}/{plan}/{cadence}?channel={AGENT_CHANNEL}"


def plans_payload() -> dict[str, Any]:
    """The four paid plans with prices, limits, terms and checkout links."""
    plans: list[dict[str, Any]] = []
    for plan, name, daily_limit in _PLANS:
        for cadence in ("monthly", "annual"):
            price, standard_price = _PRICES[(plan, cadence)]
            entry: dict[str, Any] = {
                "plan": plan,
                "name": name,
                "cadence": cadence,
                "price_usd": price,
            }
            if standard_price is not None:
                entry["standard_price_usd"] = standard_price
            entry.update(
                {
                    "term": _TERMS[cadence],
                    "auto_renews": False,
                    "daily_request_limit": daily_limit,
                    "all_endpoints": True,
                    "checkout_url": checkout_url(plan, cadence),
                }
            )
            plans.append(entry)
    return {
        "currency": CURRENCY,
        "plans": plans,
        "how_to_buy": list(HOW_TO_BUY),
    }


# Read-only like every other tool, but closed-world: the answer is bundled
# data, so the call reaches nothing outside this process.
PLANS_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
    title="List plans",
)


@mcp.tool(annotations=PLANS_TOOL_ANNOTATIONS)
@trace_mcp_tool("list_plans")
async def list_plans() -> dict[str, Any]:
    """List the paid Sugra API plans with prices, limits and checkout links.

    Four plans: Dev and Pro, each monthly or annual, priced in US dollars.
    Every plan includes every endpoint; plans differ only in the daily
    request limit. Give the chosen plan's checkout_url to your human: they
    sign up or sign in and pay in Stripe Checkout. Purchases through these
    links do not auto-renew: annual covers one fixed year, and monthly is
    bought again after the current month ends. This tool makes no network
    call and needs no API key.
    """
    return plans_payload()
