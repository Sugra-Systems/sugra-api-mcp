"""Async HTTP client for the Sugra API with size-limit enforcement.

Error contract: this client NEVER raises httpx exceptions to callers.
Transport failures (timeout, refused connection, mid-stream disconnect)
return structured dicts the agent can act on, mirroring the shape already
used for HTTP 4xx/5xx responses:

    {
        "error": "upstream_timeout" | "upstream_connect_error"
                 | "upstream_transport_error",
        "reason": "<exception class>: <message>",   # class name only if empty
        "status_code": None,                        # no HTTP status received
        "elapsed_ms": 30012,
        "url": "https://sugra.ai/api/v1/...",
        "retry_hint": "...",
        "timeout_s": 30.0,                          # upstream_timeout only
    }

Without this, httpx exceptions propagate to the MCP framework which renders
them as "Error executing tool call_endpoint: " with an EMPTY message
(httpx.ReadTimeout stringifies to ""), leaving the agent unable to pick a
retry strategy (field-test defect D2).
"""

from __future__ import annotations

import json
import time
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


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _retry_after(response: httpx.Response) -> int | str | None:
    """Parse the Retry-After header: delta-seconds -> int, anything else -> raw string.

    Never raises. str.isdigit() alone is NOT a safe gate for int(): it accepts
    unicode digit characters (e.g. superscript two) that int() rejects with
    ValueError, and this helper runs outside the transport try/except - a
    malformed header from a proxy must not break the error path. Hence the
    additional isascii() check.
    """
    raw = str(response.headers.get("Retry-After", "")).strip()
    if not raw:
        return None
    if raw.isascii() and raw.isdigit():
        return int(raw)
    return raw


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

    def __init__(self, config: Config, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.api_base,
            headers={
                "x-api-key": config.api_key,
                "User-Agent": f"sugra-api-mcp/{_pkg_version()}",
                "Accept": "application/json",
            },
            timeout=config.timeout,
            transport=transport,
        )

    async def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("GET", path, params=params)

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        return await self.request("POST", path, json=json, headers=headers)

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        clean_params = {k: v for k, v in (params or {}).items() if v is not None}
        start = time.perf_counter()
        try:
            # Per-request headers are MERGED over the instance headers by httpx
            # (request wins on key conflicts) - x-api-key stays, extras (e.g.
            # X-Internal-Token for the agent plane) ride alongside.
            response = await self._client.request(
                method.upper(),
                path,
                params=clean_params,
                json=json if json is not None else None,
                headers=headers,
            )
        # Order matters: ConnectTimeout subclasses TimeoutException (NOT
        # ConnectError), so all timeout flavors land in upstream_timeout.
        except httpx.TimeoutException as exc:
            return self._transport_error(
                "upstream_timeout",
                exc,
                start,
                path,
                retry_hint=(
                    f"No response within the gateway's configured {self._config.timeout:g}s "
                    "upstream timeout (SUGRA_TIMEOUT). A single retry often succeeds (the "
                    "aborted attempt usually completes server-side and warms upstream "
                    "caches). Otherwise narrow the request: smaller batch, fewer items, "
                    "tighter filters."
                ),
                extra={"timeout_s": self._config.timeout},
            )
        except httpx.ConnectError as exc:
            return self._transport_error(
                "upstream_connect_error",
                exc,
                start,
                path,
                retry_hint="Could not connect to the Sugra API. Retry after a short delay.",
            )
        except httpx.HTTPError as exc:
            return self._transport_error(
                "upstream_transport_error",
                exc,
                start,
                path,
                retry_hint=(
                    "Transient transport failure (connection dropped mid-request). "
                    "Retry once; if it persists, report the reason field."
                ),
            )
        return self._handle(response, elapsed_ms=_elapsed_ms(start))

    def _transport_error(
        self,
        code: str,
        exc: httpx.HTTPError,
        start: float,
        path: str,
        *,
        retry_hint: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build the structured error dict for a transport-layer failure."""
        message = str(exc).strip()
        reason = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
        result: dict[str, Any] = {
            "error": code,
            "reason": reason,
            "status_code": None,
            "elapsed_ms": _elapsed_ms(start),
            "url": self._request_url(exc, path),
            "retry_hint": retry_hint,
        }
        if extra:
            result.update(extra)
        return result

    def _request_url(self, exc: httpx.HTTPError, path: str) -> str:
        try:
            return str(exc.request.url)
        except RuntimeError:
            # httpx raises RuntimeError when the exception carries no request.
            return f"{self._config.api_base}{path}"

    @staticmethod
    def _handle(response: httpx.Response, *, elapsed_ms: int) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {"error": response.text[:500]}
        if response.status_code >= 400:
            error = payload.get("error") if isinstance(payload, dict) else str(payload)
            result: dict[str, Any] = {
                "error": error or f"HTTP {response.status_code}",
                "status_code": response.status_code,
                "url": str(response.request.url),
                "elapsed_ms": elapsed_ms,
            }
            retry_after = _retry_after(response)
            if retry_after is not None:
                result["retry_after"] = retry_after
            return result
        return _enforce_size_limit(payload, str(response.request.url))

    async def aclose(self) -> None:
        await self._client.aclose()
