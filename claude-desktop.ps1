param(
    [ValidateSet('Configure','Start','Check','Restore')][string]$Mode = 'Configure',
    [string]$DesktopPath,
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'
try {
    if ($Mode -in @('Check','Restore')) {
        . (Join-Path $PSScriptRoot 'launcher-common.ps1')
        $taskPython = Resolve-ProxyPython
        $taskAction = if ($Mode -eq 'Check') { 'status' } else { 'restore' }
        $taskArgs = @('-B', '-X', 'utf8', (Join-Path $PSScriptRoot 'claude_desktop.py'), $taskAction)
        if ($WhatIf) { $taskArgs += '--dry-run' }
        & $taskPython @taskArgs
        if ($LASTEXITCODE -ne 0) { throw 'Claude App configuration operation did not complete; see CLAUDE-DESKTOP.md.' }
        if ($WhatIf) { Write-Host 'Preview only; no settings or services changed.' }
        elseif ($Mode -eq 'Restore') {
            Write-Host 'Owned Claude App configuration restored where unchanged. User-modified local deployments are preserved; see the result above.'
            Write-Host 'Save your work, fully quit Claude App, then open it again to use your previous deployment.'
        } else { Write-Host 'Read-only configuration and ownership check. This does not verify a model request.' }
        exit 0
    }

    $taskArgs = @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',
        (Join-Path $PSScriptRoot 'configure-clients.ps1'),'-Clients','Desktop')
    if ($WhatIf) { $taskArgs += '-WhatIf' }
    & powershell.exe @taskArgs
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    if ($WhatIf) { exit 0 }
    if (@(Get-Process -Name Claude -ErrorAction SilentlyContinue).Count -gt 0) {
        Write-Host 'Claude App is running. Save your work and fully quit it, then run Launch Claude Desktop.cmd.'
        exit 0
    }
    if ($Mode -eq 'Start') {
        if (-not $DesktopPath) { $DesktopPath = Join-Path $env:LOCALAPPDATA 'AnthropicClaude\claude.exe' }
        if (-not (Test-Path -LiteralPath $DesktopPath -PathType Leaf)) {
            Write-Host 'Configuration is ready. Open your installed Claude App from the Start menu, or use -DesktopPath with its executable.'
            exit 0
        }
        Start-Process -FilePath $DesktopPath
    } else {
        Write-Host 'Configuration is ready. Run Launch Claude Desktop.cmd to open Claude App.'
    }
    exit 0
} catch {
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
