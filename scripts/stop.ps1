$ErrorActionPreference = 'Stop'
$ProxyRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'launcher-common.ps1')
$taskMutex = Enter-ProxyLifecycle
try {
    $taskPython = Resolve-ProxyPython
    $taskResult = & $taskPython -B (Join-Path $ProxyRoot 'src\supervisor.py') stop
    if ($LASTEXITCODE -ne 0) { throw ('Could not stop watchdog: ' + $taskResult) }
    $taskRecoveryRaw = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'recovery-task.ps1') -Action Disable
    if ($LASTEXITCODE -ne 0) {
        Write-Warning 'Windows task could not be disabled; the persistent stop signal still prevents scheduled recovery.'
    }
    # Compatibility with an old worker or one left after a watchdog exit.
    $taskProcess = Get-OwnedProxyProcess
    if ($taskProcess) { Stop-Process -Id $taskProcess.ProcessId }
    $taskPidFile = Join-Path $ProxyRoot 'server.pid'
    if (Test-Path -LiteralPath $taskPidFile) { Remove-Item -LiteralPath $taskPidFile }
    Write-Output 'Copilot proxy and automatic recovery stopped. Run Start API or Launch Claude to start again.'
} finally {
    $taskMutex.ReleaseMutex()
    $taskMutex.Dispose()
}
