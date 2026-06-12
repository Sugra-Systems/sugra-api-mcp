"""Deterministic live smoke set for the hosted MCP agent surface (MCP-2.4).

Run on demand against app.sugra.ai/mcp (never in CI - needs the live API and
SUGRA_TEST_API_KEY):

    python -m evals.smoke_live

Checks (design doc section 8 smoke set, adapted to what is OBSERVABLE live):
  S1  unauthenticated tools/call -> 401 (tools/list stays public by design:
      MCP discovery allowlist in auth.py; the card pins the CALL boundary)
  S2  tools/list == EXPECTED_HOSTED_TOOL_COUNT (11)
  S3  weighted cost: two sequential company_snapshot calls decrement
      billing.remaining by the recipe cost (2) each - billing is computed in
      the route BEFORE the payload cache, so a cache hit still charges
  S4  bounded output: get_timeseries max_points=5 -> <= 5 points and an honest
      downsampled flag
  S5  C5 ambiguity contract: resolve_entity("META") -> status ambiguous with
      ranked candidates and NO silently picked entity
  S6  E2 negative contract: resolve_entity(garbage) -> clean not-found, no
      fabricated entity
  S7  freshness honesty: etf_snapshot envelope carries a structurally honest
      freshness block (stale is bool or null, never invented strings)

Required-component-fail -> 502 and optional-fail -> partial are NOT injectable
against prod; they are pinned by the API unit tests (prod-sugra-ai-API
tests/test_agent_compose.py). S7 asserts the partial-semantics FIELDS exist.

Exit code 0 = all checks pass.
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from evals.live_client import DEFAULT_URL, open_session, require_key, result_json

EXPECTED_HOSTED_TOOL_COUNT = 11
COMPANY_SNAPSHOT_COST = 2

CHECKS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}")


async def s1_unauthenticated_call_401() -> None:
    # tools/list is DELIBERATELY public (MCP discovery allowlist, auth.py) -
    # the auth boundary the card pins is tools/CALL without a key -> 401.
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            DEFAULT_URL,
            json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "resolve_entity", "arguments": {"query": "AAPL"}},
            },
            headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"},
        )
    record("S1 unauthenticated tools/call -> 401", response.status_code == 401, f"got {response.status_code}")


async def s2_tool_count(session) -> None:
    tools = await session.list_tools()
    names = sorted(t.name for t in tools.tools)
    ok = len(names) == EXPECTED_HOSTED_TOOL_COUNT and {
        "resolve_entity", "get_snapshot", "get_timeseries"
    }.issubset(set(names))
    record("S2 tools/list == 11 incl agent tools", ok, f"got {len(names)}: {names}")


async def s3_weighted_cost(session) -> None:
    entity = {"namespace": "equity", "ids": {"symbol": "AAPL"}}
    args = {"recipe": "company_snapshot", "entity": entity}
    first = result_json(await session.call_tool("get_snapshot", args))
    second = result_json(await session.call_tool("get_snapshot", args))
    try:
        remaining_a = first["billing"]["remaining"]
        remaining_b = second["billing"]["remaining"]
    except (KeyError, TypeError):
        record("S3 weighted cost decrements quota", False, f"no billing block: {first} / {second}")
        return
    delta = remaining_a - remaining_b
    # Exact match expected on an otherwise idle test key; other traffic on the
    # key inflates the delta - report honestly rather than flake.
    ok = delta == COMPANY_SNAPSHOT_COST
    record(
        "S3 weighted cost decrements quota",
        ok,
        f"remaining {remaining_a} -> {remaining_b} (delta {delta}, expected {COMPANY_SNAPSHOT_COST}"
        + ("" if ok else "; concurrent traffic on the key can inflate the delta - rerun when idle")
        + ")",
    )


async def s4_bounded_points(session) -> None:
    entity = {"namespace": "equity", "ids": {"symbol": "AAPL"}}
    result = result_json(
        await session.call_tool(
            "get_timeseries",
            {"metric": "price", "entity": entity, "granularity": "1d", "max_points": 5},
        )
    )
    points = (result or {}).get("data", {}).get("points")
    downsampled = (result or {}).get("data", {}).get("downsampled")
    ok = isinstance(points, list) and len(points) <= 5 and isinstance(downsampled, bool)
    record(
        "S4 max_points bounded + honest downsampled flag",
        ok,
        f"points={len(points) if isinstance(points, list) else points} downsampled={downsampled}",
    )


async def s5_ambiguity_contract(session) -> None:
    result = result_json(await session.call_tool("resolve_entity", {"query": "META"}))
    status = (result or {}).get("status")
    candidates = (result or {}).get("candidates") or []
    namespaces = {c.get("namespace") for c in candidates}
    ok = status == "ambiguous" and len(candidates) >= 2 and "entity" not in (result or {})
    record(
        "S5 META ambiguity: ranked candidates, no silent pick",
        ok,
        f"status={status} candidates={len(candidates)} namespaces={sorted(str(n) for n in namespaces)}",
    )


async def s6_clean_not_found(session) -> None:
    result = result_json(await session.call_tool("resolve_entity", {"query": "Zyqqurat Holdings QXJ"}))
    status = (result or {}).get("status")
    ok = status in ("not_found", "none") and not (result or {}).get("entity")
    record("S6 garbage resolve: clean not-found, nothing fabricated", ok, f"status={status}")


async def s7_freshness_honesty(session) -> None:
    entity = {"namespace": "etf", "ids": {"symbol": "SPY"}}
    result = result_json(
        await session.call_tool("get_snapshot", {"recipe": "etf_snapshot", "entity": entity})
    )
    freshness = (result or {}).get("freshness") or {}
    coverage = (result or {}).get("coverage")
    status = (result or {}).get("status")
    stale = freshness.get("stale")
    ok = (
        status in ("full", "partial")
        and isinstance(coverage, list)
        and (stale is None or isinstance(stale, bool))
    )
    record(
        "S7 freshness block honest + partial-semantics fields present",
        ok,
        f"status={status} stale={stale!r} coverage_components={len(coverage) if isinstance(coverage, list) else coverage}",
    )


async def main() -> int:
    require_key()
    await s1_unauthenticated_call_401()
    async with open_session() as session:
        await s2_tool_count(session)
        await s3_weighted_cost(session)
        await s4_bounded_points(session)
        await s5_ambiguity_contract(session)
        await s6_clean_not_found(session)
        await s7_freshness_honesty(session)
    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
