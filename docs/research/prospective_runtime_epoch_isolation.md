# PROSPECTIVE_RUNTIME_EPOCH_ISOLATION

DEV implementation and synthetic proof for one immutable operational Runtime epoch, origin membership, eligibility quarantine of untagged legacy rows, and ownership guards on every mapped setup-derived public-delivery path.

This document is the architecture handoff. It is not a Runtime runbook and does not authorize cutover.

## Disposition

- Status: `READY_FOR_ARCHITECTURE_REVIEW` — DEV IMPLEMENTATION AND SYNTHETIC PROOF COMPLETE
- Runtime restart authorized: **false**
- Production cutover authorized: **false**
- Automatic merge authorized: **false**
- Runtime DB access performed: **false**
- Runtime migration/deployment performed: **false**
- Runtime epoch initialization performed: **false**
- Scanner/listener start: **false**
- Real public send: **false**
- Order execution: **false**

## Baseline

- Reviewed main SHA: `eef92b89f168bbb016715f3486b9f6bf2824f53f`
- Feature branch: `feature/prospective-runtime-epoch-isolation`
- Implementation commit SHA: `3e754ecfb649cee057bc30b742ac21714a75732b`
- Final commit SHA: *branch tip after identifier commits*
- PR URL: *filled after open*

## What was implemented

One relational operational epoch boundary on the existing SQLite architecture (schema **25 → 26**). Schema migration does not create an epoch. Explicit idempotent `initialize_runtime_epoch` is the only activator, tested only on temporary databases.

Operational origin is run registration plus a per-symbol `live_fresh` grant. Times must be strictly after the epoch UTC cutoff. Cached/resumed/replay/imported evaluations cannot be relabeled fresh.

New lifecycle rows carry immutable `runtime_epoch_id` and `creation_origin_id`. Legacy rows stay NULL. Identity algorithms are unchanged; a reobserved legacy primary key is a collision, not an overwrite. Current-row uniqueness is split: legacy `(symbol, mode, direction)` where `is_current=1 AND runtime_epoch_id IS NULL`; prospective `(runtime_epoch_id, symbol, mode, direction)` where `is_current=1 AND runtime_epoch_id IS NOT NULL`.

New public intents are stamped with epoch plus originating lifecycle in the existing reservation transaction. Global `UNIQUE(event_key)` is unchanged. Legacy PENDING/RETRYABLE/IN_FLIGHT/UNCERTAIN cannot be claimed, recovered, or rewritten. Lazy structural-anchor resolution is a read-only projection on legacy rows; current-epoch events may persist a recovered anchor. A stored legacy lifecycle primary key cannot originate a new public intent.

Canonical `scan_runs/watch_state.json` is forensic. Operational watch state is `scan_runs/epochs/<epoch_id>/watch_state.json`. Missing operational state does not fall back to the legacy file.

Cohort labels are only `LEGACY_OR_UNATTRIBUTED` and `CURRENT_EPOCH_OPERATIONAL`. They are not admission or expectancy.

## Persistence authority

| Object | Authority |
| --- | --- |
| Active epoch | `runtime_epoch_control.control_key='active'` → `runtime_epochs` |
| Run lineage | `runtime_operational_runs` before lifecycle/public effects |
| Symbol origin | `runtime_operational_origins` unique `(run_id, symbol)` |
| Lifecycle membership | `setup_lifecycle_records.runtime_epoch_id` + `creation_origin_id` (nullable, immutable) |
| Public ownership | `public_alert_events.runtime_epoch_id` + `origin_lifecycle_id` + `origin_root_event_id` |
| Operational watch JSON | `scan_runs/epochs/<epoch_id>/watch_state.json` |

Missing epoch is fatal operational startup (`require_operational_runtime`). Individual unproved observations are blocked before create/send. `RuntimeEpochError` is not a recoverable watch failure.

## Guarded entrypoints

| Entrypoint | Origin / ownership | Missing-context behavior | Test |
| --- | --- | --- | --- |
| `initialize_runtime_epoch` | Explicit identity | Same identity idempotent; different identity fails | T16, T18, T20 |
| `require_operational_runtime` / `scripts/run_scan.main` | Expected epoch + existing DB | Fail closed; no auto-create/migrate | T17, T19 |
| `SetupLifecycleService.apply_to_run_result` | Register run, per-symbol origin | Skip unproved symbol; missing epoch fatal | T08–T10, T14, T15 |
| `SQLiteSetupLifecycleRepository` writes | Current-epoch ownership | Raise; no legacy mutate | T08, T09, T19, T22 |
| `evaluate_closed_candle_outcomes` | Current-epoch lifecycle id | No-op for legacy | T02–T05 |
| `_insert_public_alert_event` | Existing event_key reused; new rows need epoch+lifecycle | No insert | T01, T06, T10 |
| `SQLitePublicTelegramOutbox.claim/recover/persist/record_part/mark_*` | `decide_public_effect` before mutate | No-op / blocked claim | T07, T19 |
| `deliver_for_run` / `deliver_for_symbol` / recover / reconcile | Epoch + owned events only | Skip unowned; no send | T01–T07, T10 |
| Watch activation live send | Blocked | Raise | T22 |
| `save_watch_state` canonical path | Refused | Raise; bytes unchanged | T22 |
| Hygiene / `repository.reset` / `--reset-lifecycle` | Rejected for legacy / destructive reset | Raise | T22 |
| `active_lifecycle_symbols` / health exemptions | Epoch-scoped states | Legacy cannot exempt | T13, T21 |
| Admin active watchlist/signal readers | Current-epoch operational only | Legacy excluded | T21 |

## External-state path

