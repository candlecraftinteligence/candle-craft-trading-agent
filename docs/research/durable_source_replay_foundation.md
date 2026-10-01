# F04 — Durable source replay foundation

**Status:** Optional capture and offline invocation replay. Capture is off by default. This document is the implementation contract. It is not Runtime activation, admission, or expectancy.

Application schema stays **26**. The evidence store uses its own schema version **1** (`cci-durable-source-replay-store-v1`) and codec `cci-durable-source-replay-codec-v1`.

## 1. What one capture is

A capture occurrence (`cap_` plus 32 hex characters) identifies one call to `evaluate_closed_candle_outcomes`. It is not a source authority, trade, opportunity, admission, or episode id. Identical candle bytes and policy bytes may share stored payloads and still have distinct occurrence rows.

Production callers on this base:

| Caller | Envelope |
| --- | --- |
| `app.lifecycle.service._apply_to_symbol_result_with_meta` | The symbol result's delivery and the handoff associated immediately before the call |
| `app.lifecycle.owner_monitoring.monitor_tracking_obligations` | Scanner-supplied candles without a delivery envelope, or candles fetched by `monitor_obligations_with_market_data` (`fetched_without_delivery_envelope`) |

`record_closed_candle_evidence_gap`, `not_run`, `None`, an empty batch, and a fetch failure are diagnostics. They are not manufactured invocations.

The dependency snapshot, taken inside the operational transaction immediately before the evaluator, includes the lifecycle row, outcome progress ordered by `id`, events ordered by timestamp then `event_id`, the active runtime-epoch control row and that epoch, the origin row for the stored `creation_origin_id` without a granted filter, and the origin run plus the call's scan run. Raw lineage strings are preserved. Lookup keys are stripped exactly as Runtime ownership strips them. Absence is stored as absence. Replay inserts only those rows. It does not grant an origin or activate an epoch.

## 2. Three claims

| Claim | Report field | When it is true |
| --- | --- | --- |
| Computation reproduction | `claims.computation_replay` | Replay status is `REPLAY_MATCH` |
| Local delivery provenance | `claims.local_delivery_provenance` | The stored envelope is a complete `CandleBatchDelivery` with `lineage_complete` and a matched batch handoff. Truncation, a missing owner envelope, or a mismatched handoff stays false |
| Operational persistence | `claims.operational_persistence` | Replay matches and `transaction_status` is `enclosing_committed` |

`authenticated_venue_evidence`, `possession_before_decision_cutoff`, `expectancy`, and `admission_or_episode` are always false. A match of a rolled-back or commit-unknown computation keeps that transaction status.

## 3. Transaction and crash protocol

The sidecar and the operational database do not share a commit.

1. Inside the operational transaction, capture copies bounded call inputs, prestate, delivery, policy, result, and effects into memory.
2. Savepoint notes record whether `lifecycle_symbol` or `owner_monitor` was released or rolled back. Nested savepoint rollbacks mark ancestry. A released savepoint is not an enclosing commit.
3. Commit and rollback on the instrumented operational connection are observed. An explicit rollback discards pending effects. A later empty commit cannot upgrade those discarded effects to `enclosing_committed`.
4. `SQLiteSetupLifecycleRepository.__exit__` finishes operational commit/rollback and closes the connection before any evidence-store write. Sidecar I/O never runs while `connection.in_transaction` is true.
5. One evidence-store `BEGIN IMMEDIATE` then writes the bundle. A crash after the operational commit and before that evidence commit loses coverage. It does not leave a complete capture row.
6. An evidence write that raises rolls back the evidence transaction. The operational result, exception, and cursor stay as they were. The failure is counted. It is not a replay pass.

If no enclosing `BEGIN` was observed, the disposition is `commit_unknown`, including when a later repository `commit()` succeeds. P5A's explicit `connection.commit()` is observed as a retaining commit when an enclosing BEGIN was noted. `commit_interrupted` is a failed operational commit. `savepoint_rolled_back` wins over a later enclosing commit.

Capture initialization, environment loading, identity/fingerprint setup, notes, and invocation wrappers contain their own failures. An optional capture init error never aborts the original evaluation.

## 4. Commands

```text
python -m app.research.durable_source_replay inspect --evidence PATH --capture-id ID
python -m app.research.durable_source_replay replay --evidence PATH --capture-id ID --scratch DIR
```

The scratch directory must already exist. Replay creates `replay_operational.sqlite` inside it, seeds captured rows, and calls the existing evaluator. It does not migrate the evidence store, fetch candles, send Telegram, or open the default operational database.

| Status | Exit |
| --- | --- |
| `REPLAY_MATCH` / `INSPECTION_OK` | 0 |
| usage, including a missing scratch directory | 1 |
| `EVIDENCE_INCOMPLETE` | 2 |
| `EVIDENCE_CORRUPT` | 3 |
| `UNSUPPORTED_FORMAT` | 4 |
| `UNSUPPORTED_IMPLEMENTATION_OR_POLICY` | 5 |
| `REPLAY_MISMATCH` | 6 |

