"""Thin live MCP session helper for the eval harness and smoke set.

Uses the official mcp SDK streamable-http client (already a package
dependency via FastMCP) rather than a hand-rolled protocol implementation -
session negotiation, SSE parsing and tool-call framing are the SDK's job.
Deliberately NOT the claude.ai connector path (known to hang from agent
sessions); this is a direct httpx-backed connection with hard timeouts.

Auth: hosted AuthMiddleware accepts raw `sugra_` API keys as Bearer
(sugra_api_mcp/auth.py), so SUGRA_TEST_API_KEY works directly.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

DEFAULT_URL = os.environ.get("SUGRA_MCP_URL", "https://app.sugra.ai/mcp")
CALL_TIMEOUT_S = float(os.environ.get("SUGRA_EVAL_CALL_TIMEOUT", "60"))


def require_key() -> str:
    key = os.environ.get("SUGRA_TEST_API_KEY", "").strip()
    if not key:
        raise SystemExit(
            "SUGRA_TEST_API_KEY is not set. Live runs need the Internal-Testing "
            "API key as Bearer for the hosted MCP - refusing to run."
        )
    return key


@asynccontextmanager
async def open_session(url: str = DEFAULT_URL, key: str | None = None):
    """Async context manager yielding an initialized ClientSession."""
    bearer = key or require_key()
    async with streamablehttp_client(
        url,
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=timedelta(seconds=CALL_TIMEOUT_S),
    ) as (read, write, _get_session_id), ClientSession(read, write) as session:
        await session.initialize()
        yield session


def result_json(result: Any) -> Any:
    """Parse the first text content block of a tools/call result as JSON."""
    for block in result.content:
        if getattr(block, "type", None) == "text":
            try:
                return json.loads(block.text)
            except ValueError:
                return {"_raw_text": block.text}
    return None
