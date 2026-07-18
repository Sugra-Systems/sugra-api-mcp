"""Broad catalog toolsets and source-family mapping."""

from __future__ import annotations

import re

BROAD_TOOLSETS = [
    "core",
    "markets",
    "technical_indicators",
    "fundamentals",
    "corporate_registry",
    "fixed_income",
    "funds",
    "predictions",
    "macro",
    "statistics",
    "central_banks",
    "public_finance",
    "hedge_fund_intelligence",
    "entity_screening",
    "environment",
    "energy",
    "hazards",
    "transport",
    "health",
    "real_estate",
    "research",
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
    "markets": (
        "Equity quotes, prices, indices, options analytics, earnings, "
        "insider activity, and market calendars"
    ),
    "technical_indicators": "Technical analysis indicators computed from market price history",
    "fundamentals": (
        "Company financial statements, SEC filing analytics, and "
        "regulatory filing events"
    ),
    "corporate_registry": "Legal entity identifiers, corporate hierarchies, and registry records",
    "fixed_income": "Treasury auctions, yield curves, secondary prices, and debt analytics",
    "funds": "ETF and fund holdings, flows, profiles, and sector weightings",
    "predictions": "Prediction market events, prices, orderbooks, trades, and settlements",
    "macro": "Macroeconomic indicators and statistical time series",
    "statistics": (
        "National statistical office data: prices, labor, population, "
        "and industry series"
    ),
    "central_banks": "Central bank rates, balance sheets, and monetary statistics",
    "public_finance": "Government finance, treasury operations, and public debt",
    "hedge_fund_intelligence": "Institutional holdings, 13F filings, and fund positioning",
    "entity_screening": (
        "Sugra Entity: sanctions and PEP screening, entity resolution, "
        "screening-corpus coverage and freshness (sources, max_age_hours, "
        "covered regimes), and wallet or ID screening"
    ),
    "environment": "Climate, weather, air quality, and emissions data",
    "energy": "Grid operating data, fuel mix, solar and PV output, and electricity prices",
    "hazards": (
        "Earthquakes, wildfires, tropical cyclones, tsunami messages, "
        "and multi-hazard alerts"
    ),
    "transport": (
        "Airports, aviation weather reports, marine and tide conditions, "
        "and vessel traffic"
    ),
    "health": "Global public health indicators, rankings, and country statistics",
    "real_estate": "Housing market data: home values, rents, inventory, and sales activity",
    "research": "Academic research: preprint and paper search, citations, and author lookups",
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
    # Air quality observations join the climate/weather surface, where the
    # /api/v1/air-quality endpoints already live - not the energy toolset.
    "air quality": "environment",
    "assets": "markets",
    # Catalog meta-endpoints (search/resolve reference data) stay in core:
    # they are cross-domain reference, not a data domain of their own.
    "catalog": "core",
    "central banks monetary": "central_banks",
    "commodities": "commodities",
    "corporate registry": "corporate_registry",
    "crypto": "crypto",
    "digital economy": "digital_infra",
    "digital economy and infrastructure": "digital_infra",
    "disasters and hazards": "hazards",
    "disasters hazards": "hazards",
    "earnings": "markets",
    "economics": "macro",
    "energy": "energy",
    # Cinema and box-office endpoints are deliberately core: too narrow for
    # a dedicated toolset and unrelated to any existing one.
    "entertainment": "core",
    "environment": "environment",
    "equities indices": "markets",
    "finance": "markets",
    "fixed income": "fixed_income",
    "forex": "forex",
    "fundamentals": "fundamentals",
    "funds and etfs": "funds",
    "funds etfs": "funds",
    # Geocoding is cross-domain place reference (resolve coordinates and
    # place names for any domain), not climate data - core, not environment.
    "geocoding": "core",
    "global economy trade": "trade",
    "global economy and trade": "trade",
    "global news media": "news",
    "global news and media": "news",
    "government": "public_finance",
    "health": "health",
    "hedge fund intelligence": "hedge_fund_intelligence",
    "insiders": "markets",
    "macro": "macro",
    "markets": "markets",
    "monetary authorities": "central_banks",
    "news": "news",
    "options": "markets",
    "physical world": "physical_world",
    "predictions": "predictions",
    "public finance government": "public_finance",
    "public finance and government": "public_finance",
    "real estate": "real_estate",
    "reference": "core",
    "research": "research",
    # SEC EDGAR filing browse, events, and ownership belong with the SEC
    # filing analytics already promised by the fundamentals description.
    "sec edgar": "fundamentals",
    "statistical agencies": "statistics",
    "sugra entity": "entity_screening",
    "sugra net atlas": "network",
    "sugra netatlas": "network",
    "technical indicators": "technical_indicators",
    "trade": "trade",
    "transportation": "transport",
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
