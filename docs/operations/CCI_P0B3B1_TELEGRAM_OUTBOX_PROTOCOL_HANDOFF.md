# CANDLE CRAFT INTELLIGENCE
# P0B3B1 — TELEGRAM OUTBOX TRANSACTION / TRANSPORT PROTOCOL FOUNDATION HANDOFF

```yaml
PHASE: P0B3B1 (FINAL CROSS-PROCESS PROOF & CONDITIONAL MERGE)
STATUS: COMPLETED
CODING_ENVIRONMENT: DEV_PC_ONLY
HOST: Windows 11 (DEV PC)
REPOSITORY: C:\CandleCraftDev
GITHUB_REPO: https://github.com/candlecraftinteligence/candle-craft-trading-agent
P0B3A_MERGE_SHA: 8308b32a885abbba6f3317bdb1cea669863db313
FEATURE_BRANCH: feature/p0b3b1-telegram-outbox-protocol-foundation
BASE_SHA: 8308b32a885abbba6f3317bdb1cea669863db313
PR_URL: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/157
LIVE_DELIVERY_PATH_MODIFIED: NO
PRODUCTION_DEPLOYMENT_APPROVED: NO
NEXT_RECOMMENDED_PHASE: P0B3B2A
```

---

## 1. Executive Summary

This phase completes the final safety-hardening and cross-process verification gate for the **P0B3B1 Telegram outbox transaction/transport protocol foundation**.

All architectural gates and evidence requirements have been verified:
1. **Cross-Process Race Proof**: Transitioned from multi-threaded concurrency to true independent OS process concurrency via `multiprocessing.get_context("spawn")` with barrier synchronization (`test_hardening_17_independent_process_claim_ownership`).
2. **Sent Evidence Contract Reconciled**: Missing HTTP status (`http_status=None`) strictly fails closed to `UNCERTAIN` (`missing_success_http_status`). A valid `SENT` outcome strictly requires HTTP 200, non-empty `telegram_message_id`, no provider error message, and not rate-limited.
3. **Fatal Runtime Epoch & Database Error Propagation**: Preserved fatal error propagation (`RuntimeEpochError`, `sqlite3.DatabaseError`) without masking as ordinary per-event `blocked` or `skipped` states, even when the caller task is cancelled. Zero transport invocations on fatal boundaries.
4. **Independent Commit Verification & Multipart Invariants**: Stage 3 independently queries SQLite to verify committed state against the target state before issuing `DurableDeliveryReceipt`, correctly accounting for multipart messages (`part=SENT` while `event=IN_FLIGHT` until all parts finish).
5. **Transparent Test Suite Accounting**: Distinct test scenarios and shared test wrappers are explicitly delineated.

---

## 2. Cross-Process Concurrency Proof (`test_hardening_17`)

