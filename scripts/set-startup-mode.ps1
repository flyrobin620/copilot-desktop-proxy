param([ValidateSet('OnDemand','SignIn')][string]$Mode = 'OnDemand')
$ErrorActionPreference = 'Stop'
$ProxyRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'launcher-common.ps1')
$taskPython = Resolve-ProxyPython
$taskMarker = Join-Path $ProxyRoot '.run\on-demand'
New-Item -ItemType Directory -Path (Split-Path -Parent $taskMarker) -Force | Out-Null
if ($Mode -eq 'OnDemand') { [IO.File]::WriteAllText($taskMarker, 'on-demand') }
elseif (Test-Path -LiteralPath $taskMarker) { Remove-Item -LiteralPath $taskMarker }
$taskArgs = @('-NoLogo','-NoProfile','-NonInteractive','-ExecutionPolicy','Bypass','-File',
    (Join-Path $PSScriptRoot 'recovery-task.ps1'),'-Action','Enable','-Python',$taskPython)
if ($Mode -eq 'OnDemand') { $taskArgs += '-OnDemand' }
& powershell.exe @taskArgs
exit $LASTEXITCODE
