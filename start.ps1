param(
    [int]$Port = 8787,
    [ValidateSet('none', 'low', 'medium', 'high', 'xhigh', 'max')]
    [string]$Effort = 'xhigh',
    [string]$Python
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launcher-common.ps1')
$taskOptionsPath = Join-Path $PSScriptRoot 'local-options.json'
if (-not $env:COPILOT_PROXY_DB -and (Test-Path -LiteralPath $taskOptionsPath)) {
    $taskSaved = Get-Content -LiteralPath $taskOptionsPath -Raw | ConvertFrom-Json
    if ($taskSaved.database -and (Test-Path -LiteralPath $taskSaved.database)) {
        $env:COPILOT_PROXY_DB = $taskSaved.database
    }
}
$Python = Resolve-ProxyPython -Preferred $Python
function Update-ProxyModels {
    & $Python -B (Join-Path $PSScriptRoot 'refresh_models.py') --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'The proxy is running, but the Claude model catalog could not be refreshed.' }
}
$taskMutex = Enter-ProxyLifecycle
try {
    $taskSupervisorRaw = & $Python -B (Join-Path $PSScriptRoot 'supervisor.py') status
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read watchdog state.' }
    $taskSupervisor = $taskSupervisorRaw | ConvertFrom-Json
    $taskHealth = Get-ProxyHealth -Port $Port
    if (-not $taskSupervisor.running) {
        $taskExisting = Get-OwnedProxyProcess
        if ($taskExisting) {
            Write-Output 'Enabling automatic recovery for the existing local proxy...'
            & (Join-Path $PSScriptRoot 'stop.ps1')
        } elseif ($taskHealth.service -eq 'copilot-desktop-proxy') {
            $taskLive = Get-ProxyHealth -Port $Port -Path '/livez'
            if (-not $taskLive.supervisor_pid -or -not (Get-Process -Id $taskLive.supervisor_pid -ErrorAction SilentlyContinue)) {
                throw 'Another folder is running a proxy without automatic recovery. Stop that copy, then start this one.'
            }
            Update-ProxyModels
            Write-Output "Using the supervised proxy already running at http://127.0.0.1:$Port; recovery is managed by its original folder."
            return
        }
    }
    $taskPrepare = & $Python -B (Join-Path $PSScriptRoot 'supervisor.py') prepare --port $Port --effort $Effort
    if ($LASTEXITCODE -ne 0) { throw 'Cannot arm proxy recovery. Check folder permissions.' }
    $taskRecoveryRaw = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'recovery-task.ps1') -Action Enable -Python $Python
    $taskRecoveryOk = $LASTEXITCODE -eq 0
    try { $taskRecovery = $taskRecoveryRaw | ConvertFrom-Json }
    catch { $taskRecoveryOk = $false; $taskRecovery = $null }
    if (-not $taskRecoveryOk -or -not $taskRecovery.task_enabled -or -not $taskRecovery.task_owned) {
        Write-Warning 'Windows watchdog recovery is unavailable on this computer. Only the local worker watchdog will run. Collect Diagnostics.cmd includes the scheduler error.'
    } else {
        # The service starts pythonw independently of this terminal/job tree.
        $taskRecoveryDeadline = (Get-Date).AddSeconds(12)
        do {
            $taskScheduledState = (& $Python -B (Join-Path $PSScriptRoot 'supervisor.py') status) | ConvertFrom-Json
            if ($taskScheduledState.running) { break }
            Start-Sleep -Milliseconds 200
        } while ((Get-Date) -lt $taskRecoveryDeadline)
    }
    # Also supports machines where policy prohibits per-user scheduled tasks.
    $taskSupervisorRaw = & $Python -B (Join-Path $PSScriptRoot 'supervisor.py') start --port $Port --effort $Effort
    if ($LASTEXITCODE -ne 0) { throw ('Could not start watchdog: ' + $taskSupervisorRaw) }
    $taskSupervisor = $taskSupervisorRaw | ConvertFrom-Json
    # Match the watchdog's startup grace plus sustained-failure observation window.
    Write-Output 'Waiting for the local proxy to become ready (up to 150 seconds on a busy machine)...'
    $taskDeadline = (Get-Date).AddSeconds(150)
    while ((Get-Date) -lt $taskDeadline) {
        $taskHealth = Get-ProxyHealth -Port $Port
        if ($taskHealth.service -eq 'copilot-desktop-proxy') {
            Update-ProxyModels
            Write-Output "Base URL: http://127.0.0.1:$Port/v1"
            Write-Output "Model: $($taskHealth.default_alias) -> $($taskHealth.default_model); automatic recovery enabled; watchdog PID: $($taskSupervisor.supervisor_pid)"
            if ($taskRecoveryOk -and $taskRecovery.task_enabled -and $taskRecovery.task_owned) {
                Write-Output 'Windows recovery enabled: restores the watchdog if it exits; resumes at your next sign-in unless explicitly stopped.'
            }
            return
        }
        Start-Sleep -Milliseconds 200
    }
    throw 'The watchdog is running, but the proxy did not become ready within 150 seconds. Check CPU/disk load and proxy.log / supervisor.log, or run Collect Diagnostics.cmd. Refresh Copilot sign-in only if authorization diagnostics report a problem.'
} finally {
    $taskMutex.ReleaseMutex()
    $taskMutex.Dispose()
}
