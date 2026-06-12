"""Agent-driven golden-query eval over the hosted MCP (MCP-2.4).

Each M0 golden query is answered by a REAL agent (Anthropic tool runner) whose
only capabilities are the 11 hosted MCP tools, connected over streamable HTTP.
The harness records which tools the agent called (selection scoring is
deterministic code, evals/scoring.py) and LLM-judges answer relevance on a
0-2 rubric. Output: a JSON result file + a markdown scorecard under
evals/results/ (gitignored except committed baselines).

This is a SCORECARD, not a gate: known_failure queries (B1 UK-CPI trap, B6
FRED-only macro loader) are tracked in their own bucket.

Run on demand (never in CI - needs live API, two secrets, and an LLM):

    pip install "anthropic[mcp]"
    SUGRA_TEST_API_KEY=... ANTHROPIC_API_KEY=... python -m evals.agent_eval [--ids A1,C5]

Env: EVAL_MODEL (default claude-opus-4-8), EVAL_JUDGE_MODEL (default = EVAL_MODEL),
SUGRA_MCP_URL, SUGRA_EVAL_CALL_TIMEOUT.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.lib.tools.mcp import async_mcp_tool

from evals.live_client import open_session, require_key
from evals.scoring import aggregate, load_manifest, selection_ok

EVAL_MODEL = os.environ.get("EVAL_MODEL", "claude-opus-4-8")
JUDGE_MODEL = os.environ.get("EVAL_JUDGE_MODEL", EVAL_MODEL)
RESULTS_DIR = Path(__file__).parent / "results"

AGENT_SYSTEM = (
    "You are a data agent for the Sugra API. Answer the user's question using "
    "the available tools. Be honest about freshness and coverage: if the data "
    "is stale, partial, ambiguous, or absent, say so explicitly - never invent "
    "values. Keep the final answer short and factual."
)

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "relevance": {"type": "integer", "enum": [0, 1, 2]},
        "rationale": {"type": "string"},
    },
    "required": ["relevance", "rationale"],
    "additionalProperties": False,
}

JUDGE_PROMPT = (
    "You are grading a data agent's answer.\n"
    "Question: {query}\n"
    "Contract note (may be empty): {notes}\n"
    "Agent's final answer:\n---\n{answer}\n---\n"
    "Score relevance 0-2: 2 = directly and correctly answers the question "
    "(honest 'data unavailable/stale/ambiguous' counts as correct when the "
    "contract note expects it); 1 = partially answers or answers with the "
    "wrong entity/country/series; 0 = does not answer, fabricates data, or "
    "silently picks one interpretation of an ambiguous query. Return JSON only."
)


async def run_query(client: AsyncAnthropic, session, spec: dict[str, Any]) -> dict[str, Any]:
    tools_result = await session.list_tools()
    tools = [async_mcp_tool(t, session) for t in tools_result.tools]
    called: list[str] = []
    answer_parts: list[str] = []
    started = time.perf_counter()

    runner = client.beta.messages.tool_runner(
        model=EVAL_MODEL,
        max_tokens=16000,
        system=AGENT_SYSTEM,
        tools=tools,
        messages=[{"role": "user", "content": spec["query"]}],
    )
    async for message in runner:
        for block in message.content:
            if block.type == "tool_use":
                called.append(block.name)
            elif block.type == "text" and block.text.strip():
                answer_parts.append(block.text)

    answer = answer_parts[-1] if answer_parts else ""
    judge = await client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=1024,
        output_config={"format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        messages=[{
            "role": "user",
            "content": JUDGE_PROMPT.format(
                query=spec["query"], notes=spec.get("notes", ""), answer=answer or "(no answer)"
            ),
        }],
    )
    verdict = json.loads(next(b.text for b in judge.content if b.type == "text"))

    return {
        "id": spec["id"],
        "known_failure": bool(spec.get("known_failure")),
        "called_tools": called,
        "selection_ok": selection_ok(spec, called),
        "relevance": verdict["relevance"],
        "judge_rationale": verdict["rationale"],
        "answer": answer,
        "duration_s": round(time.perf_counter() - started, 1),
        "error": None,
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", help="comma-separated golden ids (default: all)")
    args = parser.parse_args()

    require_key()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit("ANTHROPIC_API_KEY is not set - the eval agent needs it.")

    manifest = load_manifest()
    if args.ids:
        wanted = {i.strip() for i in args.ids.split(",")}
        manifest = [q for q in manifest if q["id"] in wanted]

    client = AsyncAnthropic()
    results: list[dict[str, Any]] = []
    for spec in manifest:
        print(f"[{spec['id']}] {spec['query']}")
        try:
            # Fresh MCP session per query: no tool-result bleed between queries.
            async with open_session() as session:
                result = await asyncio.wait_for(
                    run_query(client, session, spec),
                    timeout=600,
                )
        except Exception as exc:
            result = {
                "id": spec["id"], "known_failure": bool(spec.get("known_failure")),
                "called_tools": [], "selection_ok": False, "relevance": None,
                "judge_rationale": None, "answer": None, "duration_s": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        marker = "ERR " if result["error"] else ("ok  " if result["selection_ok"] else "SEL!")
        print(f"  {marker} tools={result['called_tools']} relevance={result['relevance']}")
        results.append(result)

    summary = aggregate(results)
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = {"model": EVAL_MODEL, "judge_model": JUDGE_MODEL, "summary": summary, "results": results}
    json_path = RESULTS_DIR / f"golden_{stamp}.json"
    json_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")

    lines = [
        f"# Golden eval scorecard {stamp}", "",
        f"Agent model: {EVAL_MODEL} | Judge: {JUDGE_MODEL}", "",
        f"- queries: {summary['total']} (errored: {summary['errored']})",
        f"- selection accuracy (core): {summary['selection_accuracy_core']}",
        f"- selection accuracy (known failures): {summary['selection_accuracy_known_failures']}",
        f"- relevance mean (0-2): {summary['relevance_mean']}",
        f"- core selection misses: {summary['failed_ids'] or 'none'}", "",
        "| id | sel | rel | tools | note |", "|---|---|---|---|---|",
    ]
    for r in results:
        note = r["error"] or (r["judge_rationale"] or "")[:80]
        lines.append(
            f"| {r['id']}{' (kf)' if r['known_failure'] else ''} "
            f"| {'+' if r['selection_ok'] else '-'} | {r['relevance']} "
            f"| {', '.join(r['called_tools']) or '-'} | {note} |"
        )
    md_path = RESULTS_DIR / f"golden_{stamp}.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"\nsummary: {json.dumps(summary)}")
    print(f"results: {json_path}\nscorecard: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
