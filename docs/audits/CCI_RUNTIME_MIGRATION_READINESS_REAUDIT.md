# Candle Craft Intelligence Runtime Migration Readiness Re-audit

Audit date: 2026-07-18
Audit branch: audit/runtime-migration-readiness-recheck
Audited source: 5e34b84ee49ff4128e06ac8ee526896757f5f3d4
Target: main and local origin/main at the audited source when validation began
Schema: v16

## 1. Executive verdict

**GO_FOR_COPY_REHEARSAL**

The repository is ready for a controlled, copy-based Runtime database backup and migration rehearsal. There are zero current P0 findings and zero current P1 blockers. Both historical post-remediation blockers are resolved, the original concurrency regression passed 25 consecutive isolated runs, the WAL matrix passed five consecutive runs, every focused group passed, and the 1,734-test suite passed twice.

This authorizes only migration of a verified, separately restored copy. It does not authorize direct modification of the live Runtime database and does not claim production migration is complete.

## 2. Baseline and scope

| Item | Evidence |
|---|---|
| Branch | audit/runtime-migration-readiness-recheck |
| Source SHA | 5e34b84ee49ff4128e06ac8ee526896757f5f3d4 |
| PR #68 | The audited SHA is the merge commit for PR #68, "Fix SQLite WAL initialization race". |
| PR #68 files | app/storage/database.py, docs/RUNTIME_SQLITE_MAINTENANCE.md, tests/test_sqlite_maintenance.py, tests/test_storage_database.py |
| PR #66 | 95e9f8c is PR #68's first parent and the merged PR #66 integration commit. |
| Schema | app/storage/database.py defines SCHEMA_VERSION = 16; migration tests assert user_version 16. |
| Initial worktree | Only CCI_FULL_SCANNER_DEEP_AUDIT.md was untracked. |

PR #67 is an outdated NO_GO audit. It was used only to enumerate historical finding IDs and the two prior blockers. It was not modified, merged, or treated as current evidence. Current conclusions come from source at 5e34b84 and the fresh validation below.

CCI_FULL_SCANNER_DEEP_AUDIT.md remained untracked and was not written, staged, or included.

## 3. Safety boundaries

No audit command accessed the Runtime PC, production database, any .env file, Telegram, Binance, an exchange/order endpoint, or any application live-network route. No listener or long-running watch loop was started. Database writes were temporary pytest fixtures.

Every pytest command used:

    TELEGRAM_DRY_RUN=true
    TELEGRAM_SIGNALS_ENABLED=false
    LOCAL_MANUAL_MODE=true
    ORDER_EXECUTION_ENABLED=false
    TELEGRAM_BOT_TOKEN=
    TELEGRAM_CHAT_ID=
    TELEGRAM_SIGNAL_CHANNEL_ID=
    TELEGRAM_SIGNAL_CHANNEL_INVITE_LINK=
    TELEGRAM_VIP_CHANNEL_ID=
    TELEGRAM_WOLF_BRIEFING_CHANNEL_ID=
    PYTHONDONTWRITEBYTECODE=1

This re-audit changes only tests and this report; no production module changed. GitHub is used only after validation for the requested push and draft PR.

Prohibited boundaries remain:

- No initialization, migration, checkpoint, repair, or other write against the live Runtime database.
- No unverified copy and no ignoring active WAL/SHM state.
- No live Telegram or Binance route during the copy rehearsal without separate authorization.
- No order execution, withdrawals, or transfers.
- No claim that production migration is complete.

## 4. Previous blocker resolution

| Historical blocker | Status | Current evidence |
|---|---|---|
| CCI-POST-P1-001 - concurrent fresh-database WAL initialization | **RESOLVED** | Bounded busy/locked retry, timeout restoration, mandatory WAL verification, and connection cleanup are implemented. The original reservation regression passed 25/25 runs. Fresh/already-WAL thread/process stress passed 5/5 matrices. |
| CCI-POST-P1-002 - representative v14-to-v15-to-v16 preservation unverified | **RESOLVED** | The representative fixture proves lifecycle/Telegram preservation, delivery mapping, no duplicate claim eligibility, idempotence, rollback, and verified backup/restore. |

## 5. WAL bootstrap stress

