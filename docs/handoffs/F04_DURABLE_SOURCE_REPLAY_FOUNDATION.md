# F04 — Durable Source Replay Foundation

## Phase

F04_DURABLE_SOURCE_REPLAY_FOUNDATION

This phase records one runtime `evaluate_closed_candle_outcomes` invocation in a separate evidence store and replays that call offline against a fresh scratch repository. It does not implement scanner decision replay, expectancy, admission/episode ownership, authenticated venue evidence, fees/execution, adaptive learning, strategy changes, Telegram changes, or Runtime activation.

Capture is off by default. Application schema remains 26. Public quality 88 / grade A / RR 3, F03 population isolation, and P5A owner-monitoring behavior are unchanged.

## Delivery

- Branch: `feature/f04-durable-source-replay-foundation`
- Base SHA: `b7c6422e3d1e2596115b7cd9cf5b1caea15072ef` (merged F03 on `origin/main`; incorporates reviewed F03 head `438624dc5f291d271b4c85be5f83262ea0f40d85`)
- Draft PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/128
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

## First corrective repair (REQUEST_CHANGES on `4c26ac1`)

Independent review disposition REQUEST_CHANGES identified seven acceptance failures that green CI missed. The first corrective tip `78e4c4d` / docs tip `255c036` repaired R2, R5, and R6, and partially addressed R1/R3/R4/R7. Independent re-review of exact head `255c036` remained **REQUEST_CHANGES** with R1/R3/R4/R7 still open.

| ID | First-repair status after `255c036` re-review | Notes |
| --- | --- | --- |
| F04-R1 | Still open | Explicit rollback/empty commit fixed; released descendants and SQL transaction replacement still claimed persistence |
| F04-R2 | Accepted | Identity init failure containment preserved |
| F04-R3 | Still open | First-store/large-bundle cases fixed; pinned-reader WAL growth still committed then raised |
| F04-R4 | Still open | Compound corruption short-circuited; reverse savepoint/enclosing contradiction and malformed ints remained |
| F04-R5 | Accepted | Padded authority lookups preserved |
| F04-R6 | Accepted | Attestation v2 / state_machine fingerprint preserved |
| F04-R7 | Still open | Header/prestate/history bounds fixed; payload materialization and reference loading remained unbounded |

## Second corrective repair (REQUEST_CHANGES on `255c036`)

Same branch and draft PR #128. No merge, deploy, or Runtime capture enablement.

| ID | Repair | Primary modules | Independent regression tests |
| --- | --- | --- | --- |
| F04-R1 | Occurrence savepoint ancestry + transaction generations; SQL `COMMIT`/`ROLLBACK`/`ROLLBACK TO` observation; retain only matching non-discarded generation | `capture._Pending`, `_Session.generation_fate`, `_discard_savepoint_ancestry`, `_observe_sql`, `_assign_disposition` | `test_f04_r1_released_descendant_outer_rollback_is_not_persistence`, `test_f04_r1_sql_rollback_then_new_transaction_is_not_persistence`, `test_f04_r1_generation_preserves_earlier_commit_across_later_rollback` (plus existing explicit-rollback test) |
| F04-R3 | Pre-commit admission with estimated content growth + pinned-reader/WAL reserve; rollback rejected bundles; no post-commit delete of accepted rows | `store.write_bundle`, `_reject_if_over_budget` | `test_f04_r3_pinned_reader_rejects_before_commit_and_preserves_prior` (plus existing first-write/diagnostics budget test) |
| F04-R4 | Full tx/savepoint/enclosing consistency matrix including reverse contradictions; guarded integer/metadata/payload-shape validation before positive claims | `replay._validate_envelope`, `_guarded_int`, `_load` | `test_f04_r4_savepoint_rolled_back_cannot_claim_enclosing_committed`, `test_f04_r4_malformed_reference_count_is_structured_corrupt` (plus existing compound conflict test) |
| F04-R7 | `LIMIT max_references+1` before ref materialization; SQL `length(canonical_bytes)` and remaining decode budget before BLOB fetch | `store.load_capture`, `store.read_payload`, `replay._load` | `test_f04_r7_payload_and_reference_bounds_before_materialization` (plus existing header/failure-retention test) |

Remaining limitations unchanged: no venue authentication, no possession-before-cutoff, no expectancy/admission, no Runtime capture activation, no historical backfill. Unsupported pre-v2 attestations fail closed on replay. **Independent re-review acceptance is not claimed by this handoff.**

## Transaction and crash protocol

The sidecar and the operational database do not share a commit.

