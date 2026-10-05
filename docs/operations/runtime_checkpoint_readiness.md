# Runtime checkpoint readiness

Checkpoint name: **FORENSIC_FOUNDATION_RUNTIME_CHECKPOINT**.

This document is the operator runbook for the first controlled Runtime checkpoint. Completing the readiness PR does **not** mean `GO_FOR_RUNTIME_DEPLOYMENT`. Until Runtime evidence exists and Adam separately authorizes a concrete operational package, the operational status is:

**`RUNTIME_CHECKPOINT_NOT_YET_AUTHORIZED`**

This phase prepares bounded read-only preflight diagnostics, synthetic compatibility proofs, and this procedure. It does not execute the checkpoint. DEV must not open, query, copy, migrate, write, inspect, or mount the live Runtime database.

## 1. What this checkpoint is

Historical functional cutoff: the forensic foundation through PR #120. That history is not approval of later `main`.

Accepted foundation for the next Runtime release packet, as merged on `main`:

| Phase | Merge | Accepted head | Role |
| --- | --- | --- | --- |
| P5A | `44777a3b4f53919875b66940f231ea542ca38bd4` | recorded in the P5A handoff | owner monitoring and the additive plan-state index |
| F03 | `b7c6422e3d1e2596115b7cd9cf5b1caea15072ef` | `438624dc5f291d271b4c85be5f83262ea0f40d85` | prospective research population isolation |
| F04 | `44efa90daff8476c480eb21ed8ba43654ee8e5de` | `54bd2385dcf3b6265b3cdff8e68ecb484f9cc3a8` | durable source replay; capture stays off for the first rollout |
| F05 | `011b9e2fc8a5c93578acceafe5505774537866df` | `9b79fb4773d5c6cb73dec275336a7ed0dc62cfea` | strict discovery failure handling |

Application schema for this release packet is **26**. Schema 26 does not by itself install `ix_lifecycle_records_epoch_locked_plan_state`. `open_operational_database` does not create that index. An existing schema-26 file needs a controlled `migrate_existing_database` (or another explicit `initialize_database`) on the restored copy. `CREATE INDEX IF NOT EXISTS` does not repair a same-named index with a different definition and does not rewrite historical rows.

Expected index definition, inspected from metadata. The locked-plan query uses `INDEXED BY` this name, so another name is not a substitute:

```sql
CREATE INDEX IF NOT EXISTS ix_lifecycle_records_epoch_locked_plan_state
ON setup_lifecycle_records(runtime_epoch_id, current_state, lifecycle_id)
WHERE plan_version_id IS NOT NULL;
```

Supplied source metadata and the target rehearsal must both carry that exact name, table `setup_lifecycle_records`, column order `runtime_epoch_id`, `current_state`, `lifecycle_id`, `unique` false, and `partial` true. The predicate normalizes to `plan_version_id is not null`. A missing or mistyped name, table, column list, uniqueness, partial flag, or predicate is incomplete. A well-typed wrong name, table, column order, `unique` true, `partial` false, or different predicate is adverse. A name-only inventory entry does not prove the definition. An explicit absence on an older source, with a matching target rehearsal, stays a preflight finding. That absence record is the collector shape: expected index name, `status` `absent`, null table, uniqueness, partial flag, and predicate, and `columns` `[]`. A supplied list, number, or string is not absence, and neither is an absence record whose columns or flags have the wrong type. An omitted observation is not the same thing as that record. An index inventory cannot erase a supplied type error.

Source epoch and lineage entries use a strict boolean `present`. `true` requires the expected column list. `false` is the permitted table-absence finding. A missing, null, string, number, or container marker is incomplete and is not coerced to false. A well-typed column mismatch on another table stays adverse.

Live-source compatibility is recorded separately from target compatibility on the restored copy. An older source schema, or a schema-26 source that has not received the additive index, can be an expected preflight finding. The target is not ready until the actual-schema copy rehearsal proves migration to schema 26 and this index. Final cutover must verify both again before consumers start.

Epoch and operational lineage tables required on the target are `runtime_epochs`, `runtime_epoch_control`, `runtime_operational_runs`, and `runtime_operational_origins`. Collect and assess do not create an epoch, install an index, or backfill lineage.

Keep these identities distinct:

