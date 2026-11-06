# Loaded by the user's PowerShell profile after opting in with Enable Agency.cmd.
$taskAgencyApplication=Get-Command agency.exe -CommandType Application -ErrorAction SilentlyContinue
if ($taskAgencyApplication) {
    $taskAgencyExecutable=$taskAgencyApplication.Source
    $taskProxyStartup=Join-Path $PSScriptRoot 'agency-start.ps1'
    $taskAgencyWrapper={
        if ($args.Count -gt 0 -and $args[0] -eq 'claude') {
            & $taskProxyStartup
        }
        & $taskAgencyExecutable @args
    }.GetNewClosure()
    Set-Item -Path Function:global:agency -Value $taskAgencyWrapper
}
