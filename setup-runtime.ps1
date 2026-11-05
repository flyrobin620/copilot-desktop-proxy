[CmdletBinding(SupportsShouldProcess = $true)]
param([switch]$SkipClaude)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') {
    throw 'This installer supports Windows x64 (Intel/AMD).'
}
$taskLock = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'package-lock.json') -Raw | ConvertFrom-Json
if (-not $PSCmdlet.ShouldProcess($PSScriptRoot, 'Download official runtimes into runtime/')) { return }
$taskCache = Join-Path $PSScriptRoot '.package-cache'
New-Item -ItemType Directory -Path $taskCache -Force | Out-Null
function Get-OfficialDownload {
    param([string]$Url, [string]$Name, [string]$ExpectedHost)
    $taskUri = [Uri]$Url
    if ($taskUri.Scheme -ne 'https' -or $taskUri.Host -ne $ExpectedHost) { throw 'Unexpected runtime download origin.' }
    $taskDestination = Join-Path $taskCache $Name
    if (-not (Test-Path -LiteralPath $taskDestination)) {
        $taskPartial = $taskDestination + '.' + [Guid]::NewGuid().ToString('N') + '.partial'
        try {
            Write-Host "Downloading $Name from $ExpectedHost..."
            Invoke-WebRequest -Uri $Url -OutFile $taskPartial -UseBasicParsing -TimeoutSec 300
            Move-Item -LiteralPath $taskPartial -Destination $taskDestination
        } finally {
            if (Test-Path -LiteralPath $taskPartial) { Remove-Item -LiteralPath $taskPartial }
        }
    }
    return $taskDestination
}
$taskPythonRoot = Join-Path $PSScriptRoot 'runtime\python'
$taskPython = Join-Path $taskPythonRoot 'python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) {
    $taskArchive = Get-OfficialDownload $taskLock.python.url "python-$($taskLock.python.version)-embed-amd64.zip" 'www.python.org'
    Expand-Archive -LiteralPath $taskArchive -DestinationPath $taskPythonRoot -Force
    $taskPth = @(Get-ChildItem -LiteralPath $taskPythonRoot -Filter 'python*._pth')
    if ($taskPth.Count -ne 1) { throw 'Unexpected embedded Python layout.' }
    $taskZipName = $taskPth[0].BaseName + '.zip'
    [IO.File]::WriteAllText($taskPth[0].FullName, "$taskZipName`r`n.`r`n..\..`r`n", [Text.Encoding]::ASCII)
}
& $taskPython -c "import sys, sqlite3, ctypes; assert sys.version_info >= (3,11); print('Python runtime ready:', sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) { throw 'Python runtime validation failed.' }
if (-not $SkipClaude) {
    $taskClaudeRoot = Join-Path $PSScriptRoot 'runtime\claude'
    $taskClaude = Join-Path $taskClaudeRoot 'claude.exe'
    if (-not (Test-Path -LiteralPath $taskClaude)) {
        $taskDownload = Get-OfficialDownload $taskLock.claude.url "claude-$($taskLock.claude.version)-win32-x64.exe" 'downloads.claude.ai'
        New-Item -ItemType Directory -Path $taskClaudeRoot -Force | Out-Null
        Copy-Item -LiteralPath $taskDownload -Destination $taskClaude
    }
    & $taskClaude --version
    if ($LASTEXITCODE -ne 0) { throw 'Claude Code runtime validation failed.' }
}
Write-Host 'Runtime setup complete. Sign in to Microsoft Copilot desktop Code, then run Launch Claude.cmd or Launch Claude Desktop.cmd.'