| Identity | Meaning |
| --- | --- |
| Historical functional anchor | `2bac6b4eda271bc69f2c34f42d6b7592e37fa8cc`. Ancestor of the release line. Not approval of every later `main`. |
| Collector checkout SHA | Git SHA of the diagnostic process. Not the running application. |
| Running application identity | Process evidence only. Unavailable unless supplied independently. |
| Proposed release SHA | Operator input for the exact candidate. A 40-hex SHA is not proof of review, CI, or the code that is executing. |

A reported successful deployment does not make missing packet evidence complete. Do not deploy a moving `main`, a draft-PR head, or a synthetic merge SHA. F06 changes need their own review, merge, and release pin. This runbook still does not grant `GO_FOR_RUNTIME_DEPLOYMENT`.

Trigger (all required; none are satisfied by this DEV assignment):

1. Readiness PR merged by Adam.
2. Exact release SHA and its successful push CI verified.
3. Runtime baseline and capacity audit accepted.
4. All consumer paths accounted for.
5. Actual-schema copy rehearsal and restore proof accepted.
6. Cutover, recovery, and observation budgets fixed.
7. Adam separately authorizes that concrete operational package.

If a gate fails, the next work is the smallest named blocker remediation. Do not add unrelated research layers to avoid the checkpoint.

## 2. Safety invariants

Runtime invariants:

```
LOCAL_MANUAL_MODE=true
ORDER_EXECUTION_ENABLED=false
```

DEV and isolated rehearsals:

```
TELEGRAM_DRY_RUN=true
TELEGRAM_SIGNALS_ENABLED=false
LOCAL_MANUAL_MODE=true
ORDER_EXECUTION_ENABLED=false
```

Public Telegram delivery is restored only as explicitly specified in Adam's approved Runtime procedure. There is never an overlapping old listener or an incompatible reader.

Normal new `symbol_refs_v1` encoding is allowed only after every relevant reader/writer is decoder-compatible. `SCAN_RAW_PAYLOAD_INLINE_ONLY=true` stops future reference encoding on a decoder-capable release. It does not convert existing reference rows or make an old reader compatible. Do not downgrade `user_version` or rewrite history.

## 3. Path boundary

| Role | Path | Status |
| --- | --- | --- |
| DEV checkout | `C:\CandleCraftDev` | This assignment |
| Runtime code (reported) | `C:\Users\aspir\Desktop\Candle Craft Inteligence` | Must be re-verified from process evidence |
| Live DB (reported) | `S:\CandleCraftRuntime\scan_runs\main_live_runtime.sqlite` | Must be re-verified from process/configuration evidence |

`S:` is a drive letter, not a volume identity. Map it on Runtime with `Get-Volume` / `Get-Partition` / `Get-Disk` and the collector's volume block. A drive letter does not establish local storage, durability, or free capacity on another destination. Collector `local_device` is True only for `DRIVE_FIXED`, `DRIVE_REMOVABLE`, or `DRIVE_RAMDISK`; `DRIVE_REMOTE` is False; `DRIVE_UNKNOWN` / `DRIVE_NO_ROOT_DIR` remain unavailable. Not-remote is not proven local.

The listener has its own `--database-path`, `--manifest-path`, `--state-path`, and `--audit-path`. Do not assume its defaults point at the scanner database.

Bring the diagnostic to Runtime as a **pinned, isolated tool checkout**. Do not update the code directory used by running old processes, and do not load their `.env` merely to inspect SQLite.

## 4. Diagnostic budgets

Verified entry point: `scripts/runtime_checkpoint_readiness.py`.

| Budget | Default | Meaning |
| --- | --- | --- |
| `--busy-timeout-ms` | 250 | SQLite busy wait on the preflight connection |
| `--query-deadline-ms` | 2000 | Progress-handler abort for metadata/sample queries |
| `--recent-run-limit` | 5 | Maximum optional `scan_runs` sample rows |
| Progress opcode interval | 1000 | SQLite VM opcodes between deadline checks |

Collection is one-shot. Repeated observations are operator steps, not a newly installed daemon.

Default SQLite queries are schema/version/column/index metadata and cheap page metadata (`page_count`, `page_size`, `freelist_count`). Optional `--include-recent-runs` requires `ix_scan_runs_timestamp`, a proven `EXPLAIN QUERY PLAN`, a fixed `LIMIT`, and the query deadline. `LIMIT` alone does not bound an unindexed scan.

The collector never runs full table `COUNT(*)`, `dbstat`, JSON-history scans, `integrity_check` / `quick_check`, recursive archive inventory, checksumming of the live DB, checkpoint, `VACUUM`, journal-mode changes, cleanup, pruning, schema modification, or `open_initialized_database`.

