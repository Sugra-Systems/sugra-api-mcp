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
    "physical_world",
    "news",
    "trade",
    "commodities",
    "crypto",
    "forex",
]

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


def ordered_toolsets(counts: dict[str, int]) -> list[dict[str, int | str]]:
    known = [
        {"name": name, "endpoint_count": counts[name]}
        for name in BROAD_TOOLSETS
        if counts.get(name, 0) > 0
    ]
    extras = [
        {"name": name, "endpoint_count": count}
        for name, count in sorted(counts.items())
        if name not in BROAD_TOOLSETS
    ]
    return [*known, *extras]
