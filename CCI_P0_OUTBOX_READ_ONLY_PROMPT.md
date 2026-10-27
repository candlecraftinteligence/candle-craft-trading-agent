# Next read-only Runtime check

Paste this into PowerShell on Runtime. Attach the generated Desktop text file. This reads the existing scan manifest only; no database, message delivery or process control.

```powershell
$cciOutput = Join-Path ([Environment]::GetFolderPath('Desktop')) `
    ("CCI_Outbox_Diagnostics_{0}.txt" -f (Get-Date -Format 'yyyyMMdd_HHmmss'))

& {
    Set-Location 'C:\Users\aspir\Desktop\Candle Craft Inteligence'
    function Get-CciValue($v) {
        if ($null -eq $v) { 'N/A' } else { $v }
    }
    $cciManifest = '.\scan_runs\scan_run_manifest.jsonl'
    if (!(Test-Path -LiteralPath $cciManifest -PathType Leaf)) {
        throw 'Scanner manifest is absent; no database will be opened.'
    }
    Get-Content -LiteralPath $cciManifest -Tail 3 | ForEach-Object {
        $r = $_ | ConvertFrom-Json
        $w = $r.watch_supervisor
        $p = $w.telegram_provenance
        if ($null -eq $p) { $p = $r.telegram_provenance }
        [pscustomobject]@{
            timestamp = Get-CciValue $r.timestamp
            telegram_phase = Get-CciValue $w.phase_statuses.telegram_outbox
            outbox_counts = Get-CciValue $w.telegram_outbox_status
            provenance_status = Get-CciValue $p.status
            coverage_complete = Get-CciValue $p.coverage_complete
            inspected = Get-CciValue $p.inspected
            inspection_limit = Get-CciValue $p.inspection_limit
            unresolved = Get-CciValue $p.unresolved
            reason_counts = Get-CciValue $p.by_reason
            provenance_error_type = Get-CciValue $p.error
            provenance_unavailable_reason = Get-CciValue $p.reason
            retry_backoff_eligible = Get-CciValue $w.retry_backoff_eligible
            retry_backoff_reason = Get-CciValue $w.retry_backoff_reason
            missed_interval_count = Get-CciValue $w.missed_interval_count
        }
    } | ConvertTo-Json -Depth 6
} *>&1 | Out-File -LiteralPath $cciOutput -Encoding utf8 -Width 4096

Write-Host "Saved: $cciOutput"
```
