# P0B1 — Lifecycle SQLite worker isolation

BASE_SHA: d19cce2eb471702f827fef2565b65b33a6096ad2
FEATURE_BRANCH: feature/p0b1-lifecycle-sqlite-worker-isolation
IMPLEMENTATION_SHA: c4bf50dcf2c655f6be9f5b44071d68ff61c219bb
PR_URL: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/154
GITHUB_CI: PASS on the implementation SHA above, Python 3.11 tests,
https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40551840202
EXACT_HEAD_CI: See PR #154's description for the final documentation-head SHA,
result and immutable Actions URL, recorded after this documentation commit.
Review must match that SHA to the current PR head; the implementation-head run
above alone is not evidence for a later head.
RUNTIME_DEPLOYMENT_APPROVED: NO

Remote main was fetched and independently verified at the supplied baseline,
including merged PR #153. Work is isolated in
`C:\CandleCraftDev\.worktrees\p0b1-lifecycle-sqlite-worker-isolation`.
The original checkout, other worktrees, local environment files and PR #152
were preserved. No Runtime host, production database, scanner process,
listener, Telegram transport or exchange network was operated.

## Ownership and completion

LIFECYCLE_CALL_GRAPH:

`_main` (one-shot) / `_run_watch_mode` → `_attempt_watch_scan_iteration` →
`_run_watch_scan_iteration` → awaited `_apply_lifecycle_isolated` →
`LifecycleWorker.call` → worker `_apply_lifecycle_if_enabled` →
`apply_lifecycle_to_run_result` → `SetupLifecycleService.apply_to_run_result`.

The worker owns observation preparation, repository open, the existing epoch
inspection/validation connections, health reads, `BEGIN IMMEDIATE`, run
registration validation, symbol savepoints, transition/activation/outcome
accounting, in-transaction owner monitoring, repository commit or rollback,
operational close, research source-capture exit/flush, and result construction.
Only after that whole callable returns does it issue a completion receipt.

CONNECTION_OWNER: The single non-daemon `cci-p0b1-lifecycle` thread opens,
uses and closes every connection belonging to this boundary. `sqlite3` thread
checking stays enabled. No live connection is submitted or returned.

TRANSACTION_OWNER: Existing `SetupLifecycleService` and
`SQLiteSetupLifecycleRepository`, executing on that worker. The transaction
SQL and canonical lifecycle service are unchanged.

COMMIT_ROLLBACK_OWNER: The existing repository context manager. A new optional
context-local observer records its disposition without SQL, retries or changes
to exception precedence. Receipt success means existing commit/close/cleanup
has completed; it adds no new fsync or power-loss durability guarantee.

WORKER_ADMISSION: One worker per watch session, one admitted call and at most
one waiting caller. A third call is rejected. The queue has `maxsize=1`;
admission is held by the awaiting owner until receipt processing. A one-shot
call owns and closes its worker. Workers are bound to one asyncio loop.
Importing modules starts no thread. There is no per-symbol thread or unbounded
executor submission. The P0A file worker is unchanged and remains separate.

CANCELLATION_FENCE: Cancellation before admission does not execute work.
After admission, every cancellation is delayed inside the awaiting coroutine
until the receipt is known. Successful completion after cancellation is
discarded and cancellation is re-raised. Worker failure takes precedence over
concurrent cancellation, retaining its original explicit cause and fatal or
recoverable classification. `SystemExit` and `KeyboardInterrupt` travel as
receipt data, rather than escaping a detached asyncio task. One-shot cleanup
cancellation cannot replace an already-received fatal epoch failure.

No caller can start the next lifecycle call, continuation monitor, public
delivery, failed-iteration write, or next scanner iteration using an unresolved
transaction. The watch cancel handler sees cancellation only after the worker
has completed. The existing session lease encloses that whole fence.

SHUTDOWN_JOIN: `aclose` closes admission, acquires it after the caller releases
its receipt, sends the shutdown sentinel and joins the non-daemon worker.
Repeated cancellation also waits for this join. Shutdown has no artificial
timeout that could detach a transaction. The lifecycle and P0A workers finish
before socket cleanup and watch-lease release. The existing watch-session
receipt persists `lifecycle_isolation_shutdown`, including join and in-flight
observations, under the still-held lease.

EPOCH_VERIFICATION: The existing operational opener validates the full expected
Runtime identity on worker-owned inspection and repository connections. The
service then requires the active epoch inside `BEGIN IMMEDIATE`.

SCAN_RUN_REGISTRATION_CHECK: Existing persisted operational run validation is
unchanged, inside the transaction and before symbol writes. New tests disable
the suite's automatic synthetic registration when testing this fail-closed
boundary.

