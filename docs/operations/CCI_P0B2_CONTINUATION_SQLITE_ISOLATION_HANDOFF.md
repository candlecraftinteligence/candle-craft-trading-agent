# P0B2 — Continuation Monitoring SQLite Isolation

BASE_SHA: a158b451b3c8417101ed3fcfc117d28f1466afd6
FEATURE_BRANCH: feature/p0b2-continuation-monitoring-sqlite-isolation
INITIAL_IMPLEMENTATION_SHA: 6542d58ce2643bd1b635fbf485b9c3d3216a16b6
INITIAL_DOC_SHA: 88b961f86f01f7b73e23b6e79beda83ac2ba795c
PR_URL: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/155
GITHUB_CI_INITIAL_HEAD: PASS on 88b961f86f01f7b73e23b6e79beda83ac2ba795c (https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40569392800)
FINAL_REVIEW_HEAD: Pending final commit
RUNTIME_DEPLOYMENT_APPROVED: NO

Remote main was fetched and independently verified at the baseline commit `a158b451b3c8417101ed3fcfc117d28f1466afd6`, which includes merged PR #151 (CMC Shadow Decision Recovery), PR #153 (P0A Disk Persistence Isolation), and PR #154 (P0B1 Lifecycle SQLite Worker Isolation).
All work was developed strictly on the dedicated DEV environment. PR #152 (selective Fibonacci and native 1D timeframe) remains OPEN and completely separate; it was not touched or merged. No live exchange order execution was implemented, no live Runtime databases or hosts were accessed, and all 11 historical quarantined Telegram reservations were preserved.

---

## 1. Executive Summary

Phase P0B2 isolates synchronous SQLite reads and writes out of the asynchronous continuation monitoring event loop (`monitor_obligations_with_market_data`) while maintaining exchange candle acquisition asynchronously on the event loop.

Continuation monitoring was previously executing synchronous SQLite queries (obligation discovery and progress tracking) and synchronous SQLite outcome updates (`monitor_tracking_obligations` with `BEGIN IMMEDIATE` / `COMMIT`) directly on the scanner's main asyncio thread. Under high database load or pagefile pressure, this caused substantial event-loop scheduling stalls (up to 480ms–500ms).

P0B2 implements a clean three-stage continuation pipeline:
- **Stage 1 (Worker-Owned SQLite Read)**: Discovers open tracking obligations and their progress cursors on a serialized worker thread. No live SQLite connection escapes the worker.
- **Stage 2 (Async Market-Data Acquisition)**: Awaits external exchange candles on the event loop without holding any open SQLite connection or transaction.
- **Stage 3 (Worker-Owned SQLite Write)**: Evaluates outcomes and records progress updates on the serialized worker thread within a bounded transaction, revalidating against stale or superseded plans before commit.

---

## 2. Three-Stage Pipeline Architecture

```
                  EVENT LOOP THREAD                    SERIALIZED WORKER THREAD (cci-p0b1-lifecycle)
                  =================                    =============================================

Stage 1 Read:     await worker.call(
                    stage_1_continuation_read, ...) ───> Open SQLiteSetupLifecycleRepository
                                                         connection.execute("BEGIN")
                                                         require_active_runtime_epoch(connection)
                                                         select_tracking_obligations(repository)
                                                         list_outcome_progress_for_lifecycles(...)
                                                         Close repository (connection closed)
                  <───────────────────────────────────── Return detached dataclasses & keys
                  (Zero SQLite connections held)

Stage 2 Fetch:    for (symbol, timeframe) in needed:
                    await fetch_candles(...)
                  Classify gaps / errors
                  (Zero SQLite transactions open)

Stage 3 Write:    await worker.call(
                    stage_3_continuation_write, ...) ──> Open SQLiteSetupLifecycleRepository
                                                         connection.execute("BEGIN IMMEDIATE")
                                                         require_active_runtime_epoch(connection)
                                                         Validate run registration (if specified)
                                                         Revalidate open obligations against Stage 1
                                                         monitor_tracking_obligations(repository, ...)
                                                         repository.__exit__ -> connection.commit()
                                                         Close repository (connection closed)
                                                         flush durable source capture
                  <───────────────────────────────────── Return OwnerMonitoringResult & Receipt

Post-Monitoring:  Telegram public alert delivery
                  (only after Stage 3 receipt)
```

