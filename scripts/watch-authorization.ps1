param([switch]$Once)
$ErrorActionPreference = 'Stop'
$ProxyRoot = Split-Path -Parent $PSScriptRoot
$taskLog = Join-Path $ProxyRoot 'authorization.log'
$taskSeen = New-Object 'System.Collections.Generic.HashSet[string]'
function Format-TimingDate($Value) {
    if (-not $Value) { return 'unknown' }
    try { return ([DateTimeOffset]::Parse($Value)).ToLocalTime().ToString('yyyy-MM-dd HH:mm:ss zzz') }
    catch { return 'unknown' }
}
Write-Host 'Copilot authorization timing (displayed in local time; file timestamps are UTC).'
Write-Host 'Samples are recorded by the proxy every 5 minutes. This viewer sends no model requests.'
Write-Host 'expiry_advanced means a later expiry was observed, not proof of its cause.'
Write-Host 'Close this window or press Ctrl+C to stop viewing. Proxy logging continues.'
Write-Host ''
do {
    if (Test-Path -LiteralPath $taskLog) {
        # Reopen on each pass so the viewer does not retain a handle during log rotation.
        $taskLines = @(Get-Content -LiteralPath $taskLog -Tail 20 -ErrorAction SilentlyContinue)
        foreach ($taskLine in $taskLines) {
            try { $taskRecord = $taskLine | ConvertFrom-Json } catch { continue }
            if ($taskRecord.event -notmatch '^authorization_[a-z_]+$') { continue }
            $taskKey = '{0}/{1}/{2}' -f $taskRecord.pid,$taskRecord.time,$taskRecord.event
            if (-not $taskSeen.Add($taskKey)) { continue }
            Write-Host ('[{0}] {1} (PID {2})' -f (Format-TimingDate $taskRecord.time),$taskRecord.event,$taskRecord.pid)
            if ($taskRecord.credential_readable) {
                Write-Host ('  remaining={0}s; lifetime={1}s; change={2}s' -f $taskRecord.credential_expires_in_seconds,$taskRecord.credential_lifetime_seconds,$taskRecord.credential_expiry_change_seconds)
                Write-Host ('  issued: {0}; expires: {1}' -f (Format-TimingDate $taskRecord.credential_issued_at),(Format-TimingDate $taskRecord.credential_expires_at))
            } else {
                Write-Host '  Timing could not be read. See Collect Diagnostics.cmd for metadata.'
            }
        }
        if ($taskSeen.Count -gt 1000) {
            $taskSeen.Clear()
            foreach ($taskLine in $taskLines) {
                try {
                    $taskRecord = $taskLine | ConvertFrom-Json
                    [void]$taskSeen.Add(('{0}/{1}/{2}' -f $taskRecord.pid,$taskRecord.time,$taskRecord.event))
                } catch { }
            }
        }
    } elseif ($Once) {
        Write-Host 'No timing log yet. Start this folder using agency claude or Start API.cmd (1.1.9 or later).'
    }
    if (-not $Once) { Start-Sleep -Seconds 2 }
} while (-not $Once)
