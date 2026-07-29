# Post-restart scanner funnel audit

`scripts/audit_post_restart_funnel.py` is an observational, read-only report for determining whether low scanner activity is associated with recorded market scarcity, a concentrated quality gate, lifecycle progression, Telegram delivery, or missing evidence. It does not change strategy qualification, scoring, quality gates, timeframes, lifecycle transitions, Telegram behaviour, symbol selection, market-data acquisition, or any database writes.

The audit opens the supplied SQLite file only through SQLite URI `mode=ro`, verifies `PRAGMA query_only=1`, and, for the live-mutable audit path, explicitly does not request `immutable=1`. It starts a bounded read transaction to pin a coherent snapshot; if that cannot be established safely, it fails rather than falling back to a writable connection. It never migrates, copies, checkpoints, vacuums, repairs, compacts, backs up, or changes the database. It verifies an existing indexed query plan before every high-volume source, selects only an explicit compact column allowlist, fetches in batches, and caps/normalizes required JSON evidence. If a metric would require an unsafe table scan or incomplete capped evidence, it is reported as `NOT_RECORDED`, `NOT_VERIFIABLE`, or `DATA_INSUFFICIENT` instead.

Run it only against the Runtime database from the Runtime PC after the requested checkpoint. Do not copy the Runtime database to Dev, and do not use the full Runtime M: drive for output.

```powershell
.\.venv\Scripts\python.exe scripts\audit_post_restart_funnel.py `
  --database-path scan_runs\main_live_runtime.sqlite `
  --window-start-utc 2026-07-29T08:40:54Z `
  --window-end-utc 2026-08-01T08:40:54Z `
  --expected-watch-interval-sec 300 `
  --output-dir S:\CCI_SCANS\phase1_funnel_audits `
  --report-label snapshot-b-72h
```

Snapshot C uses the same arguments except for its exact seven-day endpoint and label:

```powershell
.\.venv\Scripts\python.exe scripts\audit_post_restart_funnel.py `
  --database-path scan_runs\main_live_runtime.sqlite `
  --window-start-utc 2026-07-29T08:40:54Z `
  --window-end-utc 2026-08-05T08:40:54Z `
  --expected-watch-interval-sec 300 `
  --output-dir S:\CCI_SCANS\phase1_funnel_audits `
  --report-label snapshot-c-7d
```

The command produces one deterministic-order text report and one JSON report. It refuses to overwrite either output file.

## Identity and checkpoints

The audit has two deliberate identity levels:

- A stable setup is `setup_lifecycle_records.lifecycle_id` where it is persisted.
- A scanned evaluation is `scan_runs.run_id + symbol_results.symbol`. It is never presented as a unique setup.

`setup_candidates.id` is a persisted candidate-row event, not a stable cross-run identity. Repeated WATCH-loop events remain in event counts; lifecycle unique-setup counts deduplicate only by `lifecycle_id`.

Snapshot A begins at the scanner restart boundary, `2026-07-29T08:40:54Z`, and must remain `DATA_INSUFFICIENT`. Compare it with Snapshot B after at least 72 hours, then Snapshot C after seven days, keeping the start time, 300-second interval, database source, and audit options unchanged except for end time and report label.

## Evidence labels

`NOT_RECORDED` means the relevant table, field, or explicit event was not persisted. `NOT_VERIFIABLE` means related data exists but cannot establish the metric safely, for example no stable identity links a candidate row to a lifecycle. `DATA_INSUFFICIENT` is a verdict: the requested window, coverage, or bounded evidence does not support a strong diagnosis.

The report does not infer Telegram delivery from a created signal or eligibility. It requires an explicit `telegram_status='sent'` and `sent_at`. It does not call an open setup a loss. It does not claim that a `target_inside_chop` setup would have won without persisted, attributable outcome evidence.
