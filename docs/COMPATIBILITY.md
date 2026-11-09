# Gateway compatibility

Compatibility changes happen before sending, never as automatic retries. They match the actual
upstream hostname and resolved model. Unknown combinations pass through. Unrelated JSON bytes,
tool schemas and inputs, thinking signatures and headers remain unchanged.

| Option (default true) | Exact scope | Adjustment |
| --- | --- | --- |
| `loki_context_management_compat` | DF / Opus 5.5 | Omit the exact rejected keep-all-thinking directive |
| `loki_thinking_display_compat` | DF / Opus 5.5, streaming adaptive thinking | Omit the reproduced `display=updates` and `thinking-display-updates-2026-08-18` beta pair |
| `loki_cache_ttl_compat` | PROD / Opus 5.5 | Shorten protocol `ephemeral` cache TTL from `1h` to `5m` |
| `loki_tool_search_beta_compat` | PROD / any model | Rename the `anthropic-beta` value `tool-search-tool-2025-10-19` to `advanced-tool-use-2025-11-20`; header only |

PROD and DF refer to the exact allowed gateway hosts in `src\app_config.py`. The backend comes from
the signed-in Copilot Code provider. Local configuration does not grant backend or model access.

## Long-lived cache error

On 2026-11-04, a paired minimal PROD Opus 5.5 test returned 400 for `ttl=1h` and 200 for the
identical request with `ttl=5m`. After the patch, non-streaming and streaming desktop-route requests
sent with `1h` both returned 200 and the expected synthetic reply.

The workaround visits only protocol cache locations: root automatic cache control, system blocks,
tool definitions, message content blocks and nested tool-result content. It keeps caching enabled,
shortens retention, leaves unknown TTLs unchanged and does not edit saved conversations.
Diagnostics record only `cache_ttl_compat_applied` and counts.

## Refused tool-search beta

On 2026-11-09, PROD answered 400 "The requested anthropic-beta feature is not supported." to the
`tool-search-tool-2025-10-19` value that Claude Code sends in gateway mode (Claude Desktop Code
sessions). The identical request with `advanced-tool-use-2025-11-20`, the value Claude Code sends to
first-party endpoints, returned 200 and honoured `defer_loading` and `tool_reference`. Paired probes
covered Opus 5.5, Sonnet 5.5, Sonnet 5 and Haiku 5.5. The rename is not limited to those models:
the refusal concerns the header value, and every probed model refused the original and accepted the
replacement. DF was not tested and is not adjusted. Without
the rename Claude Code turns tool search off and resends every MCP tool in full. Details:
[MCP tool search](TOOL-SEARCH.md).

The workaround replaces only that exact value in the header, in place, and drops it instead when the
replacement is already present. Other values keep their text and order; the request body is not
changed. Diagnostics record `tool_search_beta_compat_applied`, and `beta_tool_search` now counts
`tool-search-tool-*`. Any remaining upstream refusal of a beta value is recorded as
`upstream_beta_unsupported` on `upstream_http_error`, without the value or the message.

Set an option to `false` in a local `proxy-options.json` in the installation root (untracked; unspecified options keep their defaults) and rerun `Install.cmd -NoPull` to restart the proxy.
After an upstream fix, verify the original request with compatibility disabled before removing its exception.
Do not generalize one model's probe to every model, feature or full context capacity.

## Optional backend configuration

`Configure Copilot Client.cmd` saves `COPILOT_BYOK_BACKEND=loki-sdf` for the current user.
Use only when intended and authorized for that backend; it is unnecessary for normal PROD operation.
It does not enable Code or grant access. Save work and fully restart Copilot to inherit the change.
`Check Copilot Client.cmd` reads configured values; `Restore Copilot Client Defaults.cmd` removes
the user override. Machine policy and application defaults may still apply.
