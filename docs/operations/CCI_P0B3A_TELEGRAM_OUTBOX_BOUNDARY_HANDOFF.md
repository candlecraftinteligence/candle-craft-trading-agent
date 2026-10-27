# P0B3A — Telegram Outbox Transaction / Transport Boundary Isolation

BASE_SHA: bcc1e330b1f06660a7c4c06edcc6eac368836eae
FEATURE_BRANCH: feature/p0b3a-telegram-outbox-boundary-isolation
PR_URL: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/156
FINAL_IMPLEMENTATION_SHA: 0ff869bad9abec9b2035246e6567577419f2628b
FINAL_PR_HEAD_SHA: 60bfa9237825c132ff43d37fecbe4c995909e91e
EXACT_HEAD_CI: PASS on 60bfa9237825c132ff43d37fecbe4c995909e91e (https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40582454444)
RUNTIME_DEPLOYMENT_APPROVED: NO

---

## 1. Executive Summary & Architectural Scope Decision

Candle Craft Intelligence (CCI) observed approximately 59–65 seconds of total Telegram outbox phase duration per scanner iteration during the Production Runtime audit on the 4 GB Windows runtime host. This duration encompasses multiple categories of work: candidate discovery, eligibility audits, CMC shadow evaluations, cooldown queries, HTTP network round-trips, and synchronous SQLite operations. No measured performance improvement from P0B3B exists yet, and DEV simulations must not be used to claim production runtime improvements.

This phase investigates the transaction and transport ownership boundaries of the Telegram outbox and designs the future isolation of synchronous SQLite operations from the main asyncio event loop without introducing duplicate public signals, orphaned reservations, unsafe HTTP retries, or economic identity corruption.

### Scope Decision (Stop-Fence Justification)
In accordance with Master Prompt Section 6 (*"P0B3A Scope Decision"*) and Section 12 (*"Safety Stop Conditions"*):
- A shallow refactoring (such as wrapping outer methods in `asyncio.to_thread` or attempting a partial offload of individual SQLite calls) violates core repository safety directives:
  1. In `TelegramLifecycleDeliveryService.deliver_for_run`, the outer context managers `with SQLiteTelegramAlertAttemptRepository(...)` and `with SQLiteSetupLifecycleRepository(...)` hold open SQLite connections on the main asyncio event loop across all network awaits.
  2. Inside `_deliver_committed_public_alert_intent`, SQLite write transactions (`persist_intent_parts`, `claim`, `mark_part_in_flight`), network HTTP transport (`send_part` / `send_text`), and SQLite result persistence (`record_part_result`, `_finalize_sent_locked`) are tightly interleaved within an active loop spanning the caller's repository context.
  3. Attempting an ad-hoc offload while outer connections remain committed-but-open risks connection lifecycle confusion and database lock contention under multi-threaded writers.
  4. Moving live `sqlite3.Connection` objects across threads is strictly prohibited (`check_same_thread=True`).
  5. Offloading only read-only provenance diagnostics (`collect_provenance_diagnostics`) does not alter outbox transport blocking, while decomposing `_deliver_committed_public_alert_intent` requires an explicit 3-stage protocol (*Worker Preparation -> Async Transport -> Worker Persistence*) and refactoring the caller hierarchy across `deliver_for_run`.
- **DECISION**: As mandated by Section 6 and Section 12, P0B3A executes the comprehensive, source-grounded architectural investigation and ownership mapping, proves the exact boundary stop conditions, establishes the implementation-ready blueprint for P0B3B, and prevents unsafe premature code modifications.

---

## 2. TELEGRAM_CALL_GRAPH

The following source-grounded call graph traces every execution path from the scanner orchestrator through `TelegramLifecycleDeliveryService.deliver_for_run` to SQLite repositories, external HTTP transports, and CoinMarketCap (CMC) audit layers:

```
scripts/run_scan.py
 └── _deliver_telegram_manual_signals_if_enabled (L2963-L2990) [@profiled("telegram_outbox")]
      └── TelegramLifecycleDeliveryService.deliver_for_run (app/alerts/telegram_lifecycle.py:3869)
           │
           ├── [CONN 1 OPEN] SQLiteTelegramAlertAttemptRepository.__enter__ (L3922)
           │    └── open_repository_database -> sqlite3.connect (MainThread)
           │
           ├── collect_provenance_diagnostics (L3926-L3928) [READ-ONLY DIAGNOSTIC]
           │    └── app/alerts/provenance_diagnostics.py:91 (pages up to 256 rows from public_alert_events)
           │         ├── decide_public_effect (app/runtime_epoch/ownership.py:17)
           │         └── require_canonical_reservation_for_public_effect (app/runtime_epoch/ownership.py:18)
           │
           ├── [CONN 2 OPEN] SQLiteSetupLifecycleRepository.__enter__ (L3929)
           │    └── open_repository_database -> sqlite3.connect (MainThread)
           │
           ├── repository.list_sent_watchlist_alerts (L3933) [SQLITE READ]
           │
           ├── prepare_selection (L3955) [CMC SHADOW / DECISION]
           │    └── app/cmc/integration.py:154 (candidate evaluation, potential CMC HTTP query)
           │
           ├── reconcile_publicly_tracked_outcomes (L3956, L5101-L5238) [SQLITE READ/WRITE + AWAIT HTTP]
           │    ├── _match_public_tracking_outcome (L5116) [SQLITE READ]
           │    ├── _supersede_public_target_intent (L5190) [SQLITE WRITE]
           │    ├── _persist_public_outcome_tracking_audit (L5196) [SQLITE WRITE]
           │    └── _send_canonical_public_outcome (L5226, L5240-L5370)
           │         ├── _reserve_public_lifecycle_event (L5337, L3079) [SQLITE WRITE - RESERVATION]
           │         └── _deliver_committed_public_alert_intent (L5363, L3428-L3641) [TRANSPORT OUTBOX PATH]
           │
           ├── recover_public_lifecycle_alerts (L3965, L4234-L4360) [SQLITE READ/WRITE + AWAIT HTTP]
           │    ├── queries public_alert_events for PENDING, RETRYABLE, IN_FLIGHT (L4244)
           │    ├── mark_terminal_without_send (L4279, L4329) [SQLITE WRITE]
           │    └── _deliver_committed_public_alert_intent (L4348, L3428-L3641) [TRANSPORT OUTBOX PATH]
           │
           ├── _public_watchlist_scan_selection (L3979) [SQLITE READ - COOLDOWNS & CAPS]
           │
           ├── audit_selection (L3982) [CMC SELECTION AUDIT]
           │    └── app/cmc/selection_audit.py:43 -> asyncio.to_thread(service.store.put, "selection_intent")
           │
           ├── LOOP: for symbol_result in result.results (L3986-L4071)
           │    ├── _public_watchlist_candidate_audit (L3987) [SQLITE READ]
           │    ├── _confirmed_alert_attempt_prefilter (L4012) [CPU / LOGIC]
           │    └── deliver_transitions_for_symbol (L4032, L4124-L4232)
           │         ├── _canonical_entry_progress_for_coalescing (L4168) [SQLITE READ]
           │         ├── _persist_public_lifecycle_suppression_audit (L4206) [SQLITE WRITE]
           │         └── deliver_for_symbol (L4216, L4362-L4840)
           │              ├── load_active_runtime_epoch (L4377) [SQLITE READ]
           │              ├── reserve_public_watchlist_event (L4703, L2727) [SQLITE WRITE - RESERVATION]
           │              ├── _reserve_public_lifecycle_event (L4722, L3079) [SQLITE WRITE - RESERVATION]
           │              ├── _deliver_committed_public_alert_intent (L4769, L3428) [PRIMARY PUBLIC PATH]
           │              └── fallback: repository.commit_before_network_activity (L4817)
           │                   └── await sender.send_text (L4818) [LEGACY ASYNC HTTP]
           │
           ├── reconcile_sent_watchlists (L4072, L5375-L5555) [SQLITE READ/WRITE + AWAIT HTTP]
           │    ├── _prior_watchlist_expiry_decision (L5397) [SQLITE READ]
           │    ├── _persist_watchlist_expiry_audit (L5400) [SQLITE WRITE]
           │    ├── _send_watchlist_outcome_update (L5430, L5542-L5655)
           │    │    ├── repository.commit_before_network_activity (L5588)
           │    │    ├── await sender.send_text (L5589) [ASYNC HTTP]
           │    │    └── repository.insert_attempt (L5623) [SQLITE WRITE]
           │    └── _send_reconciliation_update (L5529, L5657-L5710)
           │         ├── repository.commit_before_network_activity (L5668)
           │         ├── await sender.send_text (L5669) [ASYNC HTTP]
           │         └── repository.insert_attempt (L5678) [SQLITE WRITE]
           │
           ├── maybe_send_research_watch_alerts (L4083, L5000-L5099) [SQLITE READ/WRITE + AWAIT HTTP]
           │    ├── repository.commit_before_network_activity (L5077)
           │    ├── await sender.send_text (L5078) [ASYNC HTTP]
           │    └── _persist_research_watch_attempt (L5085) [SQLITE WRITE]
           │
           ├── [CONN 2 CLOSE] SQLiteSetupLifecycleRepository.__exit__ (L4089)
           ├── [CONN 1 CLOSE] SQLiteTelegramAlertAttemptRepository.__exit__ (L4089)
           │
           └── record_application (L4092) [CMC SELECTION APPLICATION]
                └── app/cmc/selection_audit.py:92 -> asyncio.to_thread(service.store.put, "selection_application")
```

