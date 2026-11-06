<#
.SYNOPSIS
Install or update this Copilot Desktop Proxy folder in one run.

.DESCRIPTION
Idempotent. Safe to rerun at any time; every step skips work that is already done.
  1. Checks for active requests before anything is restarted.
  2. git pull --ff-only (Git checkouts), then continues with the pulled installer.
  3. Installs or upgrades the pinned runtimes from config\package-lock.json.
  4. Prepares Microsoft sign-in (standalone mode: no Copilot desktop app needed)
     and silently migrates an older Copilot-database profile.
  5. Re-points client integrations after folder layout changes.
  6. Restarts the proxy if it was running (keeps a deliberate stop and the
     startup mode), then verifies version, readiness and one short request.

.EXAMPLE
Install.cmd
.EXAMPLE
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\install.ps1 -SignIn
#>
[CmdletBinding()]
param(
    # Run the one-time Microsoft account sign-in (opens a Windows sign-in dialog).
    [switch]$SignIn,
    # Account hint for -SignIn, e.g. name@contoso.com.
    [string]$Username,
    # Do not run git pull; use the files as they are.
    [switch]$NoPull,
    # Restart even if requests are in flight (they will be interrupted).
    [switch]$Force,
    # Leave the proxy as it is: no stop, start or verification request.
    [switch]$NoStart,
    # Skip the final short model request (local checks still run).
    [switch]$SkipInferenceCheck,
    # Change when the proxy starts. Keep = unchanged.
    [ValidateSet('Keep', 'OnDemand', 'AtLogon')][string]$StartupMode = 'Keep',
    # Install the pinned Python/Claude Code into runtime\ even if usable copies exist elsewhere.
    [switch]$BundledRuntime,
    # Do not download Claude Code (Claude App / API-only use).
    [switch]$SkipClaude,
    # A full Python 3.11+ (with venv) used once to create the sign-in environment.
    [string]$AuthPython
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$ProxyRoot = Split-Path -Parent $PSScriptRoot
$script:Notes = New-Object System.Collections.Generic.List[string]
$script:Summary = [ordered]@{}
$script:StepNumber = 0

function Write-Step([string]$Text) {
    $script:StepNumber++
    Write-Host ''
    Write-Host ("[{0}/6] {1}" -f $script:StepNumber, $Text) -ForegroundColor Cyan
}
function Write-Info([string]$Text) { Write-Host ('  ' + $Text) }
function Add-Note([string]$Text) {
    $script:Notes.Add($Text)
    Write-Host ('  NOTE: ' + $Text) -ForegroundColor Yellow
}
function Test-Interactive {
    try { return [Environment]::UserInteractive -and -not [Console]::IsInputRedirected } catch { return $false }
}
function Invoke-Json {
    param([string]$Exe, [string[]]$Arguments)
    $raw = & $Exe @Arguments 2>$null
    $code = $LASTEXITCODE
    $text = ($raw | Out-String).Trim()
    $value = $null
    if ($text) { try { $value = $text.Split("`n")[-1] | ConvertFrom-Json } catch { } }
    return [pscustomobject]@{ Code = $code; Value = $value; Text = $text }
}
function Get-Local([string]$Path) {
    # Loopback only; never sent through a system proxy.
    $response = $null
    try {
        $request = [Net.HttpWebRequest]::Create('http://127.0.0.1:8787' + $Path)
        $request.Proxy = $null; $request.Timeout = 3000; $request.ReadWriteTimeout = 3000
        try { $response = $request.GetResponse() } catch [Net.WebException] { $response = $_.Exception.Response }
        if (-not $response) { return $null }
        $reader = New-Object IO.StreamReader($response.GetResponseStream())
        try { return ($reader.ReadToEnd() | ConvertFrom-Json) } finally { $reader.Dispose() }
    } catch { return $null } finally { if ($response) { $response.Dispose() } }
}
function Invoke-Script([string]$Name, [string[]]$Arguments = @()) {
    & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot $Name) @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Name failed (exit $LASTEXITCODE)." }
}

