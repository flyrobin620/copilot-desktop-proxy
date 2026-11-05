# Development

Python 3.11+ and its standard library are sufficient. Windows is required for real Copilot authorization.
`setup-runtime.ps1` installs a pinned x64 runtime locally.

```powershell
.\runtime\python\python.exe -B -X utf8 -m unittest discover -p "test_*.py" -q
```

Offline tests use synthetic configuration, temporary directories, mocks and loopback servers. They cover
forwarding, SSE, usage, models, exact compatibility transforms, policy ownership, recovery and redaction.
Actual upstream checks are separate: `smoke_test.py`, `smoke_claude.py`, `check_authorization.py`.

## Components

- `app_config.py`: local Code provider and protected authorization.
- `server.py`, `adapter.py`: native Messages/count_tokens and OpenAI Chat Completions.
- `native_compat.py`: evidence-based exceptions preserving unrelated bytes.
- `model_selection.py`, `desktop_models.py`: separate CLI/App model catalogs.
- `usage_store.py`, `web/`: local accounting and dashboard.
- `supervisor.py`, `parent_lifetime.py`, launchers: owned-process lifecycle and recovery.
- `configure_claude.py`, `claude_desktop.py`: client configuration and ownership-aware restoration.
- `diagnostics.py`, `authorization.py`: allowlisted operational metadata.

## Releases

Update the version in `diagnostics.py` and `package-lock.json`. Update `package-files.json` when adding
distributable files. `build-portable.ps1` creates a source installer ZIP with licenses and installation
instructions, excluding runtimes, credentials, account state, generated catalogs, diagnostics and usage.
Extract it and run `Install Runtime.cmd` before launching. Downloads use official HTTPS URLs.
The build and installer do not calculate executable hashes.

Review every included file and documentation link before publication. Never package a live installation
wholesale. Keep upstream URL validation, managed policy and ACLs intact. Compatibility changes require
focused wire-preservation tests.