### The Public Transport Path (`_deliver_committed_public_alert_intent`)
Located at `app/alerts/telegram_lifecycle.py:3428-3641`:
1. **Persist Message Intent Parts** (L3455–L3474):
   - Calls `persist_intent_parts(connection, event_id, ...)` (`app/alerts/telegram_outbox.py:75`).
   - Updates `telegram_alert_attempts` SET `delivery_state = 'PENDING'`, `delivery_part_count = len(parts)`.
   - Executes `connection.commit()` (L3474) or `connection.rollback()` (L3476) on error.
2. **Preflight Check** (L3490–L3526):
   - Handles dry-run (`SKIPPED_DRY_RUN`) or policy disabling (`POLICY_DISABLED`) via `outbox.mark_terminal_without_send`.
3. **Atomic Outbox Claim** (L3528–L3548):
   - Calls `outbox.claim(...)` (`app/alerts/telegram_outbox.py:192`).
   - Executes `with self._transaction():` (`BEGIN IMMEDIATE` -> `require_event_reservation_association` -> `decide_public_effect` -> `_recover_stale_locked` -> `UPDATE public_alert_events SET delivery_state = 'IN_FLIGHT'` -> `UPDATE telegram_alert_attempts` -> `COMMIT`).
4. **Multipart Transmission Loop** (L3558–L3635):
   For each part in `claim.parts`:
   - `outbox.mark_part_in_flight(part_id, attempt_id)` (L3559): Executes `BEGIN IMMEDIATE` -> updates part state to `IN_FLIGHT` -> `COMMIT`.
   - `repository.commit_before_network_activity()` (L3574): Calls `self._connection.commit()`.
   - **EXTERNAL ASYNC HTTP**: `send_result = await send_part(...)` (L3578) or `await sender.send_text(...)` (L3586).
   - `outbox.record_part_result(...)` (L3601): Executes `BEGIN IMMEDIATE` -> updates part to `SENT`, `RETRYABLE`, `UNCERTAIN`, or `FAILED_FINAL`. If all parts `SENT`, calls `_finalize_sent_locked` -> `COMMIT`.

---

## 3. DATABASE_CONNECTION_OWNERSHIP

Three distinct database connection instances operate during scanner runs:

1. **Connection 1: `SQLiteTelegramAlertAttemptRepository`**
   - **Instantiated / Opened**: `app/alerts/telegram_lifecycle.py:3922` via `open_repository_database(self.database_path, expected_identity=self.expected_identity)`.
   - **Thread Confinement**: Opened and retained exclusively on the main asyncio event loop thread (`MainThread`).
   - **Lifetime**: Spans lines 3922 to 4089 (the entire 167 lines of `deliver_for_run`).
   - **Enforcement**: Uses `check_same_thread=True` (default Python SQLite).
   - **Closed**: In `__exit__` (L1163–L1170) on `MainThread`.

2. **Connection 2: `SQLiteSetupLifecycleRepository`**
   - **Instantiated / Opened**: `app/alerts/telegram_lifecycle.py:3929` via `open_repository_database(self.database_path, expected_identity=self.expected_identity)`.
   - **Thread Confinement**: Opened and retained exclusively on `MainThread`.
   - **Lifetime**: Spans lines 3929 to 4089.
   - **Closed**: In `__exit__` (`app/lifecycle/repositories.py:53–80`) on `MainThread`.

3. **Connection 3: CMC Sidecar Database (`cmc_*.sqlite`)**
   - **Instantiated / Opened**: `app/cmc/storage.py:59` via `sqlite3.connect(self.path, timeout=0.1)`.
   - **Thread Confinement**: Opened, written, and closed inside worker threads via `asyncio.to_thread`. Completely independent of the main runtime SQLite database.

### Connection States & Lifecycle Distinctions
To reason accurately about SQLite concurrency, four distinct connection states must be distinguished:
1. **Open Connection (Idle)**: OS handle and SQLite internal structures allocated, but no read or write transaction is active (`in_transaction == False`). In WAL mode, an idle open connection does not block readers or writers, but retains OS memory and file handles.
2. **Active Read Transaction**: Begun by an explicit `BEGIN` or implicit `SELECT`. Holds a shared read lock on the WAL index (`shm`). While open across awaits, it prevents WAL truncation and WAL-index checkpoints, causing WAL file growth under write load.
3. **Active Write Transaction**: Begun by `BEGIN IMMEDIATE` or an implicit `INSERT`/`UPDATE`. Holds an exclusive write lock. In SQLite, only ONE connection across all threads and processes may hold a write lock. Any other write attempt blocks until `busy_timeout` expires.
4. **Committed-but-Open Connection**: A write transaction was ended via `COMMIT`, but the connection handle was not closed. Other readers and writers can proceed; it does not automatically cause dirty reads or instant deadlock. However, holding committed-but-open connections across threads and async yields invites thread-confinement bugs, leaks OS resources under memory pressure, and creates scheduling stalls when re-entering SQLite on the event loop.

