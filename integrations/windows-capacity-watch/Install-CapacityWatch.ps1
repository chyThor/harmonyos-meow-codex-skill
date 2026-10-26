[CmdletBinding()]
param(
    [string]$DeploymentDirectory = $PSScriptRoot,
    [string]$PythonExecutable = '',
    [ValidateSet('', '笔记本', '工位')][string]$DeviceLabel = '',
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$startupName = 'MeoWCapacityWatch'
$startupKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$launched = $false
$changed = $false
$wasRunning = $false
$backupDirectory = $null
$phase = 'preflight'

function Quote-NativeArgument([string]$Value) {
    # CommandLineToArgvW convention: escape backslashes only before quotes/end.
    return '"' + [regex]::Replace([regex]::Replace($Value, '(\\*)"', '$1$1\"'), '(\\+)$', '$1$1') + '"'
}

function Get-StartupValue {
    if (-not (Test-Path -LiteralPath $startupKey)) { return $null }
    $properties = Get-ItemProperty -LiteralPath $startupKey
    $property = $properties.PSObject.Properties[$startupName]
    if ($property) { return [string]$property.Value }
    return $null
}

function Set-StartupValue($Value) {
    if ($null -eq $Value) {
        if (Test-Path -LiteralPath $startupKey) {
            Remove-ItemProperty -LiteralPath $startupKey -Name $startupName -ErrorAction SilentlyContinue
        }
    } else {
        if (-not (Test-Path -LiteralPath $startupKey)) { New-Item -Path $startupKey -Force | Out-Null }
        New-ItemProperty -LiteralPath $startupKey -Name $startupName -Value ([string]$Value) -PropertyType String -Force | Out-Null
    }
}

function Is-OwnedStartup([string]$Value, [string]$ScriptPath, [string]$SettingsPath) {
    if ([string]::IsNullOrWhiteSpace($Value)) { return $false }
    $readable = $Value
    if ($Value -match '^powershell\.exe -NoProfile -WindowStyle Hidden -EncodedCommand ([A-Za-z0-9+/=]+)$') {
        try { $readable = [Text.Encoding]::Unicode.GetString([Convert]::FromBase64String($Matches[1])) }
        catch { return $false }
    }
    return $readable.IndexOf((Quote-NativeArgument $ScriptPath), [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $readable.IndexOf((Quote-NativeArgument $SettingsPath), [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $readable -match '(?i)(?:^|\s)run(?:\s|$)'
}

function Invoke-Control([string]$Action) {
    $controlOutput = & $pythonPath $watcherPath $Action --config $settingsPath 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $controlOutput) { throw 'Watcher control failed' }
    return (($controlOutput -join [Environment]::NewLine) | ConvertFrom-Json)
}

function Start-HiddenWatcher {
    $pythonwPath = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
    $launcher = if (Test-Path -LiteralPath $pythonwPath -PathType Leaf) { $pythonwPath } else { $pythonPath }
    $arguments = (Quote-NativeArgument $watcherPath) + ' run --config ' + (Quote-NativeArgument $settingsPath)
    Start-Process -FilePath $launcher -ArgumentList $arguments -WindowStyle Hidden | Out-Null
}

function Wait-Running {
    for ($attempt = 0; $attempt -lt 12; $attempt++) {
        Start-Sleep -Milliseconds 300
        try {
            $status = Invoke-Control 'status'
            if ($status.status -eq 'running' -and $status.source_ready -and $status.healthy) { return }
        } catch {}
    }
    throw 'Watcher did not become healthy'
}

function Write-PrivateJson([string]$Path, $Value) {
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N') + '.tmp'
    $utf8 = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 8), $utf8)
    if (Test-Path -LiteralPath $Path) {
        $atomicBackup = $temporary + '.bak'
        [IO.File]::Replace($temporary, $Path, $atomicBackup)
        Remove-Item -LiteralPath $atomicBackup -Force
    } else { [IO.File]::Move($temporary, $Path) }
}

try {
    $deploymentPath = (Resolve-Path -LiteralPath $DeploymentDirectory).ProviderPath
    if (-not (Test-Path -LiteralPath $deploymentPath -PathType Container)) { throw 'Deployment directory missing' }
    $watcherPath = Join-Path $deploymentPath 'capacity_watch.py'
    $settingsPath = Join-Path $deploymentPath 'capacity-watch.settings.json'
    $sourceWatcher = Join-Path $PSScriptRoot 'capacity_watch.py'
    $notificationSettingsPath = Join-Path $deploymentPath 'codex.notification.settings.json'
    $notificationSettings = if (Test-Path -LiteralPath $notificationSettingsPath -PathType Leaf) {
        Get-Content -LiteralPath $notificationSettingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
    } else { $null }

    if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
        if ($notificationSettings -and $notificationSettings.pythonExecutable -and
            (Test-Path -LiteralPath ([string]$notificationSettings.pythonExecutable) -PathType Leaf)) {
            $PythonExecutable = [string]$notificationSettings.pythonExecutable
        } else {
            $pythonCommand = Get-Command python.exe -CommandType Application -ErrorAction SilentlyContinue
            if ($pythonCommand) { $PythonExecutable = $pythonCommand.Source }
        }
    }
    if ([string]::IsNullOrWhiteSpace($PythonExecutable) -or -not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
        throw 'Usable local Python missing'
    }
    $pythonPath = (Resolve-Path -LiteralPath $PythonExecutable).ProviderPath
    $versionCheck = & $pythonPath -c 'import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.10 or newer required' }

    $oldStartup = Get-StartupValue
    if ($null -ne $oldStartup -and -not (Is-OwnedStartup $oldStartup $watcherPath $settingsPath)) {
        throw 'Startup name belongs to a different configuration'
    }
    $hadSettings = Test-Path -LiteralPath $settingsPath -PathType Leaf
    $hadWatcher = Test-Path -LiteralPath $watcherPath -PathType Leaf

    if ($Uninstall) {
        if ($hadWatcher -and $hadSettings) {
            $stopped = Invoke-Control 'stop'
            if ($stopped.status -ne 'stopped') { throw 'Watcher has not stopped' }
        }
        if ($null -ne $oldStartup) { Set-StartupValue $null }
        [pscustomobject]@{ ok=$true; status='uninstalled'; final_notifications_preserved=$true } | ConvertTo-Json -Compress
        exit 0
    }

    foreach ($name in @('CodexNotify.ps1', 'MeowPush.ps1', 'classify_codex_thread.py', 'meow.config.json')) {
        if (-not (Test-Path -LiteralPath (Join-Path $deploymentPath $name) -PathType Leaf)) {
            throw 'Existing MeoW notification deployment is incomplete'
        }
    }
    if (-not (Test-Path -LiteralPath $sourceWatcher -PathType Leaf)) { throw 'Package watcher missing' }
    $selectedLabel = if ($DeviceLabel) { $DeviceLabel } elseif ($notificationSettings) { [string]$notificationSettings.deviceLabel } else { '' }
    if ($selectedLabel -notin @('笔记本', '工位')) { throw 'Device label is not configured' }
    if (-not $env:LOCALAPPDATA) { throw 'Local application-data directory unavailable' }
    $codexDirectory = if ($env:CODEX_HOME) { [IO.Path]::GetFullPath($env:CODEX_HOME) } else { Join-Path $env:USERPROFILE '.codex' }
    $logDirectory = Join-Path $env:LOCALAPPDATA 'Codex\Logs'
    if (-not (Test-Path -LiteralPath $codexDirectory -PathType Container)) { throw 'Codex user directory missing' }

    $phase = 'backup'
    $backupDirectory = Join-Path $deploymentPath ('.state\capacity-watch-backups\' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
    Write-PrivateJson (Join-Path $backupDirectory 'startup.json') ([ordered]@{ exists=($null -ne $oldStartup); command=$oldStartup })
    if ($hadSettings) { Copy-Item -LiteralPath $settingsPath -Destination (Join-Path $backupDirectory 'capacity-watch.settings.json') }
    if ($hadWatcher) { Copy-Item -LiteralPath $watcherPath -Destination (Join-Path $backupDirectory 'capacity_watch.py') }

    if ($hadWatcher -and $hadSettings) {
        $phase = 'stop_previous'
        $previousStatus = Invoke-Control 'status'
        $wasRunning = $previousStatus.status -eq 'running'
        $stopped = Invoke-Control 'stop'
        if ($stopped.status -ne 'stopped') { throw 'Old watcher has not stopped' }
    }

    $changed = $true
    $phase = 'configure'
    if ([IO.Path]::GetFullPath($sourceWatcher) -ne [IO.Path]::GetFullPath($watcherPath)) {
        Copy-Item -LiteralPath $sourceWatcher -Destination $watcherPath -Force
    }
    $config = [ordered]@{
        deploy_dir=$deploymentPath; codex_home=$codexDirectory; log_root=$logDirectory
        state_dir=(Join-Path $deploymentPath '.state\capacity-watch'); device_label=$selectedLabel
        poll_seconds=2; cooldown_seconds=300
    }
    Write-PrivateJson $settingsPath $config
    $pythonwPath = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
    $startupLauncher = if (Test-Path -LiteralPath $pythonwPath -PathType Leaf) { $pythonwPath } else { $pythonPath }
    # pythonw is preferred; fallback startup invokes a hidden PowerShell launcher.
    if ($startupLauncher -eq $pythonwPath) {
        $startupCommand = (Quote-NativeArgument $startupLauncher) + ' ' + (Quote-NativeArgument $watcherPath) + ' run --config ' + (Quote-NativeArgument $settingsPath)
    } else {
        $hiddenCommand = 'Start-Process -FilePath ' + "'" + $pythonPath.Replace("'", "''") + "'" + ' -WindowStyle Hidden -ArgumentList ' + "'" + ((Quote-NativeArgument $watcherPath) + ' run --config ' + (Quote-NativeArgument $settingsPath)).Replace("'", "''") + "'"
        $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($hiddenCommand))
        $startupCommand = 'powershell.exe -NoProfile -WindowStyle Hidden -EncodedCommand ' + $encoded
    }
    $phase = 'register_startup'
    Set-StartupValue $startupCommand
    $phase = 'start'
    Start-HiddenWatcher
    $launched = $true
    $phase = 'verify_running'
    Wait-Running
    [pscustomobject]@{ ok=$true; status='installed'; running=$true; startup='current-user-logon'; final_notifications_preserved=$true } | ConvertTo-Json -Compress
} catch {
    # Do not echo raw exception text, private settings or startup command lines.
    $recovery = 'not_needed'
    if ($changed -and $backupDirectory) {
        try {
            if ($launched) {
                $newStopped = Invoke-Control 'stop'
                if ($newStopped.status -ne 'stopped') { throw 'New watcher did not stop' }
            }
            Set-StartupValue $oldStartup
            if ($hadWatcher) {
                Copy-Item -LiteralPath (Join-Path $backupDirectory 'capacity_watch.py') -Destination $watcherPath -Force
            } elseif (Test-Path -LiteralPath $watcherPath -PathType Leaf) { Remove-Item -LiteralPath $watcherPath -Force }
            if ($hadSettings) {
                Copy-Item -LiteralPath (Join-Path $backupDirectory 'capacity-watch.settings.json') -Destination $settingsPath -Force
            } elseif (Test-Path -LiteralPath $settingsPath -PathType Leaf) { Remove-Item -LiteralPath $settingsPath -Force }
            if ($wasRunning -and $hadWatcher -and $hadSettings) { Start-HiddenWatcher; Wait-Running }
            $recovery = 'restored'
        } catch { $recovery = 'manual_check_required' }
    }
    [Console]::Error.WriteLine((@{ ok=$false; status='install_failed'; phase=$phase; recovery=$recovery; error_class=$_.Exception.GetType().Name } | ConvertTo-Json -Compress))
    exit 1
}
