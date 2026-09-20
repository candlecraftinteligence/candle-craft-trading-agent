# PROSPECTIVE_RUNTIME_EPOCH_ISOLATION

DEV implementation and synthetic proof for one immutable operational Runtime epoch, origin membership, eligibility quarantine of untagged legacy rows, and ownership guards on every mapped setup-derived public-delivery path.

This document is the architecture handoff. It is not a Runtime runbook and does not authorize cutover.

## Disposition

- Status: `READY_FOR_ARCHITECTURE_RE_REVIEW` — PR #123 repaired
- PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/123
- Feature branch: `feature/prospective-runtime-epoch-isolation`
- Runtime restart authorized: **false**
- Production cutover authorized: **false**
- Automatic merge authorized: **false**
- Runtime DB access performed: **false**
- Runtime migration/deployment performed: **false**
- Runtime epoch initialization performed: **false**
- Scanner/listener start: **false**
- Real public send: **false**
- Order execution: **false**

This repair does **not** claim research admission, source provenance completeness, tracking completeness, canonical outcome authority, expectancy, profitability, or Runtime restart authorization.

## Baseline and reviewed heads

- Baseline main SHA: `eef92b89f168bbb016715f3486b9f6bf2824f53f`
- Old reviewed blocked PR HEAD: `d616069843a3d27eb6f223bf3f4ea7d2e1afbc00`
- Architecture review verdict on that HEAD: `CHANGES_REQUIRED_ORIGIN_AND_PUBLIC_OWNERSHIP_BYPASSES`
- Repair commit SHA: recorded after commit in this same document section (see Git record below)
- Final PR HEAD: recorded after push

## What remains from the original phase (kept)

One relational operational epoch boundary on the existing SQLite architecture (schema **25 → 26**). Schema migration does not create an epoch. Explicit idempotent `initialize_runtime_epoch` is the only activator, tested only on temporary databases.

New lifecycle rows carry immutable `runtime_epoch_id` and `creation_origin_id`. Legacy rows stay NULL. Identity algorithms are unchanged; a reobserved legacy primary key is a collision, not an overwrite. Current-row uniqueness is split: legacy `(symbol, mode, direction)` where `is_current=1 AND runtime_epoch_id IS NULL`; prospective `(runtime_epoch_id, symbol, mode, direction)` where `is_current=1 AND runtime_epoch_id IS NOT NULL`.

Global `UNIQUE(event_key)` is unchanged. Legacy PENDING/RETRYABLE/IN_FLIGHT/UNCERTAIN cannot be claimed, recovered, or rewritten as operational delivery. Cohort labels remain only `LEGACY_OR_UNATTRIBUTED` and `CURRENT_EPOCH_OPERATIONAL`. Canonical `scan_runs/watch_state.json` remains forensic. Strategy gates (RR, confirmation, quality, targets, economic identity) were not changed.

## Repair of review blockers

### B1 — origin / freshness authority

- `ScannerSymbolResult.evaluation_origin_kind` defaults to `unspecified`, not `live_scan`.
- Only the live producer path (`_build_symbol_result`) assigns `live_scan`.
- Missing evaluation, decision-cutoff, or producer observation time does **not** fall back to processing `now` or to the other timestamps. Origin grant is `BLOCKED` (`origin_times_unknown`).
- Producer observation is the adapter `observed_completed_at` evidence, not inferred decision time.
- A post-epoch cache HIT or closed-subset selection does not prove post-epoch acquisition. Cached batches acquired before cutoff cannot become `live_fresh` merely because they were delivered later.
- Pre-epoch candles may remain lookback context. The decisive observation used to authorize a new lifecycle must have provable post-cutoff acquisition/evaluation.
- Deserialization / provenance-lost payloads stay `unspecified` and non-operational.

Regression: R01, R02, R03, R04.

### B1b — per-symbol origin ownership

Before lifecycle create/upsert, persistence proves: active epoch matches, origin exists, origin belongs to the registered operational run, origin `runtime_epoch_id` matches, origin symbol matches the lifecycle symbol under canonical normalization, `creation_origin_id` is immutable, lifecycle `runtime_epoch_id` is immutable. An epoch-wide token cannot insert another symbol.

Regression: R05 (direct repository path).

### B2 — public intent requires a persisted owned lifecycle

A new setup-derived public intent is not inserted unless the referenced lifecycle is durably persisted and proves:

event → lifecycle → creation origin → registered operational run → active epoch

`require_lifecycle_public_intent` fails closed on a missing lifecycle (raises). `_insert_public_alert_event` does not treat missing IDs as success. `_resolve_origin_lifecycle_id_for_plan` only returns an already-persisted owned candidate. `decide_public_effect` rejects a forged/inconsistent event→lifecycle chain even when the event row itself carries the active epoch ID. `UNIQUE(event_key)` remains global.

