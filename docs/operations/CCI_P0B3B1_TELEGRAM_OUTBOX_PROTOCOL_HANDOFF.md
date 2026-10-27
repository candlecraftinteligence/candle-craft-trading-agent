# CANDLE CRAFT INTELLIGENCE
# P0B3B1 — TELEGRAM OUTBOX TRANSACTION / TRANSPORT PROTOCOL FOUNDATION HANDOFF

```yaml
PHASE: P0B3B1
STATUS: COMPLETED
CODING_ENVIRONMENT: DEV_PC_ONLY
HOST: Windows 11 (DEV PC)
REPOSITORY: C:\CandleCraftDev
GITHUB_REPO: https://github.com/candlecraftinteligence/candle-craft-trading-agent
P0B3A_MERGE_SHA: 8308b32a885abbba6f3317bdb1cea669863db313
P0B3A_MERGED_MAIN_CI: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40585971949
FEATURE_BRANCH: feature/p0b3b1-telegram-outbox-protocol-foundation
BASE_SHA: 8308b32a885abbba6f3317bdb1cea669863db313
LIVE_DELIVERY_PATH_MODIFIED: NO
PRODUCTION_DEPLOYMENT_APPROVED: NO
NEXT_RECOMMENDED_PHASE: P0B3B2
```

---

## 1. Executive Summary & Architectural Scope

P0B3B1 implements and validates the fundamental three-stage protocol foundation for **one already-committed, canonically reserved public alert part**:

$$\text{WORKER PREPARATION / CLAIM} \longrightarrow \text{DETACHED RECEIPT} \longrightarrow \text{ASYNC INJECTED TRANSPORT} \longrightarrow \text{WORKER PERSISTENCE} \longrightarrow \text{DURABLE RECEIPT}$$

### Scope Boundaries
- **Protocol Foundation Only**: `app/alerts/telegram_outbox_protocol.py` is implemented and verified as a standalone, modular protocol.
- **Disconnected From Production**: Existing `TelegramLifecycleDeliveryService.deliver_for_run` remains 100% unmodified in this phase (`LIVE_DELIVERY_PATH_MODIFIED: NO`).
- **Dev-Only Verification**: All execution, stress-testing, and concurrency proofs were conducted on DEV PC with temporary SQLite databases and synthetic transports. Zero live HTTP calls or live order execution occurred.
- **Stop Fence Maintained**: Full production refactoring of `TelegramLifecycleDeliveryService.deliver_for_run` is deferred to P0B3B2 as planned.

---

## 2. Three-Stage Protocol Architecture

### Stage 1: Worker-Owned Preparation & Atomic Claim (`stage_1_outbox_preparation`)
- **Execution Target**: Dedicated worker thread (`TelegramOutboxWorker`, thread name `cci-telegram-outbox`).
- **Connection Lifecycle**: Opened with `open_repository_database(database_path, expected_identity=expected_identity)`. Completely confined to the worker thread; never escapes across threads or yields to the event loop.
- **Validation Gates**:
  1. Active runtime epoch verified (`load_active_runtime_epoch`).
  2. Canonical reservation association verified (`require_event_reservation_association`).
  3. Public effect authority verified (`require_canonical_reservation_for_public_effect`, `decide_public_effect`).
  4. Current delivery state verified (`PENDING`, `RETRYABLE`, or `IN_FLIGHT` with matching attempt ID).
- **Atomic Claim**:
  - Uses `SQLitePublicTelegramOutbox.claim(...)` under `BEGIN IMMEDIATE` write lock to resolve cross-process/multi-threaded claim races deterministically.
  - Transitions part to `IN_FLIGHT` via `mark_part_in_flight`.
  - Commits transaction and unconditionally closes SQLite connection in `finally:`.
- **Output**: Detached, immutable `CommittedPartReservationReceipt` containing strictly decoupled strings and integers (no live connection objects).