### The Architectural Defect
Connections 1 and 2 remain open simultaneously on the asyncio event loop thread (`MainThread`) while HTTP requests to Telegram and CMC are awaited. While committed-but-open connections do not inherently deadlock or read dirty data in SQLite WAL mode, executing synchronous SQLite operations on `MainThread` interleaves CPU/disk I/O directly into the event loop. Under Windows disk write-cache flushing, pagefile paging, or WAL checkpointing on a 4 GB RAM host, these synchronous calls block event-loop scheduling, delaying WebSocket frame processing and scanner heartbeat timers.

---

## 4. TRANSACTION_BOUNDARIES

Every transaction boundary, explicit lock, commit, and rollback in the Telegram alert subsystem is cataloged below:

| Location | Function / Method | Mechanism | Scope & Invariant |
| :--- | :--- | :--- | :--- |
| `telegram_lifecycle.py:1167` | `SQLiteTelegramAlertAttemptRepository.__exit__` | `self.connection.commit()` | Commits any trailing uncommitted write on repository exit. |
| `telegram_lifecycle.py:1175` | `commit_before_network_activity` | `self._connection.commit()` | Flushes pending writes before HTTP; does not close the connection. |
| `telegram_lifecycle.py:3474` | `_deliver_committed_public_alert_intent` | `connection.commit()` | Commits `persist_intent_parts` and attempt `PENDING` state before outbox claim. |
| `telegram_lifecycle.py:3476` | `_deliver_committed_public_alert_intent` | `connection.rollback()` | Rolls back on SQLite exception during intent persistence; raises `StorageError`. |
| `telegram_outbox.py:670` | `SQLitePublicTelegramOutbox._transaction` | `self.connection.execute("BEGIN IMMEDIATE")` | SQLite exclusive write lock for all outbox state transitions. |
| `telegram_outbox.py:674` | `SQLitePublicTelegramOutbox._transaction` | `self.connection.rollback()` | Rolls back on any exception inside outbox transaction block. |
| `telegram_outbox.py:677` | `SQLitePublicTelegramOutbox._transaction` | `self.connection.commit()` | Commits outbox state transition atomically. |
| `repositories.py:69` | `SQLiteSetupLifecycleRepository.__exit__` | `connection.commit()` | Commits lifecycle updates; rolls back on exception (L64, L74). |

### Flaw in Current Contract
The call to `commit_before_network_activity()` terminates the write transaction, but leaves the underlying connection committed-but-open. While this allows external readers and writers to operate on the WAL database without an active write lock during network I/O, it leaves connection lifecycle management tied to the outer scope of `deliver_for_run`. If a worker thread were introduced to handle intermediate outbox state while the caller retains an open repository, managing cross-thread connection lifetimes without passing connection handles violates `check_same_thread=True` or requires fragile ad-hoc re-connection logic.

---

## 5. NETWORK_AWAIT_BOUNDARIES

The subsystem contains 9 external await points:

1. `app/alerts/telegram_lifecycle.py:3578`: `await send_part(...)` — Async HTTP POST to Telegram API (`send_telegram_message_part`).
2. `app/alerts/telegram_lifecycle.py:3586`: `await sender.send_text(...)` — Async HTTP POST to Telegram API (`send_telegram_messages`).
3. `app/alerts/telegram_lifecycle.py:3955`: `await prepare_selection(...)` — CMC candidate evaluation and prospective market intelligence queries.
4. `app/alerts/telegram_lifecycle.py:3982`: `await audit_selection(...)` — Non-blocking put to CMC sidecar storage (`asyncio.wait_for(asyncio.to_thread(...), timeout=0.25)`).
5. `app/alerts/telegram_lifecycle.py:4092`: `await record_application(...)` — Non-blocking put to CMC sidecar storage (`asyncio.wait_for(asyncio.to_thread(...), timeout=0.25)`).
6. `app/alerts/telegram_lifecycle.py:4818`: `await self.sender.send_text(...)` — Fallback Telegram send in `deliver_for_symbol`.
7. `app/alerts/telegram_lifecycle.py:5078`: `await self.sender.send_text(...)` — Research watch Telegram send in `maybe_send_research_watch_alerts`.
8. `app/alerts/telegram_lifecycle.py:5589`: `await self.sender.send_text(...)` — Watchlist outcome update in `_send_watchlist_outcome_update`.
9. `app/alerts/telegram_lifecycle.py:5669`: `await self.sender.send_text(...)` — Reconciliation update in `_send_reconciliation_update`.

### Await Invariants
- All Telegram HTTP transport calls **MUST REMAIN ASYNC** on the event loop. They must not run on a worker thread.
- **ZERO SQLite connections or transactions may be open across any of these awaits.**

---

