# PROSPECTIVE_RUNTIME_EPOCH_ISOLATION

DEV implementation and synthetic proof for one immutable operational Runtime epoch, origin membership, eligibility quarantine of untagged legacy rows, and ownership guards on every mapped setup-derived public-delivery path.

This document is the architecture handoff. It is not a Runtime runbook and does not authorize cutover.

## Disposition

- Status: `READY_FOR_ARCHITECTURE_RE_REVIEW_4`
- `ACTIVE_DETAIL_EXACT_PROVENANCE` = **TRUE**
- `LEGACY_AUDIT_EVIDENCE_FROZEN` = **TRUE**
- `PROSPECTIVE_AUDIT_COMPACTION_ONLY` = **TRUE**
- `CANONICAL_RESERVATION_SEMANTICS_BOUND` = **TRUE**
- `GENUINE_V25_V26_SCHEMA_PARITY` = **TRUE**
- `PRODUCTION_ORCHESTRATION_REGISTRATION_PROVEN` = **TRUE**
- `REAL_ORCHESTRATED_POSITIVE_PATH_PROVEN` = **TRUE**
- `RUN_REGISTRATION_BEFORE_PRODUCTION` = **TRUE**
- `CANONICAL_RESERVATION_AUTHORITY` = **TRUE**
- `AUDIT_ATTEMPTS_NON_OPERATIONAL` = **TRUE**
- `PUBLIC_DIRECTION_BOUND_TO_LIFECYCLE` = **TRUE**
- `ADMIN_ROUTE_FAIL_CLOSED` = **TRUE**
- `ACTIVE_DETAIL_OWNERSHIP_BOUND` = **TRUE**
- `REAL_DELAYED_POSITIVE_PATH_PROVEN` = **TRUE**
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
- Architecture-reviewed PR HEAD before this fourth repair: `fa2c8b99436db2bc48368fc42d4470797a29db58`
- Verdict on that HEAD: `CHANGES_REQUIRED_PUBLIC_AUTHORITY_AND_V25_MIGRATION`
- Prior architecture-reviewed PR HEAD (third review): `3cdb78e0489d7dda298ec841f4559e0778a68637`
- Verdict on that earlier HEAD: `CHANGES_REQUIRED_RUN_TIMING_AND_PUBLIC_AUTHORITY_BYPASSES`
- Prior architecture-reviewed PR HEAD (second review): `01cd99f146ea607cdc60650bbddc724af19e5155`
- Verdict on that earlier HEAD: `CHANGES_REQUIRED_CROSS_RECORD_OWNERSHIP_AND_OPERATIONAL_OPEN_BYPASSES`

Previous repairs (kept, not regressed): origin unspecified default, pre-cutoff cache rejection, BTC origin cannot insert ETH lifecycle, missing lifecycle cannot auto-create a public event, legacy watch payload adoption blocked, genuine-v25 fixture, unchanged strategy/economic gates, run registration before acquisition, canonical reservation pointer, audit non-claimable, public side bound to lifecycle.

## Fourth bounded repair (public authority and v25 migration)

This repair does **not** weaken strategy gates, rewrite legacy audit evidence, backfill canonical reservation, drop SQLite UNIQUE autoindexes with `DROP INDEX`, remap attempt IDs, or treat R57/R85 as the sole production-orchestration proof.

### ACTIVE-detail exact provenance

Operational ACTIVE list/detail starts from the already-owned public event:

`public_alert_event` → `origin_lifecycle_id` → owned current-epoch lifecycle → `canonical_reservation_attempt_id` → exact originating plan/setup/run relationships.

ACTIVE grouping is by `origin_lifecycle_id`, not by `event_key` or `signal_id`. Follow-up rows (`limit_hit`, `tp1_hit`, …) stay on the same owned chain as `signal_confirmed`.

Operational enrichment for quality, rationale, invalidation, confirmation facts, gate text, and plan/setup fields is allowed only from records demonstrably on that chain:

- canonical SENT reservation attempt for the owned event
- `attempt.scan_run_id` and the lifecycle creation-origin run
- later `symbol_results` only when `raw_result.lifecycle_id` matches the owned lifecycle, or the result `run_id` is one of those owned runs
- `setup_candidates` only when `run_id` is in the owned-run set

Latest same-symbol result, opposite-direction result, different-setup result, legacy/unattributed result, and foreign candidate/plan are omitted rather than borrowed. Historical/diagnostic watchlist screens retain broader lookup. Missing enrichment is omitted; it is not invented.

