$ErrorActionPreference = 'Stop'
$ProxyRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'launcher-common.ps1')
$taskPython = Resolve-ProxyPython
& $taskPython -B (Join-Path $ProxyRoot 'src\collect_diagnostics.py')
if ($LASTEXITCODE -ne 0) { throw 'Diagnostics could not be collected. Check folder permissions and free disk space.' }
Write-Host 'Diagnostics saved above. No data was uploaded.'
