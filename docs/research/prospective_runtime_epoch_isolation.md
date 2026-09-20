# PROSPECTIVE_RUNTIME_EPOCH_ISOLATION

DEV implementation and synthetic proof for one immutable operational Runtime epoch, origin membership, eligibility quarantine of untagged legacy rows, and ownership guards on every mapped setup-derived public-delivery path.

This document is the architecture handoff. It is not a Runtime runbook and does not authorize cutover.

## Disposition

- Status: `READY_FOR_ARCHITECTURE_RE_REVIEW_2`
- `REAL_POSITIVE_PATH_PROVEN` = **TRUE**
- `STRATEGY_GATES_UNCHANGED` = **TRUE**
- PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/123
- Feature branch: `feature/prospective-runtime-epoch-isolation`
- Runtime restart authorized: **false**
- Production cutover authorized: **false**
- Telegram restart authorized: **false**
- Automatic merge authorized: **false**
- Runtime DB access performed: **false**
- Runtime migration/deployment performed: **false**
- Runtime epoch initialization performed: **false**
- Scanner/listener start: **false**
- Real public send: **false**
- Order execution: **false**

This repair does **not** claim research admission, source provenance completeness, tracking completeness, canonical outcome authority, expectancy, profitability, or Runtime restart authorization.

Cohort labels remain only `LEGACY_OR_UNATTRIBUTED` and `CURRENT_EPOCH_OPERATIONAL`. This phase does not introduce or imply `ADMITTED`, `EXPECTANCY_READY`, or `PROFITABLE`.

## Baseline and reviewed heads

- Baseline main SHA: `eef92b89f168bbb016715f3486b9f6bf2824f53f`
- Prior architecture-reviewed PR HEAD (second review): `01cd99f146ea607cdc60650bbddc724af19e5155`
- Verdict on that HEAD: `CHANGES_REQUIRED_CROSS_RECORD_OWNERSHIP_AND_OPERATIONAL_OPEN_BYPASSES`
- Second bounded repair commit: `869154aba3db80ce07b400552845e926d4e12626`

Previous repair (kept, not regressed): origin unspecified default, pre-cutoff cache rejection, BTC origin cannot insert ETH lifecycle, missing lifecycle cannot auto-create a public event, legacy watch payload adoption blocked, genuine-v25 fixture, unchanged strategy/economic gates.

## What remains from the original phase (kept)

One relational operational epoch boundary on the existing SQLite architecture (schema **25 → 26**). Schema migration does not create an epoch. Explicit idempotent `initialize_runtime_epoch` is the only activator, tested only on temporary databases.

New lifecycle rows carry immutable `runtime_epoch_id` and `creation_origin_id`. Legacy rows stay NULL. Identity algorithms are unchanged; a reobserved legacy primary key is a collision, not an overwrite. Current-row uniqueness is split: legacy `(symbol, mode, direction)` where `is_current=1 AND runtime_epoch_id IS NULL`; prospective `(runtime_epoch_id, symbol, mode, direction)` where `is_current=1 AND runtime_epoch_id IS NOT NULL`.

Global `UNIQUE(event_key)` is unchanged. Legacy PENDING/RETRYABLE/IN_FLIGHT/UNCERTAIN cannot be claimed, recovered, or rewritten as operational delivery. Canonical `scan_runs/watch_state.json` remains forensic. Strategy gates (RR, confirmation, quality, targets, economic identity) were not changed.

## Second bounded repair (cross-record ownership and operational open)

### Evaluation-completion authority

A new operational origin requires independent post-epoch evidence for:

1. producer acquisition / observation
2. evaluation completion
3. decision cutoff

`SetupLifecycleService` does **not** fill missing evaluation completion from service processing `now`. `ScannerSymbolResult.evaluation_completed_at` is a non-serialized producer field assigned by `ScannerRunner` when evaluation genuinely completes. Missing evaluation, missing decision cutoff, and missing producer acquisition each fail for their own reason.

Regression: R31, R32.

### Malformed tagged lifecycle

Any current-epoch operational lifecycle mutation requires a complete chain:

lifecycle → creation_origin → registered run → active epoch

plus matching symbol. A row tagged with the active `runtime_epoch_id` but missing `creation_origin_id` fails closed. The row is not repaired, origin-synthesized, untagged, or backfilled.

Regression: R33.

### Public root / predecessor contract

Public event types are explicit:

| Family | Types | Root rule |
| --- | --- | --- |
| Root | `initial_watchlist` (`watchlist` alias) | Must have no predecessor |
| Chain starter | `initial_watchlist`, `setup_triggered`, `signal_confirmed` | May start a chain with no predecessor |
| Follow-up | `setup_triggered`, `signal_confirmed`, `limit_hit`, `tp1_hit`, `tp2_hit`, `tp3_hit`, `sl_hit`, `invalidated`, `expired`, `cooldown`, `no_longer_tracking` | Required root must resolve to an owned chain-starter for the same lifecycle, epoch, symbol, and side |

A root that is missing when required, legacy/unattributed, foreign-epoch, different lifecycle, wrong symbol, wrong side, or incompatible family is rejected before insert. `event_key` is not epoch-prefixed. Global `UNIQUE(event_key)` remains global.

Regression: R34, R35, R36.

### Event / reservation / part / attempt association

Every mapped mutation validates the exact target relationship inside the same transaction before write:

event → reservation attempt → delivery part → delivery attempt/claim → lifecycle → origin → run → epoch

A valid `event_id` does not authorize mutation of a reservation, part, or attempt that belongs to another event.

- `claim`: association is proven before stale recovery
- `record_part_result`, `mark_terminal_without_send`, `mark_uncertain_after_persistence_failure`: part/reservation/attempt must belong to the supplied owned event
- `mark_part_in_flight`: part and claim/attempt must belong to the same owned event
- `persist_intent_parts`: caller-supplied `event_key` must match the fetched event
- `insert_attempt`: operational statuses require an existing owned parent event when a public event key is present; unkeyed rows are not public reservations
- audit-only `blocked`/`skipped` rows cannot become claimable or SENT
- `replace_attempt_with_reservation`: rejects unowned/wrong replacement; unkeyed diagnostic adoption cannot carry SENT/UNCERTAIN/in-flight history
- `compact_repeated_attempt`: mutates only rows tied to the validated owned event key (or explicitly unkeyed audit rows)

Rejected borrowed-current-event attacks leave affected rows unchanged. UNCERTAIN is never blindly retried. Legacy rows are not rewritten to record why they were rejected.

Regression: R37–R47, R59, R60.

### Operational context and generation identity

Operational access is distinct from offline/migration/historical initialized access. Omitted expected identity is an operational configuration error, not permission to initialize or migrate.

`OperationalContext` carries the durable identity (`epoch_id`, cutoff, contract version, reviewed release SHA, generation binding). It is validated once before operational subordinate readers/writers. Repositories do not fall back from `open_repository_database` to `open_initialized_database` when identity is missing.

The same `epoch_id` with a different release or generation binding is rejected before operational mutation.

CLI/Settings-selected identity is passed explicitly into operational services. Repositories do not treat `os.environ["RUNTIME_EPOCH_ID"]` as sufficient authority.

Regression: R48, R49, R51, R52.

### Symbol-health pre-gate

`SetupLifecycleService.apply_to_run_result` opens the operational repository and validates identity **before** symbol-health loading. Health reads use the already-validated connection (`load_symbol_health_records_from_connection`). Operational use cannot independently `open_initialized_database` and migrate a genuine v25 DB.

Regression: R50.

### Watch repeated-preparation

Operational watch preparation derives a stable canonical base from the selected `WATCH_STATE_PATH` parent, unwrapping an existing `epochs/` segment. Repeated preparation in one process does not nest `epochs/<id>/epochs/<id>/`. Strict watch membership validation is unchanged.

Regression: R53. Original B5 data-adoption repair is kept.

### Research-watch absent context

A setup-derived research-watch send requires valid operational ownership. Missing, invalid, or mismatched epoch/context yields **no send**, even when feature flags are enabled and a fake sender is attached. Default-disabled flags are not the ownership boundary. Matching identity plus an owned lifecycle on the candidate remains the only send path (cooldown tests).

Regression: R54. R22 still requires owned lifecycle, not epoch identity alone.

### Admin / draft exact-setup ownership

Operational setup-derived admin/draft/report send binds to the exact owned setup identity in the supplied object (`lifecycle_id` / `setup_id` / `setup_identity`, plus symbol and direction). Symbol-wide authorization is not sufficient. Missing expected epoch/context is denied. Unrelated admin/listener commands are unchanged.

Regression: R55.

### Active detail consistency

`load_active_signal_detail` continues to exclude directly untagged legacy base signals. Attempt fields used for current detail must belong to the same valid owned event/lifecycle chain. Orphan or foreign attempt rows cannot become ACTIVE merely because some associated event passes epoch ownership.