Regression: R91–R96.

### Legacy audit freeze / prospective compaction authority

`compact_repeated_attempt` may mutate a target row only when **both** the incoming observation and the target row prove current-epoch run lineage:

`last_scan_run_id` then `scan_run_id` → `runtime_operational_runs` → active Runtime epoch.

Shared `event_key`, shared `signal_id`, shared `alert_type`, wall-clock time, NULL/`N/A` public event key, and absence of a canonical pointer are not prospective authority. Unkeyed audit rows follow the same freeze rule.

If a current diagnostic matches a legacy/unattributed blocked or skipped row, the legacy snapshot is left unchanged and a new prospective diagnostic row is inserted when the schema permits it.

Prospective compaction of two current-epoch diagnostic rows may remain. Canonical reservation is excluded. Compaction never promotes audit status into operational authority.

Regression: R97–R102.

### Canonical reservation semantic contract

`canonical_reservation_attempt_id` names which row is operational. It does not prove the row's content matches the owning event/lifecycle/plan.

`public_reservation_record_mismatch_reason` validates before insert, replace, claim, part delivery, SENT finalization, and terminal/uncertain update:

- event key, symbol, direction, canonical plan id
- all alert-type representations (`public_alert_event_type`, `alert_type`, `attempted_alert_type`) against `event.event_type` via existing family normalization
- economic geometry against event + lifecycle using `canonical_stored_price` / existing `_identity_price` (entry zone, stop, TP1/TP2/TP3, planned RR where persisted)
- setup_id / plan_version_id when both sides persist them

N/A on either side is not treated as a conflict. Rejected replacement/insert leaves event, canonical pointer, parts, and attempt state unchanged. A malformed persisted canonical row cannot be claimed merely because its id is canonical.

Regression: R103–R107.

### Genuine v25 → v26 attempt-table schema parity

A genuine v25 `telegram_alert_attempts` table has table-level `UNIQUE(signal_id, alert_type)` (SQLite autoindex, `origin='u'`). That autoindex cannot be removed with ordinary `DROP INDEX`.

When `_telegram_alert_attempts_has_unconditional_signal_alert_unique` is true, `_migrate_runtime_epoch_isolation_v26` rebuilds the table inside the same v26 `BEGIN IMMEDIATE` transaction:

1. rename `telegram_alert_attempts` → `telegram_alert_attempts_pre_v26_unique`
2. recreate from the original `CREATE TABLE` SQL with the unconditional UNIQUE clause stripped
3. copy every row through `_copy_telegram_alert_attempts_pre_v26_unique_rows` (preserves `id` and all common columns; tests may inject failure here)
4. restore `sqlite_sequence` to `max(preserved_seq, MAX(id))`
5. drop the pre-v26 table
6. recreate any table triggers
7. `_ensure_telegram_alert_attempt_indexes` restores the intended partial unique index `ux_telegram_alert_attempts_signal_alert_operational` excluding audit statuses

Attempt IDs are not remapped. Legacy values are not rewritten, merged, or backfilled into canonical authority. Fresh v26 and migrated v25 then allow audit + canonical coexistence under the same partial uniqueness. Global `UNIQUE(event_key)` and canonical reservation uniqueness are unchanged.

**Transaction boundary:** the rebuild is inside `_migrate_runtime_epoch_isolation_v26`'s `BEGIN IMMEDIATE`. Failure rolls back **that v26 transaction**. This document does **not** claim that the entire `initialize_database` routine and `user_version` update are one globally atomic transaction. Injected failure during the copy step leaves schema version 25, original rows/IDs, the unconditional UNIQUE, no active epoch, and a retryable `open_initialized_database`. Repeated migration is idempotent: no obsolete unique, indexes remain, row values/IDs unchanged, no epoch auto-created.

Regression: R108–R114.

### Production orchestration proof

R57/R85 remain **component** integration evidence: those tests still call `register_operational_scan_run` themselves. They are not the sole production-orchestration proof.

R115–R118 invoke the actual `scripts.run_scan.main` path that owns `_register_operational_scan_run_before_acquisition` → `_run_scanner`. The tests do not call `register_operational_scan_run` and do not stub the registration function into a fake success. Neutralizing `_register_operational_scan_run_before_acquisition` fails closed before origin/lifecycle/send.

Proven on temp DB with fake market adapter and fake Telegram sender: T0 (production `runtime_operational_runs` commit) < T1 < T2 (producer acquisition) < T3 (lifecycle consumption); both symbol origins belong to the production-created run; valid ≥3R geometry reaches the fake sender.

