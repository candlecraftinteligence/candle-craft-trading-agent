# F04 — Durable Source Replay Foundation

## Phase

F04_DURABLE_SOURCE_REPLAY_FOUNDATION

This phase records one runtime `evaluate_closed_candle_outcomes` invocation in a separate evidence store and replays that call offline against a fresh scratch repository. It does not implement scanner decision replay, expectancy, admission/episode ownership, authenticated venue evidence, fees/execution, adaptive learning, strategy changes, Telegram changes, or Runtime activation.

Capture is off by default. Application schema remains 26. Public quality 88 / grade A / RR 3, F03 population isolation, and P5A owner-monitoring behavior are unchanged.

## Delivery

- Branch: `feature/f04-durable-source-replay-foundation`
- Base SHA: `b7c6422e3d1e2596115b7cd9cf5b1caea15072ef` (merged F03 on `origin/main`; incorporates reviewed F03 head `438624dc5f291d271b4c85be5f83262ea0f40d85`)
- Implementation SHA: `0b44dc41e9dea72495d4f387ca338171731f7936`
- PR #128 corrective repair: see Corrective repair (REQUEST_CHANGES) section
- Draft PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/128
- Final head: see Final head section
- Schema version: remains 26
- Evidence store schema: `cci-durable-source-replay-store-v1` / `STORE_SCHEMA_VERSION = 1`
- Codec: `cci-durable-source-replay-codec-v1`
- Implementation attestation: `cci-durable-source-replay-impl-v2`
- Runtime database: not opened, copied, or modified

Verified: local `main` and `origin/main` both resolve to `b7c6422e3d1e2596115b7cd9cf5b1caea15072ef`, which is an ancestor of this branch. Capture stays off unless `SOURCE_REPLAY_CAPTURE_ENABLED=true` and `SOURCE_REPLAY_EVIDENCE_PATH` are both set.

Contract detail: `docs/research/durable_source_replay_foundation.md`.

## Objective and acceptance question

Can an isolated offline process reproduce one recorded invocation's result and repository effects without fetching replacement candles, guessing lineage, or reading today's Runtime state?

A replay match proves captured computation under the stated support and transaction qualification. It does not prove venue authentication, possession before the decision cutoff, expectancy, or an admitted episode.

## Implementation plan (executed)

1. Inventory production callers of `evaluate_closed_candle_outcomes` on the F03 merge base.
2. Capture at the evaluator seam (after lifecycle transitions; inside the operational transaction), not before the whole service.
3. Separate versioned SQLite evidence store with content-addressed payloads and distinct occurrence rows.
4. Typed codec preserving Decimal / absence / null / empty / order / timestamps.
5. Dependency-closure prestate for evaluator reads and nested repository writes.
6. Sidecar flush after enclosing commit disposition; savepoint and commit status reported separately.
7. Read-only inspect and offline replay CLI against a disposable scratch repository.
8. Adversarial tests for lineage, integrity, transaction, bounds, and default-off nonregression.
9. Focused and full pytest; compileall; draft PR; exact-head CI; this handoff.

## Caller inventory (base `b7c6422`)

Production call sites of `evaluate_closed_candle_outcomes(` after F04:

| Path | Role |
| --- | --- |
| `app/lifecycle/outcomes.py` | Definition |
| `app/lifecycle/service.py` | Passes the live function into `invoke_closed_candle_outcomes` after transitions and handoff association |
| `app/lifecycle/owner_monitoring.py` | Same wrapper for tracking obligations (scanner-supplied and independently fetched candles) |
| `app/research/durable_source_replay/replay.py` | Offline reproduction only |

`record_closed_candle_evidence_gap`, `not_run`, `None`, empty batches, and fetch failures are diagnostics via `note_non_invocation`. They are not manufactured invocations.

Evidence lineage labels:

| Label | Source |
| --- | --- |
| `lifecycle_service_symbol_result` | Scanner-fed service path with optional delivery envelope |
| `scanner_supplied_without_delivery_envelope` | P5A `evidence_from_symbol_results` |
| `fetched_without_delivery_envelope` | P5A `monitor_obligations_with_market_data` |
| `supplied_without_delivery_envelope` | Direct/supplied monitoring evidence |

Missing adapter envelopes stay missing. Equal OHLC does not invent delivery provenance.

## Dependency closure inventory

Snapshot taken immediately before the evaluator, under the existing operational transaction:

| Entity | Capture rule |
| --- | --- |
| `setup_lifecycle_records` | Row for the lifecycle id |
| `setup_lifecycle_outcome_progress` | All rows for that lifecycle, ordered by `id` |
| `setup_lifecycle_events` | All rows for that lifecycle, ordered by `timestamp`, `event_id` |
| `runtime_epoch_control` | Active control row (`control_key = 'active'`) |
| `runtime_epochs` | Epoch named by that control row |
| `runtime_operational_origins` | Origin for the stored `creation_origin_id` without a granted filter |
| `runtime_operational_runs` | Origin's `run_id` and the call's `scan_run_id` when present |

