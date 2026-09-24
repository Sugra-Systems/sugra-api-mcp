"""buy_plan: a plan bought over the Payment HTTP authentication scheme.

The purchase endpoint is replaced by an in-process fake that follows the web
app's rules: a challenge id is an HMAC over the challenge fields, the request
and opaque values are base64url of canonical JSON, and the digest binds the
request body. A credential passes only when its echoed challenge still hashes
to its id and its body digest matches, so a round trip through the tool proves
the MCP challenge object converts back to exactly what the endpoint issued and
that both calls sent the same body bytes. The MCP side runs on the real
streamable HTTP app behind AuthMiddleware, without a Bearer token.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

import sugra_api_mcp.tools  # noqa: F401  (registers the tools on server.mcp)
from sugra_api_mcp import server
from sugra_api_mcp.auth import Authenticator, AuthMiddleware
from sugra_api_mcp.config import AuthConfig
from sugra_api_mcp.tools import purchase

REPO_ROOT = Path(__file__).resolve().parent.parent

SECRET = b"test-secret"
REALM = "app.sugra.ai"
EXPIRES = "2099-01-01T00:00:00Z"
DESCRIPTION = "Sugra API Dev plan, 1 month, one-time, no renewal"
# Canonical JSON written out by hand, so the test does not trust the module's
# own serializer: sorted keys, no whitespace.
REQUEST_JSON = (
    '{"amount":"2500","currency":"usd","description":"' + DESCRIPTION + '",'
    '"methodDetails":{"networkId":"net_test","paymentMethodTypes":["card","link"]}}'
)
OPAQUE_JSON = '{"cadence":"monthly","plan":"dev"}'
GOOD_SPT = "spt_good_token"
API_KEY = "sugra_new_key_for_test"
PAYMENT_INTENT = "pi_test_123"
CHECKOUT = "https://app.sugra.ai/subscribe/dev/monthly?channel=agent"

HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25",
        "capabilities": {},
        "clientInfo": {"name": "tests", "version": "0"},
    },
}
ARGUMENTS = {"plan": "dev", "cadence": "monthly", "email": "buyer@example.com", "accept_terms": True}


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


REQUEST_B64 = _b64url(REQUEST_JSON.encode("utf-8"))
OPAQUE_B64 = _b64url(OPAQUE_JSON.encode("utf-8"))


def _digest(body: bytes) -> str:
    return "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode("ascii") + ":"


def _challenge_id(fields: dict[str, str]) -> str:
    joined = "|".join(
        [
            fields.get("realm", ""),
            fields.get("method", ""),
            fields.get("intent", ""),
            fields.get("request", ""),
            fields.get("expires", ""),
            fields.get("digest", ""),
            fields.get("opaque", ""),
        ]
    )
    return _b64url(hmac.new(SECRET, joined.encode("utf-8"), hashlib.sha256).digest())


class FakePurchaseEndpoint:
    """The purchase endpoint's 402 flow, reduced to what the tool relies on."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.account_exists = False
        self.status_override: int | None = None
        self.issued = 0

    def challenge_header(self, body: bytes) -> str:
        self.issued += 1
        fields = {
            "realm": REALM,
            "method": "stripe",
            "intent": "charge",
            "request": REQUEST_B64,
            "expires": EXPIRES,
            "digest": _digest(body),
            "opaque": OPAQUE_B64,
        }
        params = {"id": _challenge_id(fields), **fields, "description": DESCRIPTION}
        return "Payment " + ", ".join(f'{key}="{value}"' for key, value in params.items())

    def _problem(self, status: int, code: str, detail: str, **extra: Any) -> dict[str, Any]:
        return {
            "type": f"https://paymentauth.org/problems/{code}",
            "title": code,
            "status": status,
            "detail": detail,
            **extra,
        }

    def _payment_required(self, body: bytes, code: str, detail: str) -> httpx.Response:
        return httpx.Response(
            402,
            json=self._problem(402, code, detail),
            headers={"WWW-Authenticate": self.challenge_header(body)},
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = request.content
        if self.status_override is not None:
            return httpx.Response(
                self.status_override,
                json=self._problem(self.status_override, "internal-payment-error", "Retry the same credential."),
            )
        authorization = request.headers.get("authorization")
        if self.account_exists:
            return httpx.Response(
                409,
                json=self._problem(409, "bad-request", "This email already has a Sugra account.", checkout_url=CHECKOUT),
            )
        if not authorization:
            return self._payment_required(body, "payment-required", "Pay this challenge to buy the plan.")
        credential = json.loads(_b64url_decode(authorization.split(" ", 1)[1]))
        echo = credential["challenge"]
        bound = {name: echo.get(name, "") for name in ("realm", "method", "intent", "request", "expires", "digest", "opaque")}
        if (
            not hmac.compare_digest(_challenge_id(bound), echo.get("id", ""))
            or echo.get("request") != REQUEST_B64
            or echo.get("opaque") != OPAQUE_B64
            or echo.get("digest") != _digest(body)
        ):
            return self._payment_required(body, "invalid-challenge", "The challenge was not issued for this request.")
        if credential["payload"].get("spt") != GOOD_SPT:
            return self._payment_required(body, "verification-failed", "The payment was not accepted: declined.")
        receipt = {"method": "stripe", "reference": PAYMENT_INTENT, "status": "success", "timestamp": "2026-09-24T10:00:00Z"}
        return httpx.Response(
            200,
            json={
                "api_key": API_KEY,
                "plan": "dev",
                "daily_limit": 5000,
                "cadence": "monthly",
                "starts_at": "2026-09-24T10:00:00Z",
                "ends_at": "2026-10-24T10:00:00Z",
                "renews": False,
                "account_email": "buyer@example.com",
                "account": "A Sugra account was created for this email.",
                "usage": "Send the key as the x-api-key header to https://sugra.ai.",
            },
            headers={"Payment-Receipt": _b64url(json.dumps(receipt, separators=(",", ":")).encode())},
        )


@pytest.fixture
def endpoint(monkeypatch) -> FakePurchaseEndpoint:
    fake = FakePurchaseEndpoint()
    monkeypatch.setattr(purchase, "_transport", httpx.MockTransport(fake.handler))
    return fake


class McpClient:
    """Raw JSON-RPC over the real streamable HTTP app, no Bearer token."""

    def __init__(self, client: httpx.AsyncClient, session_id: str) -> None:
        self._client = client
        self._session_id = session_id
        self._next_id = 10

    async def call(self, arguments: dict[str, Any], meta: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        params: dict[str, Any] = {"name": "buy_plan", "arguments": arguments}
        if meta is not None:
            params["_meta"] = meta
        response = await self._client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": self._next_id, "method": "tools/call", "params": params},
            headers={**HEADERS, "mcp-session-id": self._session_id},
        )
        assert response.status_code == 200, response.text
        message = next(
            json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
        )
        assert message["id"] == self._next_id
        return message


async def _with_client(monkeypatch, scenario) -> None:
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    app = server.mcp.streamable_http_app()
    authenticator = Authenticator(
        AuthConfig(app_url="http://127.0.0.1:9", jwks_url="http://127.0.0.1:9/jwks", internal_token="x")
    )
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    try:
        async with server.mcp.session_manager.run():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8002") as client:
                opened = await client.post("/mcp", json=INITIALIZE, headers=HEADERS)
                assert opened.status_code == 200
                session_id = opened.headers["mcp-session-id"]
                await client.post(
                    "/mcp",
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                    headers={**HEADERS, "mcp-session-id": session_id},
                )
                await scenario(McpClient(client, session_id))
    finally:
        await authenticator.aclose()


def _credential(challenge: dict[str, Any], spt: str = GOOD_SPT) -> dict[str, Any]:
    return {purchase.CREDENTIAL_META_KEY: {"challenge": challenge, "payload": {"spt": spt}}}


async def test_first_call_answers_payment_required_with_the_challenge(endpoint, monkeypatch) -> None:
    async def scenario(mcp: McpClient) -> None:
        message = await mcp.call(ARGUMENTS)
        assert "result" not in message
        error = message["error"]
        assert error["code"] == -32042
        assert error["message"] == "Payment Required"
        data = error["data"]
        assert data["httpStatus"] == 402
        assert data["problem"]["type"] == "https://paymentauth.org/problems/payment-required"
        [challenge] = data["challenges"]
        assert challenge["realm"] == REALM
        assert challenge["method"] == "stripe"
        assert challenge["intent"] == "charge"
        # request is a JSON object on the JSON-RPC side, not base64url.
        assert challenge["request"] == json.loads(REQUEST_JSON)
        assert challenge["expires"] == EXPIRES
        assert challenge["opaque"] == OPAQUE_B64
        assert challenge["description"] == DESCRIPTION
        assert challenge["digest"] == _digest(endpoint.requests[0].content)
        assert challenge["id"]

    await _with_client(monkeypatch, scenario)
    [request] = endpoint.requests
    assert request.method == "POST"
    assert str(request.url) == "https://app.sugra.ai/agent/mpp/dev/monthly"
    assert "authorization" not in request.headers
    assert json.loads(request.content) == {"accept_terms": True, "email": "buyer@example.com"}


async def test_paid_call_returns_the_key_and_the_receipt(endpoint, monkeypatch) -> None:
    async def scenario(mcp: McpClient) -> None:
        challenge = (await mcp.call(ARGUMENTS))["error"]["data"]["challenges"][0]
        message = await mcp.call(ARGUMENTS, meta=_credential(challenge))
        result = message["result"]
        assert result["isError"] is False
        assert result["structuredContent"]["api_key"] == API_KEY
        assert result["structuredContent"]["renews"] is False
        assert json.loads(result["content"][0]["text"])["api_key"] == API_KEY
        receipt = result["_meta"][purchase.RECEIPT_META_KEY]
        assert receipt == {
            "method": "stripe",
            "reference": PAYMENT_INTENT,
            "status": "success",
            "timestamp": "2026-09-24T10:00:00Z",
            "challengeId": challenge["id"],
        }

    await _with_client(monkeypatch, scenario)
    first, second = endpoint.requests
    # The same bytes on both calls: the challenge digest binds them.
    assert first.content == second.content
    credential = json.loads(_b64url_decode(second.headers["authorization"].removeprefix("Payment ")))
    # The echoed request is the endpoint's own base64url string again.
    assert credential["challenge"]["request"] == REQUEST_B64
    assert credential["payload"] == {"spt": GOOD_SPT}


class _Span:
    def __init__(self) -> None:
        self.attributes: dict[str, object] = {}

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def set_status(self, status: object) -> None:
        pass

    def end(self) -> None:
        pass


class _Tracer:
    def __init__(self) -> None:
        self.spans: list[_Span] = []

    def start_span(self, name: str) -> _Span:
        span = _Span()
        self.spans.append(span)
        return span


async def test_spans_carry_no_key_credential_or_email(endpoint, monkeypatch) -> None:
    from sugra_api_mcp import observability

    tracer = _Tracer()
    monkeypatch.setattr(observability, "_TRACER", tracer)

    async def scenario(mcp: McpClient) -> None:
        challenge = (await mcp.call(ARGUMENTS))["error"]["data"]["challenges"][0]
        await mcp.call(ARGUMENTS, meta=_credential(challenge))

    await _with_client(monkeypatch, scenario)
    spans = [s.attributes for s in tracer.spans if s.attributes.get("mcp.tool.name") == "buy_plan"]
    assert [s.get("mcp.success") for s in spans] == [False, True]
    assert spans[0]["mcp.exception.type"] == "PaymentError"
    text = repr([s.attributes for s in tracer.spans])
    for secret in (API_KEY, GOOD_SPT, "buyer@example.com", REQUEST_B64, PAYMENT_INTENT):
        assert secret not in text


async def test_body_bytes_ignore_email_case_and_whitespace(endpoint, monkeypatch) -> None:
    async def scenario(mcp: McpClient) -> None:
        challenge = (await mcp.call(ARGUMENTS))["error"]["data"]["challenges"][0]
        shouted = {**ARGUMENTS, "email": "  Buyer@Example.COM "}
        message = await mcp.call(shouted, meta=_credential(challenge))
        assert message["result"]["structuredContent"]["api_key"] == API_KEY

    await _with_client(monkeypatch, scenario)
    first, second = endpoint.requests
    assert first.content == second.content


async def test_refused_payment_answers_verification_failed_with_a_fresh_challenge(endpoint, monkeypatch) -> None:
    async def scenario(mcp: McpClient) -> None:
        challenge = (await mcp.call(ARGUMENTS))["error"]["data"]["challenges"][0]
        message = await mcp.call(ARGUMENTS, meta=_credential(challenge, spt="spt_declined"))
        error = message["error"]
        assert error["code"] == -32043
        assert error["message"] == "Payment Verification Failed"
        data = error["data"]
        assert data["httpStatus"] == 402
        assert data["failure"] == {
            "reason": "verification-failed",
            "detail": "The payment was not accepted: declined.",
        }
        [fresh] = data["challenges"]
        assert fresh["request"] == json.loads(REQUEST_JSON)
        # The retry with the fresh challenge goes through.
        retry = await mcp.call(ARGUMENTS, meta=_credential(fresh))
        assert retry["result"]["structuredContent"]["api_key"] == API_KEY

    await _with_client(monkeypatch, scenario)
    assert endpoint.issued == 2
    assert len({request.content for request in endpoint.requests}) == 1


async def test_a_challenge_for_another_body_is_refused(endpoint, monkeypatch) -> None:
    async def scenario(mcp: McpClient) -> None:
        challenge = (await mcp.call(ARGUMENTS))["error"]["data"]["challenges"][0]
        other = {**ARGUMENTS, "email": "someone.else@example.com"}
        message = await mcp.call(other, meta=_credential(challenge))
        assert message["error"]["code"] == -32043
        assert message["error"]["data"]["failure"]["reason"] == "invalid-challenge"

    await _with_client(monkeypatch, scenario)


async def test_existing_account_is_a_tool_error_with_the_checkout_link(endpoint, monkeypatch) -> None:
    endpoint.account_exists = True

    async def scenario(mcp: McpClient) -> None:
        message = await mcp.call(ARGUMENTS)
        result = message["result"]
        assert result["isError"] is True
        payload = result["structuredContent"]
        assert payload["error"] == "account_exists"
        assert payload["checkout_url"] == CHECKOUT
        assert CHECKOUT in result["content"][0]["text"]

    await _with_client(monkeypatch, scenario)


async def test_malformed_credential_is_invalid_params(endpoint, monkeypatch) -> None:
    async def scenario(mcp: McpClient) -> None:
        for bad in (
            "not-an-object",
            {"payload": {"spt": GOOD_SPT}},
            {"challenge": {"realm": REALM}, "payload": {"spt": GOOD_SPT}},
            {"challenge": {"id": "abc", "request": {}}},
        ):
            message = await mcp.call(ARGUMENTS, meta={purchase.CREDENTIAL_META_KEY: bad})
            assert message["error"]["code"] == -32602, bad
            assert message["error"]["data"]["detail"]

    await _with_client(monkeypatch, scenario)
    assert endpoint.requests == []


async def test_other_answers_pass_the_problem_through(endpoint, monkeypatch) -> None:
    endpoint.status_override = 500

    async def scenario(mcp: McpClient) -> None:
        result = (await mcp.call(ARGUMENTS))["result"]
        assert result["isError"] is True
        payload = result["structuredContent"]
        assert payload["error"] == "purchase_failed"
        assert payload["status_code"] == 500
        assert payload["message"] == "Retry the same credential."
        assert payload["problem"]["type"] == "https://paymentauth.org/problems/internal-payment-error"

    await _with_client(monkeypatch, scenario)


async def test_disabled_endpoint_is_unavailable(endpoint, monkeypatch) -> None:
    endpoint.status_override = 404

    async def scenario(mcp: McpClient) -> None:
        payload = (await mcp.call(ARGUMENTS))["result"]["structuredContent"]
        assert payload["error"] == "purchase_unavailable"
        assert "list_plans" in payload["message"]

    await _with_client(monkeypatch, scenario)


async def test_unreachable_endpoint_tells_a_payer_to_retry(monkeypatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(purchase, "_transport", httpx.MockTransport(refuse))
    challenge = {
        "id": "abc", "realm": REALM, "method": "stripe", "intent": "charge",
        "request": json.loads(REQUEST_JSON),
    }

    async def scenario(mcp: McpClient) -> None:
        unpaid = (await mcp.call(ARGUMENTS))["result"]["structuredContent"]
        assert unpaid["error"] == "purchase_unavailable"
        assert "same credential" not in unpaid["message"]
        paid = (await mcp.call(ARGUMENTS, meta=_credential(challenge)))["result"]["structuredContent"]
        assert paid["error"] == "purchase_unavailable"
        assert "same credential" in paid["message"]

    await _with_client(monkeypatch, scenario)


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({**ARGUMENTS, "accept_terms": False}, "terms_not_accepted"),
        ({**ARGUMENTS, "email": "not-an-address"}, "invalid_email"),
    ],
)
async def test_refused_before_any_request(endpoint, monkeypatch, arguments, code) -> None:
    async def scenario(mcp: McpClient) -> None:
        result = (await mcp.call(arguments))["result"]
        assert result["isError"] is True
        assert result["structuredContent"]["error"] == code

    await _with_client(monkeypatch, scenario)
    assert endpoint.requests == []


