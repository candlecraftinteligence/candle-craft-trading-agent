# Runtime PC SQLite maintenance

The active Candle Craft database remains selected with `--database-path`. Its archive directory is a separate, explicit location. Keep the active database on a local Windows filesystem; do not host it on a network share, NAS mount, OneDrive/Dropbox/cloud-sync folder, or another concurrently synchronized location. A dedicated external local SSD partition is supported, but it must remain connected for the entire runtime session. An unexpected disconnect is a visible database failure and must be investigated before restarting.
The writable runtime profile verifies `foreign_keys=ON`, a 5,000 ms busy timeout, `journal_mode=WAL`, `synchronous=FULL`, and a 1,000-page WAL auto-checkpoint on every writable connection. When concurrent processes first open a database that is not yet in WAL mode, journal initialization retries only SQLite busy/locked failures in short slices until that same bounded timeout expires. SQLite refusal of WAL, timeout exhaustion, or any policy mismatch remains a visible storage error; no Telegram delivery or `UNCERTAIN` state is retried by this storage-open handling. Repository write transactions are committed or rolled back and closed deterministically, and Telegram network calls do not run while a repository write transaction is open.



Automated migration tests cover a representative v14 lifecycle/Telegram database through the v15 outcome additions and v16 outbox additions, including idempotence, rollback, and verified backup/restore migration on a copy. This evidence does not replace rehearsal against a verified copy of the actual Runtime database.

Before deploying schema v16 to the Runtime PC, create and verify a backup of the existing active database. Do not zip or copy only the live `.sqlite`/`.db` file while the scanner is running. In WAL mode, committed data can still be in `-wal`; a raw main-file copy can therefore be incomplete. The maintenance backup uses SQLite's online backup API and includes committed WAL data without stopping normal committed writers.

Set paths explicitly in PowerShell examples:

```powershell
$CCI_DATABASE = "C:\CandleCraft\runtime\candle_craft.db"
$CCI_ARCHIVE = "<CCI_EXTERNAL_DRIVE>:\CandleCraft\archives"
```

Read-only inspection does not create, initialize, migrate, checkpoint, repair, or change journal mode:

```powershell
.\.venv\Scripts\python.exe scripts\sqlite_maintenance.py inspect `
  --database-path $CCI_DATABASE `
  --archive-directory $CCI_ARCHIVE
```

Run the normal quick integrity and foreign-key checks:

```powershell
.\.venv\Scripts\python.exe scripts\sqlite_maintenance.py quick-check `
  --database-path $CCI_DATABASE
```

Run the potentially long full integrity check only during an explicit maintenance window:

```powershell
.\.venv\Scripts\python.exe scripts\sqlite_maintenance.py full-check `
  --database-path $CCI_DATABASE
```

Plan a backup without writing anything, then create the verified snapshot:

```powershell
.\.venv\Scripts\python.exe scripts\sqlite_maintenance.py backup `
  --database-path $CCI_DATABASE `
  --archive-directory $CCI_ARCHIVE `
  --label before-deploy-v16 `
  --dry-run

.\.venv\Scripts\python.exe scripts\sqlite_maintenance.py backup `
  --database-path $CCI_DATABASE `
  --archive-directory $CCI_ARCHIVE `
  --label before-deploy-v16
```

Every successful backup produces a uniquely named `.sqlite` snapshot and adjacent `.sqlite.manifest.json`. The manifest records UTC creation time, source and snapshot schema versions and sizes, SQLite integrity results, foreign-key results, core table counts, tool version, and the snapshot SHA-256. Existing snapshots are never overwritten. A failed or interrupted verification never promotes the `.partial` artifact and never changes the source.

Verify an existing snapshot independently:

```powershell
.\.venv\Scripts\python.exe scripts\sqlite_maintenance.py backup-verify `
  --snapshot-path "<CCI_EXTERNAL_DRIVE>:\CandleCraft\archives\<snapshot>.sqlite"
```

An explicit passive checkpoint is available for diagnosis. It is writable, bounded by the configured busy timeout, and reports busy or incomplete state. Backups do not depend on running it:

```powershell
.\.venv\Scripts\python.exe scripts\sqlite_maintenance.py checkpoint `
  --database-path $CCI_DATABASE `
  --mode PASSIVE
```

The inspection report includes main/WAL/SHM footprint, filesystem capacity, free-list usage, largest measurable tables, scan/lifecycle/outbox counts, scan timestamp range, and diagnostic-only thresholds. Growth and days-to-capacity are reported as `not enough data` until at least two matching verified manifests exist. Low-space, large-WAL, rapid-growth, backup-age, and integrity-age warnings never change scanner strategy or signal delivery and never delete data.

Use a Windows-readable local filesystem for the active database and archives. Do not select a Time Machine-only partition or another filesystem Windows cannot reliably mount and write. Confirm the external drive identity and free space before each archive operation, and never disconnect a drive that hosts the active database while Candle Craft is running.

No automatic deletion, pruning, retention, live-database rotation, or repair occurs. Historical scan, lifecycle, outbox, performance-memory, and research records are preserved.
