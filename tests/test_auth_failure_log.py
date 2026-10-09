"""The auth failure line names the token's shape and the caller's classes, never token text."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import warnings
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from sugra_api_mcp import auth as auth_module
from sugra_api_mcp.auth import Authenticator, AuthError, AuthMiddleware
from sugra_api_mcp.config import AuthConfig

LINE_RE = re.compile(
    r"auth_failed status=(?P<status>\d+) kind=(?P<kind>\S+) fp=(?P<fp>\S+) "
    r"host=(?P<host>\S+) ua=(?P<ua>\S+) origin=(?P<origin>\S+) msg=(?P<msg>.*)\Z",
    re.DOTALL,
)

WWW_AUTHENTICATE = (
    'Bearer resource_metadata="https://app.sugra.ai/.well-known/oauth-protected-resource", '
    'error="invalid_token"'
)


def _segment(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _json_segment(value: dict) -> str:
    return _segment(json.dumps(value, separators=(",", ":")).encode("utf-8"))


# Token values are fixed, and no 4-character piece of them is hex or digits only, so
# the 8-hex fingerprint and the timing numbers on other lines can never form one.
SHORT_OPAQUE = "QZXKWVMRTN"
LONG_OPAQUE = "QZXKWVMRTNBLPJGHYQZXKWVMRTNBLPJGHYQZXKWVMR"
KEY_SHAPED = "sugra_QZXKWVMRTNBLPJGHYQZXKWVMRTNBLPJGHY"
PLACEHOLDER = "${SUGRA_API_KEY}"
JWT_HEADER = _json_segment({"alg": "RS256", "typ": "JWT"})
JWT_BAD_SIGNATURE = f"{JWT_HEADER}.QZXKWVMRTNBLPJGH.WVMRTNBLPJGHQZXKWVMRTNBLPJGHQZXK"
JWT_SHAPED_GARBAGE = "QZXKWV.MRTNBL.PJGHQZ"

_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def _pieces(value: str) -> set[str]:
    pieces = {value[i:i + 4] for i in range(len(value) - 3)}
    assert not any(re.fullmatch(r"[0-9a-f]{4}", piece) for piece in pieces), value
    return pieces


def _texts(records: list[logging.LogRecord]) -> list[str]:
    """The rendered message, every non-standard string attribute, and exception text."""
    texts: list[str] = []
    for record in records:
        texts.append(record.getMessage())
        texts.extend(
            value for key, value in vars(record).items()
            if key not in _STANDARD_ATTRS and isinstance(value, str)
        )
        exc = record.exc_info[1] if record.exc_info else None
        seen: set[int] = set()
        while exc is not None and id(exc) not in seen:
            seen.add(id(exc))
            texts.append(str(exc))
            exc = exc.__cause__ or exc.__context__
    return texts


@pytest.fixture
def records():
    """Every record any logger creates during the test, at DEBUG and up."""
    captured: list[logging.LogRecord] = []
    base_factory = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = base_factory(*args, **kwargs)
        captured.append(record)
        return record

    loggers = [logging.getLogger()] + [
        item for item in logging.Logger.manager.loggerDict.values()
        if isinstance(item, logging.Logger)
    ]
    saved = [(item, item.level, item.disabled) for item in loggers]
    saved_disable = logging.root.manager.disable
    logging.disable(logging.NOTSET)
    for item in loggers:
        item.disabled = False
        if item is logging.getLogger() or item.level != logging.NOTSET:
            item.setLevel(logging.DEBUG)
    logging.setLogRecordFactory(factory)
    try:
        yield captured
    finally:
        logging.setLogRecordFactory(base_factory)
        for item, level, disabled in saved:
            item.setLevel(level)
            item.disabled = disabled
        logging.disable(saved_disable)


@pytest.fixture
def authenticator() -> Authenticator:
    """A real authenticator whose JWKS answers from memory with one unrelated key."""
    auth = Authenticator(AuthConfig(
        app_url="https://app.sugra.ai",
        jwks_url="https://app.sugra.ai/oauth/jwks.json",
        internal_token="internal",
    ))
    unrelated_pair = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signing_key = MagicMock()
    signing_key.key = unrelated_pair.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    signing_key.key_id = "k1"
    auth._jwks.get_signing_keys = MagicMock(return_value=[signing_key])
    return auth


def _client(auth: Authenticator) -> TestClient:
    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/mcp", ok, methods=["POST"])])
    app.add_middleware(AuthMiddleware, authenticator=auth)
    return TestClient(app)


def _post(client: TestClient, token: str, headers: dict[str, str] | None = None):
    return client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
              "params": {"name": "list_toolsets", "arguments": {}}},
        headers={"authorization": f"Bearer {token}", **(headers or {})},
    )


def _failure_lines(records: list[logging.LogRecord]) -> list[dict[str, str]]:
    lines = []
    for record in records:
        message = record.getMessage()
        if record.name == "sugra_mcp.auth" and message.startswith("auth_failed "):
            assert record.levelno == logging.WARNING
            match = LINE_RE.match(message)
            assert match is not None, message
            lines.append(match.groupdict())
    return lines


def _key_shaped_authenticator(auth: Authenticator) -> Authenticator:
    """A sugra_ key resolves without failing today; make one fail to reach the line."""
    auth.resolve = AsyncMock(
        side_effect=AuthError("User has no API key", status=403))
    return auth


@pytest.mark.parametrize(
    ("token", "status", "message", "key_shaped"),
    [
        (SHORT_OPAQUE, 401, "Malformed token: Not enough segments", False),
        (LONG_OPAQUE, 401, "Malformed token: Not enough segments", False),
        (PLACEHOLDER, 401, "Malformed token: Not enough segments", False),
        (JWT_BAD_SIGNATURE, 401, "Invalid token: Signature verification failed", False),
        (KEY_SHAPED, 403, "User has no API key", True),
    ],
    ids=["short_opaque", "long_opaque", "placeholder", "jwt_bad_signature", "key_shaped"],
)
def test_auth_failure_line_carries_no_token_text(
    authenticator, records, token, status, message, key_shaped,
):
    auth = _key_shaped_authenticator(authenticator) if key_shaped else authenticator
    pieces = _pieces(token)

    response = _post(_client(auth), token)

    # The client still gets the same answer.
    assert response.status_code == status
    assert response.json() == {"error": "auth_failed", "message": message}
    assert response.headers.get("WWW-Authenticate") == (
        WWW_AUTHENTICATE if status == 401 else None)

    texts = _texts(records)
    assert any(text.startswith("auth_failed ") for text in texts)
    for text in texts:
        found = sorted(piece for piece in pieces if piece in text)
        assert not found, (found, text)
    lines = _failure_lines(records)
    assert len(lines) == 1
    assert lines[0]["status"] == str(status)
    assert lines[0]["msg"] == message


@pytest.mark.parametrize(
    ("token", "kind", "key_shaped"),
    [
        (SHORT_OPAQUE, "other", False),
        (LONG_OPAQUE, "other", False),
        (JWT_BAD_SIGNATURE, "jwt", False),
        (JWT_SHAPED_GARBAGE, "jwt", False),
        (PLACEHOLDER, "placeholder", False),
        ("$SUGRA_API_KEY", "placeholder", False),
        ("{{SUGRA_API_KEY}}", "placeholder", False),
        ("<YOUR_API_KEY>", "placeholder", False),
        ("${env:SUGRA_API_KEY}", "placeholder", False),
        (KEY_SHAPED, "api_key", True),
        ("", "empty", False),
    ],
    ids=["short_opaque", "long_opaque", "jwt_bad_signature", "jwt_shaped_garbage",
         "placeholder_braced", "placeholder_bare", "placeholder_double_braced",
         "placeholder_angled", "placeholder_env_prefixed", "key_shaped", "empty"],
)
def test_auth_failure_line_names_the_token_shape(
    authenticator, records, token, kind, key_shaped,
):
    auth = _key_shaped_authenticator(authenticator) if key_shaped else authenticator

    response = _post(_client(auth), token)

    assert response.json()["error"] == "auth_failed"
    lines = _failure_lines(records)
    assert len(lines) == 1
    assert lines[0]["kind"] == kind
    if kind == "empty":
        assert lines[0]["fp"] == "-"
    else:
        assert re.fullmatch(r"[0-9a-f]{8}", lines[0]["fp"])


def test_auth_failure_fingerprint_links_repeats_of_one_token(authenticator, records):
    client = _client(authenticator)

    for token in (SHORT_OPAQUE, SHORT_OPAQUE, LONG_OPAQUE, PLACEHOLDER):
        assert _post(client, token).status_code == 401

    fps = [line["fp"] for line in _failure_lines(records)]
    assert len(fps) == 4
    assert all(re.fullmatch(r"[0-9a-f]{8}", fp) for fp in fps)
    assert fps[0] == fps[1]
    assert len({fps[0], fps[2], fps[3]}) == 3
    # Keyed per process: not a plain digest anyone could recompute from a guess.
    assert fps[0] != hashlib.sha256(SHORT_OPAQUE.encode()).hexdigest()[:8]


def test_a_new_fingerprint_key_stops_linking_to_the_old_fingerprints(monkeypatch):
    monkeypatch.setattr(auth_module, "_fingerprint_key", auth_module._fingerprint_key)
    before = auth_module._token_fingerprint(SHORT_OPAQUE)
    assert auth_module._token_fingerprint(SHORT_OPAQUE) == before

    auth_module._draw_fingerprint_key()

    after = auth_module._token_fingerprint(SHORT_OPAQUE)
    assert re.fullmatch(r"[0-9a-f]{8}", after)
    assert after != before
    assert auth_module._token_fingerprint(SHORT_OPAQUE) == after


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
def test_a_forked_worker_fingerprints_under_its_own_key():
    parent = auth_module._token_fingerprint(SHORT_OPAQUE)
    read_end, write_end = os.pipe()
    with warnings.catch_warnings():
        # The test process may run threads; the child only hashes and exits.
        warnings.simplefilter("ignore", DeprecationWarning)
        pid = os.fork()
    if pid == 0:
        try:
            os.close(read_end)
            os.write(write_end, auth_module._token_fingerprint(SHORT_OPAQUE).encode("ascii"))
        finally:
            os._exit(0)
    os.close(write_end)
    try:
        child = os.read(read_end, 64).decode("ascii")
    finally:
        os.close(read_end)
        os.waitpid(pid, 0)

    assert re.fullmatch(r"[0-9a-f]{8}", child)
    assert child != parent
    assert auth_module._token_fingerprint(SHORT_OPAQUE) == parent


@pytest.mark.parametrize(
    ("headers", "host", "ua", "origin"),
    [
        (
            {"host": "mcp.sugra.ai", "user-agent": "acme-agent/1.0 (QZXKWV-build)",
             "origin": "https://acme.example"},
            "mcp.sugra.ai", "other", "other",
        ),
        (
            {"host": "localhost:8002", "user-agent": "curl/8.5.0"},
            "loopback", "curl", "none",
        ),
    ],
    ids=["named_host", "loopback_host"],
)
def test_auth_failure_line_carries_caller_classes_not_header_text(
    authenticator, records, headers, host, ua, origin,
):
    response = _post(_client(authenticator), SHORT_OPAQUE, headers=headers)

    assert response.status_code == 401
    lines = _failure_lines(records)
    assert len(lines) == 1
    assert (lines[0]["host"], lines[0]["ua"], lines[0]["origin"]) == (host, ua, origin)
    texts = _texts(records)
    for raw in (headers["user-agent"], headers.get("origin")):
        if raw is not None:
            assert not any(raw in text for text in texts), raw
    assert not any("QZXKWV-build" in text for text in texts)


@pytest.mark.parametrize(
    "header",
    [
        json.dumps({"alg": "RS256", "crit": ["QZXKWVMRTNBL"]}).encode(),
        b'{"alg":"QZXKWVMRTNBL\x80"}',
    ],
    ids=["unsupported_crit_value", "undecodable_header_byte"],
)
def test_malformed_token_reason_never_quotes_the_decoded_header(
    authenticator, records, header,
):
    token = f"{_segment(header)}.QZXKWVMRTNBLPJGH.WVMRTNBLPJGHQZXK"

    response = _post(_client(authenticator), token)

    assert response.status_code == 401
    assert response.json() == {"error": "auth_failed", "message": "Malformed token"}
    lines = _failure_lines(records)
    assert len(lines) == 1
    assert lines[0]["kind"] == "jwt"
    assert lines[0]["msg"] == "Malformed token"
    for text in _texts(records):
        assert "QZXKWVMRTNBL" not in text, text
        assert "0x80" not in text, text


def test_unknown_signing_key_reason_never_quotes_the_key_id(authenticator, records):
    header = _json_segment({"alg": "RS256", "typ": "JWT", "kid": "QZXKWVMRTNBL"})
    token = f"{header}.{_json_segment({'sub': '42'})}.WVMRTNBLPJGHQZXK"

    response = _post(_client(authenticator), token)

    assert response.status_code == 401
    assert response.json() == {"error": "auth_failed", "message": "Unknown signing key"}
    lines = _failure_lines(records)
    assert len(lines) == 1
    assert lines[0]["msg"] == "Unknown signing key"
    for text in _texts(records):
        assert "QZXKWVMRTNBL" not in text, text
