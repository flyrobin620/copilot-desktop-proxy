# Gateway compatibility

Compatibility changes happen before sending, never as automatic retries. They match the actual
upstream hostname and resolved model. Unknown combinations pass through. Unrelated JSON bytes,
tool schemas and inputs, thinking signatures and headers remain unchanged.

| Option (default true) | Exact scope | Adjustment |
| --- | --- | --- |
| `loki_context_management_compat` | DF / Opus 5.5 | Omit the exact rejected keep-all-thinking directive |
| `loki_thinking_display_compat` | DF / Opus 5.5, streaming adaptive thinking | Omit the reproduced `display=updates` and `thinking-display-updates-2026-08-18` beta pair |
| `loki_cache_ttl_compat` | PROD / Opus 5.5 | Shorten protocol `ephemeral` cache TTL from `1h` to `5m` |

PROD and DF refer to the exact allowed gateway hosts in `app_config.py`. The backend comes from
the signed-in Copilot Code provider. Local configuration does not grant backend or model access.

## Long-lived cache error

On 2026-11-04, a paired minimal PROD Opus 5.5 test returned 400 for `ttl=1h` and 200 for the
identical request with `ttl=5m`. After the patch, non-streaming and streaming desktop-route requests
sent with `1h` both returned 200 and the expected synthetic reply.

The workaround visits only protocol cache locations: root automatic cache control, system blocks,
tool definitions, message content blocks and nested tool-result content. It keeps caching enabled,
shortens retention, leaves unknown TTLs unchanged and does not edit saved conversations.
Diagnostics record only `cache_ttl_compat_applied` and counts.

Set an option to `false` in `proxy-options.json` and stop/start the proxy to disable it.
After an upstream fix, verify the original request with compatibility disabled before removing its exception.
Do not generalize one model's probe to every model, feature or full context capacity.

## Optional backend configuration

`Configure Copilot Client.cmd` saves `COPILOT_BYOK_BACKEND=loki-sdf` for the current user.
Use only when intended and authorized for that backend; it is unnecessary for normal PROD operation.
It does not enable Code or grant access. Save work and fully restart Copilot to inherit the change.
`Check Copilot Client.cmd` reads configured values; `Restore Copilot Client Defaults.cmd` removes
the user override. Machine policy and application defaults may still apply.
