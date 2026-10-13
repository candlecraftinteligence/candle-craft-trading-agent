# Confirmation observation ownership and retry idempotency

Base: upstream main `208b6e99038418f5ceb136cda85a3f1468ccf40f` (PR #132).
Branch: `codex/confirmation-observation-idempotency`.
PR and final validation record: [#133](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/133).
Scope: bounded original-P4 prerequisite; no Runtime deployment or database operation.

## Defect and production reachability

The pure lifecycle reducer counts each qualifying invocation. The service previously
validated the operational origin but did not remember consuming its decision.
On the base commit, a synthetic invocation through `scripts.run_scan.main`,
the real `ScannerRunner`, real pre-acquisition run registration, and real origin
validation reached TRIGGERED/count 1, then CONFIRMED/count 2 when the same result
was consumed again. This could also latch the economic plan.

`test_producer_retry_does_not_confirm_one_scan` reproduces this with fake adapter
candles and a fake sender. It does not patch eligibility, origin validation, or
the reducer. The negative integration case removes registration and remains
fail-closed. Tests named `supplied` (and the explicit preexisting-owner tests)
use constructed decisions/owners and are not independent production-reachability
proofs. No inference about Runtime duplicate frequency is made.

## Identity and persistence

Application schema **27** adds one nullable TEXT column:
`runtime_operational_origins.lifecycle_consumption_json`.
No table, event type, index, source snapshot, or research admission stream is added.
Existing transition storage cannot represent consumption on a no-transition pass;
its event readers include outcome-boundary lookup, admin timelines, conversion
reports, activation accounting, and replay prestate. Adding accounting events
would change those readers' counting units.

An origin already identifies one registered run and symbol in the active epoch.
The current producer emits one representative lifecycle decision per symbol;
the service derives its mode from that result. Origins themselves have no mode
partition. A receipt document therefore has independent mode slots:

```json
{"version":1,"modes":{"swing":{"lifecycle_id":"owner","digest":"sha256","consumed_at":"UTC"}}}
```

The key is the validated current observation's origin plus normalized mode.
The receipt binds that slot permanently to its first lifecycle owner. Direction,
structural anchor, economics, qualifying predicates, scores, and all other
`LifecycleObservation` fields are hashed decision content, not alternate retry
identities. The digest also covers supplied evaluation kind, completion time,
decision cutoff, adapter acquisition time, and execution timeframe. It does not
substitute the owner's creation origin for the current observation's origin.
Processing time, retry UUIDs, and public event keys are not observation identity.
`consumed_at` is diagnostic only.

The receipt is checked **before generation rotation**. A matching retry loads the
bound owner's latest persisted record and skips discovery reduction, repricing,
decay, rotation, and transition insertion. It cannot restore an earlier snapshot
or lend its contribution to a replacement owner. Conflicting content or malformed
receipt evidence is a visible integrity exception in the existing symbol-error
summary (and an exception from the single-symbol API).

Every successfully persisted decision is marked, including a nonqualifying
decision or a contribution without a state transition. All origin receipts are
retained; checking only the latest observation would not stop older retries.
The document stores only small per-mode receipts, never candle arrays.

Run processing uses its existing `BEGIN IMMEDIATE` and per-symbol savepoint.
The single-symbol API now also obtains the writer lock and uses that savepoint,
including when its caller catches an error and subsequently commits. Receipt
persistence precedes the lifecycle write within that same unit. Concurrent
workers serialize before reading state. Savepoint rollback, enclosing rollback,
and failed commit discard the receipt and effect together. There is no process
cache that could survive or hide a failed commit.

## Monitoring, chronology, and public behavior

Duplicate discovery consumption still invokes the canonical closed-candle
evaluator and retains the run's independent owner-monitoring pass. Monitoring
evidence is separate from the discovery-decision digest: recovering a candle
batch for an already owned plan does not create another confirmation.
Cursor, conservative ambiguity, plan binding, and
`tp1_entry_zone_midpoint_breakeven_v2` remain unchanged.

An already consumed delayed observation is a discovery no-op. A **previously
unconsumed** valid observation, including a delayed one, follows the established
processing-order reducer and generation rules. This change adds no chronological
trading gate and does not require different candle timestamps.

Confirmation remains **scan-cycle confirmation**, with existing required cycles,
quality/RR predicates, and the oscillation repair. Different registered scans
of the same candle can still contribute. They are not proven independent market
evidence. Public gates remain quality 88 / grade A / RR 3.

No accounting transitions, activations, fills, or public events are inserted.
A duplicate returns NO_CHANGE, not a manufactured transition notice. The original
successful result retains its legitimate transition for delivery. Existing
public keys, delivery deduplication, and SENT/PENDING/UNCERTAIN handling are
unchanged; this is not a new delivery-recovery mechanism.

## Historical compatibility and rollback

Migration leaves all existing origin receipts SQL NULL. NULL means **Unverified**
historical consumption, not an empty set and not permission to recount a previous
run. Such observations cannot reduce or create a lifecycle. Where an existing
current owner is available, its monitoring can continue. The process summary
exposes `unverified_confirmation_observations`.

Only newly granted origins receive the versioned empty document. Their new
decisions can continue an existing owner's counter without resetting it,
rewriting `confirmed_at`, reannouncing a setup, or reconstructing old evidence.
Receipts prove prospective consumption only; inherited counter provenance remains
unknown. Existing confirmed/active owners and their original creation origin
remain intact. No historical public key or outcome is migrated.

The normal explicit offline migration mechanism adds the nullable column and
sets user_version 27 in one transaction after the existing v26 migration stage.
`tests/fixtures/schema_v26.sql` is an empty catalog generated by the unchanged
base initializer above, not a new schema with its version number lowered.
Migration tests preserve every historical field, verify that only the origin
table definition changes, repeat migration, and inject rollback failures.
Existing current-schema assertions now pin 27; older fixtures and their
behavioral assertions retain their original contracts.

Operational startup requires the current schema and does not silently migrate.
Readiness recognizes genuine schema-26 source metadata while requiring the new
column in a schema-27 target rehearsal and explicit preservation of consumption
receipts. It derives the target from SCHEMA_VERSION. Single-copy scan readers and
research queries keep their existing row units. Durable-source replay still
requires exact application-schema and implementation compatibility: older
schema-26 captures are not silently reinterpreted as schema 27. New synthetic
capture/replay coverage remains in the full regression suite.

A future deployment requires a separately authorized offline migration and
compatible writers/readers. Old writers must not run against schema 27. Rolling
back code must preserve the receipt contract; do not drop receipts, lower the
schema version, restore an older database over new history, or recreate counters.
None of those Runtime actions is part of this work.

## Verification and remaining work

Focused coverage is in `test_confirmation_observation_consumption.py` and
`test_confirmation_consumption_migration.py`: identical/later retries, persisted
restart, intervening observations, concurrent consumers, conflicts, owner/mode
separation, missing/foreign/invalid lineage, generation changes, first/no-transition
contributions, savepoint and commit failures, unmarked origins, continued entry/TP
monitoring, and unchanged research/public counting units.

Validation uses synthetic databases under the repository-recognized `pytest_tmp`
directory, fake exchange adapters, and fake Telegram senders. Windows sandbox
setup failures were separated from application failures; database path protections
were not changed. The final focused run passed **175 tests** on 2026-10-13, covering
the two new test modules plus `test_lifecycle.py` and
`test_runtime_checkpoint_readiness.py` (local log:
`C:\CandleCraftDev\pytest_tmp\confirmation-final-focused.log`). Final acceptance
runs the complete `python -m pytest`, `python -m compileall -q app scripts src tests`,
`python -m pip check`, and `git diff --check`. The full local log is
`C:\CandleCraftDev\pytest_tmp\confirmation-final-full.log`.
Exact final implementation SHA, completed full-suite results, review disposition,
and CI run links are recorded in [PR #133](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/133)
and the final handoff after validation. Its required validation workflow is
`.github/workflows/ci.yml` / `Python 3.11 tests`. Earlier failing/intermediate runs
are not acceptance.

The separate review pass examined origin scope, pre-rotation ownership, transaction
failure behavior, unmarked history, event consumers, schema/readiness/replay
compatibility, and all test changes. It repaired the readiness distinction between
old sources and current targets and ensured unexpected single-symbol failures
also roll back their savepoint if a caller catches the error and commits. No
unresolved correctness finding remains. No Runtime deployment, source-capture
activation, existing database access, or real Telegram send is performed.

The original E2 unique-market-evidence experiment remains separate. This repair
does not establish full original P4 acceptance. Original P5 admission population,
complete causal source coverage, coverage/attrition and independent outcome
reconciliation remain unfinished. No independent economic episode, actual fill,
net PnL, profitability, or complete strategy-research population is claimed.
