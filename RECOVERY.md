# Automatic recovery

Launchers start a hidden watchdog and attempt to register a current-user Windows scheduled task.
The task checks recovery periodically and at sign-in. No password, elevation or system service is installed.
If registration is blocked, launchers report that only local worker recovery is available.

The watchdog tracks its own child, probes loopback liveness and uses bounded restart backoff.
Authorization expiry and upstream errors do not count as process failure. Interrupted inference is not
automatically replayed; the client must retry.

`Stop Proxy.cmd` records persistent stop intent and disables this directory's task. A stale scheduled
trigger or signing in does not undo that intent. Explicit launch re-enables recovery. Different directories
have separate ownership markers; do not delete state files as a stopping mechanism.

Usage survives in `.data/usage.sqlite3`. Diagnostics contain metadata, not transcripts or tokens.
`verify_windows_recovery.py` is a separate integration check that creates a temporary worker and real
scheduled task and cleans up its own resources. Run deliberately where test task creation is permitted.
