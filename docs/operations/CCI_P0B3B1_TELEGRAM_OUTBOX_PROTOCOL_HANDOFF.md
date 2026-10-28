# CANDLE CRAFT INTELLIGENCE
# P0B3B1 — TELEGRAM OUTBOX TRANSACTION / TRANSPORT PROTOCOL FOUNDATION HANDOFF

```yaml
PHASE: P0B3B1 (FINAL CONTRACT CORRECTION & REGRESSION RESTORATION)
STATUS: COMPLETED
CODING_ENVIRONMENT: DEV_PC_ONLY
HOST: Windows 11 (DEV PC)
REPOSITORY: C:\CandleCraftDev
GITHUB_REPO: https://github.com/candlecraftinteligence/candle-craft-trading-agent
P0B3A_MERGE_SHA: 8308b32a885abbba6f3317bdb1cea669863db313
FEATURE_BRANCH: feature/p0b3b1-telegram-outbox-protocol-foundation
BASE_SHA: 8308b32a885abbba6f3317bdb1cea669863db313
REVIEWED_HEAD_SHA: 3aa0c8cc0bb753b2f281afc3393db5d9685de4c2
LAST_SUCCESSFUL_CI: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40594723659
LIVE_DELIVERY_PATH_MODIFIED: NO
PRODUCTION_DEPLOYMENT_APPROVED: NO
NEXT_RECOMMENDED_PHASE: P0B3B2
PR_URL: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/157
MERGE_STATUS: OPEN
```

---

## 1. Executive Summary

This release completes the final contract correction and regression restoration pass for the **P0B3B1 Telegram outbox transaction/transport protocol foundation**.

All four release-contract discrepancies identified during independent architecture and regression review have been resolved, deterministically verified, and confirmed against all test suites:

1. **Issue 1 — Sent Evidence Contract Reconciled**:
   - `normalize_transport_outcome()` now strictly rejects missing HTTP statuses (`http_status=None`), failing closed to `UNCERTAIN` (`missing_success_http_status`).
   - A `SENT` delivery state strictly requires: HTTP 200, non-empty `telegram_message_id`, no provider error message, and not rate limited.
   - Preserves legitimate Telegram sender compatibility while preventing ambiguous acknowledgments from being treated as proven delivery.
   - Documented the Phase P0B3B2 adapter obligation to translate raw `TelegramSendResult` from `TelegramSender` into verified `TelegramTransportOutcome`.
2. **Issue 2 — Fatal Runtime Epoch Errors vs Claim Rejections vs Ordinary Eligibility Refusals**:
   - Stage 1 cleanly distinguishes:
     - **Expected claim rejection** (`OutboxClaimRejected` $\rightarrow$ returns `status="skipped"`).
     - **Missing canonical reservation** (`OutboxPreparationError` $\rightarrow$ returns `status="blocked"` without transport call).
     - **Ordinary public eligibility refusal** (`RuntimeEpochOwnershipError` $\rightarrow$ raises `OutboxPreparationError`, returning `status="blocked"` without transport call).
     - **Fatal runtime epoch mismatch** (`RuntimeEpochConfigurationError`, `RuntimeEpochMismatchError`, `RuntimeEpochMissingError`, `RuntimeEpochCutoffExceeded` $\rightarrow$ propagates uncaught fatal `RuntimeEpochError`).
     - **Fatal database errors** (`sqlite3.DatabaseError` $\rightarrow$ propagates uncaught fatal database error).
   - Fatal runtime epoch and database errors propagate even if the caller task was cancelled, with zero transport invocations.
3. **Issue 3 — Full Baseline Test Suite Restored Alongside Hardening & Evidence Tests**:
   - Restored all 22 original baseline scenarios (`test_baseline_01` through `test_baseline_22`).
   - Retained all 20 safety hardening scenarios (`test_hardening_01` through `test_hardening_20`).
   - Retained 10 dedicated evidence contract and fatal epoch boundary scenarios (`test_evidence_...` and `test_fatal_epoch_...`).
   - Total test suite now contains **52 passing synthetic tests**, strictly checking transport invocation counts.
4. **Issue 4 — Durable Receipt Matching & Multipart Delivery Invariants**:
   - `stage_3_outbox_persistence()` independently queries SQLite rows to verify committed states against the target state before issuing `DurableDeliveryReceipt`.
   - Explicitly accounts for multipart deliveries: part 1 correctly commits with `part=SENT` while `event=IN_FLIGHT` until all parts finish.
   - Returns `DurableDeliveryReceipt.delivery_state` matching the committed SQLite row.

---

## 2. Detailed Technical Resolutions