SOURCE_CAPTURE_INTEGRITY: `copy_context` preserves capture configuration,
capture limits, replay guard and other context variables. Only the loop-owned
phase profile is suspended in the worker. The lifecycle phase is measured once
on the loop, including cancellation fencing. Capture remains inside the
existing repository exit behavior: operational close before sidecar flush;
capture failures still cannot replace operational outcomes. Tests replay
worker-created evidence and check persistence claims without asserting venue
authentication. The scanner result is borrowed by reference. The awaiting loop
does not mutate it, and the worker drops prior payload references before receipt
delivery. No second full scan result is copied for dispatch.

## Equivalence and regression evidence

CANONICAL_LIFECYCLE_EQUIVALENCE: Six deterministic comparisons cover rejected or
near-miss discovery, actionable watch candidates and confirmation candidates,
with and without an existing economic owner. The unchanged synchronous entry
point is the baseline. Tests fix UUID generation, handoff timestamps and the
temporary connections' `CURRENT_TIMESTAMP` function; production clocks and
schema are unchanged. They compare complete `ScannerRunResult` values, every
persisted table, schema definitions, monitoring summaries, outcome progress,
plan identity, activation accounting, phase status and same-run replay.

The baseline's UPSERT advances SQLite's internal AUTOINCREMENT sequence on
replay. Tests preserve that behavior, require unchanged logical records and
ownership, and compare full database snapshots after equal invocation counts.
Per-symbol failure after a write rolls back only that symbol's savepoint.

PUBLIC_DELIVERY_EQUIVALENCE: Public eligibility decisions match the synchronous
baseline. Existing public identity, event keys, delivery intent, reservations,
deduplication and provenance modules are unchanged. Scanner-ordering fixtures
replace the publication transport and prove it cannot run before completion.
Cancellation publishes nothing from the interrupted iteration. The eleven
legacy quarantined reservation events were not repaired or replayed.

STRATEGY_REGRESSION: No strategy, quality, RR, timeframe, economic geometry,
confirmation, trigger, cooldown or terminal-state change. Existing lifecycle,
outcome, owner-monitoring and public-delivery suites remain required. Canonical
schema version stays 27; no indexes, migration, journal-mode change, busy-timeout
increase, VACUUM or REINDEX is introduced. No order or transfer functionality
is added.

CMC_REGRESSION: CMC implementation, SHADOW evaluation, credit settings and both
manifests are unchanged. The collection deadline remains 30 seconds. P0A's CMC
deadline and cancellation regressions remain in the verification set.

WEBSOCKET_REGRESSION: The old synchronous starvation fixture still reproduces
blocking. The new loopback fixture sends a synthetic frame from an independent
provider thread while a real temporary lifecycle transaction holds the worker.
The async receiver receives it before the transaction can continue. Valid wire
receive progress: 1 frame. Parsed verified Binance market events: 0. No parser,
validation or market confirmation is fabricated or changed.

## DEV measurements

These are deliberately slow temporary-database simulations on the DEV PC, not
production measurements or predictions about scanner-cycle duration. A 0.6 s
simulated I/O stall executes inside the real lifecycle transaction. The loop-lag
sampler interval is 0.02 s for this experiment; Runtime remains at 0.5 s.

LOOP_LAG_BEFORE: 0.605 s maximum sampled scheduling lag, 4 timer samples.
LOOP_LAG_AFTER: 0.012 s maximum sampled scheduling lag, 22 timer samples.
LIFECYCLE_WALL_TIME: 0.618734 s synchronous; 0.634589 s isolated end-to-end;
0.632979 s worker execution in this sample.
WORKER_WAIT: 0.000091 s uncontended admission plus dispatch; completion delay
0.000254 s. Barrier tests separately exercise a waiting second invocation and
backpressure rejection.
MEMORY_OVERHEAD: 17,057 bytes peak traced Python allocation in the focused
fixture, passing an existing 500,000-byte payload by reference. A separate fresh
DEV process measured 19,587 bytes peak traced allocation and an RSS point change
from 34,340,864 to 34,512,896 bytes (+172,032 bytes), using
`windows:GetProcessMemoryInfo`. This is not peak Runtime working set or a
capacity approval. OS stack reservation and production peak memory: N/A.

Reproduce timing and Python allocation observations with the existing project
interpreter:

```powershell
& C:\CandleCraftDev\.venv\Scripts\python.exe -m pytest tests/test_p0b1_lifecycle_isolation.py -k synthetic_loop_lag_wall_time_and_memory_measurements -s
```

Metrics retain maxima and counts only, without payloads or exception text:
`LIFECYCLE_WORKER_QUEUE_WAIT`, `LIFECYCLE_WORKER_DURATION`,
`LIFECYCLE_COMPLETION_DELAY`, `COMMIT_OR_ROLLBACK_RESULT`, and
`IN_FLIGHT_AT_SHUTDOWN`. `PhaseProfile` adds
`LOOP_LAG_DURING_LIFECYCLE` from the existing phase-attributed sampler; it is
`N/A` when no lifecycle sample exists. The completion result reports commit,
rollback, commit failure plus rollback, close failure, or an explicitly unknown
or unobserved disposition. It never infers business success from duration.

