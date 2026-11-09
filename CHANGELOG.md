# Changelog

## 1.1.21 — 2026-11-09

- **MCP tool search in Claude Desktop Code sessions**: PROD refuses the `anthropic-beta` value
  `tool-search-tool-2025-10-19` that Claude Code sends in gateway mode, so Claude Code turned tool
  search off and resent every MCP tool schema (about 340k prompt tokens per request on a machine
  with ~600 tools). The proxy now renames that exact value to the accepted
  `advanced-tool-use-2025-11-20` for PROD, header only (option `loki_tool_search_beta_compat`,
  default on; diagnostics `tool_search_beta_compat_applied`). See `docs/TOOL-SEARCH.md`.
- Diagnostics: `tool-search-tool-*` counts as `beta_tool_search`; an upstream refusal of a beta
  value is recorded as `upstream_beta_unsupported`.

## 1.1.20 — 2026-11-06

- **Standalone mode**: after one Microsoft sign-in (`Install.cmd -SignIn`) the proxy no longer needs
  the Copilot desktop app. Tokens come from MSAL/WAM, the gateway URL and client headers from the
  private profile, and the model catalog from the gateway's own `/models` endpoint (cached).
  Existing database-bound profiles are migrated silently; without a profile the database fallback remains.
- **One-step install and update**: `Install.cmd` / `scripts\install.ps1` replaces `setup-runtime.ps1`,
  `setup-token-refresh.ps1` and the manual pull/stop/start/check sequence. It checks active requests,
  pulls, upgrades pinned runtimes in place, maintains sign-in dependencies, migrates client paths,
  restarts while keeping the stop state and startup mode, verifies, and lists items that need attention.
- **Repository layout**: `src/` (Python), `scripts/` (PowerShell), `shortcuts/` (double-click entries),
  `config/`, `docs/`, `tests/`. State stays in the root and per-installation identities are unchanged.
  The scheduled task, Claude App credential helper, SessionStart hook and Agency profile loader are
  re-pointed automatically; a root `claude-gateway-auth.ps1` forwards for older App configurations.
- `proxy-options.json` is now an optional, untracked override in the installation root.

## 1.1.19 — 2026-11-06

- Optional account-bound MSAL/WAM renewal with DPAPI cache, periodic refresh and bounded
  401 refresh/retry before response delivery. Includes concurrency and wire tests.
- Handles WAM's common-realm cache records and forwards the rejected token to the
  native renewal parameter, so forced refresh bypasses the broker's token cache too.
- Claude credential helper and demand-only watchdog startup without Windows login triggers.
- Local Claude integration can explicitly migrate to helper mode, including sealed static keys.

## 1.1.18 — 2026-11-05

- Initial public MIT release, with local runtime installation and source packaging.
- Includes Claude Code, Claude App gateway mode, model discovery, usage and automatic recovery.
- Includes PROD Opus 5.5 cache TTL compatibility: protocol 1h markers become 5m.
- Includes preservation tests, installation instructions, usage and troubleshooting documentation.
- Excludes credentials, account state, diagnostics, usage and third-party binaries.
