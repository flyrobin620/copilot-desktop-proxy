# Invoke from the project directory you want Claude Code to work in.
# All arguments, such as --resume or --effort high, are passed to Claude Code.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launcher-common.ps1')
$taskClaude = Resolve-ProxyClaude
$taskSettingsPath = Join-Path $PSScriptRoot '.models\claude-settings.json'
$taskHealth = $null
$taskHealth = Get-ProxyHealth
& (Join-Path $PSScriptRoot 'start.ps1')
$taskHealth = Get-ProxyHealth
if ($taskHealth.status -ne 'ready') {
    throw 'Copilot proxy is not ready. Open Copilot Code, send a message to refresh authorization, then retry.'
}
& $taskClaude --settings $taskSettingsPath --effort xhigh @args
exit $LASTEXITCODE
