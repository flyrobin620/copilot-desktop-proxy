# Claude invokes this as its credential helper. stdout must contain only the
# local placeholder; Copilot credentials never leave the proxy's auth provider.
$ErrorActionPreference = 'Stop'
try {
    . (Join-Path $PSScriptRoot 'launcher-common.ps1')
    $taskLive = Get-ProxyHealth -Path '/livez'
    if (-not $taskLive -or $taskLive.service -ne 'copilot-desktop-proxy') {
        $taskOutput = & powershell.exe -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'start.ps1') 2>&1
        if ($LASTEXITCODE -ne 0) { throw 'Proxy startup failed.' }
    }
    $taskLive = Get-ProxyHealth -Path '/livez'
    if ($taskLive.service -ne 'copilot-desktop-proxy') { throw 'Proxy is unavailable.' }
    [Console]::Out.WriteLine('local-copilot-placeholder')
    exit 0
} catch {
    [Console]::Error.WriteLine('The local Copilot proxy could not start. Run Check Setup.cmd.')
    exit 1
}
