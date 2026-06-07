"""Broad catalog toolsets and source-family mapping."""

from __future__ import annotations

import re

BROAD_TOOLSETS = [
    "core",
    "markets",
    "fundamentals",
    "macro",
    "central_banks",
    "public_finance",
    "hedge_fund_intelligence",
    "environment",
    "digital_infra",
    "network",
    "physical_world",
    "news",
    "trade",
    "commodities",
    "crypto",
    "forex",
]

# Agent-facing one-liners surfaced by list_toolsets. digital_infra is
# explicitly framed as blockchain-only and cross-references network: in the
# 2026-06-07 field test a network engineer was misled into digital_infra
# while the Sugra Net Atlas endpoints sat invisible in core.
TOOLSET_DESCRIPTIONS: dict[str, str] = {
    "core": "Cross-domain reference, system, and uncategorized endpoints",
    "markets": "Equity quotes, prices, corporate events, and market calendars",
    "fundamentals": "Company financial statements and SEC filing analytics",
    "macro": "Macroeconomic indicators and statistical time series",
    "central_banks": "Central bank rates, balance sheets, and monetary statistics",
    "public_finance": "Government finance, treasury operations, and public debt",
    "hedge_fund_intelligence": "Institutional holdings, 13F filings, and fund positioning",
    "environment": "Climate, weather, air quality, and emissions data",
    "digital_infra": (
        "Blockchain and digital-economy metrics (on-chain charts, mining, "
        "mempool); internet and network infrastructure lives in the network toolset"
    ),
    "network": (
        "Sugra Net Atlas: IP and ASN intelligence, prefixes and routing "
        "history, geolocation, abuse contacts, internet measurements, TOR "
        "and outage data"
    ),
    "physical_world": "Airports, power plants, maritime traffic, and physical assets",
    "news": "News headlines, RSS feeds, and media analytics",
    "trade": "International trade flows, tariffs, and trade indicators",
    "commodities": "Commodity prices and futures positioning",
    "crypto": "Crypto asset prices, markets, and exchange data",
    "forex": "Foreign exchange rates and currency time series",
}

TAG_TOOLSET_MAP = {
    "assets": "markets",
    "central banks monetary": "central_banks",
    "commodities": "commodities",
    "crypto": "crypto",
    "digital economy": "digital_infra",
    "digital economy and infrastructure": "digital_infra",
    "economics": "macro",
    "environment": "environment",
    "finance": "markets",
    "forex": "forex",
    "fundamentals": "fundamentals",
    "global economy trade": "trade",
    "global economy and trade": "trade",
    "global news media": "news",
    "global news and media": "news",
    "government": "public_finance",
    "hedge fund intelligence": "hedge_fund_intelligence",
    "macro": "macro",
    "markets": "markets",
    "monetary authorities": "central_banks",
    "news": "news",
    "physical world": "physical_world",
    "public finance government": "public_finance",
    "public finance and government": "public_finance",
    "reference": "core",
    "sugra net atlas": "network",
    "trade": "trade",
}


def normalize_label(value: str) -> str:
    return "_".join(re.sub(r"[^a-z0-9]+", " ", value.strip().lower()).split())


def toolset_for_tags(tags: list[str]) -> str:
    """Map OpenAPI tags into the broad MCP gateway toolset list."""
    normalized_tags = [normalize_label(tag).replace("_", " ") for tag in tags]
    for tag in normalized_tags:
        if tag in TAG_TOOLSET_MAP:
            return TAG_TOOLSET_MAP[tag]
    return "core"


def _toolset_entry(name: str, count: int) -> dict[str, int | str]:
    entry: dict[str, int | str] = {"name": name, "endpoint_count": count}
    description = TOOLSET_DESCRIPTIONS.get(name)
    if description:
        entry["description"] = description
    return entry


def ordered_toolsets(counts: dict[str, int]) -> list[dict[str, int | str]]:
    known = [
        _toolset_entry(name, counts[name])
        for name in BROAD_TOOLSETS
        if counts.get(name, 0) > 0
    ]
    extras = [
        _toolset_entry(name, count)
        for name, count in sorted(counts.items())
        if name not in BROAD_TOOLSETS
    ]
    return [*known, *extras]
