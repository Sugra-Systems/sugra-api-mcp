"""A temporary, time-boxed record of how each client identifies itself.

For a short experiment the gate writes one INFO record per POST to /mcp on
logger ``sugra_mcp.probe``: the identity the client sent (the initialize
clientInfo, the User-Agent, Origin and MCP headers), the JSON-RPC method, the
status it was answered with, the header names (the first HEADER_NAMES_MAX
sorted, with the total in header_count), and a coarse network.
It exists to learn, from real traffic, what ChatGPT, its directory scanner,
Claude and Grok send, so a later change can tell them apart without guessing.

It never holds a credential: of Authorization only the scheme, when it is a
known one, and the kind of token (jwt, sugra_key, other) are kept, of X-API-Key only whether it was sent,
and the session id only as an 8-hex sha256 prefix that links the requests of
one session. Tool arguments are never read; a tools/call keeps its tool name.
Each text value is cut to VALUE_MAX characters with control characters
replaced. The record is written only before PROBE_UNTIL; after it the probe
does nothing, so the experiment ends even if nobody removes this module.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any

from starlette.types import Scope

from . import config, observability

logger = logging.getLogger("sugra_mcp.probe")
# Set here, not inherited: the hosted root logger stays at WARNING (gate.py).
logger.setLevel(logging.INFO)

PROBE_UNTIL = _dt.datetime(2026, 10, 16, 0, 0, tzinfo=_dt.UTC)
RECORD_PREFIX = "sprobe1 "
VALUE_MAX = 160
HEADER_NAMES_MAX = 100
CAPABILITY_KEYS_MAX = 20

_TEXT_HEADERS = (
    b"user-agent",
    b"origin",
    b"mcp-protocol-version",
    b"accept",
    b"content-type",
    b"x-forwarded-proto",
)
_SCHEMES = frozenset({"bearer", "basic", "token", "apikey"})


def active(now: Callable[[], _dt.datetime] | None = None) -> bool:
    """True while the experiment runs."""
    current = now() if now is not None else _dt.datetime.now(_dt.UTC)
    return current < PROBE_UNTIL


def _clean(value: object) -> str | None:
    """A printable string of at most VALUE_MAX characters, or None."""
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    text = "".join(ch if ch.isprintable() else "?" for ch in value[:VALUE_MAX])
    return text


def _headers(scope: Scope) -> list[tuple[bytes, bytes]]:
    raw = scope.get("headers") or ()
    return [(k.lower(), v) for k, v in raw if isinstance(k, bytes) and isinstance(v, bytes)]


def _first(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    for key, value in headers:
        if key == name:
            return value.decode("latin-1")
    return None


def _token_kind(token: str) -> str:
    if token.startswith("eyJ"):
        return "jwt"
    if token.startswith("sugra_"):
        return "sugra_key"
    return "other" if token else "empty"


def _authorization(value: str | None) -> dict[str, str] | None:
    """The scheme class and token kind of an Authorization header; never any of its text.

    The scheme is named only when it is one of _SCHEMES; another scheme reads as
    "other", and a value with no separator as "none" with only its token kind, so
    a raw credential sent with no scheme never reaches the record.
    """
    if value is None:
        return None
    parts = value.split(None, 1)
    if len(parts) < 2:
        return {"scheme": "none", "kind": _token_kind(value.strip())}
    scheme = parts[0].lower()
    return {
        "scheme": scheme if scheme in _SCHEMES else "other",
        "kind": _token_kind(parts[1].strip()),
    }


def _session_tag(value: str | None) -> str | None:
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]


def body_facts(parsed: tuple[bool, object]) -> dict[str, Any]:
    """What the probe keeps of a read body: unparsed when it is not JSON, else message_facts."""
    if not parsed[0]:
        return {"method": "unparsed"}
    return message_facts(parsed[1])


def message_facts(message: object) -> dict[str, Any]:
    """What the probe keeps of a decoded JSON-RPC body; never tool arguments."""
    if isinstance(message, list):
        return {"method": "batch", "batch_size": len(message)}
    if not isinstance(message, dict):
        return {"method": "not_an_object"}
    method = message.get("method")
    facts: dict[str, Any] = {"method": _clean(method) if isinstance(method, str) else None}
    params = message.get("params")
    if not isinstance(params, dict):
        return facts
    if method == "initialize":
        info = params.get("clientInfo")
        if isinstance(info, dict):
            facts["client_name"] = _clean(info.get("name"))
            facts["client_version"] = _clean(info.get("version"))
            facts["client_title"] = _clean(info.get("title"))
        facts["protocol_version"] = _clean(params.get("protocolVersion"))
        caps = params.get("capabilities")
        if isinstance(caps, dict):
            facts["capabilities"] = sorted(
                _clean(k) or "" for k in list(caps)[:CAPABILITY_KEYS_MAX]
            )
    elif method == "tools/call":
        facts["tool"] = _clean(params.get("name"))
    return facts


def _network(scope: Scope, headers: list[tuple[bytes, bytes]]) -> str | None:
    peer = scope.get("client")
    client_addr = peer[0] if type(peer) in (tuple, list) and peer else None
    client_addr, x_real_ip = config.caller_address(client_addr, _first(headers, b"x-real-ip"))
    return observability._network_of(client_addr, x_real_ip)


def record(scope: Scope, facts: dict[str, Any] | None, status: object) -> dict[str, Any]:
    """The probe record of one request, as a dict."""
    headers = _headers(scope)
    names = sorted({k.decode("latin-1") for k, _ in headers})
    entry: dict[str, Any] = {
        "path": _clean(scope.get("path")),
        "status": status if isinstance(status, int) else None,
        "host": _clean(
            config.caller_host(_first(headers, b"host"), _first(headers, b"x-forwarded-host"))
        ),
        "net": _network(scope, headers),
        "session": _session_tag(_first(headers, b"mcp-session-id")),
        "authorization": _authorization(_first(headers, b"authorization")),
        "x_api_key": _first(headers, b"x-api-key") is not None,
        "header_names": [_clean(n) for n in names[:HEADER_NAMES_MAX]],
        "header_count": len(names),
    }
    for name in _TEXT_HEADERS:
        entry[name.decode("ascii").replace("-", "_")] = _clean(_first(headers, name))
    entry.update(facts if facts is not None else {"method": "unread"})
    return entry


def write(scope: Scope, facts: dict[str, Any] | None, status: object) -> None:
    """Log one probe record while the experiment runs; never raises."""
    try:
        if not active():
            return
        logger.info(
            "%s%s",
            RECORD_PREFIX,
            json.dumps(record(scope, facts, status), sort_keys=True, separators=(",", ":")),
        )
    except Exception as e:
        with contextlib.suppress(Exception):
            logger.warning("Probe record failed (%s).", type(e).__name__)
