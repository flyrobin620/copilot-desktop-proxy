# Authentication, standalone mode and on-demand startup

中文安装与更新步骤见 [安装与更新](UPDATE.md)。Run `Install.cmd -SignIn` once per installation folder.

## Standalone mode (no Copilot desktop app)

After a one-time Microsoft sign-in the proxy needs nothing from the Copilot desktop app:

| Needed value | Standalone source |
| --- | --- |
| Access token | MSAL/WAM silent renewal in a separate Python environment, DPAPI-encrypted cache |
| Gateway URL | Verified constant (`app_config.DEFAULT_UPSTREAM`), validated against the allowlist |
| Client headers | Fixed Code client identification (`app_config.DEFAULT_HEADERS`), stored in the private profile |
| Model catalog | The gateway's own `GET {gateway}/models`, Messages-capable entries only; refreshed every 15 minutes and cached as `models.json` |
| Account identity | ID-token claims from the sign-in (`oid`, `tid`, username) |

If Copilot Code is installed when you sign in, its account and gateway headers (for example a newer
`X-Llm-Api-Version`) are reused as defaults; otherwise the built-in values are used. Older profiles
that read the Copilot database (`"database"` instead of `"source": "standalone"`) keep working and are
converted silently by `Install.cmd` when the cached sign-in allows it. Without any profile the proxy still
falls back to the Copilot Code database, as in earlier releases.

The private profile lives outside the repository under
`%USERPROFILE%\.copilot-desktop-proxy\auth\<installation-id>\` (`settings.json`, `models.json`,
`token_cache.bin`). The installation id is derived from the folder path, so another folder is another
installation. `settings.json` and `models.json` contain no secrets; tokens never enter logs, diagnostics or
HTTP status. Never run `src\token_helper.py acquire` directly: it is a private stdin/stdout protocol.
To test a forced acquisition with the sign-in environment (metadata only):

```powershell
& "<sign-in python>" -B .\src\token_helper.py check --force
```

`token_helper.py status` shows whether a profile exists, its source and the sign-in interpreter.
The configured client must be permitted to request the Loki resource for the selected account.
Authentication does not grant service access, models, or capacity. MFA, revoked consent, account
sign-out and tenant policy can still require `Install.cmd -SignIn`.

## Runtime behavior

- The worker checks every 30 seconds and silently renews within 5 minutes of expiry. In standalone mode
  the first acquisition may also be triggered by a read-only request (`/health`, `/v1/models`).
- Requests also check freshness before forwarding. Concurrent acquisitions share a lock.
- An explicit upstream HTTP 401 triggers one forced acquisition and at most one replay,
  only if a different token is obtained. The request bytes, query, beta headers and call
  IDs are preserved. Concurrent 401s reuse the newly acquired token.
- Force refresh is rate-limited to once per 30 seconds; failures back off from 30 to
  300 seconds. A helper call has a 30-second process timeout and never opens login UI.
- MSAL 1.39's force flag bypasses its Python cache but not the WAM cache. The isolated
  helper additionally sets the native runtime's `access_token_to_renew` to the rejected
  bearer. This small, version-checked broker adapter is restored after each call and
  does not modify installed packages. Revalidate it before upgrading MSAL.
- HTTP 403/429, disconnects, timeouts and errors after response streaming begins are
  not replayed. Repeated 401 or required interaction remains visible to the client.
- A token is accepted only for the configured tenant/object id. Database-bound profiles also verify the
  selected database; a detected identity change discards cached tokens.
- A failed catalog refresh keeps the last known models; without any catalog the proxy reports
  `model_catalog_unavailable`.
- `/authorization` and `/diagnostics` expose `credential_source` and renewal state. `/livez` remains
  independent of authentication and does not block on renewal.

## Start when Claude requests its credential

For an existing local-library deployment owned by this project:

```powershell
.\Install.cmd -StartupMode OnDemand
```

Fully exit and reopen Claude after saving your work. Claude calls
`scripts\claude-gateway-auth.ps1`, which starts the proxy if necessary and prints only
`local-copilot-placeholder`. The real upstream token remains inside the proxy.
Claude caches the helper result for 60 seconds and may rerun it for silent refresh.
The local integration records ownership, so existing Check/Restore commands still work.
Managed registry deployments require their administrator to configure the equivalent
helper settings; this migration does not override enterprise policy.

On-demand mode retains a hidden **demand-only** Windows task to launch the watchdog
outside Claude's process lifetime. It has no login or periodic triggers. Worker recovery
continues while the watchdog runs. Closing Claude does not stop it; logging off ends it.
`Stop Proxy.cmd` stops it, but a later Claude credential-helper invocation is an explicit
new launch and can start it again. To stay stopped while Claude is open, restore the
Claude integration or exit Claude too.

`Install.cmd -StartupMode AtLogon` restores login and one-minute recovery triggers.
To disable renewal, set `enabled` to false in the installation's private `settings.json`
and restart the proxy. Restoring Claude's configuration does not delete the token cache.
