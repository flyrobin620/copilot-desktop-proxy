param(
    [ValidateSet('Claude','API','Report','Check','Stop')][string]$Mode = 'Claude',
    [string]$ProjectDirectory,
    [switch]$NonInteractive
)
$ErrorActionPreference = 'Stop'
try {
    . (Join-Path $PSScriptRoot 'launcher-common.ps1')
    if ($Mode -eq 'Stop') { & (Join-Path $PSScriptRoot 'stop.ps1'); exit 0 }
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'This package requires 64-bit Windows.' }
    $taskPython = Resolve-ProxyPython
    $taskOptionsPath = Join-Path $PSScriptRoot 'local-options.json'
    $taskOptions = @{}
    if (Test-Path -LiteralPath $taskOptionsPath) {
        $taskSaved = Get-Content -LiteralPath $taskOptionsPath -Raw | ConvertFrom-Json
        if ($taskSaved.database) { $taskOptions.database = $taskSaved.database }
        if ($taskSaved.project) { $taskOptions.project = $taskSaved.project }
    }
    if (-not $env:COPILOT_PROXY_DB -and $taskOptions.database -and (Test-Path -LiteralPath $taskOptions.database)) {
        $env:COPILOT_PROXY_DB = $taskOptions.database
    }
    if (-not $env:COPILOT_PROXY_DB) {
        $taskDbRoot = Join-Path $env:LOCALAPPDATA 'Microsoft\Copilot\User Data\CopilotCodingHome'
        $taskDatabases = @(Get-ChildItem -Path (Join-Path $taskDbRoot '*\data.db') -ErrorAction SilentlyContinue)
        if ($taskDatabases.Count -eq 0) { throw 'Open Microsoft Copilot desktop on this computer, sign in, and send a message in its Code tab. Then run this launcher again.' }
        if ($taskDatabases.Count -gt 1) {
            if ($NonInteractive) { throw 'Multiple Copilot account databases found. Set COPILOT_PROXY_DB or run the launcher interactively.' }
            Write-Host 'Select the Copilot account database for this computer:'
            for ($taskIndex=0; $taskIndex -lt $taskDatabases.Count; $taskIndex++) { Write-Host ("{0}. {1}" -f ($taskIndex+1), $taskDatabases[$taskIndex].FullName) }
            $taskChoice=0
            if (-not [int]::TryParse((Read-Host 'Database number'),[ref]$taskChoice) -or $taskChoice -lt 1 -or $taskChoice -gt $taskDatabases.Count) { throw 'Invalid database selection.' }
            $env:COPILOT_PROXY_DB=$taskDatabases[$taskChoice-1].FullName
            $taskOptions.database=$env:COPILOT_PROXY_DB
        }
    }
    if ($Mode -eq 'Claude') {
        $taskClaude = Resolve-ProxyClaude
        if (-not $ProjectDirectory) { $ProjectDirectory=$taskOptions.project }
        if (-not $ProjectDirectory) {
            $ProjectDirectory = Join-Path $PSScriptRoot 'workspace'
            New-Item -ItemType Directory -Path $ProjectDirectory -Force | Out-Null
        }
        if (-not (Test-Path -LiteralPath $ProjectDirectory -PathType Container)) { throw 'Project folder does not exist. Drag an existing folder onto Launch Claude.cmd.' }
        $ProjectDirectory = (Resolve-Path -LiteralPath $ProjectDirectory).Path
        $taskOptions.project=$ProjectDirectory
    }
    if ($taskOptions.Count) { $taskOptions | ConvertTo-Json | Set-Content -LiteralPath $taskOptionsPath -Encoding UTF8 }
    & (Join-Path $PSScriptRoot 'start.ps1') -Python $taskPython
    $taskHealth = Get-ProxyHealth
    if ($taskHealth.status -ne 'ready') { throw 'Copilot authorization is not ready. Send a message in the desktop Code tab, then retry.' }
    Write-Host 'OpenAI API: http://127.0.0.1:8787/v1'
    Write-Host 'Claude Code: http://127.0.0.1:8787'
    Write-Host 'Usage report: http://127.0.0.1:8787/usage-viewer'
    if ($Mode -eq 'Check') {
        & $taskPython --version
        $taskClaude=Resolve-ProxyClaude
        & $taskClaude --version
        Write-Host 'PASS: proxy is locally ready. Send a message to verify upstream access.'
    } elseif ($Mode -eq 'Report') {
        Start-Process 'http://127.0.0.1:8787/usage-viewer'
    } elseif ($Mode -eq 'Claude') {
        Set-Location -LiteralPath $ProjectDirectory
        Write-Host "Project: $ProjectDirectory"
        & (Join-Path $PSScriptRoot 'claude-copilot.ps1')
        exit $LASTEXITCODE
    }
    exit 0
} catch {
    Write-Host ''
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host 'See QUICKSTART.md and SOP.md in this folder.'
    exit 1
}
