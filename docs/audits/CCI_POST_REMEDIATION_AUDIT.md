# Candle Craft Intelligence Scanner V1 — Consolidated Post-Remediation Validation Audit

Audit date: 2026-07-18
Audit branch: `audit/post-remediation-validation`
Audited integration commit: `95e9f8c577ddf97d0223892de3c87e4efba4bafc`
Target branch: `main` at the same commit
Audit mode: read-only, except for this report

## 1. Executive verdict

**Final readiness verdict: `NO_GO`.**

The integrated remediation series materially improves candle causality, replay causality, setup-only public delivery, minimum-RR consistency, active lifecycle monitoring, internal TP/SL outcome tracking, Telegram crash semantics, watch-loop supervision, and SQLite maintenance safety. All focused suites except the integrated Telegram group passed, compilation passed, `pip check` passed, maintenance CLI help passed, and the no-order invariant passed.

The integration is nevertheless **not ready for controlled Runtime PC migration rehearsal** because the expected full-suite baseline was not met: **1721 passed, 1 failed** instead of 1722 passed, 0 failed. The failure is deterministic when rerun alone. Two threads concurrently opening a fresh database for public-watchlist reservation can race while enforcing `PRAGMA journal_mode=WAL`; one receives `sqlite3.OperationalError: database is locked`, wrapped as `StorageError`. This is a cross-PR regression introduced by the v16/WAL integration against a pre-existing concurrent reservation test.

A second P1 readiness blocker is evidentiary rather than a demonstrated data-loss bug: the suite has a representative v15→v16 Telegram/outbox fixture and rollback test, but no explicit representative **v14→v15→v16** fixture preserving both lifecycle and Telegram data. That exact migration chain was required for this audit and remains unverified.

No P0 issue was found. All eight original P1 findings are closed: seven are `RESOLVED`, and the confirmed-signal production blocker is `RESOLVED_BY_APPROVED_POLICY`. The `NO_GO` is caused by two newly confirmed post-integration P1 findings, not by reopening an original P1.

This verdict does not authorize production deployment, migration, live Telegram delivery, or order execution.

## 2. Baseline and audit limitations

### Baseline

| Item | Recorded value |
|---|---|
| Project | `C:\CandleCraftDev` |
| Branch | `audit/post-remediation-validation` |
| Audited source SHA | `95e9f8c577ddf97d0223892de3c87e4efba4bafc` |
| `origin/main` SHA | `95e9f8c577ddf97d0223892de3c87e4efba4bafc` |
| Initial branch diff | Empty |
| Initial Git status | `?? CCI_FULL_SCANNER_DEEP_AUDIT.md` only |
| Python | CPython 3.11.9 from `.venv` |
| pytest | 8.4.2 |
| Dependency health | `pip check`: `No broken requirements found.` |
| Dependency reproducibility | Incomplete: broad bounded ranges in `requirements.txt`; no lock/fully resolved environment manifest |
| Schema constant | v16 (`app/storage/database.py:7`) |
| Test configuration | `pytest.ini:1-5`: `pythonpath=.`, `testpaths=tests`, default `-q --basetemp=.pytest_tmp`, cache under `.pytest_tmp/cache` |
| Python source files | 168 under `app/` and `scripts/` |
| Python test files | 80 under `tests/` |
| Compiled Python files | 248 (`app/`, `scripts/`, and `tests/`) |
| Collected/full-suite tests | 1722 |
| Expected suite | 1722 passed, 0 failed |
| Actual suite | 1721 passed, 1 failed, 2 warnings |

The existing root-level `CCI_FULL_SCANNER_DEEP_AUDIT.md` was read as historical context. It remained untracked, was not modified, and is not part of this report or commit.

### Safety controls

Every test command was run with:

```text
TELEGRAM_DRY_RUN=true
TELEGRAM_SIGNALS_ENABLED=false
LOCAL_MANUAL_MODE=true
ORDER_EXECUTION_ENABLED=false
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_SIGNAL_CHANNEL_ID=
TELEGRAM_SIGNAL_CHANNEL_INVITE_LINK=
TELEGRAM_VIP_CHANNEL_ID=
TELEGRAM_WOLF_BRIEFING_CHANNEL_ID=
PYTHONDONTWRITEBYTECODE=1
```

Temporary pytest bases and caches were placed outside the repository. No production database, Runtime PC path, external-drive archive, `.env`, Telegram listener, permanent watch loop, or exchange/order endpoint was accessed. No live Telegram or Binance request was made by an audit command. GitHub access is used only after the audit to publish this report.

### Limitations

- No Runtime PC, production database, production WAL/SHM sidecars, or production archive was inspected.
- No production migration was run. All database tests used pytest temporary databases.
- No packet capture or host firewall log was collected; zero live Telegram/Binance activity is established by command scope, cleared credentials, disabled settings, dry-run guards, and transport-double tests—not by network telemetry.
- Crash semantics were validated through deterministic fault injection, not OS power loss.
- Thread-level concurrency was tested; sustained multi-process Runtime contention still needs rehearsal.
- Passing unit/integration tests do not establish exactly-once Telegram delivery. `UNCERTAIN` deliberately requires reconciliation and must not be blindly resent.

## 3. Summary of merged remediation outcomes

| Remediation | Integrated outcome |
|---|---|
| Telegram dry-run enforcement | Effective. Dry-run exits before transport; tests verify zero HTTP calls. |
| Closed-candle integrity | Effective for scanner, replay, synthetic 2D resampling, restart catch-up, and lifecycle outcome evaluation. |
| Replay HTF causality | Effective in deterministic mutation/no-lookahead tests. |
| Deterministic public-funnel time | Effective; focused public-funnel suite is green. |
| Setup-only public alert policy | Effective. Confirmation and TP/SL transitions remain internal; later states do not create public intents. |
| Authoritative minimum RR | Effective through CLI, strategy, target integrity, public eligibility, and reporting. Hard floors cannot be weakened. |
| Active lifecycle priority | Effective. Active symbols survive cooldown/universe/cap ordering and deduplicate to one market scan. |
| Internal lifecycle outcomes | Effective for entry, TP1/TP2/TP3, SL, restart cursors, terminal immutability, and queryability. |
| Crash-aware Telegram outbox | Semantics are materially improved and tests pass when storage opens; fresh concurrent database opening now races in WAL setup. |
| Watch-loop supervisor | Effective for start-to-start cadence, bounded backoff, fatal/recoverable classification, explicit symbol outcomes, and phase status. |
| SQLite WAL/read-only/integrity/backup | Most controls are effective. Fresh concurrent WAL initialization regressed; exact v14→v15→v16 representative preservation is not tested. |
| Schema v16 | Constant, v15 migration, rollback, idempotence, read-only inspection, and verified backup tests pass. |

The series is therefore **not conflict-free**. The concrete conflict is between mandatory per-connection WAL enforcement (`app/storage/database.py:42-86`) and concurrent fresh-database public reservation (`tests/test_telegram_lifecycle_delivery_phase42.py:4561-4615`).

## 4. Original-finding resolution matrix

### P1 findings