Regression: R115–R122.

## Third bounded repair (run timing and public authority)

This repair does **not** backdate `registered_at`, weaken `evaluation_completed_at >= registered_at`, or treat `event_key` as reservation authority.

### Run registration owner and timing

Operational owner is `scripts/run_scan.py` via `_register_operational_scan_run_before_acquisition` → `register_operational_scan_run`.

Contract:

1. Validate `RuntimeEpochIdentity`.
2. Persist `runtime_operational_runs` at T0.
3. Then live symbol acquisition / evaluation (T1, T2, …).
4. `ScannerSymbolResult` producer evidence is generated after registration.
5. `SetupLifecycleService.apply_to_run_result` **validates** the already-registered `run_id` with `require_registered_operational_run`. It does **not** insert or backdate registration.

Missing persisted registration fails closed. Processing latency (T3 > T2 > T1 > T0) does not invalidate earlier symbols. Cached/resumed/replayed/imported results remain blocked.

### Canonical reservation authority

Schema v26 additive field: `public_alert_events.canonical_reservation_attempt_id` (nullable INTEGER, unique where NOT NULL). Legacy/historical rows stay NULL. No backfill. Global `UNIQUE(event_key)` is unchanged.

Exactly one canonical operational reservation per public event. `insert_attempt` binds the canonical id only for operational keyed inserts against an owned parent. Claim, stale recovery, `record_part_result`, `mark_terminal_without_send`, `mark_uncertain_after_persistence_failure`, `mark_public_watchlist_reservation_result`, and `mark_part_in_flight` prove `event → canonical reservation` inside the transaction before mutation. Caller-supplied `reservation_id` must equal the persisted canonical id. Rejected calls leave rows unchanged.

### Audit-only attempt contract

Audit intent is explicit (`audit_only=True`) or inferred from telegram status in `{blocked, skipped, failed}`. Audit inserts:

- never bind canonical reservation
- never require operational parent mutation
- cannot be SENT / PENDING / RETRYABLE / IN_FLIGHT / UNCERTAIN
- strip operational `delivery_state` to `N/A`
- are not claim, recovery, or part-delivery targets

Unkeyed operational `insert_attempt` remains limited to the existing non-public research-watch exception. Other unkeyed operational statuses are rejected. Compaction may summarize diagnostic blocked/skipped history; it excludes the canonical reservation id and does not require operational mutation of a parent event.

Historical SENT watchlists may still receive diagnostic expiry / identity / follow-up-suppression rows. Those rows do not authorize operational send.

### Stale recovery

`recover_stale_in_flight` updates only the canonical reservation / active claim of the owned target event. Same-`event_key` audit history is untouched. The SQL predicate is not a broad event-key promotion.

### Direction / event-family / lifecycle binding

Public ownership requires `event.symbol == lifecycle.symbol` and `event.side == lifecycle.direction` under existing normalization. A LONG lifecycle cannot authorize a SHORT root. Child/root sides matching each other cannot hide a contradiction with the owning lifecycle. `replace_attempt_with_reservation` rejects same-key replacements with opposite direction, incompatible alert/event family, or mismatched plan/setup relation.

### Admin route

`route_admin_scan_report` passes `database_path` and `expected_identity` through formatter, drafts, and sender. Missing database or identity is **not** permission to recommend. `_operational_setup_recommendation_allowed` must succeed for any current/valid/tradable setup line. Exact owned setup identity (lifecycle / setup / symbol / direction) is required. An owned BTC LONG setup cannot authorize a BTC SHORT setup.

### ACTIVE detail ownership chain

Operational ACTIVE list/detail:

1. validate the public event ownership chain
2. resolve lifecycle from `public_event.origin_lifecycle_id`
3. require current-epoch owned lifecycle
4. require the attempt to be the event's canonical reservation (not a random same-key audit/foreign attempt)
5. require attempt symbol/direction/plan to agree with that chain
6. source candidate/detail fields only from that chain (owned runs / matching `lifecycle_id`)

No latest-same-symbol, fuzzy symbol/direction/geometry, or foreign-candidate fallback for ACTIVE operational output. See the fourth-repair provenance contract above.

### Delayed positive path and multi-symbol timing

R57/R85 remain component proofs with advancing clocks (those tests register the operational run themselves). R115–R117 prove the same T0 < T1 < T2 < T3 timeline through actual `scripts.run_scan.main`. R63/R86 cover one run, two symbols, later lifecycle processing, both origins remaining eligible. R58/R87/R119 keep planned RR **2.65955826** blocked by unchanged public min **3R**.

