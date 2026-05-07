"""Async HTTP client for the Sugra API with size-limit enforcement."""

from __future__ import annotations

import json
from typing import Any

import httpx

from . import __version__
from .config import Config

# Anthropic Connectors Directory requires tool results <= 25 000 tokens.
# Using ~4 chars per token as a conservative heuristic, we cap at 85 000 chars
# (~21 000 tokens) to leave headroom for MCP envelope overhead.
MAX_RESPONSE_CHARS = 85_000


def _pkg_version() -> str:
    return __version__


def _enforce_size_limit(payload: Any, url: str) -> Any:
    """Trim payload to fit MCP token limits. Returns possibly-modified dict."""
    payload_str = json.dumps(payload)
    if len(payload_str) <= MAX_RESPONSE_CHARS:
        return payload

    # Try to truncate a list inside `data` field (common envelope shape)
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        data_list = payload["data"]
        if data_list:
            empty_shell = {**payload, "data": []}
            shell_size = len(json.dumps(empty_shell))
            budget = MAX_RESPONSE_CHARS - shell_size - 500  # room for notice
            avg_item = max(1, (len(payload_str) - shell_size) // len(data_list))
            kept = max(1, min(len(data_list), budget // avg_item))
            truncated = {**payload, "data": data_list[:kept]}
            meta = dict(truncated.get("meta") or {})
            meta["truncated"] = {
                "reason": "exceeds_mcp_25k_token_limit",
                "original_count": len(data_list),
                "kept_count": kept,
                "retry_hint": "Add filters (country, date range, limit) to reduce response size.",
            }
            truncated["meta"] = meta
            return truncated

    # Unknown shape - return a structured error the agent can act on
    return {
        "error": "response_too_large",
        "message": (
            f"Response exceeds MCP 25000 token limit (approx {len(payload_str) // 4} tokens). "
            "Retry with narrower filters."
        ),
        "estimated_tokens": len(payload_str) // 4,
        "url": url,
    }


class SugraClient:
    """Thin async wrapper over the Sugra API with x-api-key auth."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.api_base,
            headers={
                "x-api-key": config.api_key,
                "User-Agent": f"sugra-api-mcp/{_pkg_version()}",
                "Accept": "application/json",
            },
            timeout=config.timeout,
        )

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("POST", path, json=json)

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        response = await self._client.request(
            method.upper(),
            path,
            params=clean_params,
            json=json if json is not None else None,
        )
        return self._handle(response)

    @staticmethod
    def _handle(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": response.text[:500]}
        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else str(payload)
            return {
                "error": error or f"HTTP {response.status_code}",
                "status_code": response.status_code,
                "url": str(response.request.url),
            }
        return _enforce_size_limit(payload, str(response.request.url))

    async def aclose(self) -> None:
        await self._client.aclose()