Regression: R56.

### Real producer positive path

R27 remains a narrower **seeded-delivery** proof (owned origin/lifecycle already present, then public event → reservation → claim → fake sender). It is not the full producer pipeline.

R57 is the actual normal production pipeline on a temp/synthetic DB, with a fake deterministic market producer and a fake Telegram sender. No real network. No real Telegram.

Required chain, now proven:

fake deterministic market producer → actual producer acquisition → explicit evaluation-completion evidence → valid post-epoch origin (granted by `SetupLifecycleService`, not stamped by the test) → actual `SetupLifecycleService` path → normal lifecycle creation → existing confirmation cycles (2) → existing quality gates → existing planned RR >= unchanged public minimum **3R** → existing target-integrity requirements (production warning path, not a stubbed A-grade target) → public event reservation (`signal_confirmed`) → correct event/reservation/part association → outbox claim → fake sender call

The test does **not** manually insert `runtime_operational_origins`, does **not** stamp lifecycle epoch ownership, and does **not** seed `ACTIONABLE_A_GRADE` or `CONFIRMED`. Confirmation cycles are the existing service path.

Honest result: a geometry-only synthetic fixture (`_public_min_rr_pullback_candles`) makes the **unchanged** strategy emit planned RR **3.06779661**. After two confirmation cycles the owned lifecycle is `CONFIRMED`, a `signal_confirmed` public event is reserved against that lifecycle/epoch, the outbox is claimed, and `FakeSender` is called once. Public min RR remains `PUBLIC_SIGNAL_MIN_RR = 3`. Strategy configured floor remains `DEFAULT_CONFIGURED_MINIMUM_RR = 2.5`. No scoring, target, confirmation, quality, or identity algorithm was changed.

R58 is the paired negative on the canonical `_strategy_pullback_candles` series: planned RR **2.65955826**, which remains below the unchanged 3R public gate. The setup can still form under the 2.5 strategy floor; public send stays **zero**. That is valid gate behavior, not a bypass.

R27 remains the seeded fake-send path.

Regression: R57, R58.

## Genuine v25 migration proof (kept)

T20 / R25 / R26 / R49 / R50 use `tests/fixtures/genuine_v25.py` (baseline-v25 schema semantics). It is not created by HEAD `initialize_database`.

The v26-specific migration stage has the tested transaction rollback behavior. This document does **not** claim that the entire `initialize_database` routine and `user_version` update are one globally atomic transaction.

No Runtime DB testing. No performance claims from synthetic DBs.

## Persistence authority

| Object | Authority |
| --- | --- |
| Active epoch | `runtime_epoch_control.control_key='active'` → `runtime_epochs` |
| Expected identity | `RuntimeEpochIdentity` (epoch_id + cutoff + contract + release SHA + generation binding) |
| Run lineage | `runtime_operational_runs` before lifecycle/public effects |
| Symbol origin | `runtime_operational_origins` unique `(run_id, symbol)` |
| Lifecycle membership | `setup_lifecycle_records.runtime_epoch_id` + `creation_origin_id` (nullable, immutable, both required when tagged) |
| Public ownership | `public_alert_events.runtime_epoch_id` + `origin_lifecycle_id` + `origin_root_event_id` |
| Operational watch JSON | `scan_runs/epochs/<epoch_id>/watch_state.json` |

Missing expected identity is fatal operational startup. Individual unproved observations are blocked before create/send.

## Acceptance map

Original T01–T22 remain in `tests/test_prospective_runtime_epoch_isolation.py`. Repair regressions R01–R29 remain in `tests/test_prospective_runtime_epoch_isolation_boundaries.py` (`no_auto_epoch`). R30 is the existing RR/quality/confirmation/target/economic-identity suite. R31–R60 are in `tests/test_prospective_runtime_epoch_isolation_repair2.py` (`no_auto_epoch`).