try {
    if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64' -or
        $env:PROCESSOR_ARCHITEW6432 -eq 'ARM64') { throw 'This proxy supports Windows x64 (Intel/AMD) only.' }
    Write-Host "Copilot Desktop Proxy setup: $ProxyRoot"

    # ---------------------------------------------------------------- 1. preflight
    Write-Step 'Checking the running proxy'
    $live = Get-Local '/livez'
    $wasRunning = $live -and $live.service -eq 'copilot-desktop-proxy'
    $diagnostics = if ($wasRunning) { Get-Local '/diagnostics' } else { $null }
    $stopped = Test-Path -LiteralPath (Join-Path $ProxyRoot '.run\stop')
    $onDemand = Test-Path -LiteralPath (Join-Path $ProxyRoot '.run\on-demand')
    $freshInstall = -not (Test-Path -LiteralPath (Join-Path $ProxyRoot '.run'))
    if ($wasRunning) {
        Write-Info ("Running version {0}, active requests: {1}." -f $diagnostics.version, $diagnostics.active_requests)
        $willRestart = -not $NoStart
        if ($willRestart -and $diagnostics.active_requests -gt 0 -and -not $Force) {
            $message = "$($diagnostics.active_requests) request(s) are in progress. Restarting interrupts a reply that is being generated (Claude retries the connection; history is kept)."
            if (Test-Interactive) {
                Write-Host ('  ' + $message) -ForegroundColor Yellow
                $answer = Read-Host '  Restart now anyway? [y/N]'
                if ($answer -notmatch '^(y|yes)$') { throw 'Cancelled. Rerun when Claude is idle, or add -Force.' }
                $PSBoundParameters['Force'] = [switch]$true
            } else {
                throw ($message + ' Rerun when idle, or pass -Force.')
            }
        }
    } else {
        Write-Info $(if ($stopped) { 'Not running (stopped deliberately).' } elseif ($onDemand) { 'Not running (on-demand mode).' } else { 'Not running.' })
    }

    # ---------------------------------------------------------------- 2. update files
    Write-Step 'Updating files'
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if ($NoPull) {
        Write-Info 'Skipped (-NoPull).'
    } elseif (-not (Test-Path -LiteralPath (Join-Path $ProxyRoot '.git'))) {
        Write-Info 'Not a Git checkout. To update a ZIP copy, download the new ZIP and run Install.cmd from the new folder.'
    } elseif (-not $git) {
        Add-Note 'Git was not found; files were not updated. Install Git or run git pull yourself.'
    } else {
        $before = (& git -C $ProxyRoot rev-parse HEAD).Trim()
        $dirty = & git -C $ProxyRoot status --porcelain --untracked-files=no
        if ($dirty) {
            throw 'This folder has uncommitted changes to tracked files. Commit or discard them, or rerun with -NoPull to install the files as they are.'
        }
        & git -C $ProxyRoot pull --ff-only
        if ($LASTEXITCODE -ne 0) { throw 'git pull --ff-only failed (see above). Nothing was changed; resolve it and rerun.' }
        $after = (& git -C $ProxyRoot rev-parse HEAD).Trim()
        $script:Summary['Files'] = if ($before -eq $after) { "already current ($($after.Substring(0,7)))" } else { "$($before.Substring(0,7)) -> $($after.Substring(0,7))" }
        Write-Info $script:Summary['Files']
        if ($before -ne $after) {
            # Continue with the installer that was just pulled.
            $forward = @('-NoPull')
            foreach ($key in $PSBoundParameters.Keys) {
                if ($key -eq 'NoPull') { continue }
                $value = $PSBoundParameters[$key]
                if ($value -is [switch]) { if ($value.IsPresent) { $forward += "-$key" } } else { $forward += @("-$key", [string]$value) }
            }
            $installer = Join-Path $ProxyRoot 'scripts\install.ps1'
            & powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File $installer @forward
            exit $LASTEXITCODE
        }
    }

    # ---------------------------------------------------------------- 3. runtimes
    Write-Step 'Checking runtimes'
    $lock = Get-Content -LiteralPath (Join-Path $ProxyRoot 'config\package-lock.json') -Raw | ConvertFrom-Json
    $runtimeRoot = Join-Path $ProxyRoot 'runtime'
    $versionsPath = Join-Path $runtimeRoot 'versions.json'
    $versions = @{}
    if (Test-Path -LiteralPath $versionsPath) {
        (Get-Content -LiteralPath $versionsPath -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $versions[$_.Name] = $_.Value }
    }
    $cache = Join-Path $ProxyRoot '.package-cache'
    function Get-Download([string]$Url, [string]$Name, [string]$ExpectedHost) {
        $uri = [Uri]$Url
        if ($uri.Scheme -ne 'https' -or $uri.Host -ne $ExpectedHost) { throw 'Unexpected runtime download origin.' }
        New-Item -ItemType Directory -Path $cache -Force | Out-Null
        $destination = Join-Path $cache $Name
        if (-not (Test-Path -LiteralPath $destination)) {
            $partial = $destination + '.' + [Guid]::NewGuid().ToString('N') + '.partial'
            try {
                Write-Info "Downloading $Name from $ExpectedHost..."
                Invoke-WebRequest -Uri $Url -OutFile $partial -UseBasicParsing -TimeoutSec 600
                Move-Item -LiteralPath $partial -Destination $destination
            } finally { if (Test-Path -LiteralPath $partial) { Remove-Item -LiteralPath $partial } }
        }
        return $destination
    }
    function Set-PythonSearchPath([string]$Folder) {
        # The embedded runtime ignores the script folder; make src importable.
        $pth = @(Get-ChildItem -LiteralPath $Folder -Filter 'python*._pth')
        if ($pth.Count -ne 1) { throw 'Unexpected embedded Python layout.' }
        $content = $pth[0].BaseName + ".zip`r`n.`r`n..\..\src`r`n"
        if ([IO.File]::ReadAllText($pth[0].FullName) -ne $content) { [IO.File]::WriteAllText($pth[0].FullName, $content, [Text.Encoding]::ASCII) }
    }
    . (Join-Path $PSScriptRoot 'launcher-common.ps1')
    $bundledPython = Join-Path $runtimeRoot 'python\python.exe'
    $externalPython = $null
    if (-not $BundledRuntime -and -not (Test-Path -LiteralPath $bundledPython)) {
        try { $externalPython = Resolve-ProxyPython } catch { $externalPython = $null }
    }
    $pythonStage = $null
    if ($externalPython) {
        Write-Info "Python: using existing $externalPython (pass -BundledRuntime to install the pinned copy)."
        $script:Summary['Python'] = "existing ($externalPython)"
    } else {
        $installed = $versions['python']
        if (-not $installed -and (Test-Path -LiteralPath $bundledPython)) {
            $installed = (& $bundledPython -c "import sys; print(sys.version.split()[0])").Trim()
        }
        if ($installed -ne $lock.python.version) {
            $archive = Get-Download $lock.python.url "python-$($lock.python.version)-embed-amd64.zip" 'www.python.org'
            $pythonStage = Join-Path $runtimeRoot 'python.new'
            if (Test-Path -LiteralPath $pythonStage) { Remove-Item -LiteralPath $pythonStage -Recurse -Force }
            Expand-Archive -LiteralPath $archive -DestinationPath $pythonStage -Force
            Set-PythonSearchPath $pythonStage
            Write-Info $(if ($installed) { "Python $installed -> $($lock.python.version) (applied while the proxy is stopped)." } else { "Python $($lock.python.version) staged." })
        } else {
            Set-PythonSearchPath (Split-Path -Parent $bundledPython)
            Write-Info "Python $installed (pinned) is current."
        }
        $script:Summary['Python'] = "bundled $($lock.python.version)"
    }
    $claudeExe = Join-Path $runtimeRoot 'claude\claude.exe'
    $externalClaude = $null
    if (-not $BundledRuntime -and -not (Test-Path -LiteralPath $claudeExe)) {
        try { $externalClaude = Resolve-ProxyClaude } catch { $externalClaude = $null }
    }
    if ($externalClaude) {
        Write-Info "Claude Code: using existing $externalClaude."
    } elseif ($SkipClaude) {
        Write-Info 'Claude Code: skipped (-SkipClaude).'
    } elseif ($versions['claude'] -ne $lock.claude.version -or -not (Test-Path -LiteralPath $claudeExe)) {
        $download = Get-Download $lock.claude.url "claude-$($lock.claude.version)-win32-x64.exe" 'downloads.claude.ai'
        New-Item -ItemType Directory -Path (Split-Path -Parent $claudeExe) -Force | Out-Null
        if (Test-Path -LiteralPath $claudeExe) {
            # A running claude.exe can be renamed but not overwritten.
            $old = $claudeExe + '.old-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
            Move-Item -LiteralPath $claudeExe -Destination $old
        }
        Copy-Item -LiteralPath $download -Destination $claudeExe
        $versions['claude'] = $lock.claude.version
        Write-Info "Claude Code $($lock.claude.version) installed."
    } else {
        Write-Info "Claude Code $($lock.claude.version) (pinned) is current."
    }
    Get-ChildItem -LiteralPath (Split-Path -Parent $claudeExe) -Filter 'claude.exe.old-*' -ErrorAction SilentlyContinue |
        ForEach-Object { Remove-Item -LiteralPath $_.FullName -ErrorAction SilentlyContinue }

    # Applies a staged Python update; only called while the proxy is stopped.
    function Complete-RuntimeUpdate {
        if ($pythonStage -and (Test-Path -LiteralPath $pythonStage)) {
            $current = Join-Path $runtimeRoot 'python'
            if (Test-Path -LiteralPath $current) { Remove-Item -LiteralPath $current -Recurse -Force }
            Move-Item -LiteralPath $pythonStage -Destination $current
            $versions['python'] = $lock.python.version
        }
        if (-not $externalPython -and -not $versions['python']) { $versions['python'] = $lock.python.version }
        if ($versions.Count) {
            New-Item -ItemType Directory -Path $runtimeRoot -Force | Out-Null
            [IO.File]::WriteAllText($versionsPath, ($versions | ConvertTo-Json), (New-Object Text.UTF8Encoding($false)))
        }
    }
    $needsStop = [bool]$pythonStage
    if ($needsStop -and $wasRunning -and $NoStart) { throw 'A Python runtime update needs a proxy restart; rerun without -NoStart.' }
    if (-not $wasRunning) { Complete-RuntimeUpdate }

    # ---------------------------------------------------------------- 4. sign-in
    Write-Step 'Checking Microsoft sign-in'
    $proxyPython = if ($externalPython) { $externalPython } elseif (Test-Path -LiteralPath $bundledPython) { $bundledPython } else { $null }
    $helper = Join-Path $ProxyRoot 'src\token_helper.py'
    $auth = $null
    if ($proxyPython) { $auth = (Invoke-Json $proxyPython @('-B', $helper, 'status')).Value }
    if (-not $auth) {
        # The new Python is applied after the stop below; any stdlib Python can read the status.
        $probe = if ($pythonStage) { Join-Path $pythonStage 'python.exe' } else { $null }
        if ($probe) { $auth = (Invoke-Json $probe @('-B', $helper, 'status')).Value }
    }
    $copilotDb = @(Get-ChildItem -Path (Join-Path $env:LOCALAPPDATA 'Microsoft\Copilot\User Data\CopilotCodingHome\*\data.db') -ErrorAction SilentlyContinue).Count -gt 0
    $configured = $auth -and $auth.configured
    $wantSignIn = $SignIn -or (-not $configured -and -not $copilotDb -and (Test-Interactive))
    $needAuthRuntime = $configured -or $wantSignIn
    $authPythonExe = $null
    if ($needAuthRuntime) {
        $authRoot = Join-Path $env:USERPROFILE '.copilot-desktop-proxy\auth-runtime'
        if ($auth -and $auth.python_exists) { $authPythonExe = $auth.python; $authRoot = Split-Path -Parent (Split-Path -Parent $auth.python) }
        elseif (Test-Path -LiteralPath (Join-Path $authRoot 'Scripts\python.exe')) { $authPythonExe = Join-Path $authRoot 'Scripts\python.exe' }
        if (-not $authPythonExe) {
            $basePython = $null
            foreach ($candidate in @($AuthPython, 'py-launcher', 'python.exe')) {
                if (-not $candidate) { continue }
                $exe = $null; $prefix = @()
                if ($candidate -eq 'py-launcher') {
                    if (Get-Command py.exe -ErrorAction SilentlyContinue) { $exe = 'py.exe'; $prefix = @('-3') }
                } elseif ($candidate -eq 'python.exe') {
                    $command = Get-Command python.exe -ErrorAction SilentlyContinue
                    if ($command -and $command.Source -notlike '*\WindowsApps\*') { $exe = $command.Source }
                } else { $exe = $candidate }
                if (-not $exe) { continue }
                & $exe @prefix -c "import sys, venv, ensurepip; assert sys.version_info >= (3, 11)" 2>$null
                if ($LASTEXITCODE -eq 0) { $basePython = @($exe) + $prefix; break }
            }
            if (-not $basePython) {
                Add-Note 'Microsoft sign-in needs a full Python 3.11+ once (python.org installer, with pip). Install it, then rerun Install.cmd -SignIn. The bundled runtime cannot create the sign-in environment.'
            } else {
                Write-Info 'Creating the sign-in environment...'
                $baseExe = $basePython[0]; $baseArgs = @($basePython | Select-Object -Skip 1)
                & $baseExe @baseArgs -m venv $authRoot
                if ($LASTEXITCODE -ne 0) { throw 'Could not create the sign-in environment (python -m venv).' }
                $authPythonExe = Join-Path $authRoot 'Scripts\python.exe'
            }
        }
        if ($authPythonExe) {
            $requirements = Join-Path $ProxyRoot 'config\auth-requirements.txt'
            $hash = (Get-FileHash -LiteralPath $requirements -Algorithm SHA256).Hash
            $marker = Join-Path $authRoot '.copilot-proxy-requirements.sha256'
            $current = if (Test-Path -LiteralPath $marker) { (Get-Content -LiteralPath $marker -Raw).Trim() } else { '' }
            if ($current -ne $hash) {
                Write-Info 'Installing pinned sign-in packages (MSAL)...'
                & $authPythonExe -m pip install --disable-pip-version-check -q -r $requirements
                if ($LASTEXITCODE -ne 0) { throw 'Could not install the sign-in packages (see pip output above).' }
                [IO.File]::WriteAllText($marker, $hash)
            } else { Write-Info 'Sign-in packages are current.' }
        }
    }
    if ($authPythonExe -and $wantSignIn) {
        Write-Info 'Opening Microsoft sign-in. Choose the work account that has Copilot Code access.'
        $arguments = @('-B', $helper, 'initialize', '--interactive')
        if ($Username) { $arguments += @('--username', $Username) }
        $result = Invoke-Json $authPythonExe $arguments
        if ($result.Code -ne 0 -or -not $result.Value.ok) {
            $reason = if ($result.Value) { $result.Value.reason } else { 'helper_unavailable' }
            throw "Sign-in did not complete ($reason). An account sign-in does not grant Copilot Code access; check that this account can use Code, then rerun with -SignIn."
        }
        $script:Summary['Sign-in'] = "standalone, $($result.Value.models) models"
        Write-Info 'Signed in. The proxy no longer needs the Copilot desktop app.'
    } elseif ($authPythonExe -and $configured) {
        $result = Invoke-Json $authPythonExe @('-B', $helper, 'migrate')
        if ($result.Code -eq 0 -and $result.Value.ok) {
            $script:Summary['Sign-in'] = 'standalone'
            Write-Info $(if ($result.Value.migrated) { 'Switched to standalone mode: tokens, gateway settings and models no longer come from the Copilot app.' } else { 'Standalone sign-in is configured.' })
        } else {
            $reason = if ($result.Value) { $result.Value.reason } else { 'helper_unavailable' }
            $script:Summary['Sign-in'] = "unchanged ($reason)"
            if ($auth.source -eq 'copilot_db') {
                Add-Note "Could not switch to standalone mode silently ($reason); the existing Copilot-app setup keeps working. Run Install.cmd -SignIn to sign in again."
            } else {
                Add-Note "Silent sign-in check failed ($reason). If requests fail with 401/503, run Install.cmd -SignIn."
            }
        }
    } elseif (-not $configured) {
        if ($copilotDb) {
            $script:Summary['Sign-in'] = 'via Copilot desktop app'
            Add-Note 'Using the Copilot desktop app''s sign-in. To remove that dependency, run Install.cmd -SignIn once.'
        } else {
            $script:Summary['Sign-in'] = 'missing'
            Add-Note 'No sign-in is configured. Run Install.cmd -SignIn in an interactive window.'
        }
    }

    # ---------------------------------------------------------------- 5. integrations
    Write-Step 'Updating client integrations'
    $proxyPython = if ($externalPython) { $externalPython } elseif ($pythonStage -and -not (Test-Path -LiteralPath $bundledPython)) { Join-Path $pythonStage 'python.exe' } else { $bundledPython }
    $desktop = Invoke-Json $proxyPython @('-B', '-X', 'utf8', (Join-Path $ProxyRoot 'src\claude_desktop.py'), 'migrate')
    if ($desktop.Value -and $desktop.Value.migrated) {
        Write-Info 'Claude App credential helper re-pointed to scripts\claude-gateway-auth.ps1.'
        Add-Note 'Claude App was re-pointed to the moved credential script. If Claude App reports a gateway credential error, save your work, fully quit it and reopen it once.'
    } elseif ($desktop.Code -ne 0) {
        if ($desktop.Value.reason -eq 'packaged_app_redirects_appdata') {
            Add-Note 'Claude App settings were not updated: this terminal runs inside a packaged app (for example Codex), where Windows redirects AppData. Rerun Install.cmd from Explorer or a normal PowerShell window.'
        } else {
            Add-Note "Claude App integration check failed ($($desktop.Value.reason)). Run shortcuts\Check Claude Desktop.cmd."
        }
    }
    $documents = [Environment]::GetFolderPath('MyDocuments')
    $oldLoader = "'" + (Join-Path $ProxyRoot 'agency-shell.ps1').Replace("'", "''") + "'"
    $newLoader = "'" + (Join-Path $ProxyRoot 'scripts\agency-shell.ps1').Replace("'", "''") + "'"
    foreach ($folder in @('WindowsPowerShell', 'PowerShell')) {
        $profilePath = Join-Path $documents ($folder + '\profile.ps1')
        if (-not (Test-Path -LiteralPath $profilePath)) { continue }
        $text = [IO.File]::ReadAllText($profilePath)
        if ($text.Contains('# BEGIN COPILOT DESKTOP PROXY') -and $text.Contains($oldLoader)) {
            [IO.File]::WriteAllText($profilePath, $text.Replace($oldLoader, $newLoader), (New-Object Text.UTF8Encoding($true)))
            Write-Info "Agency loader re-pointed in $folder profile."
        }
    }
    Write-Info 'Claude Code settings are refreshed when the proxy starts.'

    # ---------------------------------------------------------------- 6. restart and verify
    Write-Step 'Restarting and verifying'
    $startArgs = @()
    if ($NoStart) {
        Write-Info 'Skipped (-NoStart). Changes apply at the next start.'
    } elseif ($wasRunning) {
        Invoke-Script 'stop.ps1'
        Complete-RuntimeUpdate
        Invoke-Script 'start.ps1'
    } elseif ($stopped -and -not $freshInstall) {
        Complete-RuntimeUpdate
        Write-Info 'Kept stopped, as before. Start it with shortcuts\Start API.cmd or a Launch shortcut.'
    } elseif ($script:Summary['Sign-in'] -eq 'missing') {
        Complete-RuntimeUpdate
        Write-Info 'Not started: sign-in is required first.'
    } elseif ($onDemand) {
        Complete-RuntimeUpdate
        Write-Info 'On-demand mode: Claude starts the proxy when it needs it.'
    } else {
        Complete-RuntimeUpdate
        Invoke-Script 'start.ps1'
    }
    if ($StartupMode -eq 'OnDemand') {
        $helperResult = Invoke-Json $proxyPython @('-B', '-X', 'utf8', (Join-Path $ProxyRoot 'src\claude_desktop.py'), 'enable-helper')
        if ($helperResult.Code -ne 0) { throw "On-demand startup needs this proxy's own local Claude App configuration ($($helperResult.Value.reason)). See docs\TOKEN-REFRESH.md." }
        Invoke-Script 'set-startup-mode.ps1' @('-Mode', 'OnDemand')
        Add-Note 'Claude App now starts the proxy on demand. Save your work, fully quit Claude App and reopen it once.'
    } elseif ($StartupMode -eq 'AtLogon') {
        Invoke-Script 'set-startup-mode.ps1' @('-Mode', 'SignIn')
    }
    $script:Summary['Startup'] = if (Test-Path -LiteralPath (Join-Path $ProxyRoot '.run\on-demand')) { 'on demand (Claude starts it)' } else { 'at Windows sign-in, with recovery' }

    $live = if ($NoStart) { $null } else { Get-Local '/livez' }
    if ($live -and $live.service -eq 'copilot-desktop-proxy') {
        $diagnostics = Get-Local '/diagnostics'
        $health = Get-Local '/health'
        $script:Summary['Running'] = "version $($diagnostics.version)"
        if ($diagnostics.version -ne $lock.package_version) {
            Add-Note "The running proxy reports $($diagnostics.version), expected $($lock.package_version). Another folder may own port 8787; stop that copy first."
        }
        if ($health.status -eq 'ready') {
            $script:Summary['Local check'] = "ready, default model $($health.default_model)"
            if ($SkipInferenceCheck) {
                $script:Summary['Model request'] = 'skipped'
            } else {
                $check = Invoke-Json $proxyPython @('-B', (Join-Path $ProxyRoot 'src\check_authorization.py'))
                if ($check.Code -eq 0) { $script:Summary['Model request'] = 'OK' }
                else {
                    Write-Info $check.Text
                    $script:Summary['Model request'] = 'FAILED'
                    Add-Note 'The short model request failed. 401: run Install.cmd -SignIn. 403: the account or organization has no access to this model. Details: shortcuts\Collect Diagnostics.cmd.'
                }
            }
        } else {
            $script:Summary['Local check'] = 'not ready'
            Add-Note 'The proxy runs but has no upstream authorization. Run Install.cmd -SignIn.'
        }
    } elseif (-not $NoStart) {
        $script:Summary['Running'] = 'no'
    }

    Write-Host ''
    Write-Host 'Summary' -ForegroundColor Green
    foreach ($key in $script:Summary.Keys) { Write-Host ('  {0,-14} {1}' -f ($key + ':'), $script:Summary[$key]) }
    if ($script:Notes.Count) {
        Write-Host ''
        Write-Host 'Needs your attention' -ForegroundColor Yellow
        foreach ($note in $script:Notes) { Write-Host ('  - ' + $note) -ForegroundColor Yellow }
    }
    if ($script:Summary['Model request'] -eq 'FAILED') { exit 2 }
    exit 0
} catch {
    Write-Host ''
    Write-Host ('ERROR: ' + $_.Exception.Message) -ForegroundColor Red
    if ($script:Notes.Count) { foreach ($note in $script:Notes) { Write-Host ('  - ' + $note) -ForegroundColor Yellow } }
    exit 1
}