### Issue 1: Sent Evidence Contract & Adapter Obligation
- **Problem**: Previously, `normalize_transport_outcome()` allowed `SENT` outcomes where `http_status` was `None`. This contradicted the documented contract requiring verified HTTP 200 acknowledgment.
- **Resolution**:
  - In `app/alerts/telegram_outbox_protocol.py`:
    ```python
    if outcome.http_status != 200:
        err_cat = "missing_success_http_status" if outcome.http_status is None else "contradictory_success_http_status"
        err_msg = (
            "Telegram success response omitted HTTP status; acceptance is uncertain."
            if outcome.http_status is None
            else f"Transport claimed SENT with non-200 HTTP status: {outcome.http_status}"
        )
        return TelegramTransportOutcome(
            delivery_state=UNCERTAIN,
            http_status=outcome.http_status,
            telegram_message_id=msg_id,
            telegram_chat_id=outcome.telegram_chat_id,
            sent_at=outcome.sent_at,
            error_category=err_cat,
            error=err_msg,
        )
    ```
  - Documented the Phase P0B3B2 adapter obligation in docstring:
    ```python
    """Validate and normalize external transport outcome against Telegram provider evidence contracts.

    ADAPTER INTEGRATION OBLIGATION (Phase P0B3B2):
    The standalone P0B3B1 protocol requires an explicit `TelegramTransportOutcome` containing verified
    `http_status=200` and non-empty `telegram_message_id`. When integrating `TelegramSender` in P0B3B2,
    an adapter function must extract the underlying `result['http_status']` and `result['message_id']`
    from `TelegramSendResult.telegram_results` to produce a verified `TelegramTransportOutcome`.
    Missing or unverified HTTP statuses must not silently count as proven HTTP 200.
    """
    ```

### Issue 2: Fatal Runtime Epoch Errors vs Eligibility Refusals
- **Problem**: Stage 1 exception handling previously caught `RuntimeEpochError` and masked fatal epoch mismatches into `status="blocked"`. Additionally, ordinary public eligibility refusals (like unverified legacy records or audit-only attempts) were not distinguished from database-wide configuration errors.
- **Resolution**:
  - `open_repository_database()` continues to enforce operational identity. Any database-level epoch mismatch, cutoff violation, or schema version mismatch raises fatal `RuntimeEpochConfigurationError` / `RuntimeEpochMismatchError`.
  - In `stage_1_outbox_preparation()`, per-record ownership/eligibility checks (`require_event_reservation_association`, `require_canonical_reservation_for_public_effect`) raise `RuntimeEpochOwnershipError` when an event is legacy/unverified, audit-only, or has missing reservations. These are converted to `OutboxPreparationError`, returning `status="blocked"` with 0 transport calls.
  - In `deliver_single_reserved_part()`:
    ```python
    except OutboxClaimRejected as exc:
        ...
        return TelegramDeliveryProtocolResult(status="skipped", ...)
    except OutboxPreparationError as exc:
        ...
        return TelegramDeliveryProtocolResult(status="blocked", ...)
    except (asyncio.CancelledError, KeyboardInterrupt, RuntimeEpochError, sqlite3.DatabaseError):
        raise
    ```
  - In `TelegramOutboxWorker.call()`:
    - Bounded fencing is applied to both preparation and persistence stages so that fatal epoch and database errors are raised cleanly to the caller even if `task.cancelling()` was True.
    - Verified that zero transport calls occur on any fatal Stage 1 failure.