Regression: R06, R07, R08, R09, R28.

### B3 — direct delivery mutation APIs

Mapped mutation surfaces in `app/alerts/telegram_outbox.py` and `app/alerts/telegram_lifecycle.py` validate ownership before write:

- `mark_part_in_flight`, `record_part_result`, `claim`, `recover_stale_in_flight`, `mark_terminal_without_send`, `mark_uncertain_after_persistence_failure`, `persist_intent_parts`
- `replace_attempt_with_reservation`, `mark_public_watchlist_reservation_result`, `insert_attempt`, `compact_repeated_attempt`

A caller who knows a legacy part ID or attempt ID cannot mutate it. Legacy PENDING/RETRYABLE/IN_FLIGHT/UNCERTAIN stay unchanged. UNCERTAIN is not auto-retried.

`insert_attempt` may record **new** skipped/blocked audit rows that do not change the public event row. SENT / in-flight / pending / retryable / uncertain inserts against an unowned event are refused. `replace_attempt_with_reservation` may adopt a non-public attempt that has no `public_watchlist_event_key` onto a **newly owned** event; it still cannot re-own a legacy attempt that already points at an unowned event.

Regression: R10, R11, R12, R13, R29.

### B4 — fail-closed operational open before init/migrate

Operational open (`inspect_operational_database` / `open_operational_database` / `open_operational_service_database`) is separate from offline initialization/migration (`initialize_database`, `migrate_existing_database`, `open_initialized_database`).

Operational open verifies, without mutating first: selected DB exists, schema is the supported required generation, active epoch exists, expected epoch matches.

It does not create a missing DB, upgrade a v25 DB, repair schema, initialize an epoch, or choose a default DB silently.

Repository/service entrypoints that have an expected epoch use operational open. Explicit migration remains offline.

Regression: R14–R18.

### B5 — watch state is not silently adopted

If `scan_runs/epochs/<epoch_id>/watch_state.json` is missing, a fresh empty current-epoch state may be created. If it exists, `runtime_epoch_id` must already match exactly. Missing/NULL/empty/malformed/different IDs fail closed. Bytes of a rejected file are unchanged. Canonical `scan_runs/watch_state.json` is not a fallback.

Regression: R19, R20, R21.

### Alternate surfaces

- `maybe_send_research_watch_alerts`: when an active epoch exists, setup-derived research-watch send is rejected until compatible ownership exists. Fake sender only in tests. R22.
- `load_active_signal_detail`: active operational detail is current-epoch only; legacy/unattributed history is not an active recommendation. R23.
- Admin/draft setup-derived operational send requires epoch ownership when `RUNTIME_EPOCH_ID` is set and a database path is present. Diagnostic listing without a database remains separate from send. R24.

## Genuine v25 migration proof

T20 / R25 / R26 use `tests/fixtures/genuine_v25.py`, which initializes via `tests/fixtures/baseline_v25_database.py` (baseline-v25 schema semantics). It is not created by HEAD `initialize_database`.

Proven:

- v25 → v26 preserves legacy logical data
- `runtime_epoch_id` and `creation_origin_id` remain NULL
- old lifecycle and public values unchanged; event keys unchanged
- old global current-row unique index removed; two replacement lifecycle indexes present
- epoch is not auto-created
- schema is 26 only after successful migration

Injected v26-index failure: the selected DB remains at schema 25, the old uniqueness index remains, epoch tables do not appear, no half-operational generation is exposed.

**Transaction boundary actually proven:** the v26-specific index-replacement step rolls back when `_ensure_lifecycle_epoch_current_indexes` fails. This document does **not** claim that the entire `initialize_database` routine is globally atomic if earlier statements can commit before that step.

## Persistence authority

| Object | Authority |
| --- | --- |
| Active epoch | `runtime_epoch_control.control_key='active'` → `runtime_epochs` |
| Run lineage | `runtime_operational_runs` before lifecycle/public effects |
| Symbol origin | `runtime_operational_origins` unique `(run_id, symbol)` |
| Lifecycle membership | `setup_lifecycle_records.runtime_epoch_id` + `creation_origin_id` (nullable, immutable) |
| Public ownership | `public_alert_events.runtime_epoch_id` + `origin_lifecycle_id` + `origin_root_event_id` |
| Operational watch JSON | `scan_runs/epochs/<epoch_id>/watch_state.json` |

Missing epoch is fatal operational startup. Individual unproved observations are blocked before create/send.