### Stage 1: Worker-Owned SQLite Read (`stage_1_continuation_read`)
- Executes on the serialized worker thread.
- Opens a fresh `SQLiteSetupLifecycleRepository` with thread-check enabled.
- Starts a read transaction (`BEGIN`) to obtain a consistent point-in-time snapshot.
- Enforces active runtime-epoch validation via `require_active_runtime_epoch(connection)`.
- Queries open tracking obligations and their existing outcome progress rows.
- Closes the connection on the same worker thread.
- Returns only detached Pydantic and dataclass models to the event loop. No SQLite connection or cursor survives.

### Stage 2: Asynchronous Market-Data Acquisition
- Executes asynchronously on the scanner event loop.
- Awaits `fetch_candles(symbol, timeframe, candle_limit)`.
- No SQLite connections or transactions are held across network awaits.
- Handles and classifies market data errors deterministically:
  - `EXPECTED_MARKET_DATA_ERRORS` / unsupported markets -> explicit evidence gap records.
  - Setup, adapter, or programming bugs -> classified as subsystem failures, skipping obligation updates without creating synthetic gap records or fabricating missing candles.
  - Async cancellation -> raises `asyncio.CancelledError` without recording synthetic gaps.

### Stage 3: Worker-Owned SQLite Write (`stage_3_continuation_write`)
- Executes on the serialized worker thread.
- Opens a fresh `SQLiteSetupLifecycleRepository` on the worker thread.
- Executes `connection.execute("BEGIN IMMEDIATE")` to acquire the SQLite exclusive write lock.
- Verifies the active runtime epoch and optionally revalidates the operational run registration.
- **Stale Obligation Protection**: Re-selects current tracking obligations from SQLite and blocks:
  1. Any obligation whose plan was superseded or whose plan version changed during Stage 2.
  2. Any obligation that transitioned to a terminal state during Stage 2.
  3. Any new tracking obligation created while Stage 2 was fetching (deferred to the next iteration to prevent missing candle gaps).
- Executes `monitor_tracking_obligations`.
- Commits or rolls back on the worker thread in `repository.__exit__`.
- Closes the connection on the worker thread.
- Flushes research durable source capture.
- Records completion receipt with transaction disposition (`COMMITTED`, `COMMIT_FAILED_ROLLED_BACK`, or `ROLLED_BACK`).

---

## 3. Concurrency, Serialization & Threading Model

- **Worker Domain Reuse**: Reuses the established serialized `LifecycleWorker` domain (`cci-p0b1-lifecycle`) from P0B1. A single ordered worker domain handles both lifecycle accounting and continuation monitoring within each watch session, eliminating SQLite lock contention and avoiding unnecessary thread-stack overhead.
- **Strict Ordering Guarantees**:
  `Lifecycle transaction commit -> Stage 1 read -> Async candle fetch -> Stage 3 write commit -> Telegram public delivery -> WatchSession lease release / Next iteration`
- **Bounded Admission**:
  - `_admission = asyncio.Lock()` guarantees exactly one operation in-flight.
  - Queue `maxsize=1` allows at most one waiting caller; additional callers are rejected with `LifecycleWorkerSaturated`.
  - The caller retains admission until the worker completes and returns a completion receipt.
- **No Connection Leaks**:
  - Every connection is opened, used, and closed within the worker thread.
  - `sqlite3.threadsafety` and `check_same_thread=True` are strictly preserved.
  - If a worker callable returns an open `sqlite3.Connection`, `LifecycleWorker._execute` immediately closes it and raises `TypeError`.

---

## 4. Stale Obligation & Data Race Protection

Because Stage 2 awaits external exchange candles over the network, database state could evolve between Stage 1 and Stage 3. Stage 3 implements fail-closed protections:
1. **Superseded or Modified Plans**: Stage 3 compares `stage1_obligations` against current `stage3_obligations`. If an obligation's `plan_version_id` changed or is no longer present, its lifecycle ID is added to `blocked_ids`.
2. **Terminal Outcomes**: An obligation that reached a terminal state (STOPPED, TARGET_3, EXPIRED, INVALIDATED) between Stage 1 and Stage 3 is blocked from receiving further outcome updates.
3. **New Plan Arrivals During Fetch**: Any tracking obligation present in Stage 3 that was not present in Stage 1 is excluded from Stage 3 evaluation and deferred to the next iteration. This ensures new obligations are not evaluated with incomplete or missing evidence.
4. **Epoch Invalidation**: If the runtime epoch changes between Stage 1 and Stage 3, Stage 3 immediately raises `RuntimeEpochOwnershipError` or `RuntimeEpochError` and rolls back.

---

## 5. Cancellation & Shutdown Invariants

