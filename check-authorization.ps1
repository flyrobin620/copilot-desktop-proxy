$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launcher-common.ps1')
$taskPython = Resolve-ProxyPython
& $taskPython -B (Join-Path $PSScriptRoot 'check_authorization.py')
exit $LASTEXITCODE