`inspect_database` (`scripts/sqlite_maintenance.py inspect`) is **not** this preflight. Its historical read-only opener (`assume_immutable_when_sidecars_absent=True`) and the backup helper are not automatically safe for a changing WAL database with absent sidecars. Inspect still performs table counts, `dbstat`, timestamp MIN/MAX, and outbox aggregations. Do not use either helper as the bounded live collector.

## 5. Collector identity vs deployed identity

The report's `collector_identity` is the diagnostic checkout (tool version, module SHA-256, collector git SHA/dirty, collector Python/SQLite). `observed_deployed_application` is **unavailable** unless the operator supplies independently verified process evidence.

Running the diagnostic from the new checkout must never label an old running process as the new release. Do not infer loaded code from a git checkout whose files were changed after process start.

## 6. Observational preflight (do not cut over)

Unresolved values below are **blockers**. Do not invent paths or flags.

```powershell
$CollectorRoot = "<UNRESOLVED: pinned isolated collector checkout>"
$Python = Join-Path $CollectorRoot ".venv\Scripts\python.exe"
$SourceDb = "<UNRESOLVED: verified Runtime DB path from process/config evidence>"
$ReportDir = "<UNRESOLVED: explicit new directory; prefer not filling the live DB volume>"
if (-not (Test-Path $Python)) { throw "Collector interpreter missing: $Python" }
if (-not (Test-Path $SourceDb)) { throw "Source database missing: $SourceDb" }
if (-not (Test-Path $ReportDir)) { throw "Report directory missing: $ReportDir" }

$Stamp = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$FsOut = Join-Path $ReportDir "cci-runtime-checkpoint-fs-$Stamp.json"
$SqlOut = Join-Path $ReportDir "cci-runtime-checkpoint-sqlite-$Stamp.json"
if (Test-Path $FsOut) { throw "Refusing to overwrite $FsOut" }
if (Test-Path $SqlOut) { throw "Refusing to overwrite $SqlOut" }

Set-Location $CollectorRoot
& $Python scripts\runtime_checkpoint_readiness.py collect `
  --source-path $SourceDb `
  --output-path $FsOut `
  --filesystem-only

& $Python scripts\runtime_checkpoint_readiness.py collect `
  --source-path $SourceDb `
  --output-path $SqlOut `
  --include-recent-runs
```

If WAL exists and SHM is missing, SQLite fields are `unavailable` (`wal_without_shm`). If the main file is WAL-format but both sidecars are absent, SQLite fields are `unavailable` (`wal_header_without_sidecars`) because opening would create sidecars or require `immutable=1`. Retain filesystem evidence. Never create/repair sidecars, switch to writable access, or fall back to `immutable=1`. SQLite's immutable mode disables locking/change detection and is unsuitable for a changing file.

Assess a separately assembled evidence packet (collector JSON plus operator-declared process/capacity/restore facts):

```powershell
$Evidence = "<UNRESOLVED: assembled evidence JSON>"
$AssessOut = Join-Path $ReportDir "cci-runtime-checkpoint-assess-$Stamp.json"
if (Test-Path $AssessOut) { throw "Refusing to overwrite $AssessOut" }
& $Python scripts\runtime_checkpoint_readiness.py assess `
  --evidence-path $Evidence `
  --output-path $AssessOut
```

`assess` never executes remediation and never emits `go_for_runtime_deployment: true`. `PACKET_REVIEWABLE_NOT_AUTHORIZED` means the packet is internally complete, not that Runtime may be deployed.

### 6.1 24-hour ordinary-workload baseline

Collect at least a full 24-hour ordinary-workload baseline before cutover, with a starting sample, intermediate samples, and an ending sample. Existing trustworthy, contemporaneous local records may satisfy this window. Elapsed time alone is not a deploy signal.

Measure, per sample:

- Combined allocated disk (main + WAL + SHM) and volume free space
- WAL peaks vs later recycled WAL (filesystem space is not recovered merely because SQLite reused WAL)
- Internal free pages (`freelist_count`) vs filesystem free bytes
- Scan duration / configured cadence / faults
- Workload / universe
- Backup/archive allocation on each affected volume

Historical ~81 GiB and older audit means are not current measurements or p95. The original five-minute interval is not an assumed current setting.

### 6.2 Capacity gate