- **Stage 1 Cancellation**: Caller cancellation is held behind `_fenced_wait` until the worker finishes the read transaction and closes the repository.
- **Between Stage 1 & Stage 2**: Task cancellation terminates the iteration cleanly without initiating Stage 2 or Stage 3.
- **Stage 2 Cancellation**: Async cancellation during `fetch_candles` propagates cleanly as `asyncio.CancelledError`. No synthetic gap records or corrupted progress states are written.
- **Between Stage 2 & Stage 3**: Task cancellation cleanly stops before Stage 3 admission.
- **Stage 3 Cancellation**: Worker retains transaction ownership through `_fenced_wait`. The write transaction either completes commit or rolls back completely before the cancellation exception is delivered to the awaiting task.
- **Graceful Shutdown**: `run_scan.py` joins the `LifecycleWorker` thread before releasing the `WatchSession` lease. A subsequent iteration cannot race an unjoined worker.

---

## 6. Critical Provenance Review & Operational Run Analysis

A critical architectural investigation was conducted on `scripts/run_scan.py::_continue_owned_plan_monitoring`:

### Historical Implementation (P5A Baseline)
Previously, line 2863 contained:
```python
scan_run_id = scan_run_id or f"owner-monitor-{uuid4().hex}"
```
When `scan_run_id` was `None` (specifically in `_surface_monitoring_during_discovery_failure` when universe discovery crashed before an operational scan run could complete), the code fabricated an unregistered, unauthenticated string `owner-monitor-<uuid>`.
This violated canonical CCI origin contracts:
- `owner-monitor-<uuid>` was never persisted in `runtime_operational_runs`.
- It created dangling foreign references in `setup_lifecycle_events`.
- In prospective runtime epoch isolation, `public_reservation_record_mismatch_reason` and `operational_run_belongs_to_active_epoch` explicitly check that present `scan_run_id` exists in `runtime_operational_runs` under the active epoch; an unregistered synthetic string caused `public_reservation_scan_run_unregistered`.

### P0B2 Canonical Origin Contract Implementation
In P0B2:
```python
scan_run_id = scan_run_id
require_registered_run = bool(scan_run_id)
```
1. **Watch Mode & One-Shot With Operational Run**:
   - `scan_run_id` is authenticated and registered prior to acquisition.
   - `require_registered_run=True` enforces `require_registered_operational_run(connection, run_id=effective_run_id, epoch=epoch)`.
   - Any unregistered or cross-epoch run fails closed (`RuntimeEpochOriginError`) and rolls back completely.
2. **Discovery-Failure Continuation (`scan_run_id=None`)**:
   - No fake operational run is fabricated.
   - Conforms strictly to the canonical principle in `docs/research/prospective_runtime_epoch_isolation.md`: *"insert_attempt omits an unregistered scan_run_id / last_scan_run_id instead of inventing a value."*
   - Milestone and transition events carry `scan_run_id = NULL` (nullable in `setup_lifecycle_events` schema and model).
   - In accordance with `docs/research/prospective_runtime_epoch_isolation.md`: *"run_id is one provenance component, never sufficient to grant. An unregistered diagnostic run_id is not by itself a reject."* When `scan_run_id` is omitted (`None`), `public_reservation_record_mismatch_reason` does not raise `public_reservation_scan_run_unregistered`.
   - Owned plan outcome progress (`SetupLifecycleOutcomeProgress`) has no `scan_run_id` column; cursor monotonicity and milestone accounting proceed truthfully based on closed exchange candles.
   - Active epoch validation (`require_active_runtime_epoch` and `require_current_epoch_lifecycle_id`) remains strictly enforced on every record modified.

---

## 7. Telemetry & PhaseProfile Integration

Continuation monitoring telemetry is integrated into `LifecycleWorker` and reported via `PhaseProfile.snapshot()["continuation_isolation"]`:

| Telemetry Key | Description | Unit / Format |
|---|---|---|
| `CONTINUATION_READ_WORKER_DURATION` | Wall duration of Stage 1 SQLite read | Seconds (float) |
| `CONTINUATION_WRITE_WORKER_DURATION` | Wall duration of Stage 3 SQLite write | Seconds (float) |
| `CONTINUATION_WORKER_QUEUE_WAIT` | Caller wait time for worker admission | Seconds (float) |
| `CONTINUATION_TRANSACTION_RESULT` | Result of Stage 3 transaction (`COMMITTED`, `COMMIT_FAILED_ROLLED_BACK`) | String |
| `CONTINUATION_COMPLETION_DELAY` | Latency between worker finish and receipt observation | Seconds (float) |
| `LOOP_LAG_DURING_MONITORING` | Max event-loop scheduling lag sampled during continuation | Seconds (float) |
| `IN_FLIGHT_AT_SHUTDOWN` | Operations in-flight when worker shutdown was initiated | Integer (0) |

