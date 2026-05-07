"""Search aliases for common user phrases."""

from __future__ import annotations

ALIASES = {
    "nasdaq futures": ["cot", "financial futures", "index futures", "nasdaq"],
    "stock futures": ["cot", "financial futures", "equity index futures", "stock index"],
    "earnings": ["earnings calendar", "company earnings", "quarterly results"],
    "13f": ["sec 13f", "institutional holdings", "fund holdings"],
    "cot": ["commitments of traders", "traders in financial futures", "positioning"],
    "central bank rates": ["policy rates", "interest rates", "monetary authorities"],
    "air quality": ["aqi", "pollution", "particulate", "environment"],
}


def matching_aliases(query: str) -> dict[str, list[str]]:
    normalized = " ".join(query.lower().split())
    return {
        phrase: expansions
        for phrase, expansions in ALIASES.items()
        if phrase in normalized or any(expansion in normalized for expansion in expansions)
    }
