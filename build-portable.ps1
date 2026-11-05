param([string]$OutputPath)
$ErrorActionPreference = 'Stop'
$taskLock = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'package-lock.json') -Raw | ConvertFrom-Json
$taskBuildId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 6)
$taskBuildRoot = Join-Path $PSScriptRoot ('.build\' + $taskBuildId)
$taskStage = Join-Path $taskBuildRoot 'copilot-desktop-proxy'
New-Item -ItemType Directory -Path $taskStage -Force | Out-Null
# Explicit manifest: never package runtime, credentials, logs, usage or sessions.
$taskFiles = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'package-files.json') -Raw | ConvertFrom-Json
foreach ($taskFile in $taskFiles) {
    if ($taskFile -match '(^[\\/]|:|(^|[\\/])\.\.([\\/]|$))') { throw 'Invalid package source path.' }
    $taskSource = Join-Path $PSScriptRoot $taskFile
    $taskDestination = Join-Path $taskStage $taskFile
    New-Item -ItemType Directory -Path (Split-Path -Parent $taskDestination) -Force | Out-Null
    Copy-Item -LiteralPath $taskSource -Destination $taskDestination
}
if (-not $OutputPath) { $OutputPath = Join-Path $PSScriptRoot ("dist\copilot-desktop-proxy-$($taskLock.package_version)-source-$taskBuildId.zip") }
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
if (Test-Path -LiteralPath $OutputPath) { throw 'Output already exists. Choose a new OutputPath.' }
New-Item -ItemType Directory -Path (Split-Path -Parent $OutputPath) -Force | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem
[IO.Compression.ZipFile]::CreateFromDirectory($taskBuildRoot, $OutputPath, [IO.Compression.CompressionLevel]::Optimal, $false)
Write-Host 'Source package built. Extract it and run Install Runtime.cmd before launching.'
[pscustomobject]@{ package = $OutputPath; bytes = (Get-Item -LiteralPath $OutputPath).Length } | ConvertTo-Json -Compress