Exact replay requires implementation attestation `cci-durable-source-replay-impl-v2` (expanded semantic closure including `state_machine`, models, trade-plan integrity, Runtime time-contract, and reconstruction modules), the captured implementation fingerprint of that file set, captured Python/SQLite/Pydantic versions, application schema 26, codec v1, and canonical runtime policy bytes from `build_runtime_evaluation_policy`. Unsupported older attestations fail closed. Git SHA and a dirty tree are recorded. They are not the compatibility proof. Trade-simulation policy is a different family.

Inspection and replay validate the occurrence envelope before success: store/codec versions, status vocabulary, transaction/savepoint consistency, reference count/roles/ordinals, policy header versus canonical policy payload, and caller/lineage bindings. Conflicting metadata cannot manufacture `operational_persistence`.

`app/research/durable_source_replay/capture.py` and `replay.py` are the only production-tree readers of that existing manifest. The outcome evaluator, lifecycle service, owner monitor, and scanner do not import it, and the captured policy does not change the evaluator's decision.

## 5. Comparison exclusions

Progress comparison drops `id`, `created_at`, and `updated_at`, then sorts by `plan_identity`. Event comparison drops `event_id` and keeps timestamp order plus the captured ordinal. Seeding restores the original `event_id` and progress `id` so timestamp ties keep read order. The evaluator does not read those storage clocks or surrogate keys. `upsert_outcome_analytics` is a service step after the evaluator and is not an evaluator effect.

## 6. Bounds

| Limit | Default |
| --- | --- |
| Payload bytes | 8 MiB |
| Parent depth | 8 |
| Delivery nodes / payload references | 64 |
| Pending captures | 2,000 |
| Store plus WAL | 512 MiB |
| Minimum usable store budget | 48 KiB |
| Lock wait | 2,000 ms |
| Candles / events / diagnostics | 5,000 |
| Progress rows | 128 |
| Failure detail rows retained | 256 |
| Decode bytes per load | 8 MiB |

Store writes estimate schema/row/index overhead and check page-count plus WAL footprint before accepting a bundle, including first writes and diagnostic-only appends. Budgets below the minimum usable size are rejected. Exhaustion is a visible failure. There is no unbounded queue, silent drop, or automatic deletion of ordinary retained evidence. Header probes read sixteen bytes only. Event/progress queries use `LIMIT` before materialization. Failure counters stay aggregate; detail retention is truncated.

Paths that resolve to `main_live_runtime.sqlite`, `scan_runs/candle_craft.db`, an operational database, a `-wal`/`-shm` suffix, or an unrelated SQLite file are rejected. Enabled capture without a valid path is a misconfiguration counter, not a fallback.

Environment opt-in, read once per process unless tests install an override: `SOURCE_REPLAY_CAPTURE_ENABLED=true` and `SOURCE_REPLAY_EVIDENCE_PATH`. Any other enabled value stays off.

## 7. Lineage limits

Both scanner 2d routes (`_fetch_primary_candles` when the primary interval is `2d`, and `_fetch_strategy_timeframe_candles` when HTF is `2d`) already produce a closed-subset wrapper around `observe_synthetic_2d_resample`. Capture stores that object graph. It does not add a second resampler or promote a cache hit, an expiry refetch, or `acquisition_unavailable_after_restart` into an acquisition. Equal OHLC does not make two occurrences one opportunity. A nonblank `N/A` origin is a claimed id. A denied origin and a lifecycle epoch that is not the active epoch stay as stored.

## 8. Synthetic DEV measurement

Four 20-candle owner-monitoring invocations on one temporary plan, one process, capture enabled:

| Measure | Observed |
| --- | --- |
| Captures | 4 |
| Payload rows | 18 of 24 role slots (deduplicated) |
| Payload bytes | 180,341 |
| Store file after close | 237,568 |
| WAL after the writer closed | 0 |
| Elapsed | 0.4748 s |
| Peak traced memory during the burst | 465,090 bytes |

These numbers are not live disk runway. WAL can grow during the write and was checkpointed by the time the writer closed.

## 9. Rollout and rollback

Default capture stays off. Enabling a writer on Runtime requires Adam's later approval of readers and writers, the evidence path, database/WAL/archive footprint, free space, the finite budget above, retention, restore, and rollback.

Rollback is to stop optional capture. That does not change application schema 26 and does not delete the evidence store. Readers refuse an unknown store or codec version and do not migrate it. There is no historical provenance backfill.

## 10. Deferred

Scanner decision replay, authenticated venue evidence, possession before the decision cutoff, admission and episode ownership, expectancy, fees or execution, adaptive learning, strategy changes, Telegram changes, and Runtime activation remain out of scope. A replay match is captured computation under the limits above.