## Acceptance map (original T01–T22 plus repair R01–R30)

Original T01–T22 remain in `tests/test_prospective_runtime_epoch_isolation.py`. Repair regressions R01–R29 are in `tests/test_prospective_runtime_epoch_isolation_boundaries.py` and are marked `no_auto_epoch` so the autouse synthetic epoch/origin helper cannot mask fail-closed proofs. R30 is the existing RR/quality/confirmation/target/economic-identity suite in the full pytest run.

| ID | Proof |
| --- | --- |
| R01 | Missing evaluation/decision/producer evidence does not grant `live_fresh` |
| R02 | Pre-epoch adapter acquisition + post-epoch cache HIT/closed-subset does not grant `live_fresh` |
| R03 | Fresh post-epoch producer acquisition with valid decisive timing can grant origin |
| R04 | Serialization/deserialization with lost provenance does not default to live |
| R05 | BTC origin cannot create/update ETH lifecycle (repository) |
| R06 | Missing lifecycle cannot create a public intent |
| R07 | Foreign-epoch / legacy lifecycle cannot create a new public intent |
| R08 | Valid current-epoch lifecycle + matching origin/run can create a public intent |
| R09 | Public effect decision rejects a forged event→lifecycle chain |
| R10 | `mark_part_in_flight` cannot mutate a legacy part |
| R11 | `mark_public_watchlist_reservation_result` cannot mutate a legacy attempt |
| R12 | `replace_attempt_with_reservation` cannot re-own a legacy attempt |
| R13 | Remaining mapped delivery mutations leave legacy rows unchanged |
| R14 | Operational repository/service open on a genuine v25 DB does not migrate it |
| R15 | Operational open on a missing DB does not create it |
| R16 | v26 DB without epoch fails before mutation |
| R17 | Wrong expected epoch fails before mutation |
| R18 | Wrong selected DB / unsupported schema fails before mutation |
| R19 | Existing untagged epoch-specific watch payload fails closed; bytes unchanged |
| R20 | Mismatched watch epoch fails closed |
| R21 | Missing epoch-specific watch file gets a fresh empty current-epoch state without loading canonical legacy watch state |
| R22 | `maybe_send_research_watch_alerts` cannot bypass operational ownership |
| R23 | `load_active_signal_detail` excludes legacy/unattributed state from ACTIVE detail |
| R24 | Admin/draft setup-derived operational send cannot bypass epoch ownership |
| R25 | Genuine baseline-v25 migration preserves legacy rows/NULL membership |
| R26 | Injected v26 failure preserves a valid pre-operational state |
| R27 | Positive synthetic path: fresh producer origin → matching lifecycle → unchanged gates → public intent → outbox claim → fake sender |
| R28 | Global legacy SENT `event_key` remains consumed |
| R29 | Legacy UNCERTAIN remains non-auto-retryable and unchanged |
| R30 | Existing RR, quality, confirmation, target, and economic identity tests remain unchanged and pass in the full suite |

## Remaining gaps (intentionally not this phase)

- Durable source binding / provenance (batch-delivery capture remains in-memory operational isolation, not source provenance completeness)
- Research admission, tracking obligation, canonical outcomes, expectancy
- Champion/challenger, adaptive strategy, execution
- Runtime cutover, epoch initialization on Runtime, capacity refresh

## Test commands

```
python -m pytest tests/test_prospective_runtime_epoch_isolation.py
python -m pytest tests/test_prospective_runtime_epoch_isolation_boundaries.py
python -m pytest
git diff --check
```

Results (DEV PC, `C:\CandleCraftDev`, 2026-09-20):

- Focused epoch/isolation, producer/cache, public/outbox/recovery, admin/watch suites: passed (including T01–T22 and R01–R29).
- `python -m pytest` (`-q --tb=line`): **2578 collected, 2578 passed**, exit 0, elapsed 970477 ms. One unrelated `StarletteDeprecationWarning` from FastAPI's TestClient (`httpx`/`starlette.testclient`). No skips added to hide failures.
- `git diff --check`: clean (exit 0).
- GitHub CI: recorded after push of the repaired HEAD.

Environment: Windows 10, `TELEGRAM_DRY_RUN=true` / `TELEGRAM_SIGNALS_ENABLED=false` / `LOCAL_MANUAL_MODE=true` / `ORDER_EXECUTION_ENABLED=false`. No Runtime filesystem, live exchange, listener, or scanner watch loop.

## Git record

- Old reviewed blocked HEAD: `d616069843a3d27eb6f223bf3f4ea7d2e1afbc00`
- Repair commit SHA: *filled at commit time*
- Final PR HEAD: *filled after push*