## 6. CANONICAL_RESERVATION_OWNER

- **Owner Functions**: `reserve_public_watchlist_event` (`telegram_lifecycle.py:2727`) and `_reserve_public_lifecycle_event` (`telegram_lifecycle.py:3079`).
- **Enforcement Boundary**: `require_canonical_reservation_for_public_effect` (`app/runtime_epoch/ownership.py:18, 51`) and `require_event_reservation_association` (`app/runtime_epoch/ownership.py:18`).
- **Invariants**:
  1. A canonical reservation binds `runtime_epoch_id`, `event_key`, and `origin_lifecycle_id` atomically in `telegram_alert_attempts` and `public_alert_events`.
  2. A committed reservation is **NOT** confirmed Telegram delivery. It represents exclusive authorization to deliver.
  3. The eleven historical quarantined reservation records with ambiguous identity must remain quarantined; they must never be repaired, claimed, or replayed.

---

## 7. PUBLIC_EFFECT_OWNER

- **Owner Function**: `decide_public_effect(connection, row)` (`app/runtime_epoch/ownership.py:17, 230–278`).
- **Policy Checks**:
  1. Validates that the active runtime epoch exactly matches `row["runtime_epoch_id"]`.
  2. Validates origin lifecycle linkage via `require_lifecycle_public_intent`.
  3. Verifies exact symbol and direction consistency between lifecycle record and public alert event.
  4. Confirms that no terminal status (`SENT`, `FAILED_FINAL`, `POLICY_DISABLED`, `SKIPPED_DRY_RUN`) has been reached.

---

## 8. CROSS_PROCESS_LOCKING

- **Process Architecture**: The scanner loop (`scripts/run_scan.py`) and the Telegram UI listener (`scripts/run_telegram_bot.py`) run as completely separate OS processes on the host.
- **Locking Invariant**: In-process Python locks (`asyncio.Lock`, `threading.Lock`) offer **ZERO cross-process mutual exclusion**.
- **The True Mutex**: The database-enforced SQLite `BEGIN IMMEDIATE` transaction inside `SQLitePublicTelegramOutbox.claim` (`app/alerts/telegram_outbox.py:192, 670`) is the **ONLY authoritative cross-process mutex**.
- **Safety Directive**: Any refactoring must retain `BEGIN IMMEDIATE` on the operational SQLite database to prevent concurrent claim races between scanner runs and external processes.

---

## 9. CANCELLATION_AND_HTTP_UNCERTAINTY

Network transport uncertainty is governed by `app/alerts/telegram.py:67–101` and `app/alerts/telegram_outbox.py:507–606`:

1. **Cancellation Before Reservation**: Safe exit; zero records created.
2. **Cancellation After Reservation Before HTTP**: Event remains `PENDING` or `IN_FLIGHT` with lease. Can be safely recovered or cancelled without external side effect.
3. **Cancellation During Telegram HTTP**:
   - `client.post` may have transmitted bytes; the Telegram server may have accepted and broadcast the message to public channels.
   - **CRITICAL INVARIANT**: The outcome is unknown. An `IN_FLIGHT` part after an interrupted HTTP attempt must **NOT** be automatically retried unless absence of external transmission is independently and verifiably established. Automatic retry is strictly prohibited.
4. **Post-HTTP SQLite Commit Failure**:
   - If HTTP transmission succeeds (or begins) but the subsequent SQLite commit fails (e.g. disk write failure, I/O error, or host crash), the database **CANNOT guarantee that an `UNCERTAIN` or `SENT` status is durably recorded**. In such failure cases, the database remains in its pre-commit state (`IN_FLIGHT` or unrecorded), and the system must report an explicit unresolved delivery state to the caller rather than assuming durable recording succeeded.
5. **Limits of Cancellation Fencing**:
   - Python-level cancellation fencing (such as `_fenced_wait` with `asyncio.shield`) protects only against cooperative `asyncio.CancelledError` within a healthy running event loop. It provides **zero protection against hard process termination (`SIGKILL`, Windows Task Manager kill, unhandled OS-level crash) or physical power loss**.
6. **No "Exactly-Once" Telegram Broadcast**:
   - Exactly-once external Telegram delivery **cannot be claimed merely from local SQLite atomicity**. Once network bytes leave the socket, network partitions, dropped ACK packets, and Telegram server crashes can decouple HTTP transport from local persistence. The architectural goal is strictly **at-most-once publication** with fail-closed uncertainty tracking, never a false claim of network-level exactly-once delivery.

---

## 10. MULTIPART_RECOVERY

