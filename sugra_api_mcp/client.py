"""Async HTTP client for the Sugra API.

Carries `MAX_RESPONSE_CHARS` and the `_enforce_size_limit` helper that caps a
tool's response to it, but does NOT apply that helper itself: enforcement
has to run after a caller's own `fields` / `limit` projection, never on the
raw upstream body, so this client hands the parsed payload back unshaped and
leaves the size call to the caller (gateway.call_endpoint, entities tools).

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
import ssl
import threading
import time
from copy import deepcopy
from typing import Any

import httpx

from . import __version__
from .config import Config

# Anthropic Connectors Directory requires tool results <= 25 000 tokens.
# Using ~4 chars per token as a conservative heuristic, we cap at 85 000 chars
# (~21 000 tokens) to leave headroom for MCP envelope overhead.
MAX_RESPONSE_CHARS = 85_000


_ssl_context: ssl.SSLContext | None = None
_ssl_context_lock = threading.Lock()


def shared_ssl_context() -> ssl.SSLContext:
    """The one TLS context every outbound client in this process shares.

    httpx builds a fresh SSLContext per AsyncClient whenever `verify` is left
    at its default. A context is expensive in both memory and construction
    time, and a long-running server that builds a client per credential pays
    that cost again for every one of them.

    It is `httpx.create_ssl_context()`, NOT `ssl.create_default_context()`:
    httpx's own default loads certifi's CA bundle and honours SSL_CERT_FILE /
    SSL_CERT_DIR, while the stdlib constructor loads the system store, which
    is a different set of trust anchors. The anchors here stay exactly the
    ones an unconfigured httpx client would use.

    The context is built once per process, so those two environment variables
    are read once, at construction.

    ONE THING DOES write to the shared object. Before every TLS connect,
    httpcore calls `ssl_context.set_alpn_protocols(...)` on it (httpcore 1.0.9,
    `_async/connection.py`), with `["http/1.1", "h2"]` when that POOL was built
    with `http2=True` and `["http/1.1"]` otherwise. Every client in this package
    leaves httpx's `http2` default off, so every pool writes the same list and
    the write is idempotent - which is what makes one shared object safe here,
    not any promise that httpcore leaves it alone. Enabling HTTP/2 on ONE client
    while others share this context would let the pools overwrite each other's
    ALPN offer between connects; give that client its own context instead.
    A test pins the condition.

    Built on first use, not at import, so a process that never constructs a
    client on the default transport never builds one at all.

    `lru_cache` would not do here. Its miss path is not atomic, so two threads
    arriving first can each run the factory and each keep a DIFFERENT context,
    which is exactly the invariant this function exists to hold. Double-checked
    locking does hold it: the global is only ever assigned a fully-built
    context, so the fast path reads either None or the final object.
    """
    global _ssl_context
    if _ssl_context is None:
        with _ssl_context_lock:
            if _ssl_context is None:
                _ssl_context = httpx.create_ssl_context()
    return _ssl_context


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


def _cut_top_level_list(payload: dict[str, Any], payload_str: str) -> dict[str, Any]:
    """`data` is itself the oversized list - the common envelope shape."""
    data_list = payload["data"]
    empty_shell = {**payload, "data": []}
    shell_size = len(json.dumps(empty_shell))
    budget = MAX_RESPONSE_CHARS - shell_size - 500  # room for the notice
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


def _cut_record_lists(payload: dict[str, Any], data: dict[str, Any]) -> dict[str, Any] | None:
    """Shrink record lists nested one level inside `data` (a weather
    forecast's `hourly` / `daily`, for example) until the whole payload
    fits, largest list first. Returns None when `data` carries no list to
    shrink, so the caller can fall back to the structured error.

    Each shrunk key gets its own `original_count` / `kept_count` note. A
    sibling list left completely untouched is named in the retry hint, so a
    caller that only ever wanted the untouched block (e.g. `fields=["daily"]`
    on a forecast whose `hourly` block is what does not fit) learns the one
    request that would have avoided the cut entirely.
    """
    list_keys = [key for key, value in data.items() if isinstance(value, list) and value]
    if not list_keys:
        return None
    list_keys.sort(key=lambda key: len(json.dumps(data[key])), reverse=True)

    trimmed = deepcopy(payload)
    trimmed_data = trimmed["data"]
    notes: dict[str, dict[str, int]] = {}

    for key in list_keys:
        if len(json.dumps(trimmed)) <= MAX_RESPONSE_CHARS:
            break
        items = trimmed_data[key]
        shell = {**trimmed, "data": {**trimmed_data, key: []}}
        shell_size = len(json.dumps(shell))
        budget = max(0, MAX_RESPONSE_CHARS - shell_size - 500)  # room for the notice
        avg_item = max(1, (len(json.dumps(items)) - 2) // len(items))
        kept = max(0, min(len(items), budget // avg_item))
        trimmed_data[key] = items[:kept]
        notes[key] = {"original_count": len(items), "kept_count": kept}

    if len(json.dumps(trimmed)) > MAX_RESPONSE_CHARS:
        return None  # could not cut enough - caller falls back to the structured error

    hint = "Add filters (fewer days, a shorter date range, or narrower fields) to reduce response size."
    for key, note in notes.items():
        if note["kept_count"] < note["original_count"]:
            untouched = [k for k in list_keys if k != key and k not in notes]
            if untouched:
                hint = f"Request fields={untouched!r} to get the untouched block(s) without the {key} cut."
            break

    meta = dict(trimmed.get("meta") or {})
    meta["truncated"] = {
        "reason": "exceeds_mcp_25k_token_limit",
        "fields": notes,
        "retry_hint": hint,
    }
    trimmed["meta"] = meta
    return trimmed


def _size_error(payload_str: str, url: str | None) -> dict[str, Any]:
    """No list was found anywhere to shrink - the structured fallback.

    States the gateway's OWN char cap and its token estimate together with
    the directory ceiling it is kept under, rather than naming only the
    25 000-token directory ceiling: a response can trip this gate (over
    MAX_RESPONSE_CHARS chars) while still estimating under 25 000 tokens,
    and a message that names only the directory number then reads as
    self-contradictory - a real-world estimate near 21 659 tokens was once
    rejected against a stated "25000 token limit".
    """
    tokens = len(payload_str) // 4
    return {
        "error": "response_too_large",
        "message": (
            f"Response is {len(payload_str)} chars (approx {tokens} tokens), over this "
            f"gateway's {MAX_RESPONSE_CHARS}-char cap (approx {MAX_RESPONSE_CHARS // 4} tokens, "
            "kept under the MCP directory's 25000-token ceiling) and could not be cut - no "
            "list field was found to shrink. Retry with narrower filters or a smaller request."
        ),
        "estimated_tokens": tokens,
        "url": url,
    }


def _enforce_size_limit(payload: Any, url: str | None = None) -> Any:
    """Trim or cut a payload to fit inside MAX_RESPONSE_CHARS.

    Call this AFTER any caller-side `fields` / `limit` projection: measuring
    the raw, unprojected payload rejected a `fields=["daily"]` weather
    forecast that fit trivially once projected, because the size check ran
    before the projection instead of after it. A payload with a list to
    shrink is CUT with an explicit `meta.truncated` marker the model can
    read - never rejected wholesale while a workable projection was
    available, and never passed through whole over the cap.
    """
    payload_str = json.dumps(payload)
    if len(payload_str) <= MAX_RESPONSE_CHARS:
        return payload

    if isinstance(payload, dict) and isinstance(payload.get("data"), list) and payload["data"]:
        return _cut_top_level_list(payload, payload_str)

    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        cut = _cut_record_lists(payload, payload["data"])
        if cut is not None:
            return cut

    # Unknown shape - nothing to shrink - return a structured error the agent can act on
    return _size_error(payload_str, url)


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
            # Only when httpx would build a context of its own: a supplied
            # transport carries its own, and asking for the shared one there
            # would build a real TLS context for a mock-transport test.
            **({} if transport is not None else {"verify": shared_ssl_context()}),
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
        json: dict[str, Any] | list[Any] | None = None,
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
        # Any non-2xx answer is an HTTP failure. The client never follows
        # redirects and a 3xx carries no JSON body, so under the old `>= 400`
        # gate it fell through to the success path, where the failed decode
        # above had produced {"error": ""} - a failure with no reason, no
        # status and no url, which telemetry could only file as unknown.
        if response.status_code >= 300:
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
            # The API stamps every response with a request id; carrying it on the
            # failure is what lets a user quote one line and have the exact call
            # found in the logs, instead of describing what they think happened.
            request_id = response.headers.get("X-Request-ID")
            if request_id:
                result["request_id"] = str(request_id)
            return result
        # Size enforcement does NOT run here: it must measure the payload
        # AFTER the caller applies any `fields` / `limit` projection, not
        # this raw upstream body. Callers that shape their own
        # response (gateway.call_endpoint, entities tools) call
        # _enforce_size_limit themselves once they have shaped it.
        return payload

    async def aclose(self) -> None:
        await self._client.aclose()
