$ErrorActionPreference = 'Stop'

function Resolve-ProxyPython {
    param([string]$Preferred)
    $taskCandidates = @($Preferred, $env:COPILOT_PROXY_PYTHON,
        (Join-Path $PSScriptRoot 'runtime\python\python.exe'),
        (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'))
    $taskOnPath = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($taskOnPath -and $taskOnPath.Source -notlike '*\WindowsApps\*') { $taskCandidates += $taskOnPath.Source }
    foreach ($taskCandidate in $taskCandidates) {
        if ($taskCandidate -and (Test-Path -LiteralPath $taskCandidate -PathType Leaf)) {
            $taskResult = & $taskCandidate -c "import sys, sqlite3, ctypes; print('PYTHON_OK' if sys.version_info >= (3,11) else 'TOO_OLD')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $taskResult -eq 'PYTHON_OK') { return (Resolve-Path -LiteralPath $taskCandidate).Path }
            if ($taskCandidate -eq $Preferred) { throw 'The selected Python must support Python 3.11+, sqlite3 and ctypes.' }
        }
    }
    throw 'Python 3.11+ was not found. Extract the complete portable ZIP, or set COPILOT_PROXY_PYTHON to python.exe.'
}

function Resolve-ProxyClaude {
    foreach ($taskCandidate in @($env:COPILOT_PROXY_CLAUDE, (Join-Path $PSScriptRoot 'runtime\claude\claude.exe'))) {
        if ($taskCandidate -and (Test-Path -LiteralPath $taskCandidate -PathType Leaf)) { return (Resolve-Path -LiteralPath $taskCandidate).Path }
    }
    $taskCommand = Get-Command claude -ErrorAction SilentlyContinue
    if ($taskCommand) { return $taskCommand.Source }
    throw 'Claude Code was not found. Extract the complete portable ZIP, or install Claude Code.'
}

function Get-ProxyHealth {
    param([int]$Port = 8787, [ValidateSet('/health','/livez')][string]$Path = '/health')
    $taskResponse = $null
    try {
        $taskRequest = [Net.HttpWebRequest]::Create("http://127.0.0.1:$Port$Path")
        $taskRequest.Proxy = $null
        $taskRequest.Timeout = 2000
        $taskRequest.ReadWriteTimeout = 2000
        try { $taskResponse = $taskRequest.GetResponse() }
        catch [Net.WebException] { $taskResponse = $_.Exception.Response }
        if (-not $taskResponse) { return $null }
        $taskReader = New-Object IO.StreamReader($taskResponse.GetResponseStream())
        try { return ($taskReader.ReadToEnd() | ConvertFrom-Json) } finally { $taskReader.Dispose() }
    } catch { return $null }
    finally { if ($taskResponse) { $taskResponse.Dispose() } }
}

function Enter-ProxyLifecycle {
    $taskHasher = [Security.Cryptography.SHA256]::Create()
    try { $taskDigest = $taskHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($PSScriptRoot.ToLowerInvariant())) }
    finally { $taskHasher.Dispose() }
    $taskName = 'Local\CopilotProxy-' + ([BitConverter]::ToString($taskDigest).Replace('-',''))
    $taskMutex = New-Object Threading.Mutex($false, $taskName)
    try {
        try { $taskAcquired = $taskMutex.WaitOne(45000) }
        catch [Threading.AbandonedMutexException] { $taskAcquired = $true }
        if (-not $taskAcquired) { throw 'Another proxy start or stop is still running.' }
        return $taskMutex
    } catch { $taskMutex.Dispose(); throw }
}

function Get-OwnedProxyProcess {
    $taskPidFile = Join-Path $PSScriptRoot 'server.pid'
    if (-not (Test-Path -LiteralPath $taskPidFile)) { return $null }
    $taskProcessId = [int](Get-Content -LiteralPath $taskPidFile -Raw)
    $taskProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$taskProcessId"
    if ($taskProcess) {
        $taskScript = [regex]::Escape((Join-Path $PSScriptRoot 'server.py'))
        if ($taskProcess.CommandLine -notmatch ('(?:^|\s)"?' + $taskScript + '"?(?:\s|$)')) {
            throw 'PID belongs to another process; refusing to stop it.'
        }
    }
    return $taskProcess
}