Multi-part messages (alerts exceeding 4096 characters, split by `split_message`):
- Managed in `public_alert_delivery_parts` (`app/alerts/telegram_outbox.py:75–165`).
- Each part transitions independently: `PENDING` -> `IN_FLIGHT` -> `SENT` / `RETRYABLE` / `UNCERTAIN` / `FAILED_FINAL`.
- **Atomic Aggregation**: The parent event in `public_alert_events` transitions to `SENT` via `_finalize_sent_locked` (L625) **ONLY WHEN EVERY PART IS DURABLY `SENT`**.
- **Partial Multipart Failure**: If part 1 succeeds and part 2 times out, part 1 is `SENT`, part 2 becomes `UNCERTAIN`, and the event becomes `UNCERTAIN`. Part 1 is never re-transmitted.

---

## 11. CMC_COMPATIBILITY

- **Operational Mode**: `CMC_MODE` remains strictly `shadow` (`Settings.cmc_mode`).
- **Profile**: `CMC_COLLECTION_PROFILE` remains `DECISION_FOCUSED`.
- **Directives**:
  - CMC evaluations are observational only; zero active veto, zero candidate priority overrides.
  - Selection audit (`audit_selection`) and application audit (`record_application`) execute within strict non-blocking timeouts (250ms via `asyncio.to_thread`) without holding scanner execution.
  - Causal decision cutoffs and evidence binding timestamps are strictly preserved.

---

## 12. SAFE_OFFLOAD_SCOPE & OPERATION CLASSIFICATION

Every operation in the Telegram alert subsystem is classified according to Section 4:

| Operation | Current Location | Classification | Rationale |
| :--- | :--- | :--- | :--- |
| `collect_provenance_diagnostics` | `provenance_diagnostics.py:91` | `SAFE_TO_OFFLOAD_NOW` | Pure read-only query over `public_alert_events`; returns detached dict; no external side effects. |
| `list_sent_watchlist_alerts` | `telegram_lifecycle.py:1260` | `SAFE_TO_OFFLOAD_NOW` | Bounded read-only snapshot query. |
| `_public_watchlist_scan_selection` | `telegram_lifecycle.py:2460` | `SAFE_TO_OFFLOAD_NOW` | Bounded read-only calculation over cooldowns and caps. |
| `_deliver_committed_public_alert_intent` | `telegram_lifecycle.py:3428` | `REQUIRES_TRANSACTION_SPLIT` | Intermingles SQLite writes, async HTTP network awaits, and SQLite result persistence in a single loop. |
| `reconcile_publicly_tracked_outcomes` | `telegram_lifecycle.py:5101` | `REQUIRES_TRANSACTION_SPLIT` | Intermingles outcome matching reads, audit writes, reservation, and HTTP delivery. |
| `reconcile_sent_watchlists` | `telegram_lifecycle.py:5375` | `REQUIRES_TRANSACTION_SPLIT` | Intermingles watchlist expiry reads, audit writes, and HTTP sends. |
| `recover_public_lifecycle_alerts` | `telegram_lifecycle.py:4234` | `REQUIRES_TRANSACTION_SPLIT` | Intermingles recovery discovery reads with `_deliver_committed_public_alert_intent`. |
| `deliver_for_run` session scope | `telegram_lifecycle.py:3922` | `REQUIRES_PUBLIC_EFFECT_CONTRACT_REDESIGN` | Outer context managers hold 2 SQLite connections open across the entire 167-line async lifecycle. |
| `TelegramSender.send_part` | `telegram_sender.py:222` | `MUST_REMAIN_ASYNC` | External HTTP I/O using `httpx.AsyncClient`; must not block OS threads. |
| `TelegramSender.send_text` | `telegram_sender.py:191` | `MUST_REMAIN_ASYNC` | External HTTP I/O using `httpx.AsyncClient`; must not block OS threads. |
| `reserve_public_watchlist_event` | `telegram_lifecycle.py:2727` | `MUST_NOT_CHANGE` | Enforces canonical epoch and reservation binding; core public invariant. |
| `_reserve_public_lifecycle_event` | `telegram_lifecycle.py:3079` | `MUST_NOT_CHANGE` | Enforces canonical epoch, plan identity, and deduplication invariants. |
| `decide_public_effect` | `ownership.py:17` | `MUST_NOT_CHANGE` | Authoritative public mutation gate. |
| `SQLitePublicTelegramOutbox.claim` | `telegram_outbox.py:192` | `MUST_NOT_CHANGE` | Cross-process atomic claim using database `BEGIN IMMEDIATE`. |
| UNCERTAIN timeout classification | `telegram.py:78` | `MUST_NOT_CHANGE` | Guarantees non-confirmed HTTP requests are never assumed unsent. |

---

## 13. IMPLEMENTED_CHANGES

In strict compliance with prompt Section 6 (*"Scope Decision"*), Section 12 (*"Safety Stop Conditions"*), and Section 14 (*"PR Rules"*):
- Completed the source-grounded investigation of the entire Telegram SQLite-to-HTTP ownership boundary.
- Produced the complete ownership map and operation classification.
- Identified the monolithic session lifetime and coupled reservation-transport-persistence loop as the true architectural blockers.
- **Enforced the Architectural Stop Fence**: Refrained from making partial, cosmetic, or unsafe code edits to `telegram_lifecycle.py` that would violate safety stop conditions or introduce half-isolated failure modes.
- Authored this implementation-ready handoff specification and roadmap for P0B3B.

