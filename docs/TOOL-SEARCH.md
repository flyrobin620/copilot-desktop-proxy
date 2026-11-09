# MCP tool search behind the proxy

Status: investigated on 2026-11-09 with Claude Code 2.1.317 (bundled with Claude Desktop) and the
PROD gateway. No compatibility change is shipped yet; this page records the root cause, the evidence
and the candidate fix.

## Symptom

In Claude Desktop Code sessions, MCP tools start out deferred (names only), then Claude Code reports
"ToolSearch is no longer available. Tools listed earlier as deferred that are still available are
declared in full". From then on every request carries every MCP tool schema. On the affected machine
that was 528–620 tools: the first request of a session grew from about 165 KB to about 880 KB and
from an estimated ~64k to ~339k prompt tokens.

## Root cause

1. Claude Desktop starts Code sessions with `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` and turns
   tool search on through host-managed settings (`ENABLE_TOOL_SEARCH=true`,
   `CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST=1`).
2. In that configuration, and with `CLAUDE_CODE_USE_GATEWAY=1`, Claude Code names tool search with
   `anthropic-beta: tool-search-tool-2025-10-19`. It sends `advanced-tool-use-2025-11-20` only for
   first-party or Foundry providers with experimental betas enabled. Terminal Claude Code that only
   sets `ANTHROPIC_BASE_URL` therefore already works through the proxy.
3. The proxy forwards the header, the `defer_loading` tool fields and the `tool_reference` blocks
   unchanged. PROD answers
   `400 {"error":{"message":"The requested anthropic-beta feature is not supported."}}`.
4. Claude Code resends the request without deferred tools and keeps tool search off for the
   conversation (and for that endpoint in the process). It tries again later, at the cost of another
   refused request.

`count_tokens` returning 404 on the gateway is unrelated: the decision above does not count tokens.

## Evidence

Paired minimal requests sent directly to PROD on 2026-11-09:

| anthropic-beta | Body | Result |
| --- | --- | --- |
| none | plain tools | 200 |
| `tool-search-tool-2025-10-19` | plain tools | 400 (beta not supported) |
| none | `defer_loading` placeholder | 200 |
| `tool-search-tool-2025-10-19` | deferred tool + `tool_reference` result | 400 |
| none or `advanced-tool-use-2025-11-20` | deferred tool + `tool_reference` result | 200, the referenced deferred tool is called |
| both values | deferred tool + `tool_reference` result | 400 |

Opus 5.5, Sonnet 5.5, Sonnet 5 and Haiku 5.5 behaved the same. DF was not tested.

Proxy diagnostics show the same pattern: every request with `deferred_tool_count > 0` received an
upstream 400, and no successful request carried deferred tools. `beta_tool_search` stayed `false`
because the diagnostic families did not include `tool-search-tool-*`, which was counted in
`beta_unknown_count` instead.

## Reproduction

Use a test MCP server with a few dozen tools, a clean environment (a shell started by Claude Desktop
passes on its own `ENABLE_TOOL_SEARCH` and `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS`) and the proxy's
desktop route:

```powershell
$env:ANTHROPIC_BASE_URL = 'http://127.0.0.1:8787/desktop'
$env:ANTHROPIC_AUTH_TOKEN = 'local-copilot-placeholder'
$env:ENABLE_TOOL_SEARCH = 'true'
$env:CLAUDE_CODE_USE_GATEWAY = '1'
claude -p "Call the <tool> tool." --mcp-config mcp.json --strict-mcp-config --debug-file debug.log
```

`debug.log` then contains `[ToolSearch] HTTP 400 ... (tool-search-tool-2025-10-19); sending the
request again without deferred tools` followed by `Tool search is off for this conversation`.

## Candidate fix

Rename only that header value before sending to PROD: replace the exact
`tool-search-tool-2025-10-19` value in `anthropic-beta` with `advanced-tool-use-2025-11-20`
(dropping it if the equivalent is already present), leave the body bytes untouched, gate it with a
`loki_tool_search_beta_compat` option and record `tool_search_beta_compat_applied` in diagnostics.
A prototype with tests is on the branch `tsw/t248-tool-search-prototype`. Against a private proxy
instance, the reproduction above then keeps tool search on: 14 tools are sent instead of 62, and
`ToolSearch` loads the MCP tool through `tool_reference`, with every request returning 200.

Alternatives: dropping the value (also accepted, but loses the `advanced-tool-use` semantics), or
asking the gateway owners to accept `tool-search-tool-2025-10-19`; after such an upstream fix a
compatibility rename would be retired as described in [Gateway compatibility](COMPATIBILITY.md).
`ENABLE_TOOL_SEARCH=false` only avoids the refused round trip; it does not reduce context use.