| ID | Proof |
| --- | --- |
| R01–R30 | Prior review-repair regressions, preserved |
| R31 | Missing evaluation-completion blocks even when acquisition and decision cutoff are fresh |
| R32 | Actual producer path supplies evaluation-completion; a genuinely fresh result can qualify |
| R33 | Malformed active-epoch lifecycle missing `creation_origin_id` cannot mutate |
| R34 | Valid lifecycle + legacy root event rejects follow-up insertion |
| R35 | Foreign-epoch / wrong-symbol / wrong-lifecycle root rejects |
| R36 | Required root missing rejects; initial event with correct no-root semantics still works |
| R37 | Owned event + legacy reservation rejects before any mutation |
| R38 | Owned event + reservation belonging to another current event rejects |
| R39 | Owned event + nonexistent reservation rejects |
| R40 | `record_part_result` cannot mutate a reservation belonging to another event |
| R41 | `mark_terminal_without_send` / `mark_uncertain_after_persistence_failure` cannot target a foreign reservation |
| R42 | `mark_part_in_flight` rejects wrong-event attempt/claim association |
| R43 | `persist_intent_parts` rejects `event_id` / `event_key` mismatch |
| R44 | `insert_attempt` operational statuses reject missing/unowned parent event |
| R45 | Audit-only blocked/skipped attempts cannot become claimable or SENT |
| R46 | `replace_attempt_with_reservation` rejects unowned/wrong replacement associations |
| R47 | `compact_repeated_attempt` mutates only rows belonging to the validated owned event |
| R48 | Operational repository with missing expected context does not create a missing DB |
| R49 | Operational repository with missing expected context does not migrate genuine v25 |
| R50 | Lifecycle service health loading against genuine v25 fails before schema mutation |
| R51 | v26 DB with same epoch ID but different release/generation binding is rejected |
| R52 | CLI/Settings-only expected identity propagates without relying on env |
| R53 | Repeated operational watch preparation derives the same stable path, no nesting |
| R54 | Research-watch flags enabled + missing epoch/context = zero fake sender calls |
| R55 | Admin/draft exact-setup mismatch on the same symbol is rejected |
| R56 | Active detail does not combine owned event with orphan/foreign attempt fields |
| R57 | Real producer path reaches fake sender without manual ownership stamping; planned RR 3.06779661 >= unchanged public min 3 |
| R58 | Canonical 2.66R pullback still fails under the unchanged 3R public gate |
| R59 | Global legacy SENT `event_key` remains consumed |
| R60 | Legacy UNCERTAIN and legacy reservation/attempt rows remain unchanged under borrowed-current-event attacks |

## Remaining gaps (intentionally not this phase)

- Durable source binding / provenance (batch-delivery capture remains in-memory operational isolation, not source provenance completeness)
- Research admission, tracking obligation, canonical outcomes, expectancy
- Champion/challenger, adaptive strategy, execution
- Runtime cutover, epoch initialization on Runtime, capacity refresh

## Test commands

```
python -m pytest tests/test_prospective_runtime_epoch_isolation.py
python -m pytest tests/test_prospective_runtime_epoch_isolation_boundaries.py
python -m pytest tests/test_prospective_runtime_epoch_isolation_repair2.py
python -m pytest
git diff --check
```

Results (DEV PC, `C:\CandleCraftDev`, 2026-09-21):

- `test_r57_real_producer_pipeline_creates_owned_public_send`: passed.
- `test_r58_insufficient_public_rr_pipeline_does_not_send`: passed.
- Focused epoch/isolation plus authoritative RR suite (`tests/test_prospective_runtime_epoch_isolation.py`, `tests/test_prospective_runtime_epoch_isolation_boundaries.py`, `tests/test_prospective_runtime_epoch_isolation_repair2.py`, `tests/test_authoritative_minimum_rr.py`): **125 passed**, exit 0, elapsed **16.75 s**.
- `python -m pytest`: **2608 passed**, exit 0, elapsed **415.41 s**. One unrelated `StarletteDeprecationWarning` from FastAPI's TestClient (`httpx`/`starlette.testclient`). No skips added to hide failures.
- `git diff --check`: clean (exit 0).
- GitHub CI: recorded after push of this repair, if the run has completed.

Environment: Windows 10, `TELEGRAM_DRY_RUN=true` / `TELEGRAM_SIGNALS_ENABLED=false` / `LOCAL_MANUAL_MODE=true` / `ORDER_EXECUTION_ENABLED=false`. No Runtime filesystem, live exchange, listener, or scanner watch loop.

## Git record

- Baseline main: `eef92b89f168bbb016715f3486b9f6bf2824f53f`
- Prior reviewed HEAD: `01cd99f146ea607cdc60650bbddc724af19e5155`
- Second bounded repair commit: `869154aba3db80ce07b400552845e926d4e12626`
- Prior PR HEAD before this fixture repair: `1303fec1688c77c4dbf9d02f7d0d0c136d273fef`
- Positive-path fixture commit: `bad6251ef6fb86fdb081d35999a0be45d577d456`
- Final PR #123 HEAD: `b53dd52d28d42001a6614659cf4701c1279b897b`
