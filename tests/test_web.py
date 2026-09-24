"""Tests for the public web surface: landing page, /health, auth boundary."""

from __future__ import annotations

import base64
import json
import re
from html.parser import HTMLParser
from urllib.parse import unquote

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from sugra_api_mcp import __version__, web
from sugra_api_mcp.auth import PUBLIC_GET_PATHS, Authenticator, AuthMiddleware
from sugra_api_mcp.config import AuthConfig
from sugra_api_mcp.web import health, landing


@pytest.fixture
def client() -> TestClient:
    """Mirror the prod app shape: routes + AuthMiddleware (inner layer)."""

    async def mcp_ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(
        routes=[
            Route("/mcp", mcp_ok, methods=["GET", "POST"]),
            Route("/", landing, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
        ]
    )
    config = AuthConfig(
        app_url="https://app.sugra.ai",
        jwks_url="https://app.sugra.ai/oauth/jwks.json",
        internal_token="test-internal-token",
    )
    app.add_middleware(AuthMiddleware, authenticator=Authenticator(config))
    return TestClient(app, raise_server_exceptions=False)


def test_landing_serves_html_unauthenticated(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "https://mcp.sugra.ai/mcp" in resp.text
    assert "Sugra API MCP" in resp.text
    assert '<a href="https://url.sugra.ai/claude"' in resp.text
    assert '<a href="https://url.sugra.ai/openai"' in resp.text
    assert "Add to Claude" in resp.text
    assert "Add to ChatGPT" in resp.text


class _LandingParser(HTMLParser):
    """Collects the install tabs, the copy targets, the copyable blocks and the links."""

    def __init__(self) -> None:
        super().__init__()
        self.tab_labels: list[str] = []
        self.radios: list[dict[str, str | None]] = []
        self.pre_ids: list[str] = []
        self.copy_targets: list[str] = []
        self.pre_text: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []
        self.icons: list[dict[str, str | None]] = []
        self._label = False
        self._pre: str | None = None
        self._href: str | None = None
        self._link_text = ""

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "input" and attr.get("type") == "radio":
            self.radios.append(attr)
        elif tag == "label":
            self._label = True
        elif tag == "pre":
            self._pre = attr["id"]
            self.pre_ids.append(attr["id"])
            self.pre_text[attr["id"]] = ""
        elif tag == "button" and "data-copy" in attr:
            self.copy_targets.append(attr["data-copy"])
        elif tag == "a":
            self._href = attr.get("href")
            self._link_text = ""
        elif tag == "link" and "icon" in (attr.get("rel") or ""):
            self.icons.append(attr)

    def handle_endtag(self, tag):
        if tag == "label":
            self._label = False
        elif tag == "pre":
            self._pre = None
        elif tag == "a" and self._href is not None:
            self.links.append((self._link_text.strip(), self._href))
            self._href = None

    def handle_data(self, data):
        if self._label:
            self.tab_labels.append(data)
        if self._pre is not None:
            self.pre_text[self._pre] += data
        if self._href is not None:
            self._link_text += data


def _parse_landing(client: TestClient) -> _LandingParser:
    parser = _LandingParser()
    parser.feed(client.get("/").text)
    return parser


def test_landing_has_one_tab_per_client_with_the_first_open(client: TestClient) -> None:
    parser = _parse_landing(client)
    assert parser.tab_labels == [
        "Claude", "ChatGPT", "Claude Code", "Codex", "Grok",
        "Gemini CLI", "Cursor", "VS Code", "Other",
    ]
    checked = [radio["id"] for radio in parser.radios if "checked" in radio]
    assert checked == ["t-claude"]


def test_every_copy_button_targets_its_own_block(client: TestClient) -> None:
    parser = _parse_landing(client)
    assert parser.pre_ids
    assert len(set(parser.pre_ids)) == len(parser.pre_ids)
    assert parser.copy_targets == parser.pre_ids


def test_landing_never_writes_a_key_or_the_alias_host(client: TestClient) -> None:
    text = client.get("/").text
    assert "sugra_" not in text.replace("sugra_api_mcp", "")
    assert "app.sugra.ai/mcp" not in text
    assert "—" not in text
    parser = _parse_landing(client)
    commands = {
        ident: body for ident, body in parser.pre_text.items() if ident.startswith("cmd-")
    }
    servers = [body for ident, body in commands.items() if not ident.endswith("-skills")]
    assert servers
    for body in servers:
        assert "https://mcp.sugra.ai/mcp" in body
        assert "SUGRA_API_KEY" in body
    # No copyable block carries a placeholder the user would have to edit.
    for body in parser.pre_text.values():
        assert "<your" not in body
        if "Authorization" in body:
            assert "SUGRA_API_KEY" in body or "${input:sugra-api-key}" in body


def test_header_commands_are_spelled_for_their_shell(client: TestClient) -> None:
    # The shell expands the variable before the CLI sees it: PowerShell reads a
    # bare $SUGRA_API_KEY as an unset PowerShell variable and sends an empty key.
    parser = _parse_landing(client)
    endpoint = "https://mcp.sugra.ai/mcp"
    for ident, add in (
        ("claude-code", "claude mcp add --transport http sugra"),
        ("grok", "grok mcp add --transport http sugra"),
        ("gemini", "gemini mcp add --transport http sugra"),
    ):
        assert parser.pre_text[f"cmd-{ident}"] == (
            f'{add} {endpoint} --header "Authorization: Bearer $SUGRA_API_KEY"'
        )
        assert parser.pre_text[f"cmd-{ident}-powershell"] == (
            f'{add} {endpoint} --header "Authorization: Bearer $env:SUGRA_API_KEY"'
        )
    assert parser.pre_text["cmd-codex"] == (
        f"codex mcp add sugra --url {endpoint} --bearer-token-env-var SUGRA_API_KEY"
    )


def _panel(text: str, ident: str) -> str:
    match = re.search(rf'<section class="panel" id="p-{ident}"[^>]*>(.*?)</section>', text, re.S)
    assert match, ident
    return match.group(1)


def test_tabs_point_at_the_public_listings_before_the_commands(client: TestClient) -> None:
    text = client.get("/").text
    claude, openai = 'href="https://url.sugra.ai/claude"', 'href="https://url.sugra.ai/openai"'
    skills = 'href="https://chatgpt.com/plugins/plugins_6aa4f7db79848191a81e4048990545ef"'

    claude_code = _panel(text, "claude-code")
    assert claude_code.index(claude) < claude_code.index('id="cmd-claude-code"')
    assert "<code>/mcp</code>" in claude_code
    assert skills not in claude_code

    codex = _panel(text, "codex")
    assert codex.index(openai) < codex.index('id="cmd-codex"')
    assert codex.index('id="cmd-codex"') < codex.index(skills)
    assert codex.index(skills) < codex.index('id="cmd-codex-skills"')

    chatgpt = _panel(text, "chatgpt")
    assert chatgpt.index(openai) < chatgpt.index(skills)
    assert "<pre" not in chatgpt

    # Tabs with no public listing for them keep only their commands.
    for ident in ("grok", "gemini", "cursor", "vscode", "other"):
        panel = _panel(text, ident)
        for listing in (claude, openai, skills):
            assert listing not in panel, (ident, listing)

    parser = _parse_landing(client)
    assert parser.pre_text["cmd-claude-code-skills"] == (
        "claude plugin marketplace add Sugra-Systems/sugra-api-skills\n"
        "claude plugin install sugra-api@sugra-api-skills"
    )
    assert parser.pre_text["cmd-codex-skills"] == (
        "codex plugin marketplace add Sugra-Systems/sugra-api-skills\n"
        "codex plugin add sugra-api@sugra-api-skills"
    )


def test_highlighted_blocks_copy_as_the_plain_source(client: TestClient) -> None:
    # Copy takes the block's text, so the syntax spans must add nothing to it.
    parser = _parse_landing(client)
    assert json.loads(parser.pre_text["cfg-cursor"]) == {
        "mcpServers": {"sugra": web.CURSOR_CONFIG}
    }
    vscode = json.loads(parser.pre_text["cfg-vscode"])
    assert vscode["inputs"][0]["password"] is True
    assert vscode["servers"]["sugra"]["headers"] == {
        "Authorization": "Bearer ${input:sugra-api-key}"
    }
    assert parser.pre_text["cmd-grok-skills"] == (
        "grok plugin install Sugra-Systems/sugra-api-skills#plugins/sugra-api"
    )
    assert parser.pre_text["cfg-url"] == "https://mcp.sugra.ai/mcp"


def test_code_blocks_use_the_console_palette(client: TestClient) -> None:
    text = client.get("/").text
    for rule in (
        "background: #0F1115", "background: #171B22", "border: 1px solid #30363D",
        "color: #D7DAE0", ".b { color: #86EF8A; }", ".s { color: #F6B94A; }",
        ".v { color: #D879F0; }", ".k { color: #38BDF8; }",
        ".o { color: #FB7185; }", ".p { color: #CBD5E1; }",
    ):
        assert rule in text
    assert '<span class="b">claude</span>' in text
    assert '<span class="v">$env:SUGRA_API_KEY</span>' in text
    assert '<span class="k">"password"</span><span class="p">:</span>' in text
    assert '<span class="o">true</span>' in text
    assert '<span class="v">${env:SUGRA_API_KEY}</span>' in text


def test_landing_carries_the_standard_chrome(client: TestClient) -> None:
    parser = _parse_landing(client)
    rels = {icon["rel"]: icon["href"] for icon in parser.icons}
    assert rels["icon"] and rels["apple-touch-icon"]
    assert any((icon["href"] or "").startswith("data:image/svg+xml,") for icon in parser.icons)
    links = dict(parser.links)
    assert links["Get API key"].startswith("https://app.sugra.ai/register")
    assert links["Sign in"].startswith("https://app.sugra.ai/login")
    assert links["Privacy"] == "https://sugra.systems/privacy-policy"
    docs = [href for label, href in parser.links if label == "Docs"]
    assert len(docs) == 2
    assert set(docs) == {"https://docs.sugra.ai"}
    assert "decisions that matter." in client.get("/").text


def test_cursor_install_link_carries_the_env_reference_not_a_key() -> None:
    link = web.CURSOR_INSTALL_LINK
    prefix = "cursor://anysphere.cursor-deeplink/mcp/install?name=sugra&config="
    assert link.startswith(prefix)
    config = json.loads(base64.b64decode(unquote(link[len(prefix):])))
    assert config == {
        "url": "https://mcp.sugra.ai/mcp",
        "headers": {"Authorization": "Bearer ${env:SUGRA_API_KEY}"},
    }


def test_health_serves_json_unauthenticated(client: TestClient) -> None:
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "sugra-api-mcp"
    assert body["version"] == __version__


def test_unknown_path_still_requires_auth(client: TestClient) -> None:
    resp = client.get("/anything-else")
    assert resp.status_code == 401
    assert resp.json() == {"error": "missing_bearer_token"}


def test_get_mcp_still_requires_auth(client: TestClient) -> None:
    resp = client.get("/mcp")
    assert resp.status_code == 401


def test_post_to_public_paths_not_exempt(client: TestClient) -> None:
    # The allowlist is GET-only: a POST to / or /health must hit auth.
    for path in ("/", "/health"):
        resp = client.post(path)
        assert resp.status_code == 401, path


def test_head_requests_allowed_on_public_paths(client: TestClient) -> None:
    # Uptime monitors and load balancers commonly probe with HEAD.
    for path in ("/", "/health"):
        resp = client.head(path)
        assert resp.status_code == 200, path


def test_public_initialize_still_passes(client: TestClient) -> None:
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
    )
    assert resp.status_code == 200


def test_allowlist_is_exactly_root_and_health() -> None:
    assert frozenset({"/", "/health"}) == PUBLIC_GET_PATHS


def test_slash_variants_stay_behind_auth(client: TestClient) -> None:
    # STRICT matching: normalization tricks never widen the public surface.
    # (// and //// are exercised at the ASGI layer below - httpx normalizes
    # or rejects them client-side before the server ever sees them.)
    for path in ("/health/", "/health//", "/HEALTH"):
        resp = client.get(path)
        assert resp.status_code == 401, path


@pytest.mark.anyio
async def test_raw_slash_paths_stay_behind_auth() -> None:
    """Drive the middleware at the ASGI layer with paths httpx cannot send."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def ok(_request):
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/", ok), Route("/health", ok)])
    config = AuthConfig(
        app_url="https://app.sugra.ai",
        jwks_url="https://app.sugra.ai/oauth/jwks.json",
        internal_token="test-internal-token",
    )
    app.add_middleware(AuthMiddleware, authenticator=Authenticator(config))

    def make_channel(statuses: list[int]):
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message, _statuses=statuses):
            if message["type"] == "http.response.start":
                _statuses.append(message["status"])

        return receive, send

    for raw_path in ("//", "////", "//health"):
        status_holder: list[int] = []
        receive, send = make_channel(status_holder)

        scope = {
            "type": "http",
            "method": "GET",
            "path": raw_path,
            "raw_path": raw_path.encode(),
            "query_string": b"",
            "headers": [],
        }
        await app(scope, receive, send)
        assert status_holder and status_holder[0] == 401, raw_path


def test_real_http_app_route_registration() -> None:
    """Pin the PROD app shape: streamable_http_app + appended public routes."""
    from starlette.routing import Route

    from sugra_api_mcp.server import mcp
    from sugra_api_mcp.web import health, landing

    app = mcp.streamable_http_app()
    app.router.routes.append(Route("/", landing, methods=["GET"]))
    app.router.routes.append(Route("/health", health, methods=["GET"]))
    paths = [getattr(r, "path", None) for r in app.router.routes]
    # /mcp stays FIRST (appended routes cannot shadow it); both new routes present.
    assert paths.index("/mcp") < paths.index("/")
    assert paths.index("/mcp") < paths.index("/health")