---

## 8. DEV PC Benchmark Measurements

Measurements captured on dedicated DEV PC using `test_synthetic_continuation_loop_lag_wall_time_and_memory` with synthetic 500ms SQLite workload:

| Metric | Before P0B2 (Synchronous Event Loop) | After P0B2 (Isolated Worker) | Change / Delta |
|---|---|---|---|
| **Max Event Loop Scheduling Lag** | **0.480 s** (480 ms stall) | **0.012 s** (12 ms lag) | **-97.5% lag reduction** |
| **Induced Synchronous Block** | **0.500 s** | **0.000 s** | **Completely eliminated** |
| **Stage 1 Worker Read Duration** | N/A (on loop) | 0.523 s | Isolated to worker |
| **Stage 3 Worker Write Duration**| N/A (on loop) | 0.026 s | Isolated to worker |
| **Worker Queue Wait Time** | N/A | 0.000165 s (< 0.2 ms) | Negligible |
| **Worker Completion Delay** | N/A | 0.000386 s (< 0.4 ms) | Negligible |
| **Continuation Transaction Result**| Unobserved | `COMMITTED` | Verified |
| **Total Wall-Clock Duration** | 0.501 s | 0.555 s | Preserved (~50 ms overhead) |
| **Peak Python Heap Allocation** | Baseline | 18,457 bytes | Bounded (< 19 KB) |

> [!NOTE]
> DEV PC performance measurements are synthetic proofs of asyncio event loop unblocking. They do not constitute a prediction of Runtime wall-clock latency under live production database contention or disk latency.

---

## 9. Full Regression Test Results

All test runs were executed against dedicated synthetic temporary directories (`pytest-of-` OS temp root).

| Test Suite | File / Scope | Tests | Result |
|---|---|---|---|
| **P0B2 Continuation Isolation** | `tests/test_p0b2_continuation_monitoring_isolation.py` | 20 | **PASSED** (100%) |
| **P0B2 Scanner Ordering** | `tests/test_p0b2_scanner_ordering.py` | 3 | **PASSED** (100%) |
| **P0B1 Lifecycle Isolation** | `tests/test_p0b1_lifecycle_isolation.py` | 29 | **PASSED** (100%) |
| **P0B1 Scanner Ordering** | `tests/test_p0b1_scanner_ordering.py` | 5 | **PASSED** (100%) |
| **P0A Event Loop Isolation** | `tests/test_p0a_event_loop_isolation.py` | 19 | **PASSED** (100%) |
| **P0A Event Loop Benchmark** | `tests/test_p0a_event_loop_benchmark.py` | 1 | **PASSED** (100%) |
| **Owner Monitoring Accounting**| `tests/test_owner_monitoring_accounting.py` | 15 | **PASSED** (100%) |
| **P5A Active Owner Monitoring** | `tests/test_p5a_active_owner_monitoring.py` | 25 | **PASSED** (100%) |
| **Durable Source Replay** | `tests/test_f04_durable_source_replay.py` | 30 | **PASSED** (100%) |
| **CMC Recovery & Policy** | `tests/test_cmc_watch_recovery.py`, `tests/test_cmc_policy_research.py` | 74 | **PASSED** (100%) |
| **Watch Session** | `tests/test_watch_session.py` | 25 | **PASSED** (100%) |
| **Full Repository Test Suite** | All pytest suites (`--basetemp`) | **3,913** | **3,913 PASSED**, 0 failed |
| **Python Bytecode Compilation**| `python -m compileall app scripts src tests` | All | **PASSED** (0 errors) |
| **Pip Dependency Check** | `python -m pip check` | All | **PASSED** (No broken reqs) |

---

## 10. Preserved Invariants & Safety Boundaries

1. **No Schema Changes**: Database schema, pragma user_version, indexes, tables, triggers, and journal modes (`WAL`) are identical. No migrations or structural alterations were made.
2. **Deterministic Trading Signals**: Mathematical setup validation, RR gates, invalidation rules, closed-candle causality, and score gates are strictly untouched.
3. **No Live Order Execution**: No order submission, trade execution, wallet, transfer, or withdrawal capabilities exist or were introduced.
4. **Exchange Access**: Read-only exchange client defaults remain intact.
5. **Quarantined Telegram Reservations**: All 11 historical unresolved Telegram reservations remain quarantined. Public signal delivery idempotency is maintained.
6. **PR #152 Isolation**: Open PR #152 remains completely untouched and isolated.
7. **Runtime Deployment**: Runtime deployment is strictly NOT authorized in this phase.