Capacity is accounted **per affected physical volume**, not per logical role.

Do not infer physical topology from the names `db`, `backup`, and `restore`. Those are roles. Two off-DB roles may occupy the same physical volume. Role-by-role comparison against the same free-space pool is not sufficient.

Required topology evidence (no silent defaults):

- `backup_shares_db_volume` (boolean; required)
- `restore_shares_db_volume` (boolean; required)
- `backup_restore_share_volume` (boolean; required when both roles are off the DB volume)

Missing topology is `INCOMPLETE_PREREQUISITES`. Absent share flags are never assumed to mean "shares the DB volume."

Optional `db_volume_label` / `backup_volume_label` / `restore_volume_label` are operator-declared associations for grouping obligations. They are not proof of hardware identity. Collector-observed volume facts remain separate evidence and do not prove backup/restore topology.

Contradictory topology (role claims to share the DB volume while also declaring a distinct external volume identity or measurements; labels that disagree with the share flags; `backup_restore_share_volume=true` in mixed topology; unequal measurements for a claimed shared volume) is `INCOMPLETE_PREREQUISITES`, never capacity-sufficient.

After topology is resolved, aggregate **all concurrent obligations that occupy the same declared physical volume** before comparing with that volume's free space.

DB volume occupancy:

- `migration_temp_bytes` (kept on the DB volume; this contract does not name another temp volume)
- `additional_peak_WAL_and_log_bytes`
- `growth_budget_bytes`
- `new_backup_bytes` only if `backup_shares_db_volume=true`
- `concurrent_restore_or_candidate_bytes` only if `restore_shares_db_volume=true`

External backup occupancy: `new_backup_bytes` when `backup_shares_db_volume=false`.

External restore/candidate occupancy: `concurrent_restore_or_candidate_bytes` when `restore_shares_db_volume=false`.

If backup and restore both occupy one declared external volume X:

`required_X = new_backup_bytes + concurrent_restore_or_candidate_bytes + reserve_X`

Do not compare that volume's free space independently once per role.