---

## 14. DEFERRED_BOUNDARIES (P0B3B ARCHITECTURAL TARGET)

The target architecture for P0B3B decomposes the monolithic delivery loop into a strictly serialized three-phase pipeline:

```
                       EVENT LOOP THREAD                       SERIALIZED WORKER THREAD
                       =================                       ========================

Phase 1 (Preparation): reservation_receipt = await worker.call(
                         stage_1_outbox_preparation, ...) ───> Open SQLiteTelegramAlertAttemptRepository
                                                               connection.execute("BEGIN IMMEDIATE")
                                                               Verify active epoch & canonical reservation
                                                               persist_intent_parts(...)
                                                               outbox.claim(...)
                                                               outbox.mark_part_in_flight(...)
                                                               Commit & Close connection
                       <────────────────────────────────────── Return CommittedReservationReceipt
                       (Zero SQLite connections held)

Phase 2 (Transport):   for part in receipt.parts:
                         transport_result = await send_part(
                           part.payload_text, ...)
                       (HTTP 200 / Timeout / Error classified)
                       (Zero SQLite connections held)

Phase 3 (Persistence): delivery_receipt = await worker.call(
                         stage_3_outbox_persistence, ...) ───> Open SQLiteTelegramAlertAttemptRepository
                                                               connection.execute("BEGIN IMMEDIATE")
                                                               outbox.record_part_result(transport_result)
                                                               If all parts SENT -> _finalize_sent_locked
                                                               Commit & Close connection
                       <────────────────────────────────────── Return DurableDeliveryReceipt
```

### Protocol Contracts for P0B3B:
1. `CommittedReservationReceipt`: Detached frozen dataclass containing `event_id`, `reservation_id`, `event_key`, `attempt_id`, `parts` (tuple of `TelegramOutboxPart`), and `committed_at`.
2. `TelegramTransportOutcome`: Detached frozen dataclass containing `delivery_state` (`SENT`, `RETRYABLE`, `UNCERTAIN`, `FAILED_FINAL`), `http_status`, `telegram_message_id`, `telegram_chat_id`, `error_category`, and `sent_at`.
3. `DurableDeliveryReceipt`: Detached frozen dataclass confirming durable SQLite persistence of the terminal or intermediate state.

---

## 15. PUBLIC_DELIVERY_EQUIVALENCE

The proposed transport decomposition has **NOT yet demonstrated production public-delivery equivalence**. Equivalence remains a theoretical design constraint and architectural requirement to be empirically proven in P0B3B. 

To preserve equivalence when implemented, the protocol must enforce:
- `event_key` generation and plan identity derivation must remain identical.
- Public signal eligibility gates, cooldowns, and frequency caps must not be weakened.
- Quarantined historical records must never be touched.
- Delivery state definitions (`SENT`, `IN_FLIGHT`, `PENDING`, `RETRYABLE`, `UNCERTAIN`, `FAILED_FINAL`) must strictly match current semantics.

---

## 16. DUPLICATE_SEND_REGRESSION & FAILURE MODES

Analysis of all 17 double-send failure modes (distinguishing tested current behavior, architectural requirements, and failure limits):

| Failure Mode | Failure Mechanism | Architectural Guard & Verified Limit |
| :--- | :--- | :--- |
| **Two callers racing for same event** | Concurrent claim attempts | `BEGIN IMMEDIATE` atomic claim; only one receives `claim != None`. |
| **Scanner & bot racing to claim** | Multi-process race | SQLite database write lock (`BEGIN IMMEDIATE`) serializes cross-process claims. |
| **Cancellation before reservation** | Task cancelled before DB write | Clean exit; zero database rows created; zero HTTP calls. |
| **Cancellation after reservation before HTTP** | Cancelled during preparation | Receipt not returned; part remains `PENDING` or recovered as `RETRYABLE`. |
| **Cancellation during Telegram HTTP** | HTTP in flight when cancelled | Transport outcome unknown; must not be automatically retried; marked unresolved. |
| **HTTP success before result persistence** | Network 200 OK, persistence interrupted | Cooperative cancellation fenced (`_fenced_wait`); cannot protect against hard process kill. |
| **HTTP timeout with unknown outcome** | Network read timeout | Categorized as `UNCERTAIN` (`transport_timeout_uncertain`); never re-sent. |
| **Crash immediately before commit** | Process killed during prep | SQLite WAL rolls back; transaction was never committed; zero HTTP calls. |
| **Crash immediately after committed IN_FLIGHT** | Process killed before HTTP | Stale lease expires; must not be retried unless verified unsent externally. |
| **Crash after HTTP before persistence** | Process killed after send | Event remains `IN_FLIGHT`; recovered as `UNCERTAIN`; exact-once cannot be claimed. |
| **Restart with uncertain multipart** | Process restarts after partial send | Only remaining un-sent parts are sent; already-sent parts are never re-sent. |
| **Partial multipart success** | Part 1 SENT, Part 2 fails | Part 1 remains `SENT`; Part 2 becomes `UNCERTAIN`; event becomes `UNCERTAIN`. |
| **SQLite commit failure after HTTP** | Disk error on persistence | If DB commit fails, UNCERTAIN cannot be guaranteed persisted; caller must treat as unresolved. |
| **Duplicate scan replay** | Same candidate processed in next scan | Blocked by `has_attempt` and dedupe index in `telegram_alert_attempts`. |
| **Runtime epoch change during processing** | Epoch rollover mid-scan | `decide_public_effect` fails closed (`public_event_epoch_mismatch`); send aborted. |
| **Missing canonical reservation** | Public event without reservation | `require_canonical_reservation_for_public_effect` fails closed; send blocked. |
| **Historical reservation ambiguous identity** | 11 quarantined records | Hard blocked by `decide_public_effect` and quarantine filter; never sent. |