The writable profile is WAL, foreign_keys ON, FULL synchronous, 5,000 ms normal busy timeout, and 1,000-page autocheckpoint. WAL refusal or failed post-enable verification raises StorageError; there is no silent fallback.

| Required axis | Evidence |
|---|---|
| Fresh/already-WAL | Thread and process tests cover both preinitialize values. |
| Threads | Eight simultaneous openers all return WAL and schema 16. |
| Processes | Four spawned openers all return WAL and schema 16. |
| Retry success | Two deterministic SQLITE_BUSY failures then success; 250 ms timeout restored; under one second. |
| Timeout exhaustion | Retained lock with 30 ms budget fails in under one second. |
| Cleanup | Success, timeout, refusal, and verification-failure paths permit immediate rename. |
| Verification failure | Simulated enable followed by journal_mode=delete fails closed. |
| No fallback | Simulated SQLite refusal returning delete fails closed. |

Original regression, repeated 25 times:

    .\.venv\Scripts\python.exe -m pytest -q -o addopts= --disable-warnings --basetemp .pytest_tmp_reaudit_concurrency -p no:cacheprovider tests\test_telegram_lifecycle_delivery_phase42.py::test_concurrent_public_watchlist_reservations_allow_one_sender

Exact result: **25/25 invocations passed; each reported 1 passed, 1 warning.**

WAL matrix, repeated five times:

    .\.venv\Scripts\python.exe -m pytest -q -o addopts= --disable-warnings --basetemp .pytest_tmp -p no:cacheprovider tests\test_sqlite_maintenance.py -k "writable_connection_profile_is_explicit_and_verified or concurrent_thread_database_open_is_wal_safe or concurrent_process_database_open_is_wal_safe_and_closes_handles or wal_initialization_lock_retry_is_bounded_and_closes_connection or wal_initialization_retries_transient_lock_then_succeeds or writable_connection_fails_if_wal_verification_does_not_hold or writable_connection_does_not_claim_wal_when_sqlite_refuses or connection_cleanup_leaves_no_open_handle"

| Run | Exact result |
|---:|---|
| 1 | 10 passed, 41 deselected, 1 warning in 5.91s |
| 2 | 10 passed, 41 deselected, 1 warning in 7.38s |
| 3 | 10 passed, 41 deselected, 1 warning in 6.93s |
| 4 | 10 passed, 41 deselected, 1 warning in 6.97s |
| 5 | 10 passed, 41 deselected, 1 warning in 6.63s |

## 6. Representative v14-to-v15-to-v16 evidence

| Requirement | Verified evidence |
|---|---|
| Plans/identifiers | lifecycle_id v14-lifecycle and setup_identity v14-plan remain equal. |
| States/outcomes | WATCHLISTED-to-CONFIRMED history remains; analytics retains TP1/tp1_reached. |
| Entry/TP/SL/invalidation | Entry 100-101, SL 95, TP1/2/3 105/110/115, RR 3.0, and invalidation text are compared. |
| Event history | Event 41, transition, reason, scan id, note, and timestamp remain equal. |
| Telegram intentions/outcomes | Attempts 61/62 and events 71/72 retain signal, plan, event key, hash, timestamps, and status. |
| Delivery mapping | Legacy sent becomes SENT; legacy reserved becomes UNCERTAIN with legacy_reserved_acceptance_unknown. |
| Dedupe/reservation | Plan/event keys remain stable; active-event uniqueness covers claimable and ambiguous states. |
| No duplicate eligibility | Outbox claims for migrated SENT and UNCERTAIN rows return no claim. |
| Foreign keys | foreign_key_check is empty; lifecycle-event and analytics joins have zero orphans. |
| Counts/timestamps | Five table counts and important lifecycle/Telegram timestamps remain equal. |
| Idempotence | Reopening v16 leaves representative rows/counts unchanged. |
| Rollback | Faults in the v14 chain and v15-to-v16 retain original version, schema, and rows. |
| Backup/restore | Verified v14 snapshot is restored separately and migrated; source remains v14 unchanged. |

## 7. Original deep-audit P0/P1 status

The original deep audit established no P0; this re-audit found no P0.

