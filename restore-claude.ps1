param(
    [string]$SettingsPath = (Join-Path $env:USERPROFILE '.claude\settings.json'),
    [string]$DocumentsPath = [Environment]::GetFolderPath('MyDocuments'),
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launcher-common.ps1')
$taskPython = Join-Path $PSScriptRoot 'runtime\python\python.exe'
if (-not (Test-Path -LiteralPath $taskPython -PathType Leaf)) {
    throw 'Extract the complete portable package: runtime\python\python.exe is required.'
}
$taskRestoreArgs = @('-B', (Join-Path $PSScriptRoot 'restore_claude.py'),
    '--settings', $SettingsPath, '--documents', $DocumentsPath)
if ($DryRun) { $taskRestoreArgs += '--dry-run' }
$taskRestoreLock = Enter-ProxyLifecycle
try {
    & $taskPython @taskRestoreArgs
    if ($LASTEXITCODE -ne 0) { throw 'Restore failed. Configuration contents were not logged; some files may need review.' }
} finally {
    $taskRestoreLock.ReleaseMutex()
    $taskRestoreLock.Dispose()
}
if ($DryRun) {
    Write-Host 'Preview only. No settings were changed.'
} else {
    Write-Host 'Copilot overrides removed. Close existing terminals and open a new one, then run agency claude or claude.'
    Write-Host 'Existing login and unrelated settings are preserved. Copilot and the proxy were not stopped.'
    Write-Host 'Launch Claude.cmd explicitly selects Copilot again; Enable Agency.cmd reinstalls the integration.'
}