def test_request_body_is_canonical_and_stable() -> None:
    body = purchase.request_body("buyer@example.com", True)
    assert body == b'{"accept_terms":true,"email":"buyer@example.com"}'
    assert purchase.request_body("buyer@example.com", True) == body


def test_challenge_parser_reads_every_payment_challenge() -> None:
    request = _b64url(b'{"amount":"1"}')
    headers = [
        'Bearer realm="x", error="invalid_token", '
        f'Payment id="one", REALM="r", method="stripe", intent="charge", request="{request}", '
        'description="say \\"hi\\""',
        f'Payment id=two, realm=r, method=tempo, intent=charge, request={request}, extra="ignored"',
        # Dropped: no id, and a request that is not base64url JSON.
        f'Payment realm="r", method="stripe", intent="charge", request="{request}"',
        'Payment id="three", realm="r", method="stripe", intent="charge", request="%%%"',
        "Basic abc==",
    ]
    challenges = purchase.parse_payment_challenges(headers)
    assert [c["id"] for c in challenges] == ["one", "two"]
    assert challenges[0]["realm"] == "r"
    assert challenges[0]["description"] == 'say "hi"'
    assert challenges[0]["request"] == {"amount": "1"}
    assert challenges[1]["method"] == "tempo"
    assert "extra" not in challenges[1]


