param(
    [string]$SettingsPath = (Join-Path $env:USERPROFILE '.claude\settings.json'),
    [string]$DocumentsPath = [Environment]::GetFolderPath('MyDocuments'),
    [switch]$WhatIf
)
# Keep the existing entry while sharing the same setup as Claude App.
$taskArguments = @('-NoLogo','-NoProfile','-ExecutionPolicy','Bypass','-File',
    (Join-Path $PSScriptRoot 'configure-clients.ps1'),'-Clients','Agency',
    '-SettingsPath',$SettingsPath,'-DocumentsPath',$DocumentsPath)
if ($WhatIf) { $taskArguments += '-WhatIf' }
& powershell.exe @taskArguments
exit $LASTEXITCODE
