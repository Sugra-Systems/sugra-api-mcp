// Minimal agent that connects to the hosted Sugra MCP server and answers a
// question using Anthropic tool-use. The official MCP SDK provides the tools;
// the agent loop is plain Anthropic Messages tool-use. Provider-agnostic.
//
// Usage:
//   npx tsx agent.ts "What is the current US federal funds rate?"

import Anthropic from "@anthropic-ai/sdk";
import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";
import { config as loadEnv } from "dotenv";
import { resolve } from "node:path";

// Hosted Sugra MCP endpoint (Streamable HTTP transport).
const SUGRA_MCP_URL = "https://app.sugra.ai/mcp";

// Change me: any current Anthropic model id works here.
const MODEL = "claude-sonnet-5";

const MAX_TURNS = 5;
const MAX_TOOL_OUTPUT_CHARS = 8000;
const DEFAULT_QUESTION = "What is the current US federal funds rate?";

// Render an MCP tool result as text. Prefer text content, add structuredContent
// when present, and cap very large output.
function serializeToolResult(result: any): string {
  const parts: string[] = [];
  for (const block of result?.content ?? []) {
    if (block?.type === "text" && block.text) parts.push(block.text);
  }
  if (result?.structuredContent) {
    parts.push(JSON.stringify(result.structuredContent));
  }
  let out = parts.join("\n").trim() || "(empty tool result)";
  if (out.length > MAX_TOOL_OUTPUT_CHARS) {
    out = out.slice(0, MAX_TOOL_OUTPUT_CHARS) + "\n... (output truncated)";
  }
  return out;
}

async function main(): Promise<number> {
  loadEnv({ path: resolve(__dirname, "..", ".env") });

  const sugraKey = process.env.SUGRA_API_KEY;
  if (!sugraKey) {
    console.log("Error: SUGRA_API_KEY is not set. Copy .env.example to .env and fill it in.");
    return 1;
  }
  if (!process.env.ANTHROPIC_API_KEY) {
    console.log("Error: ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.");
    return 1;
  }

  const question = process.argv[2] ?? DEFAULT_QUESTION;
  const anthropic = new Anthropic();

  const transport = new StreamableHTTPClientTransport(new URL(SUGRA_MCP_URL), {
    requestInit: { headers: { Authorization: `Bearer ${sugraKey}` } },
  });
  const client = new Client({ name: "sugra-mcp-example", version: "0.1.0" });
  await client.connect(transport);

  try {
    const mcpTools = (await client.listTools()).tools;
    console.log(`Connected to the Sugra MCP. ${mcpTools.length} tools available.`);
    const tools = mcpTools.map((t) => ({
      name: t.name,
      description: t.description ?? "",
      input_schema: t.inputSchema as Anthropic.Tool.InputSchema,
    }));
    const toolNames = new Set(mcpTools.map((t) => t.name));

    const messages: Anthropic.MessageParam[] = [{ role: "user", content: question }];

    for (let turn = 0; turn < MAX_TURNS; turn++) {
      const response = await anthropic.messages.create({
        model: MODEL,
        max_tokens: 1024,
        tools,
        messages,
      });

      const toolUses = response.content.filter((b) => b.type === "tool_use");
      if (toolUses.length === 0) {
        const text = response.content
          .filter((b) => b.type === "text")
          .map((b) => (b as Anthropic.TextBlock).text)
          .join("");
        console.log(text.trim() || "(no answer)");
        return 0;
      }

      // Assistant turn (with tool_use blocks) must be appended BEFORE the user
      // turn that carries the matching tool_result blocks.
      messages.push({ role: "assistant", content: response.content });

      const toolResults: Anthropic.ToolResultBlockParam[] = [];
      for (const tu of toolUses) {
        let content: string;
        let isError = false;
        if (!toolNames.has(tu.name)) {
          content = `Unknown tool: ${tu.name}`;
          isError = true;
        } else {
          console.log(`[tool] ${tu.name} ${JSON.stringify(tu.input)}`);
          try {
            const result = await client.callTool({
              name: tu.name,
              arguments: tu.input as Record<string, unknown>,
            });
            content = serializeToolResult(result);
            isError = Boolean(result?.isError);
          } catch (err) {
            // MCP / HTTP error: report, do not crash.
            content = `Tool call failed: ${err instanceof Error ? err.message : String(err)}`;
            isError = true;
          }
        }
        toolResults.push({
          type: "tool_result",
          tool_use_id: tu.id,
          content,
          is_error: isError,
        });
      }

      // tool_result blocks come first in the user turn.
      messages.push({ role: "user", content: toolResults });
    }

    console.log("Tool loop did not converge within the turn budget. Try a simpler question.");
    return 1;
  } finally {
    await client.close();
  }
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    // Keep the message short and never echo secrets. Common causes: bad or
    // missing keys, no network, or the daily request limit (HTTP 429).
    const name = err instanceof Error ? err.name : "Error";
    console.log(
      `Request failed (${name}). Check your API keys, network, and daily request limit, then try again.`,
    );
    process.exit(1);
  });