1. Inside the operational transaction, capture copies bounded inputs, prestate, delivery, policy, result, and effects into memory.
2. Savepoint notes record whether `lifecycle_symbol` or `owner_monitor` was released or rolled back. Nested rollbacks mark ancestry of released descendants. A released savepoint is not an enclosing commit.
3. Instrumented `connection.commit` / `rollback` / SQL text observe retaining commits and discarded effects per transaction generation. An empty commit after rollback, or a later transaction's commit, cannot claim persistence for discarded captures.
4. `SQLiteSetupLifecycleRepository.__exit__` finishes operational commit/rollback and closes before sidecar flush.
5. One evidence-store transaction then writes the bundle only after conservative footprint admission. A crash between operational commit and evidence commit loses coverage without a false complete row.
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

`tests/test_f04_durable_source_replay.py` (**37 tests**) covers the original foundation matrix, the first corrective counterexamples, and the second-corrective independent cases for R1/R3/R4/R7.

Related suites kept green: source evidence, delivery capture, evaluation policy, evaluation semantics, lifecycle outcomes, F03 population isolation, P5A owner monitoring, observation-unit caller inventory.

## Local verification (second corrective)

Focused F04: `python -m pytest tests/test_f04_durable_source_replay.py` — **37 passed**.

Related (source-evidence, delivery-capture, policy, semantics, P5A, F03, lifecycle outcomes, observation-unit): all passed.

Full suite: `python -m pytest` — exit 0, **2809 collected**, no failure lines, one Starlette warning, ~492s. `python -m compileall -q app tests` — exit 0.

`LOCAL_MANUAL_MODE=true`, `ORDER_EXECUTION_ENABLED=false`, `TELEGRAM_DRY_RUN=true`, `TELEGRAM_SIGNALS_ENABLED=false`. Synthetic temporary databases only. No listener, watch loop, live Runtime DB access, secrets, weakened assertions, or xfails.

## Strategy and schema non-regression

Verified unchanged:

- `SCHEMA_VERSION = 26`
- `MIN_PUBLIC_SETUP_QUALITY_SCORE = 88`
- `MIN_PUBLIC_SIGNAL_GRADE = "A"`
- `PUBLIC_SIGNAL_MIN_RR = 3` (`DEFAULT_PUBLIC_RR_MIN`)
- `PUBLIC_WATCHLIST_MIN_RR = 3`

No strategy gate, RR, or lifecycle-semantics change. F03 research denominators are untouched. Order execution remains disabled.

## CI

Prior reviewed head `255c03600fc9224c66d9c2113481137007062016`: run [36904095734](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/36904095734) succeeded (2802 passed) but did not cover the independent R1/R3/R4/R7 failures above.

Second corrective code tip `aa6f1c2923c73934dbf25ee82e6f5b43e89bc55c`: GitHub Actions run [36910492306](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/36910492306) — `Python 3.11 tests` success, attempt 1, no retry.

## Rollout and rollback

Default capture stays off. Enabling a writer on Runtime requires Adam's later approval of readers/writers, evidence path, database/WAL/archive footprint, free-space trend, the finite budget above, retention/restore, and rollback. DEV synthetic measurements are not that approval.

Rollback is to stop optional capture. That does not change application schema 26 and does not delete the evidence store. Readers refuse unknown store/codec/attestation versions and do not migrate them. No historical provenance backfill. No old-reader/new-writer compatibility assumption.

## Deferred

Full scanner discovery/confirmation decision replay and HTF/context inputs; authenticated venue evidence; historical as-run possession reconstruction; admission/episode ownership and coverage reconciliation; expectancy proof; fees/funding/slippage or paper execution; adaptive learning; strategy/regime changes; Telegram changes; disk cleanup/backfill; Runtime activation.

## Final head

- Prior reviewed tip (still REQUEST_CHANGES): `255c03600fc9224c66d9c2113481137007062016`
- Second corrective code tip: `aa6f1c2923c73934dbf25ee82e6f5b43e89bc55c`
- Exact-head CI for that code tip: [36910492306](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/36910492306) — success, attempt 1
- Documentation tip that records this paragraph is the branch tip after this commit; its PR check is the CI result for the final head
- Local focused F04: 37 passed
- Related suites (source/delivery/policy/semantics/P5A/F03/lifecycle/observation-unit including F04): 241 passed
- Full pytest: exit 0, 2809 collected, ~492s, one Starlette warning
- `python -m compileall -q app tests`: exit 0
- Independent acceptance: **not claimed**; awaiting re-review of the exact final head