| ID | Classification | Current evidence and file/line references | Supporting tests | Remaining limitation | Runtime evidence required | Recommended next action |
|---|---|---|---|---|---|---|
| CCI-P1-001 — Open/future candles | **RESOLVED** | Scanner decisions pass through `closed_candles_as_of` in `app/pipeline/scanner_runner.py:1186-1200`; integrity policy is in `app/data/candle_integrity.py:210-230`; lifecycle receives finalized execution candles at `app/lifecycle/service.py:320-334`; outcome evaluation filters closed candles at `app/lifecycle/outcomes.py:159-187`. | `tests/test_candle_integrity_phase2.py:87-95,126-230`; `tests/test_lifecycle_outcomes.py:388-467,507-559`; focused candle and lifecycle suites green. | Correct decision timestamps and exchange timestamps remain operational dependencies. | Observe Runtime diagnostics around candle exclusion/clock skew on a non-production rehearsal copy. | Keep the closed-window helper as the single boundary and add telemetry review to rehearsal. |
| CCI-P1-002 — Replay HTF lookahead | **RESOLVED** | Replay validates timelines and builds only closed prefixes at `app/backtesting/strategy_replay.py:368-410,781-784,1571`; live and replay both use explicit decision timestamps. | `tests/test_replay_causality_phase2.py:62-120`; replay-focused group: 182 passed. | Runtime data quality cannot be inferred from synthetic fixtures. | Replay a fixed, versioned Runtime export twice and compare hashes/results. | Preserve deterministic replay fixtures and record the data/export version. |
| CCI-P1-003 — Confirmed-signal production blocker | **RESOLVED_BY_APPROVED_POLICY** | `setup_only` is the only accepted policy (`app/core/config.py:29`, `app/alerts/telegram_lifecycle.py:67-70,2911-2926`). Confirmed candidates are audited internally and skipped before delivery at `app/alerts/telegram_lifecycle.py:3114-3146`. | `tests/test_config.py:76-78`; `tests/test_telegram_lifecycle_delivery_phase42.py:5539-5570,7708-7760`. | There is intentionally no confirmed public follow-up. | Confirm product/operator acceptance of setup-only messaging during rehearsal. | Keep the approved policy explicit in deployment documentation. |
| CCI-P1-004 — Telegram dry-run bypass | **RESOLVED** | Sender construction propagates dry-run at `app/alerts/telegram_sender.py:88-116`; preflight returns before transport at `app/alerts/telegram_sender.py:152-159,204`. | `tests/test_telegram_sender_phase42.py:119-189`; outbox dry-run tests at `tests/test_telegram_outbox_recovery.py:351-422`; Telegram focused group reached the unrelated WAL race only. | A misconfigured live deployment remains an operator risk; dry-run itself is enforced. | Validate Runtime environment output with credentials redacted and egress blocked. | Make dry-run the first controlled rehearsal phase. |
| CCI-P1-005 — Telegram send/ledger crash inconsistency | **RESOLVED** | Intent is committed before transport at `app/alerts/telegram_lifecycle.py:2647-2790`; atomic claim/part state/message IDs are in `app/alerts/telegram_outbox.py:157-333`; stale in-flight becomes `UNCERTAIN` at `app/alerts/telegram_outbox.py:390-418`; transport classification is in `app/alerts/telegram.py:46-145`. | `tests/test_telegram_outbox_recovery.py:95-477`; lifecycle crash tests `tests/test_telegram_lifecycle_delivery_phase42.py:8778-9058`. | This is not exactly-once. `UNCERTAIN` needs operator reconciliation. The new WAL race occurs before reservation and is tracked separately as CCI-POST-P1-001. | Crash-injection rehearsal plus Telegram-side message-ID reconciliation in a non-public destination, only after approval. | Fix the WAL open race, then document and rehearse the `UNCERTAIN` runbook. |
| CCI-P1-006 — Lifecycle cannot reach TP/SL outcomes | **RESOLVED** | Plan-specific outcome progress and closed-candle evaluation are implemented at `app/lifecycle/outcomes.py:76-468`; terminal states are immutable at `app/lifecycle/state_machine.py:471-484`; analytics are persisted once at `app/lifecycle/service.py:338-351`. | `tests/test_lifecycle_outcomes.py:188-468,560-678`; lifecycle group: 157 passed. | No live feed/restart-duration evidence was collected. | Rehearse restart catch-up against a copied database and captured closed candles. | Validate counts/cursors before and after controlled restart. |
| CCI-P1-007 — CLI minimum RR mismatch | **RESOLVED** | Floors are centralized at `app/core/minimum_rr.py:8-61`; CLI default/wiring is at `scripts/run_scan.py:496,732`; scanner propagation is at `app/pipeline/scanner_runner.py:185,234-237,1347-1382,1884-1893,2348,2824-2832`. | `tests/test_authoritative_minimum_rr.py:166-363`; RR/strategy group: 162 passed. | Public delivery has an additional 3.0R safety floor and narrow 2.8R target-caution policy; these are stricter public rules, not weaker strategy floors. | Verify deployed CLI arguments and rendered diagnostics in rehearsal. | Keep all future thresholds derived from the effective policy object. |
| CCI-P1-008 — Active lifecycle symbol cooldown omission | **RESOLVED** | Active records are cooldown-exempt at `app/analytics/symbol_health.py:186-253`; active symbols are promoted/deduplicated at `app/lifecycle/service.py:568-604` and `scripts/run_scan.py:2197-2268`. | `tests/test_watch_mode.py:796-1029`; priority focus: 34 passed, 37 deselected. | Sustained Runtime capacity pressure was not exercised. | Rehearse with active count above discovery cap and verify one explicit outcome per queued symbol. | Add operational monitoring for active-cap displacement diagnostics. |

### P2 findings

