# Post-restart scanner funnel audit

`scripts/audit_post_restart_funnel.py` is an observational, read-only report for determining whether low scanner activity is associated with recorded market scarcity, a concentrated quality gate, lifecycle progression, Telegram delivery, or missing evidence. It does not change strategy qualification, scoring, quality gates, timeframes, lifecycle transitions, Telegram behaviour, symbol selection, market-data acquisition, or any database writes.

The audit supports only `--source-mode quiescent-immutable`. Before it connects, it requires the main database file and requires both SQLite `-wal` and `-shm` sidecars to be absent. It captures the source size, `mtime_ns`, and `ctime_ns`; opens SQLite only through URI `mode=ro&immutable=1`; verifies `PRAGMA query_only=1`; and never falls back to a writable connection. After closing, it rechecks the sidecars and metadata. A sidecar appearance or source change is a **NO-GO** and no report is written.

The scanner must be stopped and its `run_scan.py` process verified absent before the audit. The Telegram listener may remain running. This utility never stops or restarts Runtime processes; after a successful report, restart the scanner manually with its unchanged approved command. If quiescence cannot be established, do not audit the active writer.

The audit never migrates, copies, checkpoints, vacuums, repairs, compacts, backs up, or changes the database. It verifies an existing indexed query plan before every high-volume source, selects only an explicit compact column allowlist, fetches in batches, and applies a hard evidence limit. Oversized optional JSON is not selected as a partial fragment or parsed; only JSON-dependent metrics become `NOT_VERIFIABLE`. If a metric would require an unsafe table scan, it is reported as `NOT_RECORDED`, `NOT_VERIFIABLE`, or `DATA_INSUFFICIENT` instead.

Run it only against the Runtime database from the Runtime PC after the requested checkpoint. Do not copy the Runtime database to Dev, and do not use the full Runtime M: drive for output.

## PR #78 deployment-relative observation template

PR #78 can be validated only with evidence collected after its separately authorized Runtime deployment and restart. During that deployment, the operator must record the exact new scanner restart timestamp as **T0** in UTC together with the deployed commit SHA and unchanged scanner command. Never infer, backfill, or invent T0 from this document, a Git timestamp, or a prior restart.

The template below is intentionally non-executable until <RECORDED_T0_UTC_DURING_DEPLOYMENT> is replaced with the operator-recorded value. Snapshot B ends exactly at T0 + 72 hours. Snapshot C ends exactly at T0 + seven days. Both retain T0 as the inclusive window start.

~~~powershell
$cciRecordedT0UtcText = "<RECORDED_T0_UTC_DURING_DEPLOYMENT>"
if ($cciRecordedT0UtcText.StartsWith("<")) {
    throw "Record the actual PR #78 Runtime restart timestamp T0 during deployment; do not invent it."
}
$cciT0Utc = [DateTimeOffset]::Parse($cciRecordedT0UtcText).ToUniversalTime()
$cciWindowStartUtc = $cciT0Utc.ToString("yyyy-MM-ddTHH:mm:ss'Z'")
$cciSnapshotBEndUtc = $cciT0Utc.AddHours(72).ToString("yyyy-MM-ddTHH:mm:ss'Z'")
$cciSnapshotCEndUtc = $cciT0Utc.AddDays(7).ToString("yyyy-MM-ddTHH:mm:ss'Z'")
$cciAuditOutputDir = "S:\CCI_SCANS\phase1_funnel_audits"

$cciCommonAuditArgs = @(
    "--database-path", "scan_runs\main_live_runtime.sqlite",
    "--source-mode", "quiescent-immutable",
    "--window-start-utc", $cciWindowStartUtc,
    "--expected-watch-interval-sec", "300",
    "--output-dir", $cciAuditOutputDir
)

# Run only after T0 + 72 hours and after proving scanner quiescence.
& .\.venv\Scripts\python.exe scripts\audit_post_restart_funnel.py @cciCommonAuditArgs "--window-end-utc" $cciSnapshotBEndUtc "--report-label" "pr78-snapshot-b-t0-plus-72h"

# Run only after T0 + seven days and after proving scanner quiescence.
& .\.venv\Scripts\python.exe scripts\audit_post_restart_funnel.py @cciCommonAuditArgs "--window-end-utc" $cciSnapshotCEndUtc "--report-label" "pr78-snapshot-c-t0-plus-7d"
~~~

Record the resolved T0, B, and C timestamps in the operational evidence handoff. Do not substitute “now” for either endpoint, shorten either window, or reuse evidence whose window began before the PR #78 restart.

## Legacy July 29 examples — cannot validate PR #78

The following commands are retained only as historical examples for the July 29 restart. Their 2026-07-29T08:40:54Z boundary predates PR #78 and **cannot validate PR #78**, its RSS chronology, or its post-deployment behavior. Do not relabel their outputs as PR #78 Snapshot B or C.

~~~powershell
.\.venv\Scripts\python.exe scripts\audit_post_restart_funnel.py --database-path scan_runs\main_live_runtime.sqlite --source-mode quiescent-immutable --window-start-utc 2026-07-29T08:40:54Z --window-end-utc 2026-08-01T08:40:54Z --expected-watch-interval-sec 300 --output-dir S:\CCI_SCANS\phase1_funnel_audits --report-label legacy-july29-snapshot-b-72h
~~~

