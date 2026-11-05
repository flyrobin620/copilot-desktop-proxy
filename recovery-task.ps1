param(
    [ValidateSet('Enable','Disable','Status','Remove')][string]$Action = 'Status',
    [string]$Python
)
$ErrorActionPreference = 'Stop'

function Get-ProxyRecoveryName {
    $taskIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $taskHasher = [Security.Cryptography.SHA256]::Create()
    try { $taskDigest = $taskHasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($taskIdentity + '|' + $PSScriptRoot.ToLowerInvariant())) }
    finally { $taskHasher.Dispose() }
    return 'CopilotProxy-' + ([BitConverter]::ToString($taskDigest).Replace('-','').Substring(0,24))
}

function Test-ProxyRecoveryOwner {
    param($Task)
    $taskScript = Join-Path $PSScriptRoot 'supervisor.py'
    return ($Task.Actions.Count -eq 1 -and $Task.Actions[0].Arguments -eq ('-B "' + $taskScript + '" run --scheduled'))
}

function Get-ProxyRecoveryStatus {
    $taskResult = @{task_available=$true;task_exists=$false;task_enabled=$false;task_owned=$false;
                    recovery_armed=((Test-Path -LiteralPath (Join-Path $PSScriptRoot '.run\recovery-options.json')) -and
                                    -not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.run\stop')))}
    try {
        $taskName = Get-ProxyRecoveryName
        $taskCurrent = @(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object { $_.TaskName -eq $taskName })
        if ($taskCurrent.Count) {
            $taskResult.task_exists = $true
            $taskResult.task_owned = Test-ProxyRecoveryOwner $taskCurrent[0]
            $taskResult.task_enabled = [bool]$taskCurrent[0].Settings.Enabled
            $taskResult.task_state = [string]$taskCurrent[0].State
            if ($taskResult.task_owned) {
                $taskInfo = Get-ScheduledTaskInfo -TaskPath '\' -TaskName $taskName
                $taskResult.task_last_result = [long]$taskInfo.LastTaskResult
                $taskResult.task_missed_runs = [long]$taskInfo.NumberOfMissedRuns
                if ($taskInfo.LastRunTime.Year -gt 2000) { $taskResult.task_last_run = $taskInfo.LastRunTime.ToUniversalTime().ToString('o') }
                if ($taskInfo.NextRunTime.Year -gt 2000) { $taskResult.task_next_run = $taskInfo.NextRunTime.ToUniversalTime().ToString('o') }
            }
        }
    } catch {
        $taskResult.task_available = $false
        $taskResult.error_type = $_.Exception.GetType().Name
        $taskResult.winerror = $_.Exception.HResult
    }
    return $taskResult
}

function Save-ProxyRecoveryResult {
    param($Result)
    try {
        $Result.time = [DateTime]::UtcNow.ToString('o')
        $Result.event = 'task_' + $Action.ToLowerInvariant()
        $taskRunFolder = Join-Path $PSScriptRoot '.run'
        New-Item -ItemType Directory -Path $taskRunFolder -Force | Out-Null
        $taskResultPath = Join-Path $taskRunFolder 'recovery-task.json'
        [IO.File]::WriteAllText($taskResultPath, ($Result | ConvertTo-Json -Compress), (New-Object Text.UTF8Encoding($false)))
    } catch { }
}

try {
    if ($Action -eq 'Status') {
        Get-ProxyRecoveryStatus | ConvertTo-Json -Compress
        exit 0
    }
    $taskName = Get-ProxyRecoveryName
    $taskExisting = @(Get-ScheduledTask -TaskPath '\' -ErrorAction Stop | Where-Object { $_.TaskName -eq $taskName })
    if ($taskExisting.Count -and -not (Test-ProxyRecoveryOwner $taskExisting[0])) {
        throw 'The recovery task name belongs to a different action; refusing to modify it.'
    }
    if ($Action -eq 'Enable') {
        if (-not $Python) { throw 'A verified Python runtime is required.' }
        $taskPythonW = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
        if (-not (Test-Path -LiteralPath $taskPythonW -PathType Leaf)) {
            throw 'The Python runtime lacks pythonw.exe; use the complete portable package.'
        }
        $taskUser = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
        $taskRun = New-ScheduledTaskAction -Execute $taskPythonW -WorkingDirectory $PSScriptRoot -Argument ('-B "' + (Join-Path $PSScriptRoot 'supervisor.py') + '" run --scheduled')
        $taskRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
        $taskLogon = New-ScheduledTaskTrigger -AtLogOn -User $taskUser
        $taskPrincipal = New-ScheduledTaskPrincipal -UserId $taskUser -LogonType Interactive -RunLevel Limited
        $taskSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries `
            -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
        $taskSettings.Hidden = $true
        $taskDefinition = New-ScheduledTask -Action $taskRun -Trigger @($taskRepeat,$taskLogon) -Principal $taskPrincipal `
            -Settings $taskSettings -Description 'Recover this user and folder Copilot proxy watchdog while explicitly enabled.'
        Register-ScheduledTask -TaskPath '\' -TaskName $taskName -InputObject $taskDefinition -Force | Out-Null
        Start-ScheduledTask -TaskPath '\' -TaskName $taskName
    } elseif ($Action -eq 'Disable' -and $taskExisting.Count) {
        # The persistent Python stop signal takes effect before this script is called.
        Disable-ScheduledTask -TaskPath '\' -TaskName $taskName | Out-Null
    } elseif ($Action -eq 'Remove' -and $taskExisting.Count) {
        if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot '.run\stop'))) {
            throw 'Run Stop Proxy.cmd before removing its recovery task.'
        }
        Unregister-ScheduledTask -TaskPath '\' -TaskName $taskName -Confirm:$false
    }
    $taskResult = Get-ProxyRecoveryStatus
    Save-ProxyRecoveryResult $taskResult
    $taskResult | ConvertTo-Json -Compress
} catch {
    # No task XML, account identifiers, arguments, paths or exception messages in diagnostics.
    $taskResult = @{task_available=$false;error_type=$_.Exception.GetType().Name;winerror=$_.Exception.HResult}
    Save-ProxyRecoveryResult $taskResult
    $taskResult | ConvertTo-Json -Compress
    exit 1
}