| ID | Classification | Current evidence and file/line references | Supporting tests | Remaining limitation | Runtime evidence required | Recommended next action |
|---|---|---|---|---|---|---|
| CCI-P2-009 — Universe lacks exchange contract metadata | **OPEN** | Universe selection uses symbols, quote volume, and optional market-cap rank (`tests/test_symbol_universe.py:42-223`). Contract metadata is not carried by the scanner universe; Telegram falls back to diagnostic keys or price-derived tick inference at `app/alerts/telegram_lifecycle.py:8122-8151`. A separate ORM model has `tick_size` (`app/models/exchange_symbol.py:26`) but it is not the universe contract. | Universe tests pass but do not assert lot size, min notional, quantity precision, or authoritative tick filters. | Geometry/display normalization can rely on inferred increments; no order execution exists. | Compare rehearsal universe entries with read-only exchange contract metadata without enabling private endpoints. | Add a read-only, versioned symbol-contract DTO/cache and reject/mark unavailable metadata as `N/A`/`Unverified`. |
| CCI-P2-010 — Lifecycle/delivery/scan-history phase consistency | **PARTIALLY_RESOLVED** | Watch iterations now carry explicit phase statuses at `scripts/run_scan.py:3194-3413`, and outbox/database failures affect iteration status at `app/watch_iteration.py:88-138`. Scan history, lifecycle, and delivery remain distinct persistence operations rather than one atomic cross-phase transaction. | `tests/test_watch_supervisor_reliability.py:153-213`; `tests/test_watch_mode.py:1425-1463`; watch focus: 56 passed. | A crash between successful phases can leave a coherent but partial iteration requiring reconciliation. | Crash between scanner persistence, lifecycle persistence, and outbox persistence on a copied DB; verify phase/status reconciliation. | Define a shared iteration/phase identity and an explicit reconciliation query/runbook. |
| CCI-P2-011 — Watch iteration exceptions terminate monitoring | **RESOLVED** | Fatal/recoverable classification and bounded backoff are in `app/watch_supervisor.py:1-191`; the runner reports phase failures and continues recoverable iterations at `scripts/run_scan.py:2624-2907,3194-3413`. | `tests/test_watch_supervisor_reliability.py:38-213`; `tests/test_watch_mode.py:1324-1463`; 56 passed. | Long-duration resource behavior is not established by unit tests. | Run a bounded dry-run soak with injected recoverable failures and resource monitoring. | Add rehearsal acceptance thresholds for loop health and backoff. |
| CCI-P2-012 — SQLite concurrency, integrity and growth controls | **PARTIALLY_RESOLVED** | WAL/FULL/busy-timeout/autocheckpoint are verified at `app/storage/database.py:42-86,176-205`; read-only open is at `app/storage/database.py:102-152`; verified online backup and diagnostics are at `app/storage/maintenance.py:213-589,804-851`; no deletion policy is documented at `docs/RUNTIME_SQLITE_MAINTENANCE.md:71-75`. | Storage/maintenance focus: 83 passed; WAL backup `tests/test_sqlite_maintenance.py:342-439`; no-delete API `:702-704`. | Fresh concurrent open races at WAL mode enforcement; no automatic pruning exists by design. | Multi-process contention, disk-growth, WAL-size, and backup-age observation on Runtime-like storage. | Resolve CCI-POST-P1-001, retain non-destructive diagnostics, and define operator-managed capacity/retention policy. |
| CCI-P2-013 — Telegram retries/multipart partial duplication | **RESOLVED** | Per-part states and message IDs prevent confirmed parts from being selected again (`app/alerts/telegram_outbox.py:222-333`); uncertain transport outcomes are not blind retries (`app/alerts/telegram.py:46-145`). | `tests/test_telegram_outbox_recovery.py:185-333,423-477`; lifecycle crash tests `:8862-9058`. | Exactly-once is not claimed; ambiguous acceptance remains `UNCERTAIN`. | Reconcile a deliberately ambiguous non-public test message with Telegram records after approval. | Maintain operator reconciliation and never auto-retry `UNCERTAIN`. |
| CCI-P2-014 — Regime rejection erases prior history | **OPEN** | When regime blocks, the overlay replaces history with only `REJECTED_BY_REGIME` at `app/pipeline/scanner_runner.py:3551-3566`, although rejection reasons are appended. | Regime/public gate tests pass (`tests/test_regime_intelligence.py:417-459`) but do not preserve the prior status sequence. | Prior idea/alert/journal statuses are no longer visible in `status_history`, complicating audit lineage. | Inspect representative persisted scan rows across a regime block. | Preserve prior history and append regime rejection; add persistence/manifest regression tests. |
| CCI-P2-015 — Same-side lifecycle plan identity limitations | **PARTIALLY_RESOLVED** | Public canonical plan IDs and outcome progress are plan-specific (`app/storage/database.py:424-453`; `app/lifecycle/outcomes.py:86-109`). The core lifecycle record still has `UNIQUE(symbol, mode, direction)` at `app/storage/database.py:350-400`, and lookup uses that tuple at `app/lifecycle/repositories.py:38-46`. | Canonical-plan/dedup tests `tests/test_telegram_lifecycle_delivery_phase42.py:4007-4701`; outcome plan isolation `tests/test_lifecycle_outcomes.py:468-506`. | Concurrent or rapidly replaced same-side plans share one core lifecycle slot even though outcome/public identities are stronger. Post-cooldown delivery of a genuinely new plan lacks one explicit end-to-end test. | Rehearse sequential material plan replacement across cooldown and restart. | Make lifecycle identity explicitly plan-aware or document single-slot replacement semantics; add post-cooldown test. |
| CCI-P2-016 — Date-dependent public-funnel test | **RESOLVED** | Public-funnel time is injected/fixed rather than derived from wall-clock date. | Public-funnel/public-safety focus: 43 passed on 2026-07-18. | Runtime clock skew remains an operational concern, not a test nondeterminism issue. | Confirm NTP/timezone and timestamp diagnostics on Runtime PC. | Keep injected clocks in all expiry/cooldown tests. |
| CCI-P2-017 — Global timeout/downstream isolation | **RESOLVED** | Global timeout creates explicit `NOT_RUN` outcomes at `app/pipeline/scanner_runner.py:743-751`; per-symbol errors are isolated at `:714-723`; downstream phases independently report failure at `scripts/run_scan.py:3318-3413`. | Scanner focus: 131 passed; watch focus: 56 passed; explicit outcome tests `tests/test_watch_supervisor_reliability.py:153-190`. | Runtime external-service latency distribution is unknown. | Bounded soak with induced symbol and downstream timeouts. | Monitor timeout classifications and phase statuses during rehearsal. |

### P3 findings

| ID | Classification | Current evidence and file/line references | Supporting tests | Remaining limitation | Runtime evidence required | Recommended next action |
|---|---|---|---|---|---|---|
| CCI-P3-018 — Duplicate public-quality helper | **OPEN** | `_public_setup_quality_score_decimal` is defined twice, identically, at `app/alerts/telegram_lifecycle.py:8387-8406`; the latter silently shadows the former. | Public/Telegram tests pass but do not detect duplicate definitions. | Maintenance divergence risk; no current behavior failure. | None. | Remove one definition in a separate remediation and add a static duplicate-definition check. |
| CCI-P3-019 — Strategy engine recomputes all modes | **OPEN** | Scanner loops configured modes at `app/pipeline/scanner_runner.py:1359-1374`; each strategy call computes challenge, swing, and scalp before selecting the requested setup at `app/strategies/liquidity_grab_pullback.py:465-472`. | Strategy tests pass; no performance regression test measures redundant work. | Avoidable CPU cost can reduce universe capacity/cadence headroom. | Profile representative Runtime universe and candle sizes. | Compute shared features once and evaluate only requested modes; add deterministic equivalence and performance-budget tests. |
| CCI-P3-020 — Delay-after-work watch cadence | **RESOLVED** | Supervisor calculates start-to-start schedule and skips overrun slots without overlapping iterations (`app/watch_supervisor.py:93-146`). | `tests/test_watch_supervisor_reliability.py:38-100`; watch focus: 56 passed. | Long-running Windows scheduler/clock behavior needs observation. | Bounded soak with iteration durations below/above cadence. | Record planned vs actual start time in rehearsal telemetry. |
| CCI-P3-021 — Dependency/audit reproducibility | **OPEN** | `requirements.txt:1-9` contains broad version ranges; no lock or resolved dependency manifest exists. `pip check` proves current consistency only. | Current environment: `pip check` green and suite executes. | A fresh environment may resolve different versions and warnings. | Recreate the environment from a future lock on a clean Runtime-like host. | Adopt a hashed/resolved lock plus documented Python/SQLite versions and CI reproduction command. |

### Original-finding totals

| Severity | Resolved | Resolved by approved policy | Partially resolved | Open | Regressed | Runtime-only classification |
|---|---:|---:|---:|---:|---:|---:|
| P1 | 7 | 1 | 0 | 0 | 0 | 0 |
| P2 | 4 | 0 | 3 | 2 | 0 | 0 |
| P3 | 1 | 0 | 0 | 3 | 0 | 0 |
| **Total** | **12** | **1** | **3** | **5** | **0** | **0** |

## 5. Cross-PR integration results

| Integration area | Result | Audit conclusion |
|---|---|---|
| Candle finality and lifecycle outcomes | PASS | Closed-window policy is shared; replay/live/synthetic 2D/restart paths remain causal and symbol-isolated. |
| Setup-only delivery and lifecycle | PASS with documented limitation | One setup intent is public; confirmation and outcomes are internal; post-cooldown new-plan end-to-end evidence should be added. |
| RR integration | PASS | Hard floors are centralized, overrides only strengthen, and public gates remain at least as strict. |
| Active lifecycle priority/outcomes | PASS | Active plans survive discovery/cooldown/cap logic; market scan dedupe does not collapse plan-specific outcome cursors. |
| Telegram dry-run/outbox | **FAIL** | Outbox semantics pass, but a fresh concurrent DB open can fail before claim due WAL initialization race. |
| Watch supervisor/outbox | PASS with storage caveat | Recoverable phases do not terminate; fatal corruption/schema does. The WAL race is surfaced as storage failure, but the underlying concurrent reservation contract still fails. |
| SQLite/schema/backup | **FAIL readiness** | Most safety controls pass; fresh concurrency regresses and exact v14→v15→v16 representative preservation is absent. |
| Research integrity | PARTIAL | Replay and terminal outcome capture are causal/queryable; live lifecycle terminal outcomes are not fed into performance memory, and row cohorts lack producer version. |
| Public gate integrity | PASS | Mandatory quality, score, RR, regime, target, freshness, geometry/data, and cooldown checks remain reachable and fail closed. |
| No-order invariant | PASS | Configuration rejects order execution and no execution API path was found. |