Regression: R61–R90.

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
- `insert_attempt`: operational statuses require an existing owned parent event when a public event key is present; unkeyed rows are not public reservations except the existing non-public research-watch exception
- audit-only `blocked`/`skipped`/`failed` rows cannot become claimable or SENT; they do not occupy `(signal_id, alert_type)` operational uniqueness
- table-level `UNIQUE(signal_id, alert_type)` is replaced by partial unique index `ux_telegram_alert_attempts_signal_alert_operational` excluding audit statuses (v26, no historical rewrite)
- `replace_attempt_with_reservation`: rejects unowned/wrong replacement; unkeyed diagnostic adoption cannot carry SENT/UNCERTAIN/in-flight history
- `compact_repeated_attempt`: mutates only matching diagnostic `blocked`/`skipped` history; never the canonical reservation. Audit inserts compact matching history before creating another diagnostic row.

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

R57/R85 remain **component** integration on a temp/synthetic DB: they call `register_operational_scan_run` from the test, then run `ScannerRunner` with a fake deterministic market producer and a fake Telegram sender. No real network. No real Telegram. They prove the producer/lifecycle/public path after registration exists. They do **not** prove that `scripts/run_scan.py` itself is the registration owner.

R115–R117 are the production-orchestration proof: they invoke `scripts.run_scan.main` (or the one-shot/watch iteration that contains `_register_operational_scan_run_before_acquisition` → `_run_scanner`). Those tests do not register the run themselves and do not monkeypatch registration into a fake success.

Required chain, now proven (component R57/R85 and orchestrated R115–R117):

fake deterministic market producer → actual producer acquisition → explicit evaluation-completion evidence → valid post-epoch origin (granted by `SetupLifecycleService`, not stamped by the test) → actual `SetupLifecycleService` path → normal lifecycle creation → existing confirmation cycles (2) → existing quality gates → existing planned RR >= unchanged public minimum **3R** → existing target-integrity requirements (production warning path, not a stubbed A-grade target) → public event reservation (`signal_confirmed`) → correct event/reservation/part association → outbox claim → fake sender call

The tests do **not** manually insert `runtime_operational_origins`, do **not** stamp lifecycle epoch ownership, and do **not** seed `ACTIONABLE_A_GRADE` or `CONFIRMED`. Confirmation cycles are the existing service path. Production orchestration (R115) persists `runtime_operational_runs` **before** producer evaluation; lifecycle consumption is later and does not invent `registered_at`.

Honest result: a geometry-only synthetic fixture (`_public_min_rr_pullback_candles`) makes the **unchanged** strategy emit planned RR **3.06779661**. After two confirmation cycles the owned lifecycle is `CONFIRMED`, a `signal_confirmed` public event is reserved against that lifecycle/epoch, the outbox is claimed, and `FakeSender` is called once. Public min RR remains `PUBLIC_SIGNAL_MIN_RR = 3`. Strategy configured floor remains `DEFAULT_CONFIGURED_MINIMUM_RR = 2.5`. No scoring, target, confirmation, quality, or identity algorithm was changed.

R58 is the paired negative on the canonical `_strategy_pullback_candles` series: planned RR **2.65955826**, which remains below the unchanged 3R public gate. The setup can still form under the 2.5 strategy floor; public send stays **zero**. That is valid gate behavior, not a bypass.

R27 remains the seeded fake-send path.

Regression: R57, R58.

## Genuine v25 migration proof (kept)

T20 / R25 / R26 / R49 / R50 use `tests/fixtures/genuine_v25.py` (baseline-v25 schema semantics). It is not created by HEAD `initialize_database`.

The v26-specific migration stage has the tested transaction rollback behavior, including lossless rebuild of `telegram_alert_attempts` when the genuine-v25 table-level `UNIQUE(signal_id, alert_type)` autoindex is present. Injected copy-step failure rolls back that v26 `BEGIN IMMEDIATE` transaction (schema remains 25; rows/IDs preserved; retryable). This document does **not** claim that the entire `initialize_database` routine and `user_version` update are one globally atomic transaction.

No Runtime DB testing. No performance claims from synthetic DBs.

## Persistence authority

