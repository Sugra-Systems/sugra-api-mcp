"""Configuration loading from environment variables."""

from __future__ import annotations

import ipaddress
import math
import os
import re
from dataclasses import dataclass

from . import __version__

# Remediation copy for the call-time missing_api_key error. Shared by the
# keyless stand-in client (server.py) and the CLI doctor warning (__main__.py).
MISSING_API_KEY_HINT = (
    "Set SUGRA_API_KEY. Get one free at https://app.sugra.ai/register"
)

DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "https://chatgpt.com",
    "https://chat.openai.com",
    "https://platform.openai.com",
    "https://claude.ai",
    "https://claude.com",
    "https://cursor.sh",
    "https://app.cursor.sh",
)

# The opt-in switch for the MCP Apps price-chart widget, and the
# only values that turn it on (compared after strip + lower).
UI_WIDGETS_ENV = "SUGRA_MCP_UI_WIDGETS"
_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})

# The product token of the Server response header, and the opt-in switch that
# adds the package version to that header and to the /health response.
SERVER_PRODUCT = "sugra-api-mcp"
SERVER_VERSION_ENV = "SUGRA_MCP_SERVER_VERSION"

# The opt-in switch that makes the caller's host and address come from the
# X-Forwarded-Host and X-Real-IP headers a trusted proxy sets in front of the
# server, instead of the Host header and the connection peer.
TRUST_PROXY_HEADERS_ENV = "SUGRA_MCP_TRUST_PROXY_HEADERS"

# The opt-in switch for the request limits: the admission lanes, the budget for
# requests without verified credentials, the failed-check shield and the per-key
# call counters, all counted in the process's own memory (sugra_api_mcp.limits),
# and the extra demand counts. Its limit settings are read by
# sugra_api_mcp.limits.load_settings, and only when this one is on.
LIMITS_ENV = "SUGRA_MCP_LIMITS"


@dataclass(frozen=True)
class Config:
    api_base: str
    api_key: str
    timeout: float
    tool_deadline: float = 40.0


@dataclass(frozen=True)
class AuthConfig:
    """Settings for OAuth / JWT validation on the HTTP transport."""

    app_url: str
    jwks_url: str
    internal_token: str | None


def load_config(*, require_api_key: bool = True) -> Config:
    """Load main client config.

    An empty SUGRA_API_KEY is always allowed here: startup must never fail on
    a missing key. MCP clients and directory evaluators launch the server
    without env configured and expect initialize plus tools/list introspection
    to work before the user supplies credentials. The key requirement is
    enforced at call time instead - ``server.get_client`` hands out a stand-in
    client whose network methods return the structured ``missing_api_key``
    error when no key is available.

    ``require_api_key`` is kept for signature compatibility and no longer
    triggers a raise. On the HTTP transport the auth middleware supplies a
    per-request key resolved from the Bearer token, exactly as before.
    """
    del require_api_key  # retained for signature compatibility only
    api_key = os.environ.get("SUGRA_API_KEY", "").strip()
    return Config(
        api_base=os.environ.get("SUGRA_API_BASE", "https://sugra.ai").rstrip("/"),
        api_key=api_key,
        timeout=float(os.environ.get("SUGRA_TIMEOUT", "30")),
        # End-to-end budget for ONE tool call, wrapped
        # around dispatch in SugraFastMCP.call_tool. Must sit BELOW common
        # client read timeouts (a measured client cut at ~45s while the
        # gateway kept working to its 60s outbound budget, so the typed
        # timeout envelope never reached the agent).
        tool_deadline=_positive_seconds("SUGRA_TOOL_DEADLINE", "40"),
    )


# The lowest tool timeout an MCP client is documented to use: the canonical
# timeout chain records clients cutting at 60-180s. The server must be able to
# answer with its typed envelope before the earliest of those, so the whole
# server-side path - auth plus dispatch - has to fit underneath this.
CLIENT_TIMEOUT_FLOOR_SECONDS = 60.0


def validate_startup_budgets() -> None:
    """Refuse a configuration whose server-side path can outlive the client.

    The tool budget bounds DISPATCH, and auth is bounded
    separately by AuthMiddleware, so the server-side worst case is the SUM of
    the two, reached only on a cold auth. That sum is what has to stay under
    the client's own cut, or the typed timeout envelope never arrives and the
    caller sees a dead connection instead - the exact failure the budget was
    introduced to prevent.

    Called from both transports at startup. Previously nothing called
    load_config on the startup path at all, so a nonpositive budget started
    happily and surfaced as an unstructured HTTP 500 from inside the auth
    middleware on the first authenticated request.
    """
    from .auth import AUTH_BUDGET_SECONDS

    total = load_config(require_api_key=False).tool_deadline
    worst_case = total + AUTH_BUDGET_SECONDS
    if worst_case > CLIENT_TIMEOUT_FLOOR_SECONDS:
        raise ValueError(
            f"SUGRA_TOOL_DEADLINE={total:g}s plus the {AUTH_BUDGET_SECONDS:g}s "
            f"auth budget is {worst_case:g}s, past the "
            f"{CLIENT_TIMEOUT_FLOOR_SECONDS:g}s floor of documented client "
            "timeouts. A client would cut the connection before the timeout "
            "envelope arrives."
        )