## 6. Full test results

### Full suite

```text
1 failed, 1721 passed, 2 warnings in 135.11s
```

Failure:

```text
tests/test_telegram_lifecycle_delivery_phase42.py::test_concurrent_public_watchlist_reservations_allow_one_sender
sqlite3.OperationalError: database is locked
app/storage/database.py:65 -> PRAGMA journal_mode=WAL
app/storage/database.py:95 -> StorageError
app/alerts/telegram_lifecycle.py:1015 -> open_initialized_database
```

The failing node was rerun alone and failed again: **1 failed in 2.87s**. The focused Telegram group also reproduced it: **396 passed, 1 failed in 53.91s**. The test predates the WAL remediation; blame identifies WAL connection setup as the changed integration surface. This is not dismissed as flakiness.

The two suite warnings were non-failing: one pytest configuration warning caused by disabling the cache provider in the full-suite command, and one Starlette/httpx deprecation warning. Focused runs used an external cache path and did not depend on repository cache output.

### Focused results

| Group | Result |
|---|---:|
| Candle integrity and timeframes | 12 passed |
| Replay and replay research/validation | 182 passed |
| Strategy and RR | 162 passed |
| Scanner runner and CLI runner | 131 passed |
| Lifecycle and outcomes | 157 passed |
| Symbol-health/active priority selection | 34 passed, 37 deselected |
| Telegram sender/lifecycle/outbox/channel/runtime | **396 passed, 1 failed** |
| Watch supervisor/watch mode | 56 passed |
| Storage/schema/migration/maintenance | 83 passed |
| Public funnel/gate safety | 43 passed |
| Performance memory/regime/edge/research | 60 passed |
| Explicit no-order safety nodes | 5 passed |
| Python compilation | 248 files compiled |
| `pip check` | No broken requirements |
| Scanner `--help` | Exit 0 |
| SQLite maintenance `--help` | Exit 0; inspect, quick-check, full-check, backup, backup-verify, checkpoint visible |

## 7. Candle/replay causality

The closed-candle boundary is explicit and compatible across live scanner, replay, and lifecycle outcome evaluation:

- `app/data/candle_integrity.py:118-230` validates ordering, duplicates/gaps, explicit or derived close times, and filters to `close_timestamp <= decision_timestamp`.
- `app/pipeline/scanner_runner.py:644-650,1186-1200` resolves the decision clock and applies the closed window before strategy decisions.
- `app/data/timeframes.py:14-84` creates UTC-anchored deterministic 2D buckets from complete daily pairs only.
- `app/pipeline/scanner_runner.py:1224-1229,1465-1526` applies the same policy to synthetic and optional higher timeframes.
- `app/backtesting/strategy_replay.py:368-410,781-784,1571` validates and truncates replay input at each decision.
- `app/lifecycle/outcomes.py:159-187` re-applies closed-window/cursor constraints during outcome catch-up.
- Per-symbol candle failures become explicit scan errors at `app/pipeline/scanner_runner.py:714-723`; they do not corrupt other symbols.

No inspected path allows a future/open candle to create entry, TP, or SL. Focused candle, replay, scanner, and lifecycle tests support this conclusion.

## 8. Setup-only delivery integrity

`setup_only` is enforced by configuration and delivery. One eligible canonical setup can create one public intent/message; subsequent confirmation, entry, TP1/TP2/TP3, SL, invalidation, expiry, and cooldown transitions do not create later public attempts. Internal lifecycle transitions and research storage continue.

Evidence:

- Policy constant/fail-closed validation: `app/alerts/telegram_lifecycle.py:67-70,2911-2926`.
- Confirmed delivery disabled after internal audit: `app/alerts/telegram_lifecycle.py:3114-3146`.
- Non-watchlist/later state suppression: `app/alerts/telegram_lifecycle.py:3233-3250,3355-3373`.
- Research public delivery skipped while internal research remains: `app/alerts/telegram_lifecycle.py:3741-3750`.
- Active-signal displays use lifecycle records: `tests/test_telegram_active_watchlists_phase50b.py:684-821`.
- Setup-only and terminal silence: `tests/test_telegram_lifecycle_delivery_phase42.py:7708-7760,8741-8777`; `tests/test_lifecycle_outcomes.py:560-601`.

Canonical plan IDs distinguish materially new plans for public dedupe. Symbol/side cooldown still intentionally blocks even a changed plan during the active window. An explicit after-cooldown, materially new-plan end-to-end delivery test was not found; this limitation is included under CCI-P2-015.

## 9. RR policy integrity

The strategy hard floors are:

- scalp: 2.5R
- swing: 2.5R
- challenge: 3.0R

`app/core/minimum_rr.py:8-61` is authoritative. Lower values fail validation or are raised to hard floors; higher configured values propagate through strategy input, target intelligence, setup quality, public eligibility, Telegram context, and reporting. `tests/test_authoritative_minimum_rr.py:166-404` covers CLI defaults/overrides, unsafe lower values, inclusive boundaries, target/public/report propagation, unchanged default eligibility, and absence of an order-execution surface.

Public delivery applies an additional general 3.0R floor (`app/alerts/telegram_lifecycle.py:202-209,8438-8445`). That stricter public threshold does not conflict with or weaken the 2.5R strategy floors. The narrow `TARGET_INSIDE_CHOP` target-caution policy can use 2.8R, but only with explicit A/A+ grade, score ≥88, technical/opportunity thresholds, exact warning severity, confirmation readiness, and no other failed gate (`app/alerts/telegram_lifecycle.py:8531-8630,8873-8890`). Higher operator overrides remain monotonic.

No conflicting lower raw threshold was found that can weaken a hard floor.

## 10. Lifecycle monitoring and outcomes

Active lifecycle monitoring is reconstructed from stored non-terminal records. Active symbols outside discovery remain queued, health cooldown does not omit them, and more active records than the configured discovery cap remain represented. Multiple plans for a symbol deduplicate to one market-data scan, while `setup_lifecycle_outcome_progress` uses `(lifecycle_id, plan_identity)` and retains plan-specific cursors.

Outcome rules are deterministic for entry, TP1/TP2/TP3, and SL; ambiguous same-candle outcomes follow tested policy. Terminal records return without reactivation. Restart catch-up resumes after the stored close cursor and cannot consume open candles.

Evidence: `app/analytics/symbol_health.py:186-253`; `app/lifecycle/service.py:568-604`; `scripts/run_scan.py:2197-2333`; `app/storage/database.py:424-453`; `app/lifecycle/outcomes.py:76-468`; `app/lifecycle/state_machine.py:471-484`; `tests/test_watch_mode.py:796-1029`; `tests/test_lifecycle_outcomes.py:188-559`.

The remaining identity limitation is the single core lifecycle row per `(symbol, mode, direction)`.

## 11. Telegram outbox and crash semantics

The outbox now has explicit `PENDING`, `IN_FLIGHT`, `SENT`, `RETRYABLE`, `UNCERTAIN`, and `FAILED_FINAL` states. Intent and exact parts are persisted before transport. Claims use `BEGIN IMMEDIATE`, confirmed parts keep Telegram message IDs, and only unsent/retryable parts are selected again. Stale `IN_FLIGHT` becomes `UNCERTAIN`, not `RETRYABLE`. Proven connection/non-acceptance failures can be retryable; ambiguous read/write outcomes are uncertain; permanent client rejection is final.