| Object | Authority |
| --- | --- |
| Active epoch | `runtime_epoch_control.control_key='active'` → `runtime_epochs` |
| Expected identity | `RuntimeEpochIdentity` (epoch_id + cutoff + contract + release SHA + generation binding) |
| Run lineage | `runtime_operational_runs` before lifecycle/public effects |
| Symbol origin | `runtime_operational_origins` unique `(run_id, symbol)` |
| Lifecycle membership | `setup_lifecycle_records.runtime_epoch_id` + `creation_origin_id` (nullable, immutable, both required when tagged) |
| Public ownership | `public_alert_events.runtime_epoch_id` + `origin_lifecycle_id` + `origin_root_event_id` + side/direction bound to lifecycle |
| Canonical reservation | `public_alert_events.canonical_reservation_attempt_id` (NULL for legacy; unique where set; no backfill) |
| Operational watch JSON | `scan_runs/epochs/<epoch_id>/watch_state.json` |

Missing expected identity is fatal operational startup. Individual unproved observations are blocked before create/send.

## Acceptance map

Original T01–T22 remain in `tests/test_prospective_runtime_epoch_isolation.py`. Repair regressions R01–R29 remain in `tests/test_prospective_runtime_epoch_isolation_boundaries.py` (`no_auto_epoch`). R30 is the existing RR/quality/confirmation/target/economic-identity suite. R31–R60 are in `tests/test_prospective_runtime_epoch_isolation_repair2.py` (`no_auto_epoch`). R61–R90 are in `tests/test_prospective_runtime_epoch_isolation_repair3.py` (`no_auto_epoch`). R91–R122 are in `tests/test_prospective_runtime_epoch_isolation_repair4.py` (`no_auto_epoch`).

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
| R57 | Component producer path reaches fake sender without manual ownership stamping; planned RR 3.06779661 >= unchanged public min 3. Test still calls `register_operational_scan_run`; not the sole production-orchestration proof. |
| R58 | Canonical 2.66R pullback still fails under the unchanged 3R public gate |
| R59 | Global legacy SENT `event_key` remains consumed |
| R60 | Legacy UNCERTAIN and legacy reservation/attempt rows remain unchanged under borrowed-current-event attacks |
| R61 | Operational run is registered before producer acquisition/evaluation |
| R62 | Fresh evaluation at T1 consumed at T2>T1 remains eligible because registration occurred at T0<T1 |
| R63 | Multi-symbol run with later lifecycle processing does not reject earlier symbols |
| R64 | Missing persisted run registration blocks lifecycle consumption; service does not create/backdate it |
| R65 | Keyed blocked/skipped audit attempt cannot be used as canonical reservation |
| R66 | Owned event + keyed audit passed to claim = reject, unchanged rows |
| R67 | Stale recovery updates only the canonical reservation; keyed audit unchanged |
| R68 | `record_part_result` rejects audit/noncanonical attempt for an owned event |
| R69 | `mark_terminal_without_send` rejects audit/noncanonical attempt |
| R70 | `mark_uncertain_after_persistence_failure` rejects audit/noncanonical attempt |
| R71 | `mark_public_watchlist_reservation_result` rejects audit/noncanonical attempt |
| R72 | `replace_attempt_with_reservation` rejects same-event-key opposite direction |
| R73 | Replacement rejects incompatible alert/event family or plan/setup relation |
| R74 | LONG lifecycle cannot create/authorize SHORT root event |
| R75 | Child/root both SHORT cannot bypass LONG lifecycle ownership |
| R76 | Operational `insert_attempt` with no owned parent cannot create SENT |
| R77 | Operational `insert_attempt` with no owned parent cannot create PENDING/RETRYABLE/IN_FLIGHT/UNCERTAIN |
| R78 | Explicit audit-only insertion cannot later be claimed or SENT |
| R79 | Admin route with database/context absent cannot send a current Valid Setups recommendation |
| R80 | Admin route with valid context but different same-symbol setup identity/direction cannot recommend |
| R81 | Valid admin operational recommendation still works for the exact owned setup (fake transport) |
| R82 | ACTIVE detail with owned event + mismatched attempt direction = no ACTIVE detail |
| R83 | ACTIVE detail with owned event + attempt pointing to unrelated lifecycle/plan = no ACTIVE detail |
| R84 | ACTIVE detail resolves lifecycle from `event.origin_lifecycle_id`, not a fuzzy fallback |
| R85 | R57 component positive path succeeds with advancing producer/consumer clock (not production `run_scan.main`) |
| R86 | Real multi-symbol producer/lifecycle integration preserves valid earlier symbol origins |
| R87 | Canonical ~2.66R negative remains blocked by unchanged 3R public gate |
| R88 | Legacy SENT `event_key` remains globally consumed; event row frozen |
| R89 | Legacy UNCERTAIN remains untouched/non-auto-retryable |
| R90 | Strategy/economic regression suite remains unchanged |
| R91 | Positive ACTIVE detail exists from the exact owned chain; detail is not None |
| R92 | Later opposite-direction same-symbol raw result cannot change ACTIVE operational fields |
| R93 | Later different-setup same-symbol result cannot change ACTIVE operational fields |
| R94 | Legacy/unattributed same-symbol result cannot enrich ACTIVE detail |
| R95 | Foreign same-symbol candidate cannot enrich ACTIVE detail |
| R96 | ACTIVE detail uses the event's exact canonical reservation attempt |
| R97 | Keyed legacy blocked audit snapshot unchanged after a new current diagnostic |
| R98 | Keyed legacy skipped audit snapshot unchanged |
| R99 | Unkeyed legacy blocked audit snapshot unchanged |
| R100 | Unkeyed legacy skipped audit snapshot unchanged |
| R101 | Current-epoch audit compaction still works only against current-epoch audit state |
| R102 | Canonical reservation is never compacted as audit |
| R103 | Replacement with contradictory actual `alert_type` is rejected; snapshots unchanged |
| R104 | Replacement with contradictory `attempted_alert_type` is rejected |
| R105 | Replacement with contradictory entry/stop/targets is rejected |
| R106 | Valid semantically equivalent replacement still succeeds; claim/result still works |
| R107 | Malformed canonical reservation cannot be claimed merely because its ID is canonical |
| R108 | Genuine v25→v26: canonical-first then same-signal/type audit both persist |
| R109 | Genuine v25→v26: audit-first then canonical both persist; canonical is bound |
| R110 | Fresh v26 has equivalent coexistence behavior to migrated v25 |
| R111 | Migrated v25 has no obsolete unconditional `UNIQUE(signal_id, alert_type)` |
| R112 | Repeated migration is semantically idempotent (schema, indexes, row values/IDs, no epoch) |
| R113 | Injected v26 attempt-table rebuild failure preserves rows/IDs and retryability; v26 `BEGIN IMMEDIATE` boundary |
| R114 | Post-migration `PRAGMA foreign_key_check` / integrity check pass; intended partial index present |
| R115 | Actual `scripts.run_scan.main` registers the operational run before acquisition; test does not register it |
| R116 | Actual production orchestration multi-symbol timeline satisfies T0 < T1 < T2 < T3; both origins granted |
| R117 | Actual production orchestration valid ≥3R path reaches the fake sender |
| R118 | Removing/neutralizing production registration fails closed before origin/lifecycle/send |
| R119 | ~2.65955826R remains blocked by unchanged public minimum 3R |
| R120 | Legacy SENT global event-key consumption remains intact |
| R121 | Legacy UNCERTAIN remains frozen/non-auto-retryable |
| R122 | Full strategy/economic regression remains unchanged |

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
python -m pytest tests/test_prospective_runtime_epoch_isolation_repair3.py
python -m pytest tests/test_prospective_runtime_epoch_isolation_repair4.py
python -m pytest
git diff --check
```

Results (DEV PC, `C:\CandleCraftDev`, 2026-09-21, fourth bounded repair):

- `python -m pytest`: **2670 passed**, 0 failed, 1 warning (`StarletteDeprecationWarning` from FastAPI/Starlette `TestClient`), **488.34s** (0:08:08), **exit 0**
- `git diff --check`: **clean** (exit 0)
- GitHub CI: recorded after push of this repair, if the run has completed.

Environment: Windows 10, `TELEGRAM_DRY_RUN=true` / `TELEGRAM_SIGNALS_ENABLED=false` / `LOCAL_MANUAL_MODE=true` / `ORDER_EXECUTION_ENABLED=false`. No Runtime filesystem, live exchange, listener, or scanner watch loop.

## Git record

- Baseline main: `eef92b89f168bbb016715f3486b9f6bf2824f53f`
- Architecture-reviewed PR HEAD before this fourth repair: `fa2c8b99436db2bc48368fc42d4470797a29db58`
- Verdict on that HEAD: `CHANGES_REQUIRED_PUBLIC_AUTHORITY_AND_V25_MIGRATION`
- Fourth bounded repair commit: pending pin after this commit
- Final PR #123 HEAD: pending pin after push
