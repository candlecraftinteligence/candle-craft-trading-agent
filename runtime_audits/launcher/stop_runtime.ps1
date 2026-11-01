param(
    [Parameter(Mandatory=$true)][ValidateSet('scanner','listener')][string]$Role,
    [Parameter(Mandatory=$true)][int]$WorkerPid,
    [Parameter(Mandatory=$true)][int]$LauncherPid,
    [Parameter(Mandatory=$true)][datetime]$WorkerCreatedUtc,
    [Parameter(Mandatory=$true)][datetime]$LauncherCreatedUtc,
    [Parameter(Mandatory=$true)][string]$OwnerApprovalRef
)
$ErrorActionPreference = 'Stop'
if ($env:COMPUTERNAME -ne 'DESKTOP-G7M42AL' -or [string]::IsNullOrWhiteSpace($OwnerApprovalRef)) {
    throw 'Approved Runtime-only operation; no DEV process control'
}
$RuntimeRepo = 'C:\Users\aspir\Desktop\Candle Craft Inteligence'
$HelperPath = Join-Path $RuntimeRepo 'audits\runtime_rollout_20261014_console.py'
$PythonPath = Join-Path $RuntimeRepo '.venv\Scripts\python.exe'
if ((Get-FileHash -LiteralPath $HelperPath -Algorithm SHA256).Hash -ne 'B02899683A0A51D718A7ED3B33456B144DA1109CA0BD83473F84A30DE537906F') {
    throw 'Console helper differs from reviewed preflight'
}
$Worker = Get-CimInstance Win32_Process -Filter "ProcessId = $WorkerPid"
$Launcher = Get-CimInstance Win32_Process -Filter "ProcessId = $LauncherPid"
$ScriptName = if ($Role -eq 'scanner') {'run_scan.py'} else {'run_telegram_bot.py'}
$ExpectedScript = Join-Path $RuntimeRepo "scripts\$ScriptName"
if (-not $Worker -or -not $Launcher -or $Worker.ParentProcessId -ne $LauncherPid -or
    $Worker.Name -ne 'python.exe' -or $Launcher.Name -ne 'python.exe' -or
    $Worker.CreationDate.ToUniversalTime() -ne $WorkerCreatedUtc.ToUniversalTime() -or
    $Launcher.CreationDate.ToUniversalTime() -ne $LauncherCreatedUtc.ToUniversalTime() -or
    -not $Worker.CommandLine.Contains($ExpectedScript) -or
    -not $Launcher.CommandLine.Contains($ExpectedScript)) {
    throw 'Owner pair, creation time or script identity changed; re-inspect, do not signal'
}
Set-Location -LiteralPath $RuntimeRepo
# First inspect membership without a signal. Helper itself rejects unexpected members.
& $PythonPath -I -B $HelperPath $WorkerPid --allow $LauncherPid $WorkerPid
if ($LASTEXITCODE -ne 0) { throw 'Console membership inspection failed' }
# Exactly one approved graceful Ctrl+C; never force-kill, remove locks or repeat.
& $PythonPath -I -B $HelperPath $WorkerPid --allow $LauncherPid $WorkerPid --interrupt
if ($LASTEXITCODE -ne 0) { throw 'Graceful interrupt failed; inspect original owners' }
$StopDeadline = [DateTime]::UtcNow.AddSeconds(60)
do {
    $StillPresent = @(Get-CimInstance Win32_Process -Filter "ProcessId = $WorkerPid OR ProcessId = $LauncherPid")
    if ($StillPresent.Count -eq 0) { break }
    Start-Sleep -Milliseconds 500
} while ([DateTime]::UtcNow -lt $StopDeadline)
if ($StillPresent.Count -ne 0) { throw 'Owner still present; no force-kill or replacement allowed' }
Write-Output 'Owner processes exited. Verify FINALIZED/drained-worker receipts before any replacement; preserve all state.'