Dry-run does not call HTTP. Setup-only suppresses later lifecycle outbox rows. The clean/crash restart tests are coherent and intentionally do not claim exactly-once.

The integrated defect occurs earlier: `open_initialized_database()` calls per-connection WAL enforcement before claim/reservation. On a fresh file, two concurrent initializers can both observe non-WAL and race to change journal mode. One fails rather than waiting/retrying. This breaks the tested “one reservation wins, the other is cleanly rejected” contract.

## 12. Watch-loop resilience

Watch behavior passes focused tests:

- `RETRYABLE`, `UNCERTAIN`, and `FAILED_FINAL` are summarized into phase status and do not terminate watch mode.
- Unsupported schema/corruption classification remains fatal.
- Recoverable failures use bounded exponential backoff.
- Scheduling is start-to-start and skips missed anchors without overlap.
- Every queued symbol must receive one unique explicit outcome.
- Scanner, lifecycle, outbox, storage, symbol-health, and output phases affect iteration status.
- Cancellation/failure tests verify cleanup paths.

Evidence: `app/watch_supervisor.py:1-191`; `app/watch_iteration.py:34-138`; `scripts/run_scan.py:2624-2907,3194-3413`; `tests/test_watch_supervisor_reliability.py:38-213`; `tests/test_watch_mode.py:1324-1463`.

The supervisor can survive a recoverable storage failure, but that does not make the concurrent WAL reservation defect acceptable: public intent processing can be skipped for that iteration, and the expected full suite remains red.

## 13. SQLite/schema/backup integrity

Confirmed controls:

- `SCHEMA_VERSION = 16` at `app/storage/database.py:7`.
- Writable connections verify `foreign_keys=ON`, 5-second busy timeout, WAL, `synchronous=FULL`, and 1000-page auto-checkpoint at `app/storage/database.py:42-86,176-205`.
- Read-only inspection uses `mode=ro`, conditionally `immutable=1`, and `query_only=ON`; it does not create or migrate (`app/storage/database.py:102-152`).
- Migration is enclosed in `BEGIN IMMEDIATE` with rollback on SQLite error (`app/storage/database.py:209-221,856-861`).
- A representative v15 Telegram/public-event fixture survives v16 migration and is idempotent (`tests/test_storage_database.py:849-976`).
- Migration failure rollback is tested from v15 (`tests/test_storage_database.py:979+`).
- Online backup uses SQLite’s backup API and includes committed WAL records (`app/storage/maintenance.py:296-433,536-555`; `tests/test_sqlite_maintenance.py:342-439`).
- Corrupt/incomplete snapshots are not promoted; existing backups are not overwritten; manifests exclude environment secrets (`tests/test_sqlite_maintenance.py:362-567,696-725`).
- No automatic delete/prune/rotate API exists (`tests/test_sqlite_maintenance.py:702-704`; `docs/RUNTIME_SQLITE_MAINTENANCE.md:71-75`).
- Runtime backup before v16 is mandatory (`docs/RUNTIME_SQLITE_MAINTENANCE.md:8-59`).

Readiness failures:

1. Fresh concurrent WAL initialization fails the integrated reservation test.
2. Search found no `user_version = 14`, v14 fixture, or explicit v14→v15→v16 test in `tests/`. Generic legacy migration tests and the v15 fixture do not prove preservation of representative v14 lifecycle plus Telegram data through both steps.

No temporary database, snapshot, manifest, or sidecar was created in the repository.

## 14. Research and adaptive integrity

Replay is deterministic/causal, and lifecycle TP1/TP2/TP3/SL progress and terminal analytics are queryable. Terminal lifecycle analytics are persisted once (`app/lifecycle/service.py:338-351`; `tests/test_lifecycle_outcomes.py:602-678`). Replay performance-memory ingestion deduplicates terminal trade IDs (`app/analytics/performance_memory.py:363-440`; `tests/test_performance_memory.py:225-243`).

Regime and performance overlays do not turn an invalid strategy result into a valid setup: regime can downgrade/block (`tests/test_market_regime.py:217+`; `tests/test_regime_intelligence.py:417-459`), and performance memory is documented as historical evidence rather than a mandatory-gate override (`app/analytics/performance_memory.py:24-25`). Missing confirmations remain explicit strategy failures; no adaptive code inspected fabricates them.

Two limitations remain:

- The runtime performance-memory path ingests `replay_summary` only (`scripts/run_scan.py:888-899`). Live lifecycle terminal analytics are stored once but are not consumed into performance memory. The audit requirement is therefore only partially met.
- Database schema version identifies the database, not row cohorts. Scan/lifecycle/outbox rows do not carry immutable scanner/strategy/producer versions. Pre- and post-remediation rows cannot be reliably separated by version metadata.

## 15. Public gate integrity

The public watchlist gate remains fail-closed and fixture-reachable. It validates explicit source/state, expiry/freshness, grade, score, plan completeness/geometry, actionability, status/rejection history, data health, RR, target integrity, failed-gate class, regime compatibility, rolling limits, and symbol/side cooldown (`app/alerts/telegram_lifecycle.py:7023-7190,7717-7818,8380-8562,9466-9515`). Missing mandatory fields produce blockers; optional unavailable/unreliable fields remain `N/A` or `Unverified` rather than being promoted to verified facts.

`TARGET_INSIDE_CHOP` is accurately characterized as follows:

- In scanner target integrity it is a soft target warning, not a hard trade-map rejection (`app/pipeline/scanner_runner.py:109-116`; `tests/test_scanner_runner.py:364-383`).
- A generic candidate with `target_inside_chop` but without the explicit caution contract remains blocked (`tests/test_telegram_lifecycle_delivery_phase42.py:5733-5759`).
- A narrowly marked `target_caution_actionable` candidate may be public only at A/A+, score ≥88, RR ≥2.8, technical/opportunity ≥95, exact inside-chop warning, required confirmation, no terminal state, and no conflicting failed gate (`app/alerts/telegram_lifecycle.py:202-209,7023-7074,8531-8630,8873-8890`; tests `:601-780,2247-2295`).

No remediation was found to bypass a mandatory gate or broadly increase public frequency. The public-funnel focused suite passed. The intended target-caution exception is an existing explicit policy, not an accidental bypass.

## 16. No-order invariant

The invariant is green:

- `order_execution_enabled` defaults false and true is rejected by model validation (`app/core/config.py:65,116-120`; `tests/test_config.py:51,71-73`).
- Source search found no `create_order`, `place_order`, `new_order`, `futures_create_order`, withdrawal, or transfer implementation under Python production paths.
- Strategy has no order-execution surface (`tests/test_authoritative_minimum_rr.py:404+`).
- Public-watchlist and confirmed-signal delivery tests install an execution tripwire and verify it is not called (`tests/test_telegram_lifecycle_delivery_phase42.py:4749-4750,5655-5664,5812-5813,6026-6060`).
- Lifecycle outcomes, Telegram outbox, and watch supervision import no order executor and use only state/persistence/alert interfaces.
- The explicit five-node no-order command passed.

## 17. Remaining confirmed findings

### CCI-POST-P1-001 — Fresh concurrent WAL initialization breaks public reservation

- **Severity:** P1
- **Confidence:** High; reproduced in the full suite, focused Telegram suite, and isolated node.
- **Evidence:** `connect_database` reads and conditionally changes journal mode at `app/storage/database.py:63-79`; `SQLiteTelegramAlertAttemptRepository.__enter__` opens/initializes at `app/alerts/telegram_lifecycle.py:1009-1016`; concurrent fresh-file test at `tests/test_telegram_lifecycle_delivery_phase42.py:4561-4615` fails with `database is locked` at line 65.
- **Impact:** A concurrent delivery worker can fail before reservation/claim. The one-winner/one-clean-rejection contract is not met, the full suite is red, and controlled migration rehearsal is blocked.
- **Recommended focused remediation:** Make mandatory WAL initialization safe under concurrent first open using bounded, explicit retry/serialization around journal-mode initialization while retaining fail-closed verification and transaction guarantees. Do not transform an ambiguous delivery into a retry.
- **Regression tests required:** Existing failing test must pass repeatedly; add fresh-file and already-WAL thread and multi-process cases; assert one claim, no duplicate attempt/part/message, bounded timeout, connection cleanup, and no retry of `UNCERTAIN`.

