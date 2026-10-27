# CANDLE CRAFT INTELLIGENCE
# P0B3B1 — TELEGRAM OUTBOX TRANSACTION / TRANSPORT PROTOCOL FOUNDATION HANDOFF

```yaml
PHASE: P0B3B1 (HARDENED RELEASE GATE)
STATUS: COMPLETED
CODING_ENVIRONMENT: DEV_PC_ONLY
HOST: Windows 11 (DEV PC)
REPOSITORY: C:\CandleCraftDev
GITHUB_REPO: https://github.com/candlecraftinteligence/candle-craft-trading-agent
P0B3A_MERGE_SHA: 8308b32a885abbba6f3317bdb1cea669863db313
FEATURE_BRANCH: feature/p0b3b1-telegram-outbox-protocol-foundation
BASE_SHA: 8308b32a885abbba6f3317bdb1cea669863db313
PREVIOUS_HEAD_SHA: 3f0dcaee8c628425ef427a10b4fe013c0431267c
PREVIOUS_HEAD_CI: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40589390588
LIVE_DELIVERY_PATH_MODIFIED: NO
PRODUCTION_DEPLOYMENT_APPROVED: NO
NEXT_RECOMMENDED_PHASE: P0B3B2
```

---

## 1. Executive Summary & Hardening Scope

This phase completes the safety-hardening pass of the **P0B3B1 Telegram outbox transaction/transport protocol foundation**.

Four critical correctness issues identified during independent architectural review were investigated, deterministically reproduced, and resolved with targeted, minimal changes:

1. **Issue 1 — Repeated Cancellation During Stage 3 Persistence**: Fenced admission lock acquisition and persistence execution to prevent cancellation from interrupting Stage 3 before worker admission or database completion.
2. **Issue 2 — False Durable Receipt After SQLite Failure**: Eliminated unverified receipts caused by silent error suppression in legacy fallbacks; introduced mandatory independent SQLite commit verification queries before issuing any `DurableDeliveryReceipt`.
3. **Issue 3 — Telegram SENT Evidence & Ambiguous HTTP Outcomes**: Enforced strict provider acknowledgment validation (`SENT` requires HTTP 200, non-empty message ID, and no rate limiting) and normalized ambiguous edge errors (HTTP 5xx after transmission started fails closed to `UNCERTAIN` to prevent duplicate public signals).
4. **Issue 4 — Stage 1 Cancellation Combined with Worker Exception**: Preserved cancellation intent and chained fatal epoch errors (`RuntimeEpochOwnershipError`, `RuntimeEpochError`) so that cancellation is not masked as ordinary recoverable validation failure.

---

## 2. Hardening Investigations & Implemented Corrections

### Issue 1: Repeated Cancellation During Stage 3
- **Vulnerability**: In `TelegramOutboxWorker.call()`, `await self._admission.acquire()` was previously unprotected. If cancelled while waiting for admission to persist post-HTTP outcomes, `CancelledError` was raised before enqueueing the job, leaving the database row in `IN_FLIGHT` despite completed external HTTP transmission.
- **Reproduction**: Reproduced by cancelling the delivery task after HTTP transmission returned but before Stage 3 worker admission.
- **Correction**:
  - Admission acquisition for `stage == "persistence"` is wrapped in an `asyncio.Task` and awaited via `_fenced_wait`, ensuring admission cannot be aborted by cancellation.
  - In Python 3.11, pending cancellation is uncancelled during bounded persistence and re-raised only after durability is resolved.
  - In `TelegramOutboxWorker.aclose()`, shutdown is cleanly awaited via `_fenced_wait` without leaving zombie threads.

### Issue 2: False Durable Receipt After SQLite Failure
- **Vulnerability**: Legacy helper `SQLitePublicTelegramOutbox.mark_uncertain_after_persistence_failure()` catches and suppresses `sqlite3.Error` internally (`except sqlite3.Error: return`). Consequently, if `record_part_result` failed and the fallback also failed to commit, Stage 3 was previously returning a `DurableDeliveryReceipt(delivery_state=UNCERTAIN)` while SQLite still held `IN_FLIGHT`.
- **Reproduction**: Synthesized SQLite `OperationalError` during `record_part_result` paired with a silent fallback no-op.
- **Correction**:
  - Implemented mandatory post-commit verification query directly against SQLite: validates exact `event_id`, `reservation_id`, `part_id`, `attempt_id`, and verifies that neither event nor part remains `IN_FLIGHT`.
  - Also verified that if fallback to `UNCERTAIN` occurred, the fallback state was actually written and committed in SQLite.
  - If SQLite state does not match the requested state, raises `PersistenceCommitFailureError`. The coordinator catches this and returns `status="unresolved_persistence"` with `durable_receipt=None`.

