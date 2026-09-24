"""Plan purchase for agents: buy a Sugra API plan and receive a new API key.

``buy_plan`` bridges the MCP tool call to the purchase endpoint of the web app,
which speaks the Payment HTTP authentication scheme (HTTP 402). The JSON-RPC
side follows the MCP transport of that scheme:

- Without a credential the call fails with JSON-RPC error -32042 (Payment
  Required). ``error.data.challenges`` holds the challenges, parsed from the
  endpoint's ``WWW-Authenticate: Payment`` header, with ``request`` decoded to
  a JSON object.
- With a credential in ``params._meta["org.paymentauth/credential"]`` the call
  forwards it as ``Authorization: Payment <base64url>`` and returns the key,
  with the receipt in ``result._meta["org.paymentauth/receipt"]``. A payment
  the endpoint refuses fails with -32043 and a fresh challenge.

The challenge binds the exact request body bytes, so both calls build the body
the same way (``request_body``). The tool needs no API key. The credential and
the key are never logged; the key reaches only the tool result.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Annotated, Any, Literal

import httpx
from mcp.shared.exceptions import UrlElicitationRequiredError
from mcp.types import INVALID_PARAMS, CallToolResult, ErrorData, TextContent, ToolAnnotations
from pydantic import Field

from .. import __version__
from ..client import shared_ssl_context
from ..observability import trace_mcp_tool
from ..server import mcp

PURCHASE_BASE_URL = "https://app.sugra.ai/agent/mpp"

CREDENTIAL_META_KEY = "org.paymentauth/credential"
RECEIPT_META_KEY = "org.paymentauth/receipt"

PAYMENT_REQUIRED = -32042
PAYMENT_VERIFICATION_FAILED = -32043

# failure.reason and failure.detail of a -32043 when the endpoint's problem
# names neither.
FAILURE_REASON_FALLBACK = "verification-failed"
FAILURE_DETAIL_FALLBACK = "The purchase endpoint refused the payment credential."

# Below the tool's dispatch budget, so a slow endpoint gets this tool's own
# answer rather than the generic deadline error.
PURCHASE_TIMEOUT_SECONDS = 25.0

MAX_EMAIL_LENGTH = 255

TERMS_URL = "https://sugra.systems/terms-of-service"

# Only a shape check; the endpoint validates the address itself.
_EMAIL_SHAPE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# RFC 9110 auth-param parsing: a token, or a quoted-string with backslash escapes.
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_QUOTED = re.compile(r'"((?:[^"\\]|\\.)*)"')
_SEPARATORS = re.compile(r"[\s,]*")
_OWS = re.compile(r"[ \t]*")

# The challenge parameters a client echoes back in its credential.
_CHALLENGE_FIELDS = (
    "id", "realm", "method", "intent", "request",
    "expires", "digest", "opaque", "description", "header",
)
_REQUIRED_CHALLENGE_FIELDS = ("id", "realm", "method", "intent", "request")

# Transport tests replace this with an httpx.MockTransport.
_transport: httpx.AsyncBaseTransport | None = None


class PaymentError(UrlElicitationRequiredError):
    """A JSON-RPC error raised from the tool: -32042, -32043 or -32602.

    The SDK turns a tool exception into a tool result with isError set, with
    one exception: this base class, which it re-raises so the request is
    answered with the error's own code and data. The base class carries URL
    elicitations; this subclass replaces its error with the payment one and
    lists no elicitations.
    """

    def __init__(self, code: int, message: str, data: dict[str, Any]) -> None:
        self._elicitations = []
        self.error = ErrorData(code=code, message=message, data=data)
        Exception.__init__(self, message)


def request_body(email: str, accept_terms: bool) -> bytes:
    """The request body, byte for byte the same for the same arguments.

    The endpoint binds each challenge to a digest of these bytes, so the paying
    call must send exactly what the challenge call sent.
    """
    return json.dumps(
        {"accept_terms": accept_terms, "email": email},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes | None:
    """Decode base64url with or without its "=" padding; None when it is not base64url."""
    unpadded = value.rstrip("=")
    if len(value) - len(unpadded) > 2 or not re.fullmatch(r"[A-Za-z0-9_-]+", unpadded):
        return None
    try:
        return base64.urlsafe_b64decode(unpadded + "=" * (-len(unpadded) % 4))
    except (binascii.Error, ValueError):
        return None


def _jcs(value: Any) -> str:
    """The purchase endpoint's canonical JSON for a challenge's request or opaque.

    Not RFC 8785: object keys sorted by code point, compact separators, UTF-8
    unescaped except U+2028 and U+2029, which the endpoint's encoder escapes.
    That reproduces the endpoint's own bytes for what it issues (objects with
    ASCII keys, strings and lists of strings, no numbers), which is what the
    challenge id binds. Floats and empty objects are outside that shape.
    """
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _skip(pattern: re.Pattern[str], text: str, pos: int) -> int:
    """The position after pattern at pos; the patterns used here also match nothing."""
    match = pattern.match(text, pos)
    return match.end() if match is not None else pos


def _decode_json_param(value: str) -> Any:
    raw = _b64url_decode(value)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


def parse_payment_challenges(header_values: list[str]) -> list[dict[str, Any]]:
    """The Payment challenges in WWW-Authenticate values, as JSON-RPC challenge objects.

    Parameter names are case-insensitive. A challenge without one of the
    required parameters, or whose request does not decode to a JSON object, is
    dropped. ``request`` is returned decoded; every other value as sent.
    Challenges of other schemes are skipped.
    """
    challenges: list[dict[str, str]] = []
    for text in header_values:
        # The auth-params of the Payment challenge being read, None inside any
        # other scheme.
        current: dict[str, str] | None = None
        pos = 0
        while True:
            pos = _skip(_SEPARATORS, text, pos)
            name_match = _TOKEN.match(text, pos)
            if name_match is None:
                break
            after = _skip(_OWS, text, name_match.end())
            if after < len(text) and text[after] == "=":
                value_start = _skip(_OWS, text, after + 1)
                quoted = _QUOTED.match(text, value_start)
                bare = _TOKEN.match(text, value_start)
                if quoted is not None:
                    value = re.sub(r"\\(.)", r"\1", quoted.group(1))
                    pos = quoted.end()
                elif bare is not None:
                    value = bare.group(0)
                    pos = bare.end()
                else:
                    # A token68 such as "abc==", not an auth-param: skip it.
                    pos = after + 1
                    while pos < len(text) and text[pos] == "=":
                        pos += 1
                    continue
                if current is not None:
                    current.setdefault(name_match.group(0).lower(), value)
                continue
            # A token not followed by "=" is the scheme of the next challenge.
            if current is not None:
                challenges.append(current)
            current = {} if name_match.group(0).lower() == "payment" else None
            pos = name_match.end()
        if current is not None:
            challenges.append(current)

    usable: list[dict[str, Any]] = []
    for params in challenges:
        if any(not params.get(name) for name in _REQUIRED_CHALLENGE_FIELDS):
            continue
        request = _decode_json_param(params["request"])
        if not isinstance(request, dict):
            continue
        challenge: dict[str, Any] = {
            name: params[name] for name in _CHALLENGE_FIELDS if name in params
        }
        challenge["request"] = request
        usable.append(challenge)
    return usable


def _invalid_credential(detail: str) -> PaymentError:
    return PaymentError(INVALID_PARAMS, "Invalid params", {"detail": detail})


def _credential_from_meta() -> dict[str, Any] | None:
    """The payment credential of the request being served, or None when it has none."""
    from mcp.server.lowlevel.server import request_ctx

    try:
        context = request_ctx.get()
    except LookupError:
        return None
    meta = getattr(context, "meta", None)
    if meta is None:
        return None
    if isinstance(meta, dict):
        extras: dict[str, Any] = meta
    else:
        extras = dict(getattr(meta, "model_extra", None) or {})
        if CREDENTIAL_META_KEY not in extras and hasattr(meta, CREDENTIAL_META_KEY):
            extras[CREDENTIAL_META_KEY] = getattr(meta, CREDENTIAL_META_KEY)
    if CREDENTIAL_META_KEY not in extras:
        return None
    credential = extras[CREDENTIAL_META_KEY]
    _validate_credential(credential)
    return credential


def _validate_credential(credential: Any) -> None:
    """Refuse, as -32602, a credential that is not the transport's shape.

    Checked before any request, so a malformed credential never reaches the
    purchase endpoint.
    """
    if not isinstance(credential, dict):
        raise _invalid_credential(f"{CREDENTIAL_META_KEY} must be an object.")
    challenge = credential.get("challenge")
    if not isinstance(challenge, dict):
        raise _invalid_credential("Missing required field: challenge.")
    for name in _REQUIRED_CHALLENGE_FIELDS:
        if name == "request":
            continue
        value = challenge.get(name)
        if not isinstance(value, str) or not value:
            raise _invalid_credential(f"Missing required field: challenge.{name}.")
    if not isinstance(challenge.get("request"), dict):
        raise _invalid_credential("challenge.request must be an object.")
    for name in ("expires", "digest", "description", "header"):
        if name in challenge and not isinstance(challenge[name], str):
            raise _invalid_credential(f"challenge.{name} must be a string.")
    if "opaque" in challenge and not isinstance(challenge["opaque"], (str, dict)):
        raise _invalid_credential("challenge.opaque must be a string or an object.")
    if not isinstance(credential.get("payload"), dict):
        raise _invalid_credential("Missing required field: payload.")
    if "source" in credential and not isinstance(credential["source"], str):
        raise _invalid_credential("source must be a string.")


def encode_credential(credential: dict[str, Any]) -> tuple[str, str]:
    """The HTTP field name and value that carry a JSON-RPC credential.

    The JSON-RPC form carries ``request`` (and possibly ``opaque``) as JSON; the
    HTTP form carries them base64url-encoded in canonical JSON, which is what
    the challenge id was computed over.
    """
    challenge = dict(credential["challenge"])
    for name in ("request", "opaque"):
        if isinstance(challenge.get(name), (dict, list)):
            challenge[name] = _b64url(_jcs(challenge[name]).encode("utf-8"))
    wire: dict[str, Any] = {"challenge": challenge, "payload": credential["payload"]}
    if isinstance(credential.get("source"), str):
        wire["source"] = credential["source"]
    encoded = _b64url(json.dumps(wire, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    header = challenge.get("header")
    field = (
        "Payment-Authorization"
        if isinstance(header, str) and header.lower() == "payment-authorization"
        else "Authorization"
    )
    return field, f"Payment {encoded}"


def _problem(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _problem_fields(problem: dict[str, Any]) -> dict[str, Any]:
    return {
        key: problem[key]
        for key in ("type", "title", "status", "detail")
        if isinstance(problem.get(key), (str, int))
    }


def _problem_code(problem: dict[str, Any]) -> str | None:
    kind = problem.get("type")
    if isinstance(kind, str) and kind:
        return kind.rstrip("/").rsplit("/", 1)[-1]
    return None


def _receipt(response: httpx.Response, credential: dict[str, Any]) -> dict[str, Any] | None:
    """The Payment-Receipt as the transport's receipt object, or None when it is unusable.

    Unusable: absent, not base64url JSON, missing status, method or timestamp,
    or naming another challenge than the one paid.
    """
    header = response.headers.get("payment-receipt")
    decoded = _decode_json_param(header.strip()) if header else None
    if not isinstance(decoded, dict):
        return None
    if any(not isinstance(decoded.get(name), str) for name in ("status", "method", "timestamp")):
        return None
    challenge_id = credential["challenge"]["id"]
    if decoded.get("challengeId", challenge_id) != challenge_id:
        return None
    receipt = dict(decoded)
    receipt["challengeId"] = challenge_id
    return receipt


# Said in the result when a paid call returns a key without a usable receipt.
# The key is still returned: the payment went through and the key was issued,
# and withholding it would leave the buyer charged with nothing to show.
RECEIPT_MISSING_NOTE = (
    "The purchase succeeded and the key above is valid, but the purchase "
    "endpoint sent no usable payment receipt. Keep this result as the record "
    "of the purchase; do not pay again."
)


def _success(response: httpx.Response, credential: dict[str, Any] | None) -> Any:
    """The key as the tool result, with the receipt in its _meta; a CallToolResult."""
    body = _problem(response)
    if not isinstance(body.get("api_key"), str) or not body["api_key"]:
        return {
            "error": "purchase_failed",
            "status_code": response.status_code,
            "message": (
                "The purchase endpoint answered without a key. If a payment was "
                "made, call again with the same arguments and the same "
                "credential: it is not charged twice."
            ),
        }
    receipt = _receipt(response, credential) if credential is not None else None
    if credential is not None and receipt is None:
        body = {**body, "receipt_missing": True, "receipt_note": RECEIPT_MISSING_NOTE}
    payload: dict[str, Any] = {
        "content": [TextContent(type="text", text=json.dumps(body, indent=2))],
        "structuredContent": body,
    }
    if receipt is not None:
        payload["_meta"] = {RECEIPT_META_KEY: receipt}
    return CallToolResult.model_validate(payload)


def _payment_error(
    response: httpx.Response, credential: dict[str, Any] | None
) -> dict[str, Any]:
    """Raise the 402 as -32042 (no credential) or -32043 (a refused one).

    A refused credential always raises -32043 with failure.reason and
    failure.detail set, with an empty challenges list when none parses. Only an
    unpaid call whose 402 carries no usable challenge returns a tool error:
    there is nothing to pay.
    """
    problem = _problem(response)
    challenges = parse_payment_challenges(response.headers.get_list("www-authenticate"))
    data: dict[str, Any] = {"httpStatus": 402, "challenges": challenges}
    if problem:
        data["problem"] = _problem_fields(problem)
    if credential is None:
        if not challenges:
            return {
                "error": "purchase_failed",
                "status_code": response.status_code,
                "message": "The purchase endpoint asked for payment without a usable challenge.",
                **({"problem": _problem_fields(problem)} if problem else {}),
            }
        raise PaymentError(PAYMENT_REQUIRED, "Payment Required", data)
    detail = problem.get("detail")
    data["failure"] = {
        "reason": _problem_code(problem) or FAILURE_REASON_FALLBACK,
        "detail": detail if isinstance(detail, str) and detail else FAILURE_DETAIL_FALLBACK,
    }
    raise PaymentError(PAYMENT_VERIFICATION_FAILED, "Payment Verification Failed", data)


def _refusal(response: httpx.Response) -> dict[str, Any]:
    problem = _problem(response)
    detail = problem.get("detail") if isinstance(problem.get("detail"), str) else None
    checkout_url = problem.get("checkout_url")
    if response.status_code == 409 and isinstance(checkout_url, str) and checkout_url:
        return {
            "error": "account_exists",
            "status_code": 409,
            "message": detail or "This email already has a Sugra account.",
            "checkout_url": checkout_url,
            "hint": (
                "The owner of that account buys the plan at checkout_url while "
                "signed in. No payment was taken."
            ),
        }
    if response.status_code == 404:
        return {
            "error": "purchase_unavailable",
            "status_code": 404,
            "message": (
                "Buying a plan through this tool is not available right now. "
                "list_plans returns a checkout link for each plan."
            ),
        }
    result: dict[str, Any] = {
        "error": "purchase_failed",
        "status_code": response.status_code,
        "message": detail or f"The purchase endpoint answered HTTP {response.status_code}.",
    }
    if problem:
        result["problem"] = _problem_fields(problem)
    return result


def _http_client() -> httpx.AsyncClient:
    transport = _transport
    return httpx.AsyncClient(
        timeout=PURCHASE_TIMEOUT_SECONDS,
        follow_redirects=False,
        **({"transport": transport} if transport is not None else {"verify": shared_ssl_context()}),
    )


BUY_TOOL_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
    title="Buy a plan",
)


@mcp.tool(annotations=BUY_TOOL_ANNOTATIONS)
@trace_mcp_tool("buy_plan")
async def buy_plan(
    plan: Annotated[
        Literal["dev", "pro"],
        Field(description="Plan to buy: dev or pro. list_plans shows their prices and limits."),
    ],
    cadence: Annotated[
        Literal["monthly", "annual"],
        Field(description="monthly buys one month, annual one fixed year. Neither renews."),
    ],
    email: Annotated[
        str,
        Field(description="Email for the new Sugra account the key belongs to."),
    ],
    accept_terms: Annotated[
        bool,
        Field(strict=True, description=f"Must be true: accepts the Terms of Service at {TERMS_URL}."),
    ],
) -> Annotated[CallToolResult, dict[str, Any]]:
    """Buy a Sugra API plan and receive a new API key, paid by the agent.

    Payment uses the Payment HTTP authentication scheme with Stripe. The first
    call fails with JSON-RPC error -32042 (Payment Required) and the payment
    challenge in error.data.challenges. Pay it, then call again with the same
    arguments and the credential in
    params._meta["org.paymentauth/credential"]. The result carries api_key,
    and the receipt is in result._meta["org.paymentauth/receipt"]. A refused
    payment fails with -32043 and a fresh challenge.

    The purchase creates a new Sugra account for email; an email that already
    has one gets a checkout_url for its owner instead, and no payment is taken.
    accept_terms must be true. Purchases do not auto-renew. No API key needed.
    """
    if accept_terms is not True:
        return {
            "error": "terms_not_accepted",
            "message": f"Set accept_terms to true to accept the Terms of Service at {TERMS_URL}.",
        }
    address = email.strip().lower()
    if len(address) > MAX_EMAIL_LENGTH or not _EMAIL_SHAPE.match(address):
        return {"error": "invalid_email", "message": "email must be a valid email address."}

    credential = _credential_from_meta()
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": f"sugra-api-mcp/{__version__}",
    }
    if credential is not None:
        field, value = encode_credential(credential)
        headers[field] = value

    try:
        async with _http_client() as client:
            response = await client.post(
                f"{PURCHASE_BASE_URL}/{plan}/{cadence}",
                content=request_body(address, accept_terms),
                headers=headers,
            )
    except httpx.HTTPError:
        retry = (
            " Call again with the same arguments and the same credential: it is "
            "not charged twice."
            if credential is not None
            else ""
        )
        return {
            "error": "purchase_unavailable",
            "message": "The purchase endpoint could not be reached." + retry,
        }

    if response.status_code == 200:
        return _success(response, credential)
    if response.status_code == 402:
        return _payment_error(response, credential)
    return _refusal(response)
