[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Low')]
param(
    [ValidateSet('Dogfood', 'Status', 'Default')]
    [string]$Mode = 'Dogfood'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
    throw 'This script requires Windows.'
}

$taskVariable = 'COPILOT_BYOK_BACKEND'

function Format-BackendValue {
    param([AllowNull()][string]$Value)
    if ([string]::IsNullOrEmpty($Value)) { return '(not set)' }
    if (@('loki-prod', 'loki-msit', 'loki-sdf', 'loki-direct') -ccontains $Value) {
        return $Value
    }
    return '(custom value; not displayed)'
}

if ($Mode -ne 'Status') {
    $taskUserValue = $null
    $taskAction = 'Remove the user backend override'
    if ($Mode -eq 'Dogfood') {
        $taskUserValue = 'loki-sdf'
        $taskAction = 'Set the user backend override to loki-sdf (DF)'
    }

    if ($PSCmdlet.ShouldProcess("Current Windows user: $taskVariable", $taskAction)) {
        # The User setter persists the value and broadcasts the Windows environment change.
        # Reapplying the same value is safe and also notifies launchers again.
        [Environment]::SetEnvironmentVariable($taskVariable, $taskUserValue, 'User')
        $taskSavedValue = [Environment]::GetEnvironmentVariable($taskVariable, 'User')
        if ([string]$taskSavedValue -cne [string]$taskUserValue) {
            throw 'The user backend setting could not be verified. Run with -Mode Status to inspect it.'
        }

        # This changes only this PowerShell process, not its parent terminal or a running app.
        $taskProcessValue = $taskUserValue
        if ([string]::IsNullOrEmpty($taskProcessValue)) {
            $taskProcessValue = [Environment]::GetEnvironmentVariable($taskVariable, 'Machine')
        }
        [Environment]::SetEnvironmentVariable($taskVariable, $taskProcessValue, 'Process')

        if ($Mode -eq 'Dogfood') {
            Write-Host 'DF configuration saved for the current Windows user. No administrator rights are needed.'
        } else {
            Write-Host 'User backend override removed. Machine settings and client channel defaults may still apply.'
        }
    }
}

$taskUserValue = [Environment]::GetEnvironmentVariable($taskVariable, 'User')
$taskMachineValue = [Environment]::GetEnvironmentVariable($taskVariable, 'Machine')
$taskProcessValue = [Environment]::GetEnvironmentVariable($taskVariable, 'Process')
Write-Host ''
Write-Host "$taskVariable settings:"
Write-Host ('  User:                ' + (Format-BackendValue $taskUserValue))
Write-Host ('  Machine:             ' + (Format-BackendValue $taskMachineValue))
Write-Host ('  This script process: ' + (Format-BackendValue $taskProcessValue))
Write-Host 'These settings do not establish which backend a running Copilot instance is using.'

if ([string]::IsNullOrEmpty($taskUserValue)) {
    if ([string]::IsNullOrEmpty($taskMachineValue)) {
        Write-Host 'No persisted backend override: the client chooses its channel default (Stable/Beta: PROD).'
    } else {
        Write-Warning 'A machine backend override remains. Removing the user override does not remove it.'
    }
}

$taskProcesses = @(Get-Process -Name 'copilotapp', 'copilotapphost' -ErrorAction SilentlyContinue)
$taskAppCount = @($taskProcesses | Where-Object { $_.ProcessName -ieq 'copilotapp' }).Count
$taskHostCount = @($taskProcesses | Where-Object { $_.ProcessName -ieq 'copilotapphost' }).Count
Write-Host ("Running processes visible to this user: copilotapp={0}, copilotapphost={1}." -f $taskAppCount, $taskHostCount)
if ($taskProcesses.Count -gt 0) {
    Write-Host 'Copilot is running. When ready, save work, fully exit it, and reopen it to adopt a changed backend.'
    Write-Host 'Closing just the window may leave its host running. No processes were stopped by this script.'
}
Write-Host 'Open Copilot from a launcher with the updated environment. Existing terminals may retain old values.'
Write-Host 'If a launcher still uses old values, a Windows sign-out/sign-in after saving work refreshes inheritance.'
Write-Host ''
Write-Host 'Also required: enableNavTabCode=true in ECS, an eligible signed-in account, and a compatible Code Runtime.'
Write-Host 'This script does not change ECS flags, model selection, authentication, or token refresh settings.'
Write-Host 'No network requests were sent. See README.md for the Opus 5.5 runtime compatibility details.'