### CCI-POST-P1-002 — Required v14→v15→v16 representative preservation is unverified

- **Severity:** P1
- **Confidence:** High for the test/evidence gap; no data-loss defect is asserted.
- **Evidence:** v15 fixture and v15→v16 tests exist at `tests/test_storage_database.py:849-976`; rollback begins at `:979`; repository-wide search found no explicit `user_version = 14`/v14 fixture/test. v14 was the pre-outcome schema in the remediation sequence.
- **Impact:** The exact Runtime migration path required by this audit is not proven to preserve representative lifecycle and Telegram/outbox rows. GO criteria for schema migration evidence are not met.
- **Recommended focused remediation:** Build a faithful v14 fixture containing representative lifecycle records/events and Telegram attempts/public events, migrate through current initialization to v16, and verify row identities, states, timestamps, payload hashes, message IDs, uncertain mapping, outcome-table creation, idempotence, and rollback.
- **Regression tests required:** v14→v16 preservation, explicit intermediate expectations where practical, repeat-open idempotence, injected migration failure leaving v14 unchanged, and verified-backup/restore migration against the fixture.

### CCI-P2-009 — Universe exchange contract metadata remains absent

- **Severity:** P2
- **Confidence:** High.
- **Evidence:** Universe tests cover symbol/volume/market-cap selection (`tests/test_symbol_universe.py:42-223`); Telegram infers tick from diagnostics/prices at `app/alerts/telegram_lifecycle.py:8122-8151`.
- **Impact:** Contract-normalized display/geometry cannot be established authoritatively and future paper/execution validation would lack lot/notional constraints.
- **Recommended focused remediation:** Add read-only exchange-contract metadata with explicit availability/reliability status.
- **Regression tests required:** Tick/step/min-notional/precision parsing, stale/missing metadata fail-closed behavior, cache versioning, rate-limit/timeout/bad-response tests, and no private/order endpoint calls.

### CCI-P2-010 — Cross-phase persistence is observable but not atomic

- **Severity:** P2
- **Confidence:** Medium-high.
- **Evidence:** Explicit phase statuses at `scripts/run_scan.py:3194-3413`; scan history, lifecycle, and outbox are separate repositories/commits.
- **Impact:** Crash boundaries can leave partial but labeled iterations requiring reconciliation.
- **Recommended focused remediation:** Define a shared iteration identity and deterministic reconciliation contract rather than forcing a long cross-service transaction.
- **Regression tests required:** Crash at each phase boundary, restart reconciliation, no duplicate lifecycle/outbox intent, and accurate iteration status/history.

### CCI-P2-012 — SQLite policy is broad but concurrency/growth readiness remains partial

- **Severity:** P2 (with its concrete concurrency regression escalated separately to CCI-POST-P1-001)
- **Confidence:** High.
- **Evidence:** SQLite profile/backup diagnostics at `app/storage/database.py:42-205` and `app/storage/maintenance.py:213-851`; no automatic retention by `docs/RUNTIME_SQLITE_MAINTENANCE.md:71-75`; failing fresh concurrency path above.
- **Impact:** Runtime contention and long-term capacity remain operational risks.
- **Recommended focused remediation:** Resolve the race, preserve non-destructive policy, and define capacity thresholds plus operator retention/archive procedure.
- **Regression tests required:** Concurrent writer/open stress, WAL checkpoint behavior, disk-full/low-space handling, backup-age/growth diagnostics, and no automatic deletion.

### CCI-P2-014 — Regime overlay replaces prior status history

- **Severity:** P2
- **Confidence:** High.
- **Evidence:** `status_history` is assigned a new one-element tuple at `app/pipeline/scanner_runner.py:3551-3566`.
- **Impact:** Persisted lineage can lose evidence that an idea/alert/journal stage existed before the regime block.
- **Recommended focused remediation:** Append regime rejection to immutable prior history while clearing public/actionable payloads as policy requires.
- **Regression tests required:** Existing idea history + regime rejection order, persisted scan JSON/DB history, alert integrity manifest, and no public eligibility after block.

### CCI-P2-015 — Core same-side lifecycle identity remains single-slot

- **Severity:** P2
- **Confidence:** High.
- **Evidence:** `UNIQUE(symbol, mode, direction)` at `app/storage/database.py:399`; tuple lookup at `app/lifecycle/repositories.py:38-46`; plan-specific outcome progress at `app/storage/database.py:424-453` mitigates but does not remove the core constraint.
- **Impact:** Concurrent/replaced same-side plans may overwrite the conceptual lifecycle slot; identity semantics rely on replacement rules.
- **Recommended focused remediation:** Adopt plan-aware lifecycle identity or explicitly codify safe single-slot supersession.
- **Regression tests required:** Two materially distinct same-side plans, outcome cursor isolation, supersession/restart, cooldown expiry, and post-cooldown new public eligibility.

### CCI-POST-P2-001 — Live lifecycle terminal outcomes do not feed performance memory

- **Severity:** P2
- **Confidence:** High.
- **Evidence:** Runtime path calls `ingest_replay_summary` only at `scripts/run_scan.py:888-899`; lifecycle stores terminal analytics at `app/lifecycle/service.py:338-351`; performance memory exposes replay ingestion at `app/analytics/performance_memory.py:363-440` and no lifecycle analytics ingestion path was found.
- **Impact:** Adaptive historical memory can omit real internally tracked terminal outcomes, so the “consume terminal outcomes once” integration goal is only met for replay events.
- **Recommended focused remediation:** Add an explicit, read-only-to-gates ingestion adapter from terminal lifecycle analytics with stable event IDs and cohort metadata. It must not weaken mandatory gates.
- **Regression tests required:** Each terminal lifecycle plan ingested exactly once across restart, duplicates ignored, partial/non-terminal rows skipped, old unknown cohorts marked, and invalid setups never promoted.

### CCI-POST-P2-002 — Stored research rows lack immutable producer/cohort version metadata

- **Severity:** P2
- **Confidence:** High.
- **Evidence:** Search across storage/lifecycle/pipeline found database schema version but no scanner/strategy/producer version fields on current scan/lifecycle/outbox rows; `app/storage/maintenance.py` reports database schema only.
- **Impact:** Old and causality-corrected cohorts cannot be separated reliably for research/performance analysis. Database v16 does not mean every row was produced by v16 behavior.
- **Recommended focused remediation:** Add immutable producer version, strategy version, data/finality policy version, and cohort timestamp/source metadata to new rows; represent old rows as `N/A`/`Unverified`, not inferred.
- **Regression tests required:** New-row version persistence, old-row migration without fabricated values, cohort filtering, replay export propagation, and performance-memory segregation.

### CCI-P3-018 — Duplicate public-quality helper

- **Severity:** P3
- **Confidence:** High.
- **Evidence:** Duplicate definitions at `app/alerts/telegram_lifecycle.py:8387-8406`.
- **Impact:** Silent shadowing and future divergence risk.
- **Recommended focused remediation:** Remove the duplicate in an isolated cleanup.
- **Regression tests required:** Existing public-quality thresholds plus a static duplicate-definition/lint check.

### CCI-P3-019 — Strategy mode recomputation