| ID | Current status | Evidence |
|---|---|---|
| CCI-P1-001 - open/future candles | **RESOLVED** | Scanner/candle focus passed 158; lifecycle focus passed 171. |
| CCI-P1-002 - replay HTF lookahead | **RESOLVED** | Replay/RR/config focus passed 68. |
| CCI-P1-003 - confirmed data-health blocker | **RESOLVED_BY_APPROVED_POLICY** | setup_only remains public policy; Telegram focus passed 611. |
| CCI-P1-004 - dry-run bypass | **RESOLVED** | Dry-run reaches the sender and exits before transport. |
| CCI-P1-005 - send/ledger crash gap | **RESOLVED** | Committed intents, atomic claims, part state, IDs, and UNCERTAIN reconciliation remain. Exactly-once is not claimed. |
| CCI-P1-006 - TP/SL unreachable | **RESOLVED** | Plan-specific closed-candle outcomes cover entry, TP1/2/3, SL, invalidation, restart, and terminal analytics. |
| CCI-P1-007 - minimum-RR mismatch | **RESOLVED** | Authoritative RR remains wired through CLI, strategy, targets, and delivery. |
| CCI-P1-008 - cooldown omits active symbols | **RESOLVED** | Active priority/cooldown exemption remains; watch focus passed 94. |

No test was weakened, skipped, xfailed, deleted, or loosened.

## 8. Current findings

| Severity | Count | Rehearsal effect |
|---|---:|---|
| P0 | 0 | None |
| P1 | 0 | None |
| P2 | 6 | Non-blocking for copy rehearsal; follow-up/acceptance required. |
| P3 | 3 | Non-blocking maintainability/reproducibility work. |

### P2

| ID | Status | Current evidence |
|---|---|---|
| CCI-P2-009 - universe contract metadata | **OPEN** | Symbol universe uses quote volume/market cap but lacks exchangeInfo contract status/type/tick/step/notional metadata. |
| CCI-P2-010 - cross-phase atomicity | **PARTIAL** | Phase statuses exist, but scan, lifecycle, and outbox remain separate commits. |
| CCI-P2-014 - regime history | **OPEN** | app/pipeline/scanner_runner.py:3555-3556 replaces history with REJECTED_BY_REGIME. |
| CCI-P2-015 - lifecycle identity | **PARTIAL** | app/storage/database.py:452 retains UNIQUE(symbol, mode, direction); tuple lookup remains at app/lifecycle/repositories.py:42. |
| CCI-POST-P2-001 - live outcomes/performance memory | **OPEN** | scripts/run_scan.py:894 ingests replay summaries; live terminal analytics remain separate. |
| CCI-POST-P2-002 - producer/cohort metadata | **OPEN** | Stored rows have no immutable scanner/strategy/producer/data cohort version. |

CCI-P2-012 is resolved for repository copy-rehearsal readiness: explicit WAL/FULL/busy-timeout/autocheckpoint, read-only inspection, integrity, verified backup, growth reporting, concurrency retry, rollback, and no-delete policy are present and tested. Runtime filesystem capacity/contention are operational prerequisites.

### P3

| ID | Status | Current evidence |
|---|---|---|
| CCI-P3-018 - duplicate helper | **OPEN** | _public_setup_quality_score_decimal is defined at lines 8387 and 8398. |
| CCI-P3-019 - mode recomputation | **OPEN** | scanner_runner.py:1368-1372 invokes strategy analysis inside the mode loop. |
| CCI-P3-021 - unlocked dependencies | **OPEN** | Bounded requirements exist but no lock/constraints artifact was found. |

## 9. Commands and exact results

The first WAL command used nested --basetemp .pytest_tmp\reaudit_wal_core without an existing parent. Result: **8 setup errors, 41 deselected, 1 warning in 1.32s**; no test body ran. Correcting it to root-level .pytest_tmp_reaudit_wal_core produced **8 passed, 41 deselected, 1 warning in 7.52s**. This was an audit invocation error, not a repository failure.

Migration focus before assertion additions:

    .\.venv\Scripts\python.exe -m pytest -q -o addopts= --basetemp .pytest_tmp_reaudit_migrations -p no:cacheprovider tests\test_storage_database.py -k "schema_v14_fixture_matches_pre_outcome_pre_outbox_contract or schema_v14_to_v16_preserves_lifecycle_and_telegram_data or schema_v14_to_v16_migration_is_idempotent or schema_v14_migration_failure_rolls_back_completely or verified_v14_backup_restore_migrates_copy_without_changing_source or schema_v15_delivery_data_survives_v16_migration or schema_v16_migration_is_idempotent_for_v15_delivery_data or schema_v16_migration_failure_rolls_back_completely"