### Issue 3: Telegram SENT Evidence & Ambiguous HTTP Outcomes
- **Vulnerability**: In distributed reverse-proxy environments (e.g. Cloudflare / Telegram Bot API gateways), HTTP 5xx responses (500, 502, 503, 504) returned after transmission started can represent successful gateway ingest. Retrying these as `RETRYABLE` risks duplicate public signals. Furthermore, a provider claiming `SENT` without a message ID cannot be confirmed.
- **Reproduction**: Tested transport outcomes claiming `SENT` with missing message IDs, non-200 HTTP codes, or HTTP 5xx statuses.
- **Correction**:
  - Implemented `normalize_transport_outcome(outcome)`:
    - `SENT` requires non-empty `telegram_message_id`, `http_status == 200`, `error in (None, "", NA)`, and `not rate_limited`. Contradictory successes fail closed to `UNCERTAIN`.
    - `RETRYABLE` with HTTP status $500 \le \text{status} \le 599$ after transmission started is normalized to `UNCERTAIN` (`http_5xx_uncertain_acceptance`), preventing unsafe automated re-transmissions.

### Issue 4: Stage 1 Cancellation Precedence
- **Vulnerability**: If a task was cancelled while Stage 1 was executing, catching `OutboxPreparationError` could mask cancellation as an ordinary recoverable validation failure (`status="blocked"`), allowing subsequent scanner iterations to proceed instead of stopping cooperatively.
- **Reproduction**: Cancelled task during Stage 1 worker preparation.
- **Correction**:
  - Stage 1 preserves cancellation intent: if `task.cancelling()` is active, `deliver_single_reserved_part` re-raises `asyncio.CancelledError`.
  - Fatal errors (`RuntimeEpochOwnershipError`, `RuntimeEpochError`, `sqlite3.DatabaseError`) are never suppressed or converted into ordinary failures; they are re-raised with cancellation chained (`raise receipt.error from cancelled`).

---

## 3. Protocol Safety Invariants (Confirmed)

| Invariant | Property | Verified Status |
|---|---|---|
| `CANCELLATION_ADMISSION_SAFETY` | Stage 3 persistence cannot be aborted before admission or completion by single or repeated cancellations | **CONFIRMED** |
| `POST_HTTP_PERSISTENCE_SAFETY` | Post-HTTP persistence is fenced; durable database outcome is established before cancellation propagates | **CONFIRMED** |
| `DURABLE_RECEIPT_VERIFICATION` | `DurableDeliveryReceipt` issued ONLY after independent SQLite query confirms actual committed state; no false receipts | **CONFIRMED** |
| `TRANSPORT_SENT_EVIDENCE` | `SENT` strictly requires HTTP 200, valid provider message ID, zero error, and not rate-limited | **CONFIRMED** |
| `UNCERTAIN_RETRY_SAFETY` | HTTP 5xx after transmission started fails closed to `UNCERTAIN`; no automated duplicate public broadcasts | **CONFIRMED** |
| `CANONICAL_RESERVATION_GUARD` | Canonical reservations and historical 11 quarantined records are preserved without mutation | **CONFIRMED** |
| `ZERO_HTTP_ON_BLOCKED_STAGE1` | Any failure or cancellation prior to Stage 2 guarantees exactly 0 HTTP transport calls | **CONFIRMED** |
| `EVENT_LOOP_ZERO_CONNECTION_HOLDING` | Zero SQLite connections or transaction contexts held across asynchronous network awaits | **CONFIRMED** |

---

## 4. Hardening Test Suite Evidence