| Path | Behavior |
| --- | --- |
| `scan_runs/watch_state.json` | Canonical legacy; never overwritten by operational save |
| `scan_runs/epochs/<epoch_id>/watch_state.json` | Operational working copy; must carry `runtime_epoch_id` |
| `scan_runs/latest_scan.json` / resume payloads | Cannot grant `live_fresh` origin |
| `scan_runs/performance_memory.json` | Untouched; not activated |
| Listener `latest_processed_update_id` | Untouched; not rewound |
| Runtime recovery bundle six files | Not retrieved or modified from DEV |

## Schema / index work

- Before: `SCHEMA_VERSION=25`
- After: `SCHEMA_VERSION=26`
- New tables: `runtime_epochs`, `runtime_epoch_control`, `runtime_operational_runs`, `runtime_operational_origins`
- New nullable columns on `setup_lifecycle_records` and `public_alert_events`
- Drop `ux_lifecycle_records_current_symbol_mode_direction`
- Add split partial uniques listed above
- No bulk payload rewrite, no VACUUM, no DB copy, no legacy backfill
- Repeated `initialize_database` does not recreate the old global unique index
- Injected v26 failure rolls back that transaction; no usable half-migrated epoch is exposed

Unmeasured Runtime resource risk: index rebuild on a large `setup_lifecycle_records` table during a future authorized offline migration. Synthetic estimates are not production measurements.

The existing v25 candidate digest identifies **pre-change** content. A future authorized migration/epoch initialization changes that file; do not reuse the old digest.

## Acceptance map (T01–T22)

| ID | Proof | Effect path |
| --- | --- | --- |
| T01 | `test_t01_legacy_actionable_cannot_create_initial_public_signal` | `deliver_for_run` + `deliver_for_symbol` |
| T02 | `test_t02_legacy_confirmed_cannot_resume_limit_or_fill` | outcome + public follow-up |
| T03 | `test_t03_legacy_managing_cannot_write_tp_sl_progress` | `evaluate_closed_candle_outcomes` |
| T04 | `test_t04_legacy_triggered_cannot_progress` | outcomes no-op |
| T05 | `test_t05_terminal_legacy_is_not_backfilled` | no invented TP/SL |
| T06 | `test_t06_legacy_sent_event_key_remains_consumed` | global `event_key` unique |
| T07 | `test_t07_legacy_pending_states_are_not_claimed_or_rewritten` | outbox claim/recover |
| T08 | `test_t08_fresh_setup_can_coexist_with_legacy_current_row` | split current unique |
| T09 | `test_t09_legacy_identity_collision_is_rejected` | PK collision |
| T10 | `test_t10_fresh_valid_setup_reaches_fake_sender` | confirmation/fill + fake sender |
| T11 | `test_t11_quality_gates_are_unchanged` | existing RR/quality decision |
| T12 | `test_t12_legacy_cooldown_still_vetoes_then_expires` | `legacy_cooldown_veto` |
| T13 | `test_t13_legacy_active_state_does_not_grant_health_exemption` | `_lifecycle_states_for_symbols` |
| T14 | `test_t14_resumed_and_mixed_run_admit_only_fresh_symbols` | origin kind |
| T15 | `test_t15_timing_contract_fail_closed` | strictly-after cutoff |
| T16 | `test_t16_reopen_preserves_epoch_and_excludes_legacy` | durable epoch |
| T17 | `test_t17_missing_epoch_blocks_before_writes` | startup fail closed |
| T18 | `test_t18_crash_and_concurrent_init_leave_no_unowned_live_state` | rollback + unique control |
| T19 | `test_t19_direct_and_recoverable_paths_cannot_bypass` | repo + watch classifier |
| T20 | `test_t20_v25_upgrade_preserves_rows_and_rolls_back_failure` | v26 migration |
| T21 | `test_t21_cohort_labels_are_honest` | cohort projection |
| T22 | `test_t22_legacy_evidence_is_frozen` | watch/hygiene/reset |

## Remaining gaps (intentionally not this phase)

- Durable source binding / provenance (batch-delivery capture remains in-memory)
- Research admission, tracking obligation, canonical outcomes, expectancy
- Champion/challenger, adaptive strategy, execution
- Runtime cutover, epoch initialization on Runtime, capacity refresh

## Future release consequences

`eef92b89` does not acquire these semantics. The future release must be pinned to the reviewed merged SHA that contains this phase. Rollback to v20/old code does not re-authorize trading legacy setups; they remain evidence-only unless a later explicit compatible decision says otherwise. Do not erase new epoch/public history to make rollback easy.

## Test commands

```
python -m pytest tests/test_prospective_runtime_epoch_isolation.py
python -m pytest
git diff --check
```

Results (DEV PC, `C:\CandleCraftDev\.venv\Scripts\python.exe`, 2026-09-20):

- `python -m pytest tests/test_prospective_runtime_epoch_isolation.py`: 25 tests (T01–T22; T07 parametrized across pending/retryable/in-flight/uncertain). Included in the full run below; all passed.
- `python -m pytest` (`-q --tb=line`): **2549 collected, 2549 passed**, exit 0, elapsed 431956 ms. One unrelated `StarletteDeprecationWarning` from FastAPI's TestClient (`httpx`/`starlette.testclient`). No skips added to hide failures.
- `git diff --check`: clean (exit 0).

Environment: Windows 10, project `.venv`, `TELEGRAM_DRY_RUN=true` / `TELEGRAM_SIGNALS_ENABLED=false` / `LOCAL_MANUAL_MODE=true` / `ORDER_EXECUTION_ENABLED=false` for test iteration. No Runtime filesystem, live exchange, listener, or scanner watch loop.
