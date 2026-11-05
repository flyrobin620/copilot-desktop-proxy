param(
    [ValidateSet('Both','Agency','Desktop')][string]$Clients = 'Both',
    [string]$SettingsPath = (Join-Path $env:USERPROFILE '.claude\settings.json'),
    [string]$DocumentsPath = [Environment]::GetFolderPath('MyDocuments'),
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'
$taskAgency = $Clients -in @('Both','Agency')
$taskDesktop = $Clients -in @('Both','Desktop')
$taskStage = 'preflight'
$taskAgencyComplete = $false
$taskDesktopComplete = $false

function Get-AgencyProfilePlan {
    $taskLoader = (Join-Path $PSScriptRoot 'agency-shell.ps1').Replace("'", "''")
    $taskBlock = "# BEGIN COPILOT DESKTOP PROXY`r`nif (Test-Path -LiteralPath '$taskLoader' -PathType Leaf) {`r`n    . '$taskLoader'`r`n}`r`n# END COPILOT DESKTOP PROXY"
    foreach ($taskFolder in @('WindowsPowerShell','PowerShell')) {
        $taskPath = Join-Path $DocumentsPath ($taskFolder + '\profile.ps1')
        $taskOriginal = if (Test-Path -LiteralPath $taskPath) { [IO.File]::ReadAllText($taskPath) } else { '' }
        $taskPattern = '(?ms)^# BEGIN COPILOT DESKTOP PROXY\r?\n.*?^# END COPILOT DESKTOP PROXY(?:\r?\n|$)'
        $taskText = [regex]::Replace($taskOriginal, $taskPattern, '')
        if ($taskText -match '(?m)^# (?:BEGIN|END) COPILOT DESKTOP PROXY\r?$') {
            throw 'An Agency profile block is incomplete. Repair its BEGIN/END markers before retrying.'
        }
        [pscustomobject]@{ Path = $taskPath; Original = $taskOriginal; Content = $taskText.TrimEnd() + "`r`n`r`n" + $taskBlock + "`r`n" }
    }
}

function Assert-DesktopProxyReady {
    # Discovery is local metadata only; never send an inference to verify setup.
    $taskRequest = [Net.HttpWebRequest]::Create('http://127.0.0.1:8787/desktop/v1/models')
    $taskRequest.Proxy = $null
    $taskRequest.Timeout = 5000
    $taskRequest.ReadWriteTimeout = 5000
    $taskResponse = $null
    try {
        $taskResponse = $taskRequest.GetResponse()
        $taskReader = New-Object IO.StreamReader($taskResponse.GetResponseStream())
        try { $taskModels = $taskReader.ReadToEnd() | ConvertFrom-Json }
        finally { $taskReader.Dispose() }
        if (-not $taskModels.data -or @($taskModels.data).Count -eq 0) { throw 'Empty model catalog.' }
    } catch {
        throw 'Claude App model endpoint is unavailable. If you upgraded a running proxy, save work and run Stop Proxy.cmd, then retry.'
    } finally { if ($taskResponse) { $taskResponse.Dispose() } }
}

try {
    . (Join-Path $PSScriptRoot 'launcher-common.ps1')
    $taskPython = Resolve-ProxyPython
    $taskDesktopConfig = Join-Path $PSScriptRoot 'claude_desktop.py'
    $taskAgencyConfig = Join-Path $PSScriptRoot 'configure_claude.py'

    # Preflight both before lifecycle setup, which refreshes opted-in CLI settings.
    if ($taskDesktop) {
        & $taskPython -B -X utf8 $taskDesktopConfig enable --dry-run
        if ($LASTEXITCODE -ne 0) { throw 'Claude App configuration is blocked; see CLAUDE-DESKTOP.md. Agency was not configured by this run.' }
    }
    if ($taskAgency) {
        & $taskPython -B -X utf8 $taskAgencyConfig --settings $SettingsPath --migrate-legacy-beta --dry-run
        if ($LASTEXITCODE -ne 0) { throw 'Agency settings could not be validated. Check the settings file before retrying.' }
        $null = @(Get-AgencyProfilePlan)
    }
    if ($WhatIf) {
        Write-Host 'Preview only; no settings, profiles, registry values or services changed.'
        exit 0
    }

    $taskStage = 'proxy setup'
    # Child process contains one-click.ps1's exit. Both clients reuse one worker.
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'one-click.ps1') -Mode API
    if ($LASTEXITCODE -ne 0) { throw 'Resolve the proxy setup error before configuring clients.' }
    if ($taskDesktop) { Assert-DesktopProxyReady }

    if ($taskAgency) {
        $taskStage = 'Agency configuration'
        $taskProfiles = @(Get-AgencyProfilePlan)
        $taskGeneratedSettings = Join-Path $PSScriptRoot '.models\claude-settings.json'
        & $taskPython -B -X utf8 $taskAgencyConfig --settings $SettingsPath --template $taskGeneratedSettings --migrate-legacy-beta
        if ($LASTEXITCODE -ne 0) { throw 'Could not configure Agency Claude settings.' }
        foreach ($taskProfile in $taskProfiles) {
            if ($taskProfile.Original -ne $taskProfile.Content) {
                New-Item -ItemType Directory -Path (Split-Path -Parent $taskProfile.Path) -Force | Out-Null
                [IO.File]::WriteAllText($taskProfile.Path, $taskProfile.Content, (New-Object Text.UTF8Encoding($true)))
            }
        }
        $taskAgencyComplete = $true
    }
    if ($taskDesktop) {
        $taskStage = 'Claude App configuration'
        & $taskPython -B -X utf8 $taskDesktopConfig enable
        if ($LASTEXITCODE -ne 0) { throw 'Claude App setup did not complete; see CLAUDE-DESKTOP.md.' }
        $taskDesktopComplete = $true
    }

    if ($taskAgencyComplete) { Write-Host 'Agency configured. Open a new PowerShell terminal and run: agency claude' }
    if ($taskDesktopComplete) {
        Write-Host 'Claude App configured: http://127.0.0.1:8787/desktop'
        Write-Host 'Save work and fully quit/reopen Claude App; select the configured third-party provider.'
    }
    if ($Clients -eq 'Both') { Write-Host 'Both clients can run together using the same proxy, with separate conversations.' }
    Write-Host 'Clients were not launched or restarted. Local readiness is not an upstream inference test.'
    exit 0
} catch {
    Write-Host ('ERROR during ' + $taskStage + ': ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host ("Completed in this run: Agency={0}; Claude App={1}." -f $taskAgencyComplete, $taskDesktopComplete)
    if ($taskStage -in @('Agency configuration','Claude App configuration')) {
        Write-Host 'Setup may be partial. Fix the error and rerun this entry; completed settings are safe to reapply.'
        Write-Host 'To undo: Restore Claude Defaults.cmd handles Agency; Restore Claude Desktop.cmd handles the App.'
    }
    exit 1
}