- **Severity:** P3
- **Confidence:** High.
- **Evidence:** Scanner mode loop `app/pipeline/scanner_runner.py:1359-1374`; all three modes computed per call at `app/strategies/liquidity_grab_pullback.py:465-472`.
- **Impact:** Reduced watch cadence headroom and unnecessary CPU.
- **Recommended focused remediation:** Share feature computation and evaluate only requested modes while preserving deterministic results.
- **Regression tests required:** Byte/field-equivalent diagnostics and setups per mode plus a bounded call-count/performance test.

### CCI-P3-021 — Dependency environment is not locked

- **Severity:** P3
- **Confidence:** High.
- **Evidence:** Broad ranges in `requirements.txt:1-9`; no lock file; current `pip check` only establishes this environment.
- **Impact:** Fresh Runtime/CI installs can resolve materially different versions.
- **Recommended focused remediation:** Produce a reviewed, hashed lock/constraints artifact and document exact Python/SQLite/tool versions.
- **Regression tests required:** Clean-environment install, `pip check`, full suite, CLI help, and reproducible version manifest comparison.

### Remaining totals after this audit

- P0: 0.
- P1: 2 new blockers; 0 unresolved original P1.
- P2: 7 remaining items (2 original open, 3 original partial, 2 new).
- P3: 3 original open items.
- Regressions found: 1 concrete cross-PR regression (CCI-POST-P1-001).

## 18. Runtime-only evidence still required

1. Identify the actual Runtime database schema/version and sidecar state using read-only inspection; do not initialize it.
2. Create and verify an online backup of the active Runtime database before any migration; prove restore to a separate rehearsal path.
3. Migrate a restored copy—not production—from its actual schema through v16 and compare table counts, representative lifecycle/outbox rows, integrity checks, foreign keys, and message IDs/hashes.
4. Exercise fresh and pre-existing WAL database opens with concurrent processes on the Runtime filesystem.
5. Run a bounded, supervised dry-run soak with Telegram credentials/destinations cleared and egress blocked; observe start-to-start cadence, no overlaps, resource cleanup, active-priority inclusion, and explicit per-symbol outcomes.
6. Inject recoverable database/outbox failures and controlled restart; verify no blind `UNCERTAIN` resend and no confirmed multipart duplication.
7. Confirm Windows filesystem type, permissions, free space, WAL/autocheckpoint behavior, clock/NTP/timezone, archive location, and backup-age/growth diagnostics.
8. Capture process/network telemetry proving no unintended Telegram/Binance write path during the dry-run rehearsal.
9. Validate operator reconciliation and backup/restore runbooks before any live destination is configured.

## 19. Runtime migration rehearsal prerequisites

The following are mandatory before changing the verdict to `GO_FOR_CONTROLLED_MIGRATION_REHEARSAL`:

1. CCI-POST-P1-001 fixed and the full 1722-test baseline (or higher) green repeatedly.
2. Explicit representative v14→v15→v16 lifecycle-plus-Telegram preservation and rollback tests green.
3. Verified backup and restore rehearsal completed on a copy of the actual Runtime database.
4. `quick-check`, `full-check`, foreign-key checks, schema inspection, and backup verification green on the restored copy.
5. No-order invariant and enforced safety environment revalidated.
6. Fresh/pre-existing multi-process WAL contention tests green on the target filesystem.
7. Bounded dry-run watch soak meets cadence/resource/error-status acceptance criteria with no live Telegram/Binance requests.
8. Remaining P2/P3 limitations accepted in writing with owners and follow-up scope.

## 20. Prioritized next-phase plan

1. **P1:** Fix and stress-test concurrent WAL initialization without changing outbox uncertainty semantics.
2. **P1:** Add the faithful v14→v15→v16 lifecycle/Telegram preservation, idempotence, rollback, backup, and restore fixture.
3. Re-run the entire audit test matrix and require zero failures.
4. Prepare a verified Runtime database copy and execute the controlled migration rehearsal on that copy only.
5. **P2:** Add immutable producer/cohort metadata and a once-only lifecycle terminal-outcome adapter for performance memory.
6. **P2:** Resolve/publish lifecycle identity, cross-phase reconciliation, universe contract metadata, and regime history behavior.
7. **P3:** Remove duplicate helper, reduce mode recomputation, and lock dependencies.

## 21. Exact commands executed

Commands that were rejected by the filesystem sandbox before process launch are not listed as executed. No dependency installation command was run.

### Baseline and Git inspection

```powershell
Get-Location
git branch --show-current
git rev-parse HEAD
git rev-parse origin/main
git status --short
git diff --name-status origin/main...HEAD
git diff --check
git log --oneline --decorate -12
git remote -v
gh --version
gh auth status
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pytest --version
.\.venv\Scripts\python.exe -m pip check
Get-Content pytest.ini
Get-Content requirements.txt
(rg --files app scripts -g '*.py' | Measure-Object).Count
(rg --files tests -g '*.py' | Measure-Object).Count
rg -n "SCHEMA_VERSION" app\storage\database.py
```

### Safety prefix applied to pytest commands

```powershell
$env:TELEGRAM_DRY_RUN='true'; $env:TELEGRAM_SIGNALS_ENABLED='false'; $env:LOCAL_MANUAL_MODE='true'; $env:ORDER_EXECUTION_ENABLED='false'; $env:TELEGRAM_BOT_TOKEN=''; $env:TELEGRAM_CHAT_ID=''; $env:TELEGRAM_SIGNAL_CHANNEL_ID=''; $env:TELEGRAM_SIGNAL_CHANNEL_INVITE_LINK=''; $env:TELEGRAM_VIP_CHANNEL_ID=''; $env:TELEGRAM_WOLF_BRIEFING_CHANNEL_ID=''; $env:PYTHONDONTWRITEBYTECODE='1'
```

### Full and isolated failure reproduction

```powershell
.\.venv\Scripts\python.exe -m pytest -q -o addopts= --basetemp C:\Users\aspir\AppData\Local\Temp\cci_post_remediation_full_20260718_019f75f5 -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest -q -o addopts= -o cache_dir=C:\Users\aspir\AppData\Local\Temp\cci_post_remediation_isolated_cache --basetemp C:\Users\aspir\AppData\Local\Temp\cci_post_remediation_isolated tests\test_telegram_lifecycle_delivery_phase42.py::test_concurrent_public_watchlist_reservations_allow_one_sender
```

### Focused pytest invocations