Result: **8 passed, 17 deselected, 1 warning in 1.87s**. The four changed/new assertions then passed directly with exit code 0.

| Focused group | Exact result |
|---|---|
| Storage/migration/repair/persistence/local runtime | 107 passed, 1 warning in 13.05s |
| Lifecycle/outcome/replay audit | 171 passed, 1 warning in 12.56s |
| Every test_telegram_ file | 611 passed, 1 warning in 65.05s |
| Watch mode/supervisor/presets/symbol health | 94 passed, 1 warning in 21.17s |
| Scanner/CLI/candles/timeframes/universe/cache | 158 passed, 1 warning in 27.97s |
| Replay causality/authoritative RR/config | 68 passed, 1 warning in 5.84s |
| Explicit no-order invariant | 5 passed, 1 warning in 1.41s |

Focused commands disabled pytest's cache provider, so the configured cache_dir produced one command-induced PytestConfigWarning.

Complete command, run twice:

    .\.venv\Scripts\python.exe -m pytest

| Run | Exact result |
|---:|---|
| 1 | **1,734 passed, 1 warning in 142.44s** |
| 2 | **1,734 passed, 1 warning in 139.37s** |

Both runs had zero failures, errors, skips, and xfails. The one warning was the same third-party Starlette/httpx deprecation.

Compilation:

    .\.venv\Scripts\python.exe -c "from pathlib import Path; paths=[p for root in ('app','scripts','tests') for p in Path(root).rglob('*.py')]; [compile(p.read_text(encoding='utf-8'), str(p), 'exec') for p in paths]; print(f'compiled_python_files={len(paths)}')"

Result: **compiled_python_files=248**, exit 0.

    .\.venv\Scripts\python.exe -m pip check

Result: **No broken requirements found.**, exit 0.

    git diff --check

Result: exit 0; only CRLF normalization notices for the two edited tests, no whitespace error.

## 10. Operational prerequisites

1. Identify the actual Runtime database schema, sidecars, permissions, filesystem, free space, and archive capacity read-only.
2. Coordinate writers with an approved runbook; never copy an incoherent main file while ignoring WAL.
3. Create a unique online backup without overwriting an existing snapshot.
4. Verify manifest, checksum, schema, core counts, quick_check, foreign keys, and approved full integrity_check.
5. Restore to a separate rehearsal path; preserve the live DB and original snapshot.
6. Migrate only the restored copy to v16 and compare counts plus representative lifecycle/outbox identities, hashes/IDs, outcomes, and timestamps.
7. Reopen twice for idempotence; repeat integrity and FK checks.
8. Exercise fresh/already-WAL process concurrency on the rehearsal filesystem.
9. Rehearse rollback by discarding the copy and restoring a fresh verified snapshot.
10. Run a bounded dry-run soak with credentials cleared, egress blocked, and orders disabled.
11. Reconcile UNCERTAIN Telegram rows manually; never blindly resend.
12. Record acceptance/owners for remaining P2/P3 items before any later production authorization.

## 11. Files changed

- tests/test_sqlite_maintenance.py - two audit-only WAL retry/verification tests.
- tests/test_storage_database.py - stronger representative preservation, relationship, count, and non-claimability assertions.
- docs/audits/CCI_RUNTIME_MIGRATION_READINESS_REAUDIT.md - this report.

No production module changed.

## 12. Final verdict

**GO_FOR_COPY_REHEARSAL**

| Gate | Result |
|---|---|
| PR #68 on main; schema v16 | PASS |
| P0/P1 | PASS - 0/0 |
| Original concurrency regression | PASS - 25/25 |
| WAL matrices | PASS - 5/5, 50/50 selected cases |
| Representative migration chain | PASS |
| Idempotence/rollback/backup/restore | PASS |
| Focused validation | PASS |
| Full suite twice | PASS - 1,734 each |
| Compilation/pip/diff check | PASS |

This is not GO for direct production migration. Never modify the live Runtime database directly. Production migration is not complete.
