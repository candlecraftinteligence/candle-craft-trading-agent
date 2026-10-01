# F04 — Durable Source Replay Foundation

## Phase

F04_DURABLE_SOURCE_REPLAY_FOUNDATION

This phase records one runtime `evaluate_closed_candle_outcomes` invocation in a separate evidence store and replays that call offline against a fresh scratch repository. It does not implement scanner decision replay, expectancy, admission/episode ownership, authenticated venue evidence, fees/execution, adaptive learning, strategy changes, Telegram changes, or Runtime activation.

Capture is off by default. Application schema remains 26. Public quality 88 / grade A / RR 3, F03 population isolation, and P5A owner-monitoring behavior are unchanged.

## Delivery

- Branch: `feature/f04-durable-source-replay-foundation`
- Base SHA: `b7c6422e3d1e2596115b7cd9cf5b1caea15072ef` (merged F03 on `origin/main`; incorporates reviewed F03 head `438624dc5f291d271b4c85be5f83262ea0f40d85`)
- Implementation SHA: `0b44dc41e9dea72495d4f387ca338171731f7936`
- Draft PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/128
- Final head: recorded at the end of this handoff after the documentation commit and exact-head CI
- Schema version: remains 26
- Evidence store schema: `cci-durable-source-replay-store-v1` / `STORE_SCHEMA_VERSION = 1`
- Codec: `cci-durable-source-replay-codec-v1`
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

## Transaction and crash protocol

The sidecar and the operational database do not share a commit.

1. Inside the operational transaction, capture copies bounded inputs, prestate, delivery, policy, result, and effects into memory.
2. Savepoint notes record whether `lifecycle_symbol` or `owner_monitor` was released or rolled back. A released savepoint is not an enclosing commit.
3. After `SQLiteSetupLifecycleRepository.__exit__` commits or rolls back, one evidence-store transaction writes the bundle.
4. A crash after the operational commit and before that evidence commit loses coverage. It does not leave a complete capture row.
5. An evidence write that raises rolls back the evidence transaction. The operational result, exception, gates, and cursors stay as they were. The failure is counted. It is not a replay pass.

Dispositions: `enclosing_committed`, `enclosing_rolled_back`, `savepoint_rolled_back`, `commit_interrupted`, `commit_unknown`. `savepoint_rolled_back` wins over a later enclosing commit. Standalone caller-owned transactions without an observed enclosing `BEGIN` stay `commit_unknown`.

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

`tests/test_f04_durable_source_replay.py` (23 tests) covers:

- Codec Decimal / absence / null / empty / timestamp fidelity
- Capture-off default, schema 26 unchanged, no evidence I/O
- Owner-monitoring and lifecycle-service restart round trips
- Same plan, second cursor, distinct occurrences with shared payloads
- Entry / TP / SL / same-candle / gap / future / terminal / no-op / invalid / confirmation-event / historical progress
- Service delivery + fetched owner evidence; mutated handoff stays a separate claim
- Cache / selection / both 2d routes stored without authority promotion
- Epoch A/B, missing/conflicting/`N/A` lineage; corrupt/truncated/missing references; unsupported policy/build/version
- Savepoint rollback after evaluator success; enclosing rollback; interrupted flush; capture failure preserves operational result
- Path alias / unrelated DB rejection; network/Telegram/StrategyReplay disabled on replay; bounds and lock wait
- Missing evidence is a diagnostic, not an invocation
- Synthetic storage / dedup / latency / footprint measurement

Related suites kept green: source evidence, delivery capture, evaluation policy, evaluation semantics, lifecycle outcomes, F03 population isolation, P5A owner monitoring, observation-unit caller inventory.

## Measured overhead (synthetic DEV)

Four 20-candle owner-monitoring invocations on one temporary plan, capture enabled, one process:

| Measure | Observed |
| --- | --- |
| Captures | 4 |
| Payload rows | 18 of 24 role slots (deduplicated) |
| Payload bytes | 180,341 |
| Store file after close | 237,568 |
| WAL after the writer closed | 0 |
| Elapsed | 0.4748 s |
| Peak traced memory during the burst | 465,090 bytes |

These numbers are not live Runtime disk runway. WAL can grow during writes and was checkpointed by close time.

## Local verification

Focused F04: `python -m pytest tests/test_f04_durable_source_replay.py` — 23 passed (~19.7s).

Related: `python -m pytest tests/test_source_evidence_boundary.py tests/test_prospective_batch_delivery_capture.py tests/test_evaluation_policy_manifest.py tests/test_evaluation_semantics_contract.py tests/test_p5a_active_owner_monitoring.py tests/test_f03_prospective_research_population.py tests/test_lifecycle_outcomes.py tests/test_observation_unit_contract_r0.py` — all passed (~39.3s).

Full suite at documentation commit preparation: `python -m pytest` — exit 0, 2795 collected, no failure lines, one existing Starlette deprecation warning, ~509s. `python -m compileall -q app tests` — exit 0. `git diff --check` — clean on tracked changes.

`LOCAL_MANUAL_MODE=true`, `ORDER_EXECUTION_ENABLED=false`, `TELEGRAM_DRY_RUN=true`, `TELEGRAM_SIGNALS_ENABLED=false`. Synthetic temporary databases only. No listener, watch loop, live Runtime DB access, secrets, weakened assertions, or xfails. No test was marked xfail.

## Strategy and schema non-regression

Verified unchanged:

- `SCHEMA_VERSION = 26`
- `MIN_PUBLIC_SETUP_QUALITY_SCORE = 88`
- `MIN_PUBLIC_SIGNAL_GRADE = "A"`
- `PUBLIC_SIGNAL_MIN_RR = 3`
- `PUBLIC_WATCHLIST_MIN_RR = 3`

No strategy gate, RR, or lifecycle-semantics change. F03 research denominators are untouched. Order execution remains disabled.

## CI

Implementation head `0b44dc41e9dea72495d4f387ca338171731f7936`: GitHub Actions run [36894151811](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/36894151811) — `Python 3.11 tests` success, no retry.

Exact final-head CI after this handoff commit is recorded in the final-head section below. The known F03 Telegram TP retry pattern did not recur on the implementation-head run.

## Rollout and rollback

Default capture stays off. Enabling a writer on Runtime requires Adam's later approval of readers/writers, evidence path, database/WAL/archive footprint, free-space trend, the finite budget above, retention/restore, and rollback. DEV synthetic measurements are not that approval.

Rollback is to stop optional capture. That does not change application schema 26 and does not delete the evidence store. Readers refuse unknown store/codec versions and do not migrate them. No historical provenance backfill. No old-reader/new-writer compatibility assumption.

## Deferred

Full scanner discovery/confirmation decision replay and HTF/context inputs; authenticated venue evidence; historical as-run possession reconstruction; admission/episode ownership and coverage reconciliation; expectancy proof; fees/funding/slippage or paper execution; adaptive learning; strategy/regime changes; Telegram changes; disk cleanup/backfill; Runtime activation.

## Final head

- Final head SHA: `7c9dd95a81da469d7a41fecc416159db9fa98221` (this handoff documentation commit)
- Implementation SHA: `0b44dc41e9dea72495d4f387ca338171731f7936`
- Exact-head CI run: pending push of this tip; prior implementation-head CI is [36894151811](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/36894151811)
- Local full pytest: exit 0, 2795 collected, ~509s, one Starlette warning
- `python -m compileall -q app tests`: exit 0