All commands below also used `-o addopts=`, a unique external `--basetemp`, and an external `cache_dir`.

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_candle_integrity_phase2.py tests\test_timeframes.py
.\.venv\Scripts\python.exe -m pytest tests\test_strategy_replay.py tests\test_replay_causality_phase2.py tests\test_replay_validation_scaffold.py tests\test_replay_event_sequence_validator.py tests\test_replay_outcome_readiness.py tests\test_replay_dataset_coverage.py tests\test_replay_dataset_export.py tests\test_replay_dataset_quality.py tests\test_replay_failure_taxonomy.py tests\test_replay_research_report.py tests\test_lifecycle_replay_audit.py
.\.venv\Scripts\python.exe -m pytest tests\test_liquidity_grab_pullback.py tests\test_authoritative_minimum_rr.py tests\test_pullback_zones.py tests\test_pullback_intelligence.py tests\test_target_intelligence.py tests\test_setup_quality.py tests\test_risk_manager_agent.py tests\test_opportunity_scoring.py
.\.venv\Scripts\python.exe -m pytest tests\test_scanner_runner.py tests\test_run_scan.py
.\.venv\Scripts\python.exe -m pytest tests\test_lifecycle.py tests\test_lifecycle_eligibility.py tests\test_lifecycle_outcomes.py tests\test_outcome_lifecycle_integration.py tests\test_outcome_event_capture.py tests\test_outcome_capture_contract.py
.\.venv\Scripts\python.exe -m pytest tests\test_symbol_health.py tests\test_watch_mode.py -k "active or lifecycle_priority or adaptive_priority or cooldown"
.\.venv\Scripts\python.exe -m pytest tests\test_telegram_sender_phase42.py tests\test_telegram_lifecycle_delivery_phase42.py tests\test_telegram_outbox_recovery.py tests\test_telegram_signal_channel_routing.py tests\test_telegram_runtime_phase47a.py
.\.venv\Scripts\python.exe -m pytest tests\test_watch_supervisor_reliability.py tests\test_watch_mode.py
.\.venv\Scripts\python.exe -m pytest tests\test_storage_database.py tests\test_sqlite_maintenance.py tests\test_repair_lifecycle_hygiene.py tests\test_scan_persistence_audit.py tests\test_scan_row_visibility_audit.py
.\.venv\Scripts\python.exe -m pytest tests\test_public_alert_funnel.py tests\test_public_alert_funnel_safety.py tests\test_public_signal_quality.py tests\test_signal_readiness.py
.\.venv\Scripts\python.exe -m pytest tests\test_performance_memory.py tests\test_market_regime.py tests\test_regime_intelligence.py tests\test_edge_analytics.py tests\test_research_queries.py
.\.venv\Scripts\python.exe -m pytest -q -o addopts= -o cache_dir=C:\Users\aspir\AppData\Local\Temp\cci_post_remediation_no_order_cache --basetemp C:\Users\aspir\AppData\Local\Temp\cci_post_remediation_no_order tests\test_config.py::test_order_execution_enabled_fails_safely tests\test_authoritative_minimum_rr.py::test_strategy_has_no_order_execution_surface tests\test_telegram_lifecycle_delivery_phase42.py::test_order_execution_not_called_for_public_watchlist tests\test_telegram_lifecycle_delivery_phase42.py::test_order_execution_not_called_for_confirmed_signal tests\test_telegram_lifecycle_delivery_phase42.py::test_public_watchlist_does_not_call_order_execution
```

### Compilation and CLI checks

```powershell
@'
from pathlib import Path
paths = [p for root in ('app', 'scripts', 'tests') for p in Path(root).rglob('*.py')]
for path in paths:
    compile(path.read_text(encoding='utf-8'), str(path), 'exec')
print(f'compiled_python_files={len(paths)}')
'@ | .\.venv\Scripts\python.exe -
.\.venv\Scripts\python.exe scripts\run_scan.py --help
.\.venv\Scripts\python.exe scripts\sqlite_maintenance.py --help
```

### Material read-only evidence queries

```powershell
rg -n "exchange.*(info|metadata)|tick_size|step_size|min_notional|price_precision|quantity_precision|contract" app tests scripts
rg -n -C 18 "REJECTED_BY_REGIME|status_history.*REJECTED_BY_REGIME" app\pipeline\scanner_runner.py tests\test_scanner_runner.py tests\test_regime_intelligence.py
rg -n "def .*public.*quality|def .*quality.*public|PUBLIC_SIGNAL_ALLOWED_TARGET_CAUTION_WARNINGS|target_inside_chop" app\pipeline\scanner_runner.py app\alerts\telegram_lifecycle.py tests\test_telegram_lifecycle_delivery_phase42.py tests\test_public_signal_quality.py tests\test_scanner_runner.py
rg -n "strategy_mode|for .*mode|modes =|modes:|scalp.*swing.*challenge|challenge.*swing.*scalp" app\pipeline\scanner_runner.py app\strategies\liquidity_grab_pullback.py scripts\run_scan.py tests\test_scanner_runner.py
rg -n "UNIQUE.*setup_lifecycle|CREATE UNIQUE INDEX.*lifecycle|UNIQUE \(symbol|symbol, mode, direction|find.*lifecycle|get.*active.*plan|plan_id" app\storage\database.py app\lifecycle app\storage
rg -n "ORDER_EXECUTION_ENABLED|order_execution|create_order|place_order|new_order|futures_create_order|withdraw|transfer" app scripts tests README.md
rg -n "order_execution|create_order|place_order|new_order|futures_create_order|withdraw|transfer" app scripts --glob "*.py"
rg -n "strategy_version|scanner_version|code_version|producer_version|schema_version|data_version|model_version|cohort" app\storage app\lifecycle app\analytics app\pipeline --glob "*.py"
rg -n "ingest_replay_summary|list_outcome_analytics|SetupOutcomeAnalyticsRecord" scripts\run_scan.py app\analytics\performance_memory.py app\lifecycle\service.py
rg -n "class .*Universe|def .*universe|symbols:|quote_volume|market_cap|exchange_info|filters|tick_size|min_notional" app\data app\pipeline scripts\run_scan.py tests\test_symbol_universe.py
rg -n -C 8 "def _public_setup_quality_score_decimal" app\alerts\telegram_lifecycle.py
rg -n -C 10 "UNIQUE\(symbol, mode, direction\)|CREATE UNIQUE INDEX.*setup_lifecycle|setup_lifecycle_records" app\storage\database.py app\lifecycle\repositories.py
rg -n "scan_history|persist.*scan|lifecycle.*phase|telegram.*phase|phase_status|outbox|iteration_status|iteration_phase" scripts\run_scan.py app\watch_iteration.py tests\test_watch_mode.py tests\test_run_scan.py
rg -n "SCHEMA_VERSION|user_version.*15|v15|schema 15|version 15|migration.*rollback|rollback" tests\test_storage_database.py tests\test_sqlite_maintenance.py app\storage\database.py
rg -n "user_version.*14|v14|schema 14|version 14" tests app\storage\database.py
rg -n -C 8 "PRAGMA journal_mode=WAL|journal_mode|busy_timeout|wal_autocheckpoint|def connect_database" app\storage\database.py tests\test_storage_database.py tests\test_telegram_lifecycle_delivery_phase42.py
rg -n -C 8 "test_concurrent_public_watchlist_reservations_allow_one_sender|open_initialized_database" tests\test_telegram_lifecycle_delivery_phase42.py app\alerts\telegram_lifecycle.py app\storage\database.py
rg -n "backup|WAL|delete|prun|retention|secret|production|runtime" docs\RUNTIME_SQLITE_MAINTENANCE.md app\storage\maintenance.py tests\test_sqlite_maintenance.py
rg -n -A 170 "def _public_watchlist_gate_result" app\alerts\telegram_lifecycle.py
rg -n -C 18 "def _public_watchlist_target_caution_actionable|def _public_watchlist_target_caution_failed_gate_blockers|PUBLIC_SIGNAL_TARGET_CAUTION_MIN_RR|target_inside_chop_remains_blocked" app\alerts\telegram_lifecycle.py tests\test_telegram_lifecycle_delivery_phase42.py
rg -n "target_caution|inside_chop.*(allows|eligible|sent)|TARGET_INSIDE_CHOP" tests\test_telegram_lifecycle_delivery_phase42.py tests\test_scanner_runner.py
git blame -L 42,95 app\storage\database.py
git blame -L 4561,4615 tests\test_telegram_lifecycle_delivery_phase42.py
```

## 22. Final GO / CONDITIONAL_GO / NO_GO verdict

**`NO_GO`**

Reasoning against the stated GO criteria:

| Criterion | Status |
|---|---|
| Zero P0 | PASS |
| Zero unresolved/regressed P1 | **FAIL — two new P1 blockers** |
| Full suite green | **FAIL — 1721 passed, 1 failed** |
| Schema migration tests green and sufficient | **FAIL — current tests green, required v14 chain absent** |
| Backup tooling green | PASS in temporary tests |
| No-order invariant green | PASS |
| No live Telegram/Binance activity during audit | PASS within stated evidence limitation |
| Clear remaining P2/P3 list | PASS |

The next authorized step is focused remediation and re-audit, not Runtime migration rehearsal or production deployment.