## Verification and limits

FULL_TEST_RESULTS: PASS — 3,884 passed, 0 failed, 1 dependency deprecation warning
in 660.19 s (11:00), using the existing Python 3.11.9 environment and CI/DEV
safety variables. The confirmation run used a fresh OS temp directory with the
existing `pytest-of-` safety convention. No tests were skipped or deselected.

The initial full run completed with
3,883 passed, 1 failed and 1 dependency deprecation warning in 672.66 s. The
failure was `test_watch_session.py::test_completion_scope_and_nonrecursive_prior_observation_are_bounded`:
Windows denied an atomic receipt replacement (`WinError 5`). Its source and
`app/watch_session.py` match verified main. The same worktree-local failure
reproduced in a suite recheck (27 passed, 1 failed). No receipt implementation,
retry behavior or test was changed to hide it.

A fresh OS temporary directory using the existing `pytest-of-` synthetic-DB
whitelist passed the entire watch-session suite and P0B1 ordering tests:
28 passed in 6.71 s. Root cause of the worktree-local replacement error remains
Unverified. Full pytest then passed with that temporary-root convention.
An earlier exploratory temp directory did not match the existing safety
whitelist, so 10 setup/identity tests failed closed (18 passed); the directory
name was corrected, without weakening the whitelist or epoch guards.

P0B1 adds 28 focused tests, all included in the successful full confirmation.
A standalone run passed all 28 in 5.93 s before the final shutdown-receipt
persistence assertions. P0B1 plus inventory verification: 61 passed in 9.14 s
before the final additional fatal-cleanup test. A wider focused run passed
245 tests and exposed only the synthetic SQLite default-clock issue described
above. Early test-fixture errors were corrected; none were hidden or skipped.
Sandboxed asyncio/loopback invocations hung because of the Windows local
wakeup/network restriction and were interrupted. Verification runs use approved
execution outside that sandbox, synthetic transports and temporary databases.

Compilation: `python -m compileall -q app scripts src tests` passed.
Dependencies: `python -m pip check` passed, no broken requirements.
The inventory anchor moved from `scripts/run_scan.py:617` to line 618 because
of one import. The cadence default remains 60.0. Both generated inventory
snapshots were regenerated from `render_package`; no research result changed.

Full DEV confirmation command (with `LOCAL_MANUAL_MODE=true`,
`ORDER_EXECUTION_ENABLED=false`, `TELEGRAM_DRY_RUN=true`,
`TELEGRAM_SIGNALS_ENABLED=false`):

```powershell
$cciP0b1PytestRoot = Join-Path ([IO.Path]::GetTempPath()) ("pytest-of-p0b1-" + [guid]::NewGuid().ToString("N"))
& C:\CandleCraftDev\.venv\Scripts\python.exe -m pytest -o "addopts=" -q "--basetemp=$cciP0b1PytestRoot" --tb=short
```

OUTSTANDING_OWNER_MONITORING_BLOCKING: The separate async continuation still
opens/reads/closes a repository on the loop, fetches candles using its existing
async transport, then opens a fresh repository for writes on the loop. This PR
does not offload that async function or move a connection around a network await.
Within a scanner iteration it starts only after the lifecycle receipt. Its
worker-owned read/write redesign remains a separate P0B follow-up.

OUTSTANDING_TELEGRAM_OUTBOX_BLOCKING: Existing synchronous SQLite outbox work
still blocks the loop; no send/reservation redesign in this phase.
OUTSTANDING_SCAN_STORAGE_BLOCKING: Existing scan-storage SQLite writes remain
on the loop, after lifecycle completion and in their original order.
OUTSTANDING_CMC_THREAD_CANCELLATION: Existing sidecar `to_thread` calls may
outlive the collection waiter. The 30-second deadline was not extended.

KNOWN_LIMITATIONS: Isolation does not reduce underlying SQLite I/O duration or
remove GIL contention. SQLite still arbitrates writes against the independent
Telegram listener. The worker does not retry, repair history or coordinate
separate processes beyond existing SQLite/epoch/lease mechanisms. A blocked OS
I/O operation can delay graceful shutdown indefinitely: returning while it owns
the transaction would violate this phase's completion invariant. Abrupt process
or power loss remains SQLite's existing journal/durability contract, not a new
thread-cancellation guarantee. Production behavior, memory capacity and receive
health require a separately authorized Runtime rollout and measurements.

P0B2_RECOMMENDATION: First design worker-owned continuation-monitoring read and
write jobs around the existing async candle fetch, using the same serialization
owner and epoch checks. Then isolate Telegram outbox reservation/completion and
scan storage in separately bounded phases with public-effect regressions. Do not
repair or replay the eleven quarantined legacy reservations, merge PR #152 or
deploy this phase without separate owner approval.

RUNTIME_DEPLOYMENT_APPROVED: NO