def _positive_seconds(var: str, default: str) -> float:
    """Parse a seconds budget, refusing values that cannot bound anything.

    The budget used to go straight to asyncio.timeout. Zero
    cancelled every call the instant it started and a negative value did the
    same, both silently - the operator saw tools that "always time out" with
    no hint that the configuration was the cause.
    """
    raw = os.environ.get(var, default).strip() or default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{var} must be a number of seconds, got {raw!r}") from exc
    if not math.isfinite(value) or value <= 0:
        raise ValueError(
            f"{var} must be a finite positive number of seconds, got {raw!r}")
    return value


def load_auth_config() -> AuthConfig:
    """Load OAuth / internal-lookup settings for the HTTP transport."""
    app_url = os.environ.get("SUGRA_APP_URL", "https://app.sugra.ai").rstrip("/")
    jwks_url = os.environ.get("SUGRA_JWKS_URL", f"{app_url}/oauth/jwks.json")
    internal_token = os.environ.get("INTERNAL_API_TOKEN", "").strip() or None
    return AuthConfig(app_url=app_url, jwks_url=jwks_url, internal_token=internal_token)


def load_allowed_origins() -> list[str]:
    """Load CORS allowed origins for the HTTP transport.

    Browser-based MCP clients (ChatGPT Connectors UI) send a CORS preflight
    before the actual MCP request. Without an exact-match origin in the
    response, the browser blocks the call and the connector add flow fails
    silently. Server-to-server clients (claude.ai backend, Codex CLI, stdio
    Claude Desktop) ignore CORS entirely, which is why this only matters
    for the hosted HTTP endpoint.

    `SUGRA_MCP_ALLOWED_ORIGINS` (comma-separated) overrides the default. A
    value of `*` allows any origin; only safe because hosted access is gated
    by Bearer token rather than browser cookies.
    """
    raw = os.environ.get("SUGRA_MCP_ALLOWED_ORIGINS", "").strip()
    if not raw:
        return list(DEFAULT_ALLOWED_ORIGINS)
    if raw == "*":
        return ["*"]
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


def ui_widgets_enabled() -> bool:
    """Whether the MCP Apps price-chart widget is served at all.

    On only when SUGRA_MCP_UI_WIDGETS is 1, true, yes or on, in any
    case and with surrounding whitespace ignored. Unset, empty and every other
    value mean off. Off is the default because the widget is not ready for an
    app-directory review, and a directory scan of the server must find no UI:
    with the flag off the ui:// template is not registered and no tool
    declares it.

    Read once per process, when tools/widgets.py registers (see
    register_ui_widgets there), so a change takes a restart.
    """
    return os.environ.get(UI_WIDGETS_ENV, "").strip().lower() in _TRUTHY_ENV_VALUES


def server_version_disclosed() -> bool:
    """Whether HTTP responses name the package version.

    On only when SUGRA_MCP_SERVER_VERSION is 1, true, yes or on, in any case
    and with surrounding whitespace ignored. Unset, empty and every other value
    mean off: the Server header carries the product name alone and /health
    leaves the version out. The version stays in the telemetry resource, in the
    MCP initialize result and in `sugra-api-mcp doctor`.
    """
    return os.environ.get(SERVER_VERSION_ENV, "").strip().lower() in _TRUTHY_ENV_VALUES


def proxy_headers_trusted() -> bool:
    """Whether the caller's host and address are read from proxy headers.

    On only when SUGRA_MCP_TRUST_PROXY_HEADERS is 1, true, yes or on, in any
    case and with surrounding whitespace ignored. Unset, empty and every other
    value mean off, and off is byte-for-byte the behaviour before the setting
    existed: the Host header and the connection peer, nothing else.

    On, the server believes X-Forwarded-Host and X-Real-IP, so turn it on only
    where every request reaches the process through a proxy that overwrites
    both headers on each request (the nginx of the hosted VM). X-Forwarded-For
    is never read, on or off. Read on every request.
    """
    return os.environ.get(TRUST_PROXY_HEADERS_ENV, "").strip().lower() in _TRUTHY_ENV_VALUES


def limits_enabled() -> bool:
    """Whether the request limits are on.

    On only when SUGRA_MCP_LIMITS is 1, true, yes or on, in any case and with
    surrounding whitespace ignored. Unset, empty and every other value mean off,
    and off is byte-for-byte the behaviour before the setting existed: no limits
    object is built and no request is held at admission.

    Read once, when the HTTP transport starts (__main__), so a change takes a
    restart.
    """
    return os.environ.get(LIMITS_ENV, "").strip().lower() in _TRUTHY_ENV_VALUES


# Longest host a proxy header may carry: a 253-character name, a colon and a
# five-digit port. Anything longer is malformed.
_FORWARDED_HOST_MAX = 259
_DNS_NAME_MAX = 253
# One DNS label: 1 to 63 ASCII letters, digits, hyphens or underscores, neither
# starting nor ending with a hyphen. The underscore is allowed on purpose: the
# previous character check allowed it and internal service names use it.
_DNS_LABEL = re.compile(r"(?!-)[A-Za-z0-9_-]{1,63}(?<!-)")
_PORT = re.compile(r"[0-9]{1,5}")