A real multiprocessing regression test was implemented in [`tests/test_p0b3b1_telegram_outbox_protocol.py`](file:///C:/CandleCraftDev/tests/test_p0b3b1_telegram_outbox_protocol.py) using portable Windows `spawn` mechanics (`multiprocessing.get_context("spawn")`) and `multiprocessing.Barrier(2)`.

### Verified Properties
1. **Single Committed Claim**: Two independent OS processes start at the exact same instant via a synchronized barrier, competing for the same canonical reservation and public alert event. Exactly one process acquires the atomic claim under `BEGIN IMMEDIATE`, commits `IN_FLIGHT`, and receives a valid `CommittedPartReservationReceipt` with an `attempt_id`.
2. **No Competing Authority**: The losing process is rejected with `OutboxClaimRejected("outbox_claim_rejected:outbox_not_claimable:in_flight")`. It receives zero delivery authority (`receipt is None`).
3. **Internal Ledger Consistency**: SQLite rows in `public_alert_events` and `public_alert_delivery_parts` consistently reflect the winner's `attempt_id`, exactly 1 attempt record is linked, and `canonical_reservation_attempt_id` remains unmutated.
4. **SENT and UNCERTAIN Events Unclaimable**:
   - When an event is in `SENT`, independent processes racing to claim it are both rejected (`event_already_sent`).
   - When an event is in `UNCERTAIN`, independent processes racing to claim it are both rejected (`event_delivery_uncertain`).
5. **Process Connection Closure**: All worker SQLite connections close in their owning process upon completion. Immediately after child process joins, the parent process obtains an exclusive write lock (`BEGIN EXCLUSIVE`) without locking conflicts.
6. **Cross-Process Runtime Epoch Enforcement**: When an independent process attempts to claim with a mismatched foreign epoch identity, it is rejected across the process boundary with `RuntimeEpochError` without invoking transport.

---

## 3. Test Suite Breakdown & Evidence Transparency

The test suite in [`tests/test_p0b3b1_telegram_outbox_protocol.py`](file:///C:/CandleCraftDev/tests/test_p0b3b1_telegram_outbox_protocol.py) collects and executes **52 test items**. To provide an accurate evidence report, distinct test scenarios are distinguished from shared wrappers:

### A. Distinct Baseline Protocol Tests (22 tests)
- `test_baseline_01_valid_committed_reservation_sent`: Single-part happy path `SENT`.
- `test_baseline_02_retryable_error_advances_attempt_count`: Transient failure transitions to `RETRYABLE`.
- `test_baseline_03_failed_final_error_stops_retry`: Terminal error transitions to `FAILED_FINAL`.
- `test_baseline_04_two_competing_claimants_atomic_claim_race`: In-process coroutine claim race.
- `test_baseline_05_multithreaded_claim_race`: Multithreaded claim race (4 threads via `ThreadPoolExecutor`).
- `test_baseline_06_duplicate_existing_sent_event_rejected`: Rejects duplicate submission of `SENT` event.
- `test_baseline_07_existing_uncertain_event_rejected`: Rejects duplicate submission of `UNCERTAIN` event.
- `test_baseline_08_cancellation_before_claim`: Cancellation prior to claim raises `CancelledError` with 0 HTTP calls.
- `test_baseline_09_cancellation_during_claim_transaction`: Cancellation during preparation preserves atomicity.
- `test_baseline_10_cancellation_after_committed_claim_before_http`: Cancellation between Stage 1 and Stage 2 results in 0 HTTP calls.
- `test_baseline_11_cancellation_during_transport_await`: Cancellation during transport marks `UNCERTAIN`.
- `test_baseline_12_cancellation_after_transport_during_persistence`: Cancellation after HTTP fences durability.
- `test_baseline_13_worker_closed_rejects_new_work`: Worker shutdown rejects new requests.
- `test_baseline_14_worker_bounded_admission_rejection`: Worker bounded queue limits concurrent callers.
- `test_baseline_15_worker_aclose_drains_in_flight`: Clean worker drain and thread joining.
- `test_baseline_16_restart_after_ambiguous_http_completion`: Protocol restart recovery.
- `test_baseline_17_multipart_partial_failure_part1_sent_part2_uncertain`: Multipart partial failure state.
- `test_baseline_18_max_attempts_exhaustion_failed_final`: Exhaustion transitions to `FAILED_FINAL`.
- `test_baseline_19_no_second_http_attempt_for_sent_or_uncertain_parts`: Prevents duplicate transmissions.
- `test_baseline_20_unassociated_reservation_event_pair_rejected`: Rejects reservation mismatch.
- `test_baseline_21_database_error_during_claim_propagated`: SQLite operational errors propagate.
- `test_baseline_22_no_mutation_of_historical_quarantined_records`: 11 historical records untouched.

### B. Distinct Safety Hardening Tests (16 tests)
- `test_hardening_01_cancellation_after_http_before_stage3_admission`: Post-HTTP cancellation fences admission.
- `test_hardening_02_repeated_cancellation_during_admission_wait`: Repeated cancellation during admission wait.
- `test_hardening_03_cancellation_during_stage3_transaction`: Cancellation during persistence transaction.
- `test_hardening_04_cancellation_during_commit`: Cancellation during commit preserves durability.
- `test_hardening_05_cancellation_during_worker_shutdown`: Cancellation during worker shutdown joins thread cleanly.
- `test_hardening_06_sqlite_result_persistence_raises_before_commit`: SQLite failure falls back to `UNCERTAIN`.
- `test_hardening_07_sqlite_uncertain_fallback_silently_fails`: Unverified fallback returns `unresolved_persistence`.
- `test_hardening_08_database_remains_in_flight_after_failed_fallback`: Uncommitted state leaves row in `IN_FLIGHT`.
- `test_hardening_09_no_false_durable_delivery_receipt`: Zero false receipts issued.
- `test_hardening_10_valid_http_success_verified_message_id`: Verified Telegram message ID issued receipt.
- `test_hardening_11_malformed_sent_outcome_no_message_id`: Missing message ID fails closed to `UNCERTAIN`.
- `test_hardening_12_contradictory_sent_outcome_failed_http_status`: Failed HTTP code fails closed to `UNCERTAIN`.
- `test_hardening_13_unknown_provider_acknowledgment`: Unrecognized provider state fails closed to `UNCERTAIN`.
- `test_hardening_14_http_5xx_uncertain_acceptance`: HTTP 5xx server errors normalize to `UNCERTAIN`.
- `test_hardening_15_stage_1_cancellation_combined_with_worker_exception`: Cancellation precedence preserved.
- `test_hardening_17_independent_process_claim_ownership`: **Independent OS cross-process race proof**.

### C. Shared Hardening Wrappers (4 tests)
The following 4 hardening test functions wrap and verify baseline scenarios under hardening naming conventions:
- `test_hardening_16_two_concurrent_claimants` $\rightarrow$ delegates to `test_baseline_04`
- `test_hardening_18_multipart_success_and_partial_uncertainty` $\rightarrow$ delegates to `test_baseline_17`
- `test_hardening_19_restart_after_unresolved_persistence` $\rightarrow$ delegates to `test_baseline_16`
- `test_hardening_20_no_second_http_call_for_uncertain_delivery` $\rightarrow$ delegates to `test_baseline_19`

### D. Distinct Evidence Contract & Fatal Epoch Tests (10 tests)
- `test_evidence_sent_missing_http_status`: Rejects missing HTTP status.
- `test_evidence_sent_with_http_200_and_valid_message_id`: Confirms HTTP 200 success.
- `test_evidence_sent_with_contradictory_http_status`: Rejects non-200 HTTP code.
- `test_evidence_sent_with_missing_or_invalid_message_id`: Rejects missing message ID.
- `test_evidence_sent_with_provider_error`: Rejects success containing provider error text.
- `test_evidence_sent_with_rate_limit_indication`: Rejects success with rate limit flag.
- `test_evidence_unknown_transport_result_type`: Non-`TelegramTransportOutcome` normalized to `UNCERTAIN`.
- `test_fatal_epoch_mismatch_uncancelled_caller`: Fatal epoch mismatch propagates to caller.
- `test_fatal_epoch_mismatch_cancelled_caller`: Fatal epoch mismatch propagates even if caller is cancelled.
- `test_fatal_epoch_zero_transport_calls`: Zero transport calls occur on any fatal epoch error.

**Summary**: **48 distinct scenarios + 4 shared wrappers = 52 total tests (100% passing)**.

---

## 4. Remaining Limitations

1. **Protocol Foundation Only**: `app/alerts/telegram_outbox_protocol.py` is an isolated foundation module. Live production execution paths in `app/alerts/telegram_lifecycle.py` (`TelegramLifecycleDeliveryService.deliver_for_run`) remain unmodified and require Phase P0B3B2 integration.
2. **Adapter Obligation (P0B3B2A)**: Production integration requires an adapter function translating `TelegramSendResult` from `TelegramSender` into a verified `TelegramTransportOutcome`.

---

## 5. Next Recommended Phase

**Phase P0B3B2A — Telegram Sender Adapter Contract**:
- Construct the adapter function translating raw `TelegramSendResult` / provider response dicts into verified `TelegramTransportOutcome` dataclasses.
- Validate adapter behavior against real Telegram Bot API response payloads without invoking live external network calls.
