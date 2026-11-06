$ErrorActionPreference = 'Stop'
$ProxyRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'launcher-common.ps1')
$taskPython = Resolve-ProxyPython
& $taskPython -B (Join-Path $ProxyRoot 'src\check_authorization.py')
exit $LASTEXITCODE