Every affected physical volume requires `volume_total_bytes` and `volume_free_bytes` (DB: `volume_total_bytes` / `volume_free_bytes`; distinct backup: `backup_volume_total_bytes` / `backup_volume_free_bytes`; distinct restore: `restore_volume_total_bytes` / `restore_volume_free_bytes`; shared external volume: either role's pair, and both pairs if present must match).

Every affected physical volume has a planning reserve. Default floor: the greater of **10 GiB** and **10% of that physical volume's capacity**. A reviewed stronger reserve may only increase the requirement (`max(floor, declared reserve, stronger_reserve_requirement_bytes)` on that volume). A boolean flag cannot waive a floor downward. Free space equal to occupancy without reserve is not sufficient.

Existing allocations are already reflected in measured free space; do not charge them twice. Include all copies that coexist, target/temp volumes, future backups during observation, and recovery headroom. Do not assume compression, half-size storage, or immediate space reclamation.

The supplied `growth_budget_bytes` must trace to a timestamped ordinary-workload baseline: workload description, cadence description, observation start and end, and a stated planning horizon. Require at least 24 hours of representative baseline and at least two comparable samples inside that window. Existing reliable contemporaneous records may satisfy that window. Require at least ten days of growth runway for this rollout (the seven-day observation trial plus three days of response runway is the minimum horizon, not a shorter substitute).

`workload_baseline.samples` entries need `observed_at_utc`, non-negative integer `combined_allocated_bytes`, `comparable` true, and `quality` `"representative"`. `workload_baseline.growth_derivation` records the arithmetic in `bytes`: `observation_count`, `earliest_allocated_bytes`, `latest_allocated_bytes`, `observed_delta_bytes`, `observation_seconds`, `horizon_seconds` (`planning_horizon_days` times 86400), `projected_bytes`, non-negative `allowance_bytes`, and `derived_budget_bytes`. Evidence class is `declared_assertion` or `referenced_result`. A referenced result also needs `evidence_reference` and the same numbers. A path alone is not a derivation. `tool_attested` stays false for both classes.

Projection uses integer ceiling and does not invent a minimum growth rate:

`projected_bytes = ceil(observed_delta_bytes * horizon_seconds / observation_seconds)`

`derived_budget_bytes = projected_bytes + allowance_bytes`

The ceiling is `(delta * horizon_seconds + observation_seconds - 1) // observation_seconds` when delta, observation, and horizon are positive and allowance is non-negative. `growth_budget_bytes` must be greater than or equal to that derived budget. A smaller positive budget is adverse (`growth_budget_below_derived_requirement`). Well-typed arithmetic that disagrees with the samples or the horizon is adverse (`contradictory_growth_derivation`). Missing samples, a missing derivation, or non-integer fields stay incomplete. One observed byte is a valid delta: over 86400 seconds and a 10-day horizon it projects to 10 bytes, so a budget of 10 meets the growth check and a budget of 1 does not.

An idle, failed, missing, incomparable, flat, or shrinking sample cannot establish zero growth or infinite runway. A positive invented number cannot establish measured capacity. Capacity for the release decision is measured on Runtime. Historical ~81 GiB observations and DEV tests are not that measurement. Malformed nested values (a list where a string is required, an integer where a column list is required, an object where preservation status is required) stay field-level incomplete or adverse. They do not raise out of assess, and an adverse fact elsewhere still wins.

Account for the main database, WAL, SHM, logs, every coexisting backup, restore, candidate copy, and archive, migration temporary space, and the declared observation/response growth budget. Already occupied space is in measured free space; do not charge it twice. Incremental concurrent allocations are the new bytes. SQLite freelist pages are not filesystem free space and are not subtracted. Do not subtract cleanup that has not been performed. Do not assume a compression ratio. This repair does not add pruning, `VACUUM`, age-only retention, or a new writer.

Unknown critical terms, missing/contradictory topology, or insufficient free capacity on any required physical volume: `STOP_FOR_CAPACITY_EVIDENCE` (or a named storage prerequisite). New durable writers require their own incremental write/WAL/backup model and a new readiness decision.

Retention for this checkpoint: preserve current evidence and recovery artifacts. No automatic deletion or pruning. The finite observation period must fit without unimplemented retention. Protect active-plan dependencies, rejected observations needed for selection analysis, and admission/tracking evidence from future age-only deletion.

## 7. Isolated-copy rehearsal (Runtime only; not this DEV assignment)

Use a proven quiescent maintenance procedure, or a separately reviewed snapshot method that is compatible with the live journal mode. A raw copy of only the changing main SQLite file is not a consistent WAL backup. Do not delete `-wal` or `-shm` sidecars to make a copy look quiescent. Do not open a changing source with `immutable=1`. Do not restore an older outbox after new public sends.

`create_verified_backup` opens the source with `open_read_only_database` **historic default** (`assume_immutable_when_sidecars_absent=True`). That historical opener is not automatically safe for a changing WAL database with absent sidecars, and it is not certified for a mutable live source merely by name. Constrain backup to a separately approved, proven-quiescent maintenance window (writers stopped; WAL/SHM absence verified) or STOP for a focused backup prerequisite. This readiness repair does not add a new backup subsystem.

Verified backup entry point after quiescence:

```powershell
$Archive = "<UNRESOLVED: verified archive directory on a suitable volume>"
$QuiescentDb = "<UNRESOLVED: same verified DB path after process/DB-handle quiescence>"
& $Python scripts\sqlite_maintenance.py backup `
  --database-path $QuiescentDb `
  --archive-directory $Archive `
  --label rehearsal-pre-schema-26 `
  --dry-run

& $Python scripts\sqlite_maintenance.py backup `
  --database-path $QuiescentDb `
  --archive-directory $Archive `
  --label rehearsal-pre-schema-26
```

Every successful backup produces a uniquely named `.sqlite` snapshot and adjacent `.sqlite.manifest.json`. Existing snapshots are never overwritten.

```powershell
$Snapshot = "<UNRESOLVED: snapshot path from backup report>"
& $Python scripts\sqlite_maintenance.py backup-verify --snapshot-path $Snapshot
```

Restore to a **separate** working path. Rehearse the actual source schema on that isolated copy through schema 26, including the additive plan-state index, with credentials cleared, network egress blocked, Telegram suppressed, capture disabled, and orders disabled. File names and `restore_tested=true` are not measured restore evidence. Record source identity, verified snapshot identity, restored-copy path, and observation time separately. Keep declared assertions distinguishable from referenced test results; this tool is not a remote attestation system.

Require positive restore-integrity evidence. Missing or unknown integrity does not pass. A failed integrity check is adverse.

On the restored copy, record preservation of active plans, plan-version binding, progress and cursors, public outbox SENT/UNCERTAIN state, external listener state, and logical scan-payload compatibility. Record measured migration duration, restore duration, and peak allocation, plus the planned downtime budget and recovery budget. Do not acquire those facts with a full live table scan. Large integrity checks belong on the isolated copy, not on the live DB.

The target cannot be declared ready until that rehearsal shows schema 26 and `ix_lifecycle_records_epoch_locked_plan_state` with the expected key order and `WHERE plan_version_id IS NOT NULL`. Final cutover verifies the same facts again before consumers start. Collect and assess do not migrate the copy.

Include file-backed listener offsets, command state, manifests, `latest_scan.json`, `performance_memory.json`, and other mutable external state found during inspection.

Before cutover, set allowed downtime and recovery-time budget from the measured rehearsal. Unspecified or exceeded budgets block cutover. A successful backup without a tested restore is insufficient.

## 8. Consumer inventory (source-scoped; Runtime ownership UNKNOWN)

Every scanner, listener, lifecycle, analytics, export, audit, and scheduled reader/writer that can touch the active DB or scan payloads must be accounted for before cutover. Unknown external readers are unresolved rollout gates.

Declared from current source (also emitted on every collector report):

| ID | Role | DB / state path | Decoder / notes |
| --- | --- | --- | --- |
| scanner_watch_store | writer | `--database-path` (default `scan_runs/candle_craft.db` relative to CWD) | schema-26 encoder; ordinary default may write `symbol_refs_v1` |
| lifecycle_repository | writer | same scanner DB | lifecycle tables |
| telegram_lifecycle_outbox | writer | same scanner DB | SENT/UNCERTAIN outbox |
| symbol_health | writer | same scanner DB | health tables |
| telegram_ui_listener | reader | **its own** `--database-path`; independent manifest/state/audit | historic read-only default |
| telegram_admin_commands | reader | listener DB | historic read-only default |
| wolf_briefing | reader | listener DB | `load_logical_scan_payload` (decoder-capable) |
| active_watchlists | reader | listener DB | historic read-only default |
| scan_history_export | reader | scanner `--database-path` | summaries, not full reconstruction |
| research_queries | reader | `--research --database-path` | historic read-only default |
| public_alert_funnel_audit | reader | explicit audit path | historic read-only default |
| sqlite_maintenance_inspect | reader | explicit path | **not** bounded live preflight |
| sqlite_maintenance_backup | archive writer | explicit source + archive | quiescent window only |
| lifecycle_hygiene_repair | optional writer | explicit path | not a cutover step |
| evidence_baseline_audit | DEV reader | refuses live Runtime path | not a Runtime preflight |
| post_restart_funnel_audit | reader | explicit path | immutable/quiescent source mode |
| external_listener_state | files | manifest/state/audit/latest_scan/performance_memory/watch JSONL | not in SQLite |
| durable_source_replay | optional separate evidence writer/reader | `SOURCE_REPLAY_EVIDENCE_PATH`; not the scanner database | `SOURCE_REPLAY_CAPTURE_ENABLED`. First rollout requires the switch **disabled**. Unknown is not disabled. Enabled is outside this packet and needs its own capacity, retention, and recovery review |
| unknown_external_readers | unresolved | UNKNOWN until Runtime inspection | rollout gate |

Promote **all** required consumers onto the pinned decoder-capable schema-26 release together. An old reader against schema 26 or `symbol_refs_v1` data is a failure criterion. Durable source replay stays a separate optional store and stays disabled for this initial cutover.

## 9. Approved cutover sequence (encode; execute NONE of this here)

1. Pin and verify the approved merged-main release SHA and its successful push CI. Preserve the old code/environment configuration and a sanitized process inventory. Stage the new environment separately. Review the complete accumulated delta and all migration contracts. Require the functional anchor to be an ancestor.
2. Disable scheduled launches, auto-restarts, and administrative intake that can spawn work. Stop the Telegram listener/command intake. Gracefully stop the scanner/watch process and any separate outbox/writer workers after accounting for in-flight transactions and uncertain sends. Stop remaining readers. Verify process and DB-handle quiescence before any migration or code switch. Do not force-kill a transaction and assume clean completion.
3. Take and verify the final quiescent pre-cutover snapshot, including separately identified external state. It must cover the drained boundary; an earlier rehearsal snapshot is not the final cutover source.
4. Restore that snapshot to a separate candidate DB and run the proven schema-26 migration, including the additive plan-state index, once under the isolated maintenance process. Validate schema 26 and the exact index definition. Reopen the candidate idempotently. Preserve the original and backup. Do not build a fresh empty runtime DB or selectively discard history.
5. Before starting consumers, verify schema 26 and `ix_lifecycle_records_epoch_locked_plan_state` again on the candidate that will actually be opened. Then, with every consumer stopped, promote the complete validated candidate through the rehearsed file/path switch and activate the pinned release for **all** required consumers. Keep `SOURCE_REPLAY_CAPTURE_ENABLED` disabled unless a separate review approved capture. Preserve correct SQLite sidecar relationships; do not hand-delete a WAL or mix files from different snapshots. Verify the explicitly configured DB path for scanner and listener, and all relevant state/manifest paths.
6. Keep public delivery suppressed while performing offline/dry-run smoke validation. Do not run a live-network scan against the production DB merely because its Telegram flag says dry-run. Any bounded shadow scan belongs on an isolated copy with the intended network policy and no public side effects.
7. Record the deployment manifest and start **one** scanner/watch owner with the approved operational flags and cadence. After its first bounded operational checks pass, start exactly one Telegram listener and the remaining approved readers/workers under the same compatible release. Public sending is restored only as specified in Adam's approved Runtime procedure.
8. Verify actual process paths, interpreter, code revision, DB schema/path, effective flags, first new run IDs/provenance, normal storage decoding, and delivery ownership.
9. Observe for seven continuous days, with immediate smoke checks, reviews at approximately one hour and 24 hours, and a final Runtime forensic audit. Include at least one operator-controlled restart and actual lifecycle progress. Rare cache, gap, expiry, and turnover paths that do not occur remain unobserved; do not manufacture them in production.

Do not emit a filled-in deployment command here. Unresolved operator-specific values remain blockers.

## 10. Observation success criteria

- Exact approved code/schema/path and one-listener ownership remain true; no unapproved writer or reader appears.
- No new unexplained payload integrity failure, migration loss, identity/public-event corruption, duplicate send attributable to the checkpoint, illegal lifecycle ownership transition, or restart discontinuity.
- P2A accounting and P2B transitions reconcile within their scoped semantics at common evidence boundaries. Separate legacy carryovers and classify deviations; do not demand unsupported real-fill accounting.
- Scan p95 is below the configured interval and no more than 10% above the comparable pre-cutover baseline, using a declared sample/window and comparable workload. Missing or incomparable baseline means the performance gate is unresolved.
- Disk/WAL growth and memory/CPU stay within the predeclared budget; recoverable capacity remains available; restore viability is retained.
- For instrumentation/storage-only behavior, ordinary strategy decisions and public inputs remain consistent in applicable comparisons. Distinguish intentionally repaired lifecycle/accounting behavior from accidental changes. Changing market inputs cannot prove decision parity.
- Every observed exception has a disposition and supporting evidence. A quiet log is not proof of a contract.

A full week is an operational trial, not an expectancy sample. If a material correction changes the release or contract, open a new operational epoch and repeat the affected acceptance window; never combine populations silently.

The first checkpoint cannot audit a historical series of capture matches because that series is not persisted. Assess only existing observable contracts and operational cost; label capture details unobserved in production where appropriate.

## 11. Failure and rollback

Immediately stop the affected runtime activity, preserve evidence, and invoke the rehearsed recovery procedure for corrupted/missing references, migration or integrity failure, incompatible consumers, duplicate polling, uncertain public side effects, breached capacity reserve, unexplained lost state, or a serious safety invariant violation.

Recovery cases:

- **Before new operational writes/public side effects:** preserve the failed candidate, restore the coordinated pre-cutover code/data/external-state bundle under the rehearsed procedure, and record the downtime boundary.
- **After new writes or public side effects:** never blindly replace the active DB with the old snapshot, and do not restore an older outbox after new public sends. That would lose new events and may resend messages. Stop sending, preserve the new DB/outbox/external state, reconcile uncertainty, and use a compatible forward repair or separately reviewed recovery.
- A decoder-capable schema-26 release with inline-only future encoding may mitigate the encoding path; it does not erase stored references or solve unrelated lifecycle faults.
- `8485f033abad3333269a8f26554abade4bae5ef5` is a **candidate code fallback for removing PR #120's in-memory capture only**, because it is that merge's first parent. Verify its schema, decoder, state compatibility, and tests before listing it as approved. It is not a universal rollback for the whole forensic chain.

No destructive down-migration, mass historical rewrite, outbox reset, blind retry of UNCERTAIN sends, or old-reader restart against schema 26 or `symbol_refs_v1` data.

## 12. Deployment epoch is not research admission

Record an external immutable operational deployment record plus existing `scan_runs.runtime_stats_json.research_provenance` where present. Use schema/SHA/provenance/run association together. Timestamp-only membership is insufficient. Do not backfill missing per-run provenance from the deployment record.

Keep populations distinct:

- Historical pre-contract rows
- Post-deployment operational writes, including explicitly identified legacy carryovers
- Later retained local source observations under their actual capture contract
- Later explicitly admitted research episodes with immutable bindings and tracking obligations

No existing row becomes source-bound, policy-bound, admitted, coverage-complete, or an authoritative outcome simply because it is read by newer code.

Authoritative expectancy remains closed. Do not publish win rate, profit factor, net expectancy, or regime/family expectancy from transitional rows.

## 13. Compatibility evidence already on DEV (synthetic)

Reuse; do not treat as Runtime proof:

- Legacy schema without `raw_payload_format` reads as `inline_v1` without migration (`tests/test_storage_single_copy.py`).
- Mixed `inline_v1` / `symbol_refs_v1` populations reconstruct losslessly; unknown/missing/corrupt formats fail closed.
- Historical v24→v25 additive migration preserves payload bytes; injected v25 migration failure rolls back; reopen is idempotent. That historical proof is not the schema-26 target rehearsal.
- Representative v14 lifecycle/Telegram fixture migrates to current schema 26 preserving economic values, identity/event keys, SENT/UNCERTAIN outbox states, and cursors (`tests/test_storage_database.py`).
- This readiness suite proves the collector will not initialize, migrate, install the plan-state index, or create an epoch. It will not use `immutable=1` on sidecar-absent files, and it will not fabricate `GO_FOR_RUNTIME_DEPLOYMENT`.

A fresh schema-26 fixture is not proof of the unknown Runtime starting schema. That upgrade, including the additive index, must be rehearsed on a verified separately restored copy **on Runtime**. DEV synthetic packets are not Runtime measurements. A reported rollout does not fill this packet.

## 14. A–W matrix (source-scoped DEV status)

Runtime validation remains pending for every deployed-repair claim. This readiness phase does not close a research-authority row.

| ID | Status |
| --- | --- |
| A | PARTIAL: local in-memory evidence; no retained as-run production source history or original possession proof. |
| B | FOUNDATION PRESENT, NOT FULLY CONSUMED: immutable economics does not supply episode/analytics ownership. |
| C | RESOLVED only at P2A accounting semantics; no real-fill identity; Runtime unverified. |
| D | RESOLVED only at P2B ownership boundary; no episode-owned tracking; Runtime unverified. |
| E | Prospectively repaired in code; historical oscillation preserved; Runtime unverified. |
| F | Outcome ownership PARTIAL; no authoritative episode owner. |
| G | Attribution PARTIAL at prospective first-INSERT scope; no retrospective episode binding. |
| H | Context PARTIAL: policy identity, source limits, current-projection handoff; no durable context/admission/coverage. |
| I | Latest accepted qualifying eligibility cutoff established in scope; not full historical as-of/possession evidence. |
| J | Supplied-prefix disposition PARTIAL; not interval coverage. |
| K | Fan-out PARTIAL; ordinary same-known-anchor defect not demonstrated; import ambiguity remains. |
| L | Analytics ownership OPEN. |
| M | Unique real trade/fill OPEN / UNAVAILABLE. |
| N | Canonical episode outcome OPEN. |
| O | Replay foundation present; semantic non-equivalence and policy identities established; same-opportunity authority absent. |
| P | Trustworthy expectancy denominator OPEN. |
| Q | Target integrity/chop research DEFERRED. |
| R | Prospective dedupe exists on DEV; historical allocation remains; current Runtime capacity, compatibility, and rollout unverified. |
| S | Regime/context redesign DEFERRED. |
| T | CMC DEFERRED. |
| U | Family/target/strategy redesign DEFERRED. |
| V | Performance memory DEFERRED from authoritative learning. |
| W | Champion/challenger promotion DEFERRED. |

## 15. Candidate next phase

`RUNTIME_BASELINE_AND_CHECKPOINT_REHEARSAL`: a separately authorized Runtime-PC operational procedure, beginning with bounded baseline collection. It is not automatic deployment.