### Stage 2: Asynchronous Detached Transport (`stage_2_async_transport`)
- **Execution Target**: Main asyncio event loop.
- **Connection State**: **Zero** open SQLite connections or transaction contexts exist.
- **Transport Contract**: Invokes injected async callable `transport(TelegramTransportRequest) -> TelegramTransportOutcome`.
- **Strict Error Classification**:
  - Confirmed HTTP 200 + telegram message ID $\rightarrow$ `SENT`.
  - 429 rate limit or 5xx gateway error $\rightarrow$ `RETRYABLE`.
  - Client timeout, network connection reset after attempt started, or unexpected exception $\rightarrow$ strictly classified as `UNCERTAIN` (`transport_exception_after_attempt_started`).
  - Unconfirmed responses are **never** assumed unsent.

### Stage 3: Worker-Owned Result Persistence (`stage_3_outbox_persistence`)
- **Execution Target**: Dedicated worker thread (`TelegramOutboxWorker`).
- **Connection Lifecycle**: Fresh worker-owned connection opened and verified against active epoch.
- **Attempt Invariant Validation**: Validates that `public_alert_events.attempt_id` and `public_alert_delivery_parts.attempt_id` match `receipt.attempt_id` exactly (superseded claim protection).
- **Durable Persistence**:
  - Calls `outbox.record_part_result(...)` to update part state and resolve overall event state.
  - Commits transaction and unconditionally closes SQLite connection in `finally:`.
  - Handles commit failure via `outbox.mark_uncertain_after_persistence_failure` fallback; raises `PersistenceCommitFailureError` if unrecoverable.
- **Output**: Detached, immutable `DurableDeliveryReceipt` confirming durable database commit.

### Pipeline Coordinator (`deliver_single_reserved_part`)
- Coordinates Stages 1, 2, and 3.
- **Cancellation Fencing**:
  - Pre-transport cancellation check ensures zero HTTP calls if cancelled prior to transport.
  - If cancellation interrupts Stage 2 HTTP transmission, the transmission outcome is unknown. The coordinator fences cancellation, enters Stage 3 persistence to record `UNCERTAIN` in SQLite, and only then re-propagates `asyncio.CancelledError`.
  - If database commit fails after HTTP, returns `status="unresolved_persistence"` with `durable_receipt=None`.

---

## 3. Worker Confinement & Serialization (`TelegramOutboxWorker`)

- Bounded queue (`maxsize=1`) and serialized admission (`asyncio.Lock()`) permitting at most 1 active worker operation and 1 waiting caller.
- Rejects live `sqlite3.Connection` objects passed as arguments (`TypeError`).
- Uses `_fenced_wait` to protect active transactions from abrupt event-loop cancellation: the worker thread always finishes its transaction and closes the SQLite connection before cancellation propagates.
- Implements `aclose()` for deterministic shutdown and thread join.

---

## 4. Test Verification & Evidence