def _valid_port(text: str) -> bool:
    """Digits only, 1 to 5 of them, a value in 1..65535."""
    return _PORT.fullmatch(text) is not None and 1 <= int(text) <= 65535


def _valid_ipv4(text: str) -> bool:
    """A dotted quad of four 0-255 octets, as ipaddress reads it."""
    try:
        ipaddress.IPv4Address(text)
    except ValueError:
        return False
    return True


def _valid_ipv6(text: str) -> bool:
    """An IPv6 literal ipaddress accepts, without a zone."""
    if "%" in text:
        return False
    try:
        ipaddress.IPv6Address(text)
    except ValueError:
        return False
    return True


def _valid_dns_name(text: str) -> bool:
    """Dot-separated labels, at most 253 characters, the last one not all digits.

    A name whose last label is all digits is a malformed address ("999.8.8.8",
    "1.2.3"), not a name, so it is refused here and left to the IPv4 rule.
    """
    if not text or len(text) > _DNS_NAME_MAX:
        return False
    labels = text.split(".")
    if not all(_DNS_LABEL.fullmatch(label) for label in labels):
        return False
    return not labels[-1].isdigit()


def _header_text(value: object) -> str | None:
    """A header value as text with only edge spaces and tabs trimmed, else None.

    None for anything that is not a str, and for a value holding a control
    character (0x00-0x1f, 0x7f, tabs inside the text included) or a non-ASCII
    character anywhere. Only an ASCII space or horizontal tab at the edges is
    trimmed, so a newline, a vertical tab or a Unicode space at an edge makes
    the value malformed instead of being stripped away. Edge spaces and tabs
    are optional whitespace around a field value (RFC 9110 5.5) and are
    trimmed; every other control or non-ASCII character makes it malformed.
    """
    if type(value) is not str:
        return None
    text = value.strip(" \t")
    if not text.isascii() or any(ch < " " or ch == "\x7f" for ch in text):
        return None
    return text


def _forwarded_host(value: object) -> str | None:
    """A single plain host[:port] from X-Forwarded-Host, else None.

    The whole text has to match: a DNS name, an IPv4 dotted quad or a bracketed
    IPv6 literal, then optionally a colon and a port in 1..65535. A list, a
    space, a path, credentials, a control or non-ASCII character, an empty or
    over-long text, or a host with a missing or bad port is malformed and is
    not used.
    """
    text = _header_text(value)
    if not text or len(text) > _FORWARDED_HOST_MAX:
        return None
    if text.startswith("["):
        literal, bracket, rest = text[1:].partition("]")
        if not bracket or not _valid_ipv6(literal):
            return None
        port = rest[1:] if rest.startswith(":") else None
        if rest and port is None:
            return None
    else:
        if text.count(":") > 1:
            return None
        name, colon, port = text.partition(":")
        if not (_valid_ipv4(name) or _valid_dns_name(name)):
            return None
        if not colon:
            port = None
    if port is not None and not _valid_port(port):
        return None
    return text


def caller_host(host: object, forwarded_host: object) -> object:
    """The Host value the caller classes (host class on spans, counts and logs) are read from.

    Off: the Host header as received, X-Forwarded-Host untouched. On: a
    well-formed X-Forwarded-Host, else the Host header. This feeds the host
    classes only. The host allow-list (SUGRA_MCP_ALLOWED_HOSTS) is enforced by
    the SDK on the Host header itself and never sees X-Forwarded-Host, on or
    off, so no client-supplied X-Forwarded-Host can pass or fail it.
    """
    if not proxy_headers_trusted():
        return host
    forwarded = _forwarded_host(forwarded_host)
    return host if forwarded is None else forwarded


def _forwarded_address(value: object) -> str | None:
    """A single plain IP address from X-Real-IP in its canonical text, else None."""
    text = _header_text(value)
    if not text or len(text) > 45 or "%" in text:
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def caller_address(peer: object, real_ip: object) -> tuple[object, object]:
    """The (connection peer, X-Real-IP) pair the caller's network prefix is read from.

    Off: both as received. On with a well-formed X-Real-IP: that address in both
    places, because behind the proxy the peer is the proxy and not the caller.
    On with an absent or malformed X-Real-IP: both as received, so the prefix
    is the one the unchanged rule gives (none for a peer that is not the
    caller, loopback for a local one) and a bad header never names a network.
    """
    if not proxy_headers_trusted():
        return peer, real_ip
    address = _forwarded_address(real_ip)
    if address is None:
        return peer, real_ip
    return address, address


def server_header_value() -> str:
    """The Server response header (RFC 9110, section 10.2.4).

    The product name, followed by "/" and the package version only when
    server_version_disclosed() is on.
    """
    if server_version_disclosed():
        return f"{SERVER_PRODUCT}/{__version__}"
    return SERVER_PRODUCT