def test_credential_encoding_restores_the_wire_request() -> None:
    challenge = {"id": "x", "request": json.loads(REQUEST_JSON), "opaque": OPAQUE_B64}
    field, value = purchase.encode_credential({"challenge": challenge, "payload": {"spt": GOOD_SPT}})
    assert field == "Authorization"
    decoded = json.loads(_b64url_decode(value.removeprefix("Payment ")))
    assert decoded["challenge"]["request"] == REQUEST_B64
    assert decoded["challenge"]["opaque"] == OPAQUE_B64
    selected = {**challenge, "header": "Payment-Authorization"}
    field, _ = purchase.encode_credential({"challenge": selected, "payload": {}})
    assert field == "Payment-Authorization"


def test_buy_plan_is_listed_after_list_plans() -> None:
    names = [tool.name for tool in asyncio.run(server.mcp.list_tools())]
    assert names[-2:] == ["list_plans", "buy_plan"]


def test_existing_tools_are_unchanged_by_buy_plan() -> None:
    """Every other tool's definition is the same with and without buy_plan.

    A subprocess imports the tools with the purchase module replaced by an
    empty one, and lists them; the result must equal this process's list minus
    buy_plan, definitions and order alike.
    """
    code = (
        "import asyncio, json, sys, types\n"
        "sys.modules['sugra_api_mcp.tools.purchase'] = types.ModuleType('sugra_api_mcp.tools.purchase')\n"
        "import sugra_api_mcp.tools\n"
        "from sugra_api_mcp.server import mcp\n"
        "tools = asyncio.run(mcp.list_tools())\n"
        "print(json.dumps([t.model_dump(by_alias=True, exclude_none=True) for t in tools], sort_keys=True))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT, check=True
    )
    without = json.loads(out.stdout)
    with_buy = [
        tool.model_dump(by_alias=True, exclude_none=True)
        for tool in asyncio.run(server.mcp.list_tools())
        if tool.name != "buy_plan"
    ]
    assert without == json.loads(json.dumps(with_buy, sort_keys=True))
    assert len(without) == 9
