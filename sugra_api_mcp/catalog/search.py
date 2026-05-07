"""Runtime search over the bundled endpoint catalog."""

from __future__ import annotations

import re
from typing import Any

from .aliases import matching_aliases
from .models import Catalog, Endpoint

TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(value: str) -> list[str]:
    return [token for token in TOKEN_RE.findall(value.lower()) if len(token) >= 2]


def _endpoint_text(endpoint: Endpoint) -> str:
    parts = [
        endpoint.operation_id,
        endpoint.path,
        endpoint.summary,
        endpoint.description,
        " ".join(endpoint.tags),
        endpoint.toolset,
    ]
    for parameter in endpoint.parameters:
        parts.extend(
            [
                parameter.name,
                parameter.description,
                str(parameter.example or ""),
            ]
        )
    return " ".join(parts).lower()


def _field_has(field: str, term: str) -> bool:
    return term in _tokens(field)


def _phrase_has(field: str, phrase: str) -> bool:
    normalized_field = " ".join(_tokens(field))
    normalized_phrase = " ".join(_tokens(phrase))
    return bool(normalized_phrase) and normalized_phrase in normalized_field


def _alias_matches(endpoint_text: str, expansion: str) -> bool:
    expansion_tokens = _tokens(expansion)
    if len(expansion_tokens) <= 1:
        return expansion_tokens[0] in _tokens(endpoint_text) if expansion_tokens else False
    return _phrase_has(endpoint_text, expansion)


def _score(endpoint: Endpoint, query_terms: list[str], aliases: dict[str, list[str]]) -> tuple[int, list[str]]:
    alias_terms = [term for terms in aliases.values() for term in terms]
    all_terms = [*query_terms, *_tokens(" ".join(alias_terms))]
    why: list[str] = []
    score = 0
    endpoint_text = _endpoint_text(endpoint)

    for phrase, expansions in aliases.items():
        if any(_alias_matches(endpoint_text, expansion) for expansion in expansions):
            score += 10
            why.append(f"alias:{phrase}")
            break

    tag_text = " ".join([*endpoint.tags, endpoint.toolset, endpoint.source_family])
    param_text = " ".join(
        f"{parameter.name} {parameter.description}" for parameter in endpoint.parameters
    )

    for term in all_terms:
        if _field_has(endpoint.operation_id, term):
            score += 5
            why.append(f"operation_id:{term}")
        if _field_has(tag_text, term):
            score += 4
            why.append(f"tag_toolset:{term}")
        if _field_has(endpoint.summary, term):
            score += 3
            why.append(f"summary:{term}")
        if _field_has(endpoint.path, term):
            score += 2
            why.append(f"path:{term}")
        if _field_has(param_text, term):
            score += 2
            why.append(f"params:{term}")
        if _field_has(endpoint.description, term):
            score += 1
            why.append(f"description:{term}")
    return score, list(dict.fromkeys(why))[:5]


def search_catalog(
    catalog: Catalog,
    query: str,
    *,
    toolset: str | None = None,
    source: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search catalog operations by free-text query."""
    terms = _tokens(query)
    if not terms:
        return []
    aliases = matching_aliases(query)

    scored: list[tuple[int, Endpoint, list[str]]] = []
    for endpoint in catalog.endpoints:
        if toolset and endpoint.toolset != toolset:
            continue
        endpoint_sources = endpoint.sources or [endpoint.source_family]
        if source and source not in endpoint_sources and endpoint.source_family != source:
            continue
        score, why = _score(endpoint, terms, aliases)
        if score > 0:
            scored.append((score, endpoint, why))
    scored.sort(key=lambda item: (-item[0], item[1].operation_id))
    return [
        {
            "operation_id": endpoint.operation_id,
            "method": endpoint.method,
            "path": endpoint.path,
            "summary": endpoint.summary,
            "toolset": endpoint.toolset,
            "source_family": endpoint.source_family,
            "sources": endpoint.sources or [endpoint.source_family],
            "tags": endpoint.tags,
            "required_parameters": endpoint.required_parameters,
            "score": score,
            "why": why,
        }
        for score, endpoint, why in scored[: max(0, limit)]
    ]
