# Development

Python 3.11+ and its standard library are sufficient for the proxy. Windows is required for real
authorization. `scripts\install.ps1` installs a pinned x64 runtime locally (`-NoPull` to install
the working tree as it is).

```powershell
python -B -X utf8 -m unittest discover -s tests -t . -q
```

`tests\__init__.py` puts `src\` on `sys.path`, as `runtime\python\python*._pth` does for the embedded
runtime. Offline tests use synthetic configuration, temporary directories, mocks and loopback servers.
They cover forwarding, SSE, usage, models, exact compatibility transforms, policy ownership, recovery,
standalone sign-in, layout migration and redaction. Actual upstream checks are separate:
`tests\smoke_test.py`, `tests\smoke_claude.py`, `src\check_authorization.py`,
`tests\verify_windows_recovery.py`.

## Layout

- `src\paths.py`: `ROOT` is the installation root. Runtime state and every per-installation identity
  (auth profile, scheduled task name, lifecycle mutex) are keyed to `ROOT`, never to a source folder.
  PowerShell scripts use `$ProxyRoot` for the same value.
- `src\app_config.py`: gateway settings — verified upstream, fixed client headers, gateway `/models`
  catalog; legacy reader for the Copilot Code database and its protected authorization.
- `src\token_refresh.py`, `src\token_helper.py`: MSAL/WAM renewal, standalone profiles, sign-in and migration.
- `src\server.py`, `src\adapter.py`: native Messages/count_tokens and OpenAI Chat Completions.
- `src\native_compat.py`: evidence-based exceptions preserving unrelated bytes.
- `src\model_selection.py`, `src\desktop_models.py`: separate CLI/App model catalogs.
- `src\usage_store.py`, `src\web\`: local accounting and dashboard.
- `src\supervisor.py`, `src\parent_lifetime.py`, `scripts\`: owned-process lifecycle and recovery.
- `src\configure_claude.py`, `src\claude_desktop.py`, `src\restore_claude.py`: client configuration and
  ownership-aware restoration, including re-pointing pre-1.1.20 root-layout paths.
- `src\diagnostics.py`, `src\authorization.py`: allowlisted operational metadata.

## Releases

Update the version in `src\diagnostics.py` and `config\package-lock.json`. Regenerate
`config\package-files.json` (all tracked files) when adding distributable files.
`scripts\build-portable.ps1` creates a source ZIP with licenses and installation instructions, excluding
runtimes, credentials, account state, generated catalogs, diagnostics and usage. Extract it and run
`Install.cmd`. Downloads use official HTTPS URLs; the build and installer do not calculate executable hashes.

When moving files that external configuration points at (credential helper, SessionStart hook,
PowerShell profile loader, scheduled task action), keep recognizing the old path and let
`scripts\install.ps1` re-point it. Review every included file and documentation link before publication.
Never package a live installation wholesale. Keep upstream URL validation, managed policy and ACLs intact.
Compatibility changes require focused wire-preservation tests.