### Issue 3: Restored Full Test Suite (52 Tests)
- **Problem**: Previous commit replaced baseline regression tests with hardening tests.
- **Resolution**:
  - Combined all 22 baseline scenarios, 20 safety hardening scenarios, and 10 dedicated evidence/fatal epoch scenarios into [`tests/test_p0b3b1_telegram_outbox_protocol.py`](file:///C:/CandleCraftDev/tests/test_p0b3b1_telegram_outbox_protocol.py).
  - All 52 tests run deterministically and synchronously using `asyncio.run()`, with strict assertion on `len(transport.calls)`.

### Issue 4: Durable Receipt Matching & Multipart Invariants
- **Problem**: Multipart deliveries have distinct committed states: for part 1 of a multipart message, `public_alert_delivery_parts.delivery_state == 'SENT'` while `public_alert_events.delivery_state == 'IN_FLIGHT'` until all parts finish. A naive equality check against the event row resulted in false persistence commit failure.
- **Resolution**:
  - In `stage_3_outbox_persistence()`:
    ```python
    if state == SENT:
        if actual_part_state != SENT:
            raise PersistenceCommitFailureError(
                f"Part {receipt.part_id} delivery_state={actual_part_state} does not match expected SENT"
            )
        if all_sent and actual_event_state != SENT:
            raise PersistenceCommitFailureError(
                f"All parts sent but event {receipt.event_id} delivery_state={actual_event_state} != SENT"
            )
        if not all_sent and actual_event_state != IN_FLIGHT:
            raise PersistenceCommitFailureError(
                f"Multipart partial send: event {receipt.event_id} delivery_state={actual_event_state} != IN_FLIGHT"
            )
    elif state == UNCERTAIN:
        if actual_event_state != UNCERTAIN or actual_part_state != UNCERTAIN:
            raise PersistenceCommitFailureError(...)
    ```
  - Validates exact consistency between SQLite rows and the returned `DurableDeliveryReceipt`.

---

## 3. Protocol Safety Invariants (Confirmed)

| Invariant | Property | Verified Status |
|---|---|---|
| `CANCELLATION_ADMISSION_SAFETY` | Stage 3 persistence cannot be aborted before admission or completion by single or repeated cancellations | **CONFIRMED** |
| `POST_HTTP_PERSISTENCE_SAFETY` | Post-HTTP persistence is fenced; durable database outcome is established before cancellation propagates | **CONFIRMED** |
| `DURABLE_RECEIPT_VERIFICATION` | `DurableDeliveryReceipt` issued ONLY after independent SQLite query confirms actual committed state | **CONFIRMED** |
| `TRANSPORT_SENT_EVIDENCE` | `SENT` strictly requires HTTP 200, valid provider message ID, zero error, and not rate-limited | **CONFIRMED** |
| `MISSING_HTTP_STATUS_CLOSED` | Success outcomes missing HTTP status fail closed to `UNCERTAIN` (`missing_success_http_status`) | **CONFIRMED** |
| `FATAL_EPOCH_PROPAGATION` | Database-level epoch mismatches and SQLite disk/corruption errors propagate uncaught as fatal errors | **CONFIRMED** |
| `ORDINARY_ELIGIBILITY_BLOCKED` | Legacy/unverified records and audit-only reservations return `blocked` with 0 transport calls | **CONFIRMED** |
| `CANONICAL_RESERVATION_GUARD` | Canonical reservations and historical 11 quarantined records are preserved without mutation | **CONFIRMED** |
| `ZERO_HTTP_ON_BLOCKED_STAGE1` | Any failure or cancellation prior to Stage 2 guarantees exactly 0 HTTP transport calls | **CONFIRMED** |
| `EVENT_LOOP_ZERO_CONNECTION_HOLDING` | Zero SQLite connections or transaction contexts held across asynchronous network awaits | **CONFIRMED** |

---

## 4. Test Suite Evidence

### 4.1 Protocol Test Suite (`tests/test_p0b3b1_telegram_outbox_protocol.py`)
52 tests total:
- **Part 1: Baseline Protocol Tests (22 tests)**:
  - `test_baseline_01_single_part_happy_path_sent`
  - `test_baseline_02_single_part_retryable_error`
  - `test_baseline_03_single_part_failed_final`
  - `test_baseline_04_two_competing_claimants_atomic_claim_race`
  - `test_baseline_05_multithreaded_claim_race`
  - `test_baseline_06_in_flight_timeout_recovery_reclaim`
  - `test_baseline_07_in_flight_active_lease_rejected`
  - `test_baseline_08_uncertain_outcome_stops_subsequent_attempts`
  - `test_baseline_09_max_attempts_exhaustion_failed_final`
  - `test_baseline_10_cancellation_after_committed_claim_before_http`
  - `test_baseline_11_cancellation_during_http_uncertain_recovery`
  - `test_baseline_12_cancellation_after_http_before_receipt`
  - `test_baseline_13_worker_closed_rejects_new_calls`
  - `test_baseline_14_worker_saturation_bounded_queue`
  - `test_baseline_15_worker_drain_and_shutdown`
  - `test_baseline_16_multipart_all_parts_sent_event_marked_sent`
  - `test_baseline_17_multipart_partial_failure_part1_sent_part2_uncertain`
  - `test_baseline_18_idempotent_duplicate_submission`
  - `test_baseline_19_unassociated_reservation_rejected`
  - `test_baseline_20_missing_canonical_reservation_blocked`
  - `test_baseline_21_database_error_during_claim_propagated`
  - `test_baseline_22_no_mutation_of_historical_quarantined_records`
- **Part 2: Safety Hardening Tests (20 tests)**:
  - `test_hardening_01_cancellation_after_http_before_stage3_admission`
  - `test_hardening_02_repeated_cancellation_during_admission_wait`
  - `test_hardening_03_cancellation_during_stage3_transaction`
  - `test_hardening_04_cancellation_during_commit`
  - `test_hardening_05_cancellation_during_worker_shutdown`
  - `test_hardening_06_sqlite_result_persistence_raises_before_commit`
  - `test_hardening_07_sqlite_uncertain_fallback_silently_fails`
  - `test_hardening_08_database_remains_in_flight_after_failed_fallback`
  - `test_hardening_09_no_false_durable_delivery_receipt`
  - `test_hardening_10_valid_http_success_verified_message_id`
  - `test_hardening_11_malformed_sent_outcome_no_message_id`
  - `test_hardening_12_contradictory_sent_outcome_failed_http_status`
  - `test_hardening_13_unknown_provider_acknowledgment`
  - `test_hardening_14_http_5xx_uncertain_acceptance`
  - `test_hardening_15_stage_1_cancellation_combined_with_worker_exception`
  - `test_hardening_16_two_concurrent_claimants`
  - `test_hardening_17_independent_process_claim_ownership`
  - `test_hardening_18_multipart_success_and_partial_uncertainty`
  - `test_hardening_19_restart_after_unresolved_persistence`
  - `test_hardening_20_no_second_http_call_for_uncertain_delivery`
- **Part 3: Evidence Contract & Fatal Epoch Boundary Tests (10 tests)**:
  - `test_evidence_sent_with_http_status_missing`
  - `test_evidence_sent_with_http_200_and_valid_message_id`
  - `test_evidence_sent_with_contradictory_http_status`
  - `test_evidence_sent_with_missing_or_invalid_message_id`
  - `test_evidence_sent_with_provider_error`
  - `test_evidence_sent_with_rate_limit_indication`
  - `test_evidence_unknown_transport_result_type`
  - `test_fatal_epoch_mismatch_uncancelled_caller`
  - `test_fatal_epoch_mismatch_cancelled_caller`
  - `test_fatal_epoch_zero_transport_calls`

```
tests\test_p0b3b1_telegram_outbox_protocol.py .......................... [ 50%]
..........................                                               [100%]
============================= 52 passed in 10.25s =============================
```

### 4.2 Full Regression Suite Verification
```
tests\test_p0a_event_loop_isolation.py ..................                [ 18%]
tests\test_p0b1_lifecycle_isolation.py .............................     [ 47%]
tests\test_p0b2_continuation_monitoring_isolation.py ................... [ 66%]
.                                                                        [ 67%]
tests\test_telegram_outbox_recovery.py ...................               [ 86%]
tests\test_cmc_watch_recovery.py ..............                          [100%]
============================ 100 passed in 20.60s =============================
```

Combined outbox & recovery tests:
- Total protocol & regression tests: **152 passing**
- Regressions introduced: **0**

---

## 5. Files Modified

1. [`app/alerts/telegram_outbox_protocol.py`](file:///C:/CandleCraftDev/app/alerts/telegram_outbox_protocol.py):
   - Strict HTTP 200 requirement in `normalize_transport_outcome()`.
   - Documented Phase P0B3B2 adapter obligation for `TelegramSender`.
   - Distinct handling of fatal epoch errors vs ordinary public eligibility refusals.
   - Stage 1 and Stage 3 bounded fencing preserving fatal error propagation even when caller is cancelled.
   - Multipart-aware independent SQLite post-commit verification.
2. [`tests/test_p0b3b1_telegram_outbox_protocol.py`](file:///C:/CandleCraftDev/tests/test_p0b3b1_telegram_outbox_protocol.py):
   - 52 synthetic scenarios (22 baseline + 20 hardening + 10 evidence/fatal epoch).
3. [`docs/operations/CCI_P0B3B1_TELEGRAM_OUTBOX_PROTOCOL_HANDOFF.md`](file:///C:/CandleCraftDev/docs/operations/CCI_P0B3B1_TELEGRAM_OUTBOX_PROTOCOL_HANDOFF.md):
   - Reconciled documentation and verification report.

---

## 6. Next Recommended Phase

**Phase P0B3B2 — Production Integration & Outer Context Manager Decomposition**:
- Construct the adapter function translating `TelegramSendResult` into verified `TelegramTransportOutcome`.
- Integrate `TelegramOutboxWorker` and `deliver_single_reserved_part` into `TelegramLifecycleDeliveryService.deliver_for_run`.
- Decompose the outer open repository context managers (`SQLiteTelegramAlertAttemptRepository`, `SQLiteSetupLifecycleRepository`) to eliminate event-loop connection holding.
- Ensure zero production regressions, preserving all public delivery invariants.