Test suite [`tests/test_p0b3b1_telegram_outbox_protocol.py`](file:///C:/CandleCraftDev/tests/test_p0b3b1_telegram_outbox_protocol.py) executes all 22 required failure and concurrency scenarios with strict transport invocation count assertions:

| # | Test Scenario | Verified Behavior | Transport Calls | Status |
|---|---|---|---|---|
| **01** | Valid committed reservation | Transitions to `SENT`, durable receipt returned | Exactly 1 | **PASSED** |
| **02** | Missing canonical reservation | Fails closed before transport (`blocked`) | Exactly 0 | **PASSED** |
| **03** | Epoch mismatch | Fails closed before transport (`blocked`) | Exactly 0 | **PASSED** |
| **04** | Two competing claimants (race) | Only 1 claimant wins atomic claim; 2nd skipped | Exactly 1 | **PASSED** |
| **05** | Multithreaded claim race (4 threads) | SQLite `BEGIN IMMEDIATE` grants claim to exactly 1 | N/A (Stage 1) | **PASSED** |
| **06** | Duplicate existing `SENT` event | Claim rejected before transport (`skipped`) | Exactly 0 | **PASSED** |
| **07** | Existing `UNCERTAIN` event | Claim rejected before transport (`skipped`) | Exactly 0 | **PASSED** |
| **08** | Cancellation before claim | 0 DB writes, 0 HTTP calls; propagates `CancelledError` | Exactly 0 | **PASSED** |
| **09** | Cancellation during worker transaction | `_fenced_wait` completes cleanly; DB left in valid state | Exactly 0 | **PASSED** |
| **10** | Cancellation after claim before HTTP | Transport avoided; zero HTTP calls made | Exactly 0 | **PASSED** |
| **11** | Cancellation during HTTP transport | Persists `UNCERTAIN` to SQLite before re-raising | Exactly 1 | **PASSED** |
| **12** | HTTP 200 response | Durably persists `SENT` with telegram message ID | Exactly 1 | **PASSED** |
| **13** | HTTP timeout response | Durably persists `UNCERTAIN` with timeout category | Exactly 1 | **PASSED** |
| **14** | Worker failure during persistence | Returns `unresolved_persistence` (`durable_receipt=None`) | Exactly 1 | **PASSED** |
| **15** | Commit failure after HTTP success | Returns `unresolved_persistence` (`durable_receipt=None`) | Exactly 1 | **PASSED** |
| **16** | Restart after ambiguous HTTP | Refuses re-claim of `UNCERTAIN` event; 0 HTTP calls | Exactly 0 | **PASSED** |
| **17** | Multipart partial failure | Part 1 `SENT` (event `IN_FLIGHT`), Part 2 `UNCERTAIN` (event `UNCERTAIN`) | Exactly 2 | **PASSED** |
| **18** | Multipart confirmation sequence | Event finalized as `SENT` only after Part 2 completes | Exactly 2 | **PASSED** |
| **19** | No second attempt for SENT/UNCERTAIN | Subsequent attempts blocked at Stage 1; 0 HTTP calls | Exactly 0 | **PASSED** |
| **20** | Zero connection held during network await | Concurrent external write succeeds during HTTP await | Exactly 1 | **PASSED** |
| **21** | Existing service interface unmodified | `TelegramLifecycleDeliveryService` interface intact | N/A | **PASSED** |
| **22** | No mutation of 11 quarantined records | All 11 legacy records rejected; 0 calls; 0 DB mutations | Exactly 0 | **PASSED** |

### Test Run Outcome
```
============================= test session starts =============================
platform win32 -- Python 3.11.9, pytest-8.4.2, pluggy-1.6.0
rootdir: C:\CandleCraftDev
configfile: pytest.ini
plugins: anyio-4.13.0
collected 22 items

tests\test_p0b3b1_telegram_outbox_protocol.py ......................     [100%]

============================= 22 passed in 5.74s ==============================
```

---

## 5. Regression Test Results

Full execution across isolated modules:
- `tests/test_p0a_event_loop_isolation.py`: PASSED
- `tests/test_p0b1_lifecycle_isolation.py`: PASSED
- `tests/test_p0b2_continuation_monitoring_isolation.py`: PASSED
- `tests/test_telegram_outbox_recovery.py`: PASSED
- `tests/test_cmc_watch_recovery.py`: PASSED
- **Total Regressions**: **0**

---

## 6. Files Created

1. [`app/alerts/telegram_outbox_protocol.py`](file:///C:/CandleCraftDev/app/alerts/telegram_outbox_protocol.py): Core three-stage protocol foundation, worker thread, and receipts.
2. [`tests/test_p0b3b1_telegram_outbox_protocol.py`](file:///C:/CandleCraftDev/tests/test_p0b3b1_telegram_outbox_protocol.py): 22 synthetic test scenarios verifying protocol invariants.
3. [`docs/operations/CCI_P0B3B1_TELEGRAM_OUTBOX_PROTOCOL_HANDOFF.md`](file:///C:/CandleCraftDev/docs/operations/CCI_P0B3B1_TELEGRAM_OUTBOX_PROTOCOL_HANDOFF.md): This handoff document.

---

## 7. Next Recommended Phase

**Phase P0B3B2**:
- Safely integrate `deliver_single_reserved_part` and `TelegramOutboxWorker` into `TelegramLifecycleDeliveryService.deliver_for_run`.
- Decompose the outer open repository context managers (`SQLiteTelegramAlertAttemptRepository`, `SQLiteSetupLifecycleRepository`) to eliminate event-loop connection holding.
- Ensure end-to-end integration equivalence with zero production regressions.
