# Read-only, bounded Windows host evidence. Never emits process arguments or env values.
$ErrorActionPreference = 'Continue'
$cciHostEvidence = [ordered]@{ observed_at = (Get-Date).ToUniversalTime().ToString('o') }
try {
    $cciProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'pythonw.exe'" -ErrorAction Stop)
    $cciRelevant = @($cciProcesses | Where-Object { $_.CommandLine -match 'run_scan\.py|run_telegram_bot\.py' })
    $cciHostEvidence.scanner_count = @($cciRelevant | Where-Object { $_.CommandLine -match 'run_scan\.py' }).Count
    $cciHostEvidence.listener_count = @($cciRelevant | Where-Object { $_.CommandLine -match 'run_telegram_bot\.py' }).Count
    $cciHostEvidence.python_processes_with_unavailable_arguments = @($cciProcesses | Where-Object { -not $_.CommandLine }).Count
    $cciHostEvidence.process_count_interpretation = 'Matching Python processes only; launcher/worker parent-child pairs require operator review before declaring duplicates'
    if ($cciHostEvidence.python_processes_with_unavailable_arguments -gt 0) {
        $cciHostEvidence.scanner_count = 'Unverified: some process identities unavailable'
        $cciHostEvidence.listener_count = 'Unverified: some process identities unavailable'
    }
    $cciBefore = @{}
    foreach ($cciProcess in $cciRelevant) {
        $cciSample = Get-Process -Id $cciProcess.ProcessId -ErrorAction SilentlyContinue
        if ($cciSample) { $cciBefore[$cciProcess.ProcessId] = $cciSample.CPU }
    }
    $cciTimer = [Diagnostics.Stopwatch]::StartNew()
    Start-Sleep -Seconds 3
    $cciElapsed = $cciTimer.Elapsed.TotalSeconds
    $cciHostEvidence.processes = @($cciRelevant | ForEach-Object {
        $cciSample = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue
        if ($cciSample) {
            [ordered]@{
                pid = $_.ProcessId
                role = $(if ($_.CommandLine -match 'run_scan\.py') { 'scanner' } else { 'listener' })
                working_set_mb = [math]::Round($cciSample.WorkingSet64 / 1MB, 1)
                private_mb = [math]::Round($cciSample.PrivateMemorySize64 / 1MB, 1)
                cpu_percent_one_core = $(if ($null -ne $cciSample.CPU -and $cciBefore.ContainsKey($_.ProcessId) -and $null -ne $cciBefore[$_.ProcessId]) {
                    [math]::Round(100 * ($cciSample.CPU - $cciBefore[$_.ProcessId]) / $cciElapsed, 1)
                } else { 'N/A' })
                parent_pid = $_.ParentProcessId
            }
        }
    })
    $cciOS = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
    $cciHostEvidence.memory = @{ free_mb = [math]::Round($cciOS.FreePhysicalMemory / 1024); total_mb = [math]::Round($cciOS.TotalVisibleMemorySize / 1024) }
    $cciHostEvidence.host_cpu_percent = @(Get-CimInstance Win32_Processor | Select-Object -ExpandProperty LoadPercentage)
} catch { $cciHostEvidence.process_evidence = 'N/A: process/host counters unavailable' }
try {
    $cciHostEvidence.firewall_profiles = @(Get-NetFirewallProfile -ErrorAction Stop | Select-Object Name, Enabled, DefaultOutboundAction)
} catch { $cciHostEvidence.firewall_profiles = 'N/A: unavailable' }
try {
    $cciProxy = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
    $cciHostEvidence.user_proxy_enabled = [bool]$cciProxy.ProxyEnable
    $cciHostEvidence.proxy_autoconfig_present = [bool]$cciProxy.AutoConfigURL
} catch { $cciHostEvidence.user_proxy_enabled = 'N/A' }
$cciHostEvidence.scanner_event_loop_responsiveness = 'N/A: external process counters cannot measure the scanner event loop'
$cciHostEvidence | ConvertTo-Json -Depth 6