---

## 17. RUNTIME_EPOCH_REGRESSION

- Epoch validation via `load_active_runtime_epoch` and `require_active_runtime_epoch` operates on every connection.
- Epoch mismatches fail closed immediately with `RuntimeEpochOwnershipError`.
- No cross-epoch mutations or delivery authorizations are possible.

---

## 18. FULL_TEST_RESULTS

The full pytest test suite was executed on the DEV PC against the baseline branch:
```
============================== test session starts ==============================
platform win32 -- Python 3.11.9, pytest-8.4.2, pluggy-1.6.0
rootdir: C:\CandleCraftDev
configfile: pyproject.toml
plugins: anyio-4.11.0, asyncio-1.1.0, cov-7.0.0
asyncio: mode=Mode.STRICT, default_loop_scope=None

...
3913 passed, 1 warning in 790.57s (0:13:10)
============================== 3913 passed in 790.57s ==============================
```
- **Total Tests Passed**: 3,913
- **Failures**: 0
- **Errors**: 0
- **Regression Count**: 0
- **Verification Disclosure**: DEV-only baseline test success validates the existing codebase (`bcc1e33`), but does **NOT** validate or prove the correctness of any future, unmerged implementation.

---

## 19. PERFORMANCE_EVIDENCE

- **Tested Current Behavior**:
  - The 59–65 second Telegram outbox phase duration observed in the Production Runtime audit represents total wall-clock duration across multiple activities: candidate filtering, CMC shadow evaluation, eligibility gates, rate-limit queries, HTTP round-trips, and synchronous SQLite reads/writes.
  - Synchronous SQLite operations executed on `MainThread` under disk write contention cause measurable event-loop scheduling stalls.
- **Architectural Requirements & Hypothetical Future Benefits**:
  - The architectural goal of P0B3B is to isolate synchronous SQLite preparation and persistence to a dedicated worker thread, removing SQLite disk I/O from the event loop.
  - **No measured performance improvement from P0B3B exists yet.** Any claim of reduced wall-clock duration or event-loop latency remains a hypothetical future benefit until empirically measured on production hardware under real market conditions.

---

## 20. KNOWN_LIMITATIONS

- In the current codebase (`bcc1e330b1f06660a7c4c06edcc6eac368836eae`), `TelegramLifecycleDeliveryService.deliver_for_run` retains outer open context managers for `SQLiteTelegramAlertAttemptRepository` and `SQLiteSetupLifecycleRepository`.
- Full decoupling requires refactoring `deliver_for_run` into discrete operational stages as defined in Section 14.

---

## 21. NEXT_EXACT_PHASE (P0B3B ROADMAP)

### Phase P0B3B — Telegram Outbox Transaction / Transport Boundary Implementation
1. **Worker Infrastructure**: Instantiate `TelegramOutboxWorker` (mirroring `LifecycleWorker`) with bounded queue (size 1) and `_fenced_wait` cancellation shielding.
2. **Phase 1 Implementation**: Implement `stage_1_outbox_preparation` returning `CommittedReservationReceipt`.
3. **Phase 2 Implementation**: Implement `stage_2_async_telegram_transport` on the event loop with zero open SQLite connections.
4. **Phase 3 Implementation**: Implement `stage_3_outbox_persistence` returning `DurableDeliveryReceipt`.
5. **Caller Refactor**: Decouple `_deliver_committed_public_alert_intent` and `deliver_for_run` to use the 3-phase pipeline.
6. **Testing**: Add synthetic double-send, race condition, and cancellation barrier tests.

**RUNTIME_DEPLOYMENT_APPROVED: NO**
