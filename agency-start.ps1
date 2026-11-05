# Claude SessionStart hook. No prompt or credential input is read or written.
$ErrorActionPreference='Stop'
try {
    & (Join-Path $PSScriptRoot 'start.ps1') | Out-Null
    . (Join-Path $PSScriptRoot 'launcher-common.ps1')
    $taskHealth=Get-ProxyHealth
    if ($taskHealth.status -ne 'ready') { throw 'Open Microsoft Copilot Code and send a message to refresh authorization.' }
} catch {
    throw ('Copilot proxy: '+$_.Exception.Message)
}
