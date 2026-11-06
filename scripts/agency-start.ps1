# Claude SessionStart hook. No prompt or credential input is read or written.
$ErrorActionPreference='Stop'
try {
    & (Join-Path $PSScriptRoot 'start.ps1') | Out-Null
    . (Join-Path $PSScriptRoot 'launcher-common.ps1')
    $taskHealth=Get-ProxyHealth
    if ($taskHealth.status -ne 'ready') { throw 'Not signed in. Run "Sign In.cmd" in the proxy shortcuts folder.' }
} catch {
    throw ('Copilot proxy: '+$_.Exception.Message)
}