Test suite [`tests/test_p0b3b1_telegram_outbox_protocol.py`](file:///C:/CandleCraftDev/tests/test_p0b3b1_telegram_outbox_protocol.py) executes 20 dedicated hardening scenarios plus 3 regression guards (23 tests total):

| # | Hardening Scenario | Verified Behavior | Calls | Status |
|---|---|---|---|---|
| **01** | Cancellation after HTTP before Stage 3 admission | Fenced admission; commits `SENT`; re-raises `CancelledError` | Exactly 1 | **PASSED** |
| **02** | Repeated cancellation during admission wait | Double cancellation cannot abort admission; commits `SENT` | Exactly 1 | **PASSED** |
| **03** | Cancellation during Stage 3 transaction | Worker transaction completes atomically; commits `SENT` | Exactly 1 | **PASSED** |
| **04** | Cancellation during commit | Commit completes; commits `SENT`; re-raises `CancelledError` | Exactly 1 | **PASSED** |
| **05** | Cancellation during worker shutdown | Thread joined cleanly; zero zombie threads | Exactly 0 | **PASSED** |
| **06** | SQLite result persistence raises before commit | Fallback catches error and commits `UNCERTAIN` | Exactly 1 | **PASSED** |
| **07** | SQLite UNCERTAIN fallback silently fails | Verification detects uncommitted state; returns `unresolved_persistence` | Exactly 1 | **PASSED** |
| **08** | Database remains IN_FLIGHT after failed fallback | SQLite row verified in `IN_FLIGHT`; never falsely declared `UNCERTAIN` | Exactly 1 | **PASSED** |
| **09** | No false DurableDeliveryReceipt | `durable_receipt is None` when persistence was unverified | Exactly 1 | **PASSED** |
| **10** | Valid HTTP success with verified message ID | Validated as `SENT`; durable receipt issued | Exactly 1 | **PASSED** |
| **11** | Success missing Telegram message ID | Normalized to `UNCERTAIN` (`confirmed_success_missing_message_id`) | Exactly 1 | **PASSED** |
| **12** | Contradictory success with HTTP 500 | Normalized to `UNCERTAIN` (`contradictory_success_http_status`) | Exactly 1 | **PASSED** |
| **13** | Contradictory success reporting error message | Normalized to `UNCERTAIN` (`contradictory_success_error`) | Exactly 1 | **PASSED** |
| **14** | Contradictory success marked rate_limited | Normalized to `UNCERTAIN` (`contradictory_success_rate_limited`) | Exactly 1 | **PASSED** |
| **15** | Ambiguous HTTP 500 server error | Normalized to `UNCERTAIN` (`http_5xx_uncertain_acceptance`) | Exactly 1 | **PASSED** |
| **16** | Ambiguous HTTP 502 bad gateway | Normalized to `UNCERTAIN` (`http_5xx_uncertain_acceptance`) | Exactly 1 | **PASSED** |
| **17** | Stage 1 cancelled before worker execution | Re-raises `CancelledError`; 0 HTTP calls; 0 DB mutations | Exactly 0 | **PASSED** |
| **18** | Stage 1 worker exception and cancelled caller | Preserves cancellation intent; re-raises `CancelledError` | Exactly 0 | **PASSED** |
| **19** | Stage 1 fatal epoch error and cancelled caller | Preserves fatal `RuntimeEpochOwnershipError` with chained cancel | Exactly 0 | **PASSED** |
| **20** | Stage 1 recoverable validation error and cancelled | Propagates cancellation; does not mask as ordinary blocked | Exactly 0 | **PASSED** |
| **21** | Regression guard: zero connections during HTTP | Concurrent SQLite write succeeds during HTTP await | Exactly 1 | **PASSED** |
| **22** | Regression guard: legacy interface intact | `TelegramLifecycleDeliveryService` unaffected | N/A | **PASSED** |
| **23** | Regression guard: quarantine of 11 records | Historical records rejected; 0 calls; 0 DB mutations | Exactly 0 | **PASSED** |

### Test Run Outcome
```
tests\test_p0b3b1_telegram_outbox_protocol.py .......................    [100%]
============================= 23 passed in 5.48s ==============================
```

---

## 5. Regression Suite Verification

Full regression test run executed against all existing isolated modules:

```
collected 100 items

tests\test_p0a_event_loop_isolation.py ..................                [ 18%]
tests\test_p0b1_lifecycle_isolation.py .............................     [ 47%]
tests\test_p0b2_continuation_monitoring_isolation.py ................... [ 66%]
.                                                                        [ 67%]
tests\test_telegram_outbox_recovery.py ...................               [ 86%]
tests\test_cmc_watch_recovery.py ..............                          [100%]

============================ 100 passed in 24.28s =============================
```

Combined outbox test run:
```
tests\test_p0b3b1_telegram_outbox_protocol.py .......................    [ 54%]
tests\test_telegram_outbox_recovery.py ...................               [100%]

============================= 42 passed in 8.15s ==============================
```

- **Total Tests Passed**: **142**
- **Regressions Introduced**: **0**

---

## 6. Files Modified

1. [`app/alerts/telegram_outbox_protocol.py`](file:///C:/CandleCraftDev/app/alerts/telegram_outbox_protocol.py): Hardened admission fencing, mandatory post-commit SQLite verification, transport outcome normalization, and cancellation precedence.
2. [`tests/test_p0b3b1_telegram_outbox_protocol.py`](file:///C:/CandleCraftDev/tests/test_p0b3b1_telegram_outbox_protocol.py): 23 test scenarios covering all 20 hardening requirements plus 3 regression guards.
3. [`docs/operations/CCI_P0B3B1_TELEGRAM_OUTBOX_PROTOCOL_HANDOFF.md`](file:///C:/CandleCraftDev/docs/operations/CCI_P0B3B1_TELEGRAM_OUTBOX_PROTOCOL_HANDOFF.md): Updated handoff report.

---

## 7. Next Recommended Phase

**Phase P0B3B2 — Production Integration & Outer Context Manager Decomposition**:
- Safely integrate `deliver_single_reserved_part` and `TelegramOutboxWorker` into `TelegramLifecycleDeliveryService.deliver_for_run`.
- Decompose the outer open repository context managers (`SQLiteTelegramAlertAttemptRepository`, `SQLiteSetupLifecycleRepository`) to eliminate event-loop connection holding.
- Ensure end-to-end integration equivalence with zero production regressions.