Absence is stored as absence. Replay inserts only these rows. It does not grant an origin, activate an epoch, or bootstrap ownership so a rejection case can pass.

Also captured: call arguments (record, ordered candles, timeframe, decision/evaluated timestamps, scan run id), canonical runtime policy bytes from `build_runtime_evaluation_policy`, available delivery envelope / parent graph / handoff, returned evaluation or exception class/message, ordered semantic repository effects, build identity, implementation fingerprint of evaluator sources, dependency versions, and transaction/savepoint disposition.

## Formats and bounded storage

| Limit | Default |
| --- | --- |
| Payload bytes | 8 MiB |
| Parent depth | 8 |
| Delivery nodes / payload references | 64 |
| Pending captures | 2,000 |
| Store plus WAL | 512 MiB |
| Lock wait | 2,000 ms |
| Candles / events / diagnostics | 5,000 |
| Progress rows | 128 |

One atomic evidence-store `BEGIN IMMEDIATE` writes a bundle. Payload bytes are content-addressed and deduplicated; occurrence rows remain distinct (`cap_` + 32 hex). Exhaustion is a visible research failure. There is no unbounded queue, silent drop, automatic live-data cleanup, or silent evidence deletion.

Paths resolving to `main_live_runtime.sqlite`, `scan_runs/candle_craft.db`, an operational database, a `-wal`/`-shm` suffix, or an unrelated SQLite file are rejected. Enabled capture without a valid path is a misconfiguration counter, not a fallback to the live database.

## Corrective repair (REQUEST_CHANGES on `4c26ac1`)

Independent review disposition REQUEST_CHANGES identified seven acceptance failures that green CI missed. All seven are repaired on the same branch and draft PR #128.

| ID | Repair | Primary functions / modules | Regression test |
| --- | --- | --- | --- |
| F04-R1 | Instrument commit/rollback; discard effects on rollback; close operational connection before sidecar flush | `capture._instrument_connection`, `_assign_disposition`, `repositories.__exit__`, `owner_monitoring` explicit commit note | `test_f04_r1_explicit_rollback_then_empty_commit_is_not_persistence` |
| F04-R2 | Contain identity/env/init failures at every capture entry point | `capture_enabled`, `_load_env_once`, `_remember_identity`, notes, `invoke_closed_candle_outcomes` | `test_f04_r2_identity_init_failure_preserves_operational_evaluation` |
| F04-R3 | Estimate + page/WAL footprint budget for first writes and diagnostics; reject budgets below 48 KiB | `store.write_bundle`, `_estimate_bundle_bytes`, `connection_footprint_bytes` | `test_f04_r3_store_footprint_budget_rejects_first_write_and_diagnostics` |
| F04-R4 | Validate occurrence envelope before inspection/replay success | `replay._validate_envelope`, `_load`, `read_payload` codec check | `test_f04_r4_conflicting_occurrence_metadata_fails_closed` |
| F04-R5 | Strip authority lookup keys like Runtime; preserve raw text; reconstruct without stripping lineage | `prestate.snapshot_dependency_closure`, `reconstruct_record` | `test_f04_r5_padded_authority_lookups_replay` |
| F04-R6 | Expand fingerprint to state_machine/models/trade_plan_integrity/time_contract/reconstruction; attestation v2 | `identity.IMPLEMENTATION_FILES`, `IMPLEMENTATION_ATTESTATION_VERSION` | `test_f04_r6_implementation_fingerprint_covers_state_machine` |
| F04-R7 | Bounded 16-byte header read; LIMIT before fetchall; bounded decode and failure-detail retention | `paths.read_sqlite_header`, `prestate._all` LIMIT, `capture._remember_failure` | `test_f04_r7_bounded_header_reads_and_failure_retention` |

Remaining limitations unchanged: no venue authentication, no possession-before-cutoff, no expectancy/admission, no Runtime capture activation, no historical backfill. Unsupported pre-v2 attestations fail closed on replay.

## Transaction and crash protocol

The sidecar and the operational database do not share a commit.

1. Inside the operational transaction, capture copies bounded inputs, prestate, delivery, policy, result, and effects into memory.
2. Savepoint notes record whether `lifecycle_symbol` or `owner_monitor` was released or rolled back. Nested rollbacks mark ancestry. A released savepoint is not an enclosing commit.
3. Instrumented `connection.commit` / `rollback` observe retaining commits and discarded effects. An empty commit after rollback cannot claim persistence for discarded captures.
4. `SQLiteSetupLifecycleRepository.__exit__` finishes operational commit/rollback and closes before sidecar flush.
5. One evidence-store transaction then writes the bundle. A crash between operational commit and evidence commit loses coverage without a false complete row.
6. Evidence write failures preserve operational results/exceptions/gates/cursors and are counted; they are never a replay pass.

Dispositions: `enclosing_committed`, `enclosing_rolled_back`, `savepoint_rolled_back`, `commit_interrupted`, `commit_unknown`. Standalone calls without an observed enclosing BEGIN stay `commit_unknown`. P5A's explicit commit is observed when an enclosing BEGIN was noted.

## Replay commands and comparison