~~~powershell
.\.venv\Scripts\python.exe scripts\audit_post_restart_funnel.py --database-path scan_runs\main_live_runtime.sqlite --source-mode quiescent-immutable --window-start-utc 2026-07-29T08:40:54Z --window-end-utc 2026-08-05T08:40:54Z --expected-watch-interval-sec 300 --output-dir S:\CCI_SCANS\phase1_funnel_audits --report-label legacy-july29-snapshot-c-7d
~~~

The command produces one deterministic-order text report and one JSON report. It refuses to overwrite either output file.

## Identity and checkpoints

The audit has two deliberate identity levels:

- A stable setup is `setup_lifecycle_records.lifecycle_id` where it is persisted.
- A scanned evaluation is `scan_runs.run_id + symbol_results.symbol`. It is never presented as a unique setup.

`setup_candidates.id` is a persisted candidate-row event, not a stable cross-run identity. Repeated WATCH-loop events remain in event counts; lifecycle unique-setup counts deduplicate only by `lifecycle_id`.

For PR #78, the observation boundary is the new Runtime restart timestamp T0 recorded during its deployment. Snapshot B uses [T0, T0 + 72 hours) and Snapshot C uses [T0, T0 + seven days), keeping the recorded T0, 300-second interval, database source, and audit options unchanged except for end time and report label. The July 29 boundary is legacy evidence only and is not PR #78 Snapshot A.

## RSS sampling boundary

The RSS instrumentation boundary was reviewed against ScannerRunner.run; it was not widened through risky restructuring.

- The start sample occurs after ScannerRunConfig validation and decision-timestamp resolution, immediately before exchange-client creation and scanner work.
- Each per-symbol sample occurs after the symbol result, error, or timeout handling, progress emission, and the optional after_symbol callback. When scripts/run_scan.py --save-run supplies that callback, its partial JSON checkpoint is inside the measured boundary.
- The final sample occurs after owned exchange-client closure and market-regime computation/application, immediately before ScannerRunResult construction.

Included work is exchange-client creation/owned-client closure; market-regime input fetch and final application; per-symbol market-data and cache access; analysis, qualification gates, scoring, and dry-run agent work; progress and after_symbol callbacks; and timeout, scan-error, and not-run result construction completed before the final sample.

Excluded work is argument/config parsing and decision-timestamp resolution before the first sample; ScannerRunResult construction and runtime-stat assembly after the final sample; resume-result combination; replay, edge, and performance-memory enrichment; lifecycle application; Telegram delivery; symbol-health updates; ranking and portfolio selection; final JSON/report/export/database persistence; manifest/admin routing; dashboard formatting; watch-loop sleep; supervisor/listener overhead; and all other process work outside ScannerRunner.run.

This boundary measures a bounded scanner-run segment, not whole-process lifetime memory. Compare only like-for-like scanner workloads and review the reported coverage and failures before interpreting a trend.

## Chronological RSS evidence and interpretation

Only a process-memory record that claims Verified and has a parsed UTC scan timestamp, complete RSS start/end/peak/delta values, internally consistent delta and peak values, and complete zero-failure sample counts enters chronological evidence. A malformed Verified claim is counted, excluded from chronological calculations, and makes the aggregate status Unverified.

Fully verified observations are sorted by parsed UTC scan timestamp and then run_id. The report includes the first and last verified scan timestamps and run IDs, first verified RSS start, last verified RSS end, net RSS change across those boundaries, the highest observed peak with its timestamp/run ID, and the duration covered by verified evidence.

The early/late comparison uses a documented robust statistic: median rss_end_bytes in two disjoint bounded chronological buckets. Each bucket contains ceil(n / 4) fully verified observations—the earliest 25% and latest 25%—and middle observations are excluded. At least two fully verified observations are required. The report also includes memory-block coverage, fully verified coverage, attempted/succeeded/failed sample totals, cycles with failures, sampling-failure rate, and bounded malformed-field counts.

Interpret status explicitly:

- no process-memory block is NOT_RECORDED;
- an explicitly recorded unavailable measurement is N/A;
- partial sampling, missing blocks, unknown status, or malformed claimed-verified evidence is Unverified;
- fewer than two fully verified chronological records is DATA_INSUFFICIENT for early/late comparison.

The output is observational only. A positive, flat, or negative net/bucket difference does not automatically establish a memory leak, warm-up, or stability. Human review must compare Snapshot B and C, sampling coverage/failures, workload, and the exact sampling boundary.

## Evidence labels

`NOT_RECORDED` means the relevant table, field, or explicit event was not persisted. `NOT_VERIFIABLE` means related data exists but cannot establish the metric safely, for example no stable identity links a candidate row to a lifecycle. `DATA_INSUFFICIENT` is a verdict: the requested window, coverage, or bounded evidence does not support a strong diagnosis.

The report does not infer Telegram delivery from a created signal or eligibility. It requires an explicit `telegram_status='sent'` and `sent_at`. It does not call an open setup a loss. It does not claim that a `target_inside_chop` setup would have won without persisted, attributable outcome evidence.