```text
python -m app.research.durable_source_replay inspect --evidence PATH --capture-id ID
python -m app.research.durable_source_replay replay --evidence PATH --capture-id ID --scratch DIR
```

Scratch must already exist. Replay creates `replay_operational.sqlite` inside it, seeds captured rows, and calls the existing evaluator. It does not migrate the evidence store, fetch candles, send Telegram, open the default operational database, or use StrategyReplay.

| Status | Exit |
| --- | --- |
| `REPLAY_MATCH` / `INSPECTION_OK` | 0 |
| usage | 1 |
| `EVIDENCE_INCOMPLETE` | 2 |
| `EVIDENCE_CORRUPT` | 3 |
| `UNSUPPORTED_FORMAT` | 4 |
| `UNSUPPORTED_IMPLEMENTATION_OR_POLICY` | 5 |
| `REPLAY_MISMATCH` | 6 |

Compared: returned record/progress/transitions/processed-candle count and semantic repository effects. Documented nonsemantic exclusions only: progress `id` / `created_at` / `updated_at`; event `event_id` (pre-existing ids are seeded so timestamp ties keep order). `upsert_outcome_analytics` is a post-evaluator service step and is not an evaluator effect.

Three claims remain separate: computation replay, local delivery provenance, operational persistence. Venue authentication, possession before cutoff, expectancy, and admission/episode claims stay false.

## Lineage and provenance limitations

- Runtime epoch, origin, run, and plan identities are observed, never inferred from symbol/time/geometry.
- A nonblank `N/A` origin is a claimed id, not a blank-origin exception.
- Cross-epoch and missing/conflicting origin/run cases replay as stored rejections; they do not become valid F03 membership.
- Capture counts and replay passes do not enter F03 lifecycle/conversion/TP denominators.
- Scanner 2d closed-subset wrappers are stored as observed object graphs. Cache hit/expiry/unavailable acquisition is not promoted into an acquisition.
- Equal content may share payload bytes and still has distinct occurrence records.
- Executing build identity is separate from the Runtime epoch's recorded release identity. Compatibility proof is implementation fingerprint plus schema/codec/policy bytes, not git SHA alone.

## Adversarial evidence (tests)

`tests/test_f04_durable_source_replay.py` (30 tests) covers the original foundation matrix plus the seven independent counterexamples above.

Related suites kept green: source evidence, delivery capture, evaluation policy, evaluation semantics, lifecycle outcomes, F03 population isolation, P5A owner monitoring, observation-unit caller inventory.

## Local verification (corrective repair)

Focused F04: `python -m pytest tests/test_f04_durable_source_replay.py` — 30 passed.

Related: source-evidence, delivery-capture, policy, semantics, P5A, F03, lifecycle outcomes, observation-unit — all passed.

Full suite: `python -m pytest` — exit 0, ~2802 collected, no failure lines, one Starlette warning, ~495s. `python -m compileall -q app tests` — exit 0.

`LOCAL_MANUAL_MODE=true`, `ORDER_EXECUTION_ENABLED=false`, `TELEGRAM_DRY_RUN=true`, `TELEGRAM_SIGNALS_ENABLED=false`. Synthetic temporary databases only. No listener, watch loop, live Runtime DB access, secrets, weakened assertions, or xfails.

## Strategy and schema non-regression

Verified unchanged:

- `SCHEMA_VERSION = 26`
- `MIN_PUBLIC_SETUP_QUALITY_SCORE = 88`
- `MIN_PUBLIC_SIGNAL_GRADE = "A"`
- `PUBLIC_SIGNAL_MIN_RR = 3`
- `PUBLIC_WATCHLIST_MIN_RR = 3`

No strategy gate, RR, or lifecycle-semantics change. F03 research denominators are untouched. Order execution remains disabled.

## CI

Prior reviewed head `4c26ac18f0917cc2236dfc83b1a8ea971e125583`: run [36896489390](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/36896489390) succeeded but did not catch the seven independent failures.

Corrective exact-head CI is recorded in Final head below after push.

## Rollout and rollback

Default capture stays off. Enabling a writer on Runtime requires Adam's later approval of readers/writers, evidence path, database/WAL/archive footprint, free-space trend, the finite budget above, retention/restore, and rollback. DEV synthetic measurements are not that approval.

Rollback is to stop optional capture. That does not change application schema 26 and does not delete the evidence store. Readers refuse unknown store/codec/attestation versions and do not migrate them. No historical provenance backfill. No old-reader/new-writer compatibility assumption.

## Deferred

Full scanner discovery/confirmation decision replay and HTF/context inputs; authenticated venue evidence; historical as-run possession reconstruction; admission/episode ownership and coverage reconciliation; expectancy proof; fees/funding/slippage or paper execution; adaptive learning; strategy/regime changes; Telegram changes; disk cleanup/backfill; Runtime activation.

## Final head

- Final head SHA: pending corrective push
- Exact-head CI run: pending
- Reviewed-against head: `4c26ac18f0917cc2236dfc83b1a8ea971e125583`
- Local focused F04: 30 passed
- Related suites: passed
- Full pytest / compileall: pending
