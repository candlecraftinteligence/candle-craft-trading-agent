# P5A — Active Owner Monitoring Continuity and Immutable Progress Binding

## Authoritative inputs

- `CCI_POST_MIGRATION_RUNTIME_EVIDENCE_AUDIT_2026-09-26.md`
- `CCI_NEXT_DEVELOPMENT_RECOMMENDATIONS_2026-09-26.md`
- Original P5: independent outcome tracking and reconciliation

This phase repairs F01 owner starvation and F02 prospective `plan_version_id` binding. It does not redesign `setup_id` or `plan_version_id` generation.

## Delivery

- Branch: `fix/p5a-active-owner-monitoring-continuity`
- Base SHA: `637e1208d830b3886317b8fd5df6eb7aaa571bee`
- Implementation commit: `a0d45ec6d2787faa337a115f6b32c04696da8769`
- Schema version: remains 26

## Root cause

Outcome evaluation ran only for the lifecycle owner selected by the newest observation's symbol, mode, and direction (`is_current = 1`). A later same-symbol result such as CAKE scalp `direction=N/A` REJECTED updated that rejection owner. The pre-existing entered directional plan received no further closed-candle evaluation.

A second defect left prospective progress unbound: `upsert_outcome_progress` inserted `plan_version_id` but `ON CONFLICT` never filled a NULL row after the same lifecycle locked its plan. 47/48 audited progress rows were NULL. Setup and plan-version generation themselves recomputed correctly and were not changed.

## Previous behavior

- Discovery selection and outcome monitoring were the same step.
- A symbol dropping out of the Top-100 discovery universe, or a ranking-provider failure, stopped new discovery and also stopped owned-plan evaluation.
- Noncurrent was treated as "do not evaluate" for a rotated generation.
- Pre-lock progress stayed NULL after the plan locked.
- Legacy NULL rows had no provenance marker, so they could not be distinguished from prospective rows.

## Corrected behavior

Discovery still answers whether a new setup is admitted. Monitoring answers what happened to plans CCI already owns. Both stay in the scanner process.

After each lifecycle apply, every open tracking obligation is evaluated from candles the scan already fetched. Obligations the scan did not cover are fetched from the exchange only when lifecycle tracking is enabled. Ranking failure still fails discovery closed, then monitoring continues when exchange candles are available.

A rejected observation stays rejected. Monitoring does not create setups.

## Monitoring-owner definition

An owner has a tracking obligation when all of the following hold:

- the row belongs to the active runtime epoch
- `current_state` is in the outcome policy's eligible states (`WATCHLISTED`, `STALKING`, `TRIGGERED`, `CONFIRMED`, `ACTIONABLE_A_GRADE`, `A_GRADE_WATCH`, `EXECUTING`, `MANAGING`)
- the plan is latched (`plan_version_id` is set) or the lifecycle already has non-terminal outcome progress
- compatible plan progress is not already terminal

`is_current` is not a stop. `REJECTED`, `COOLDOWN`, and terminal states are not obligations. `TRIGGERED` without a latched plan and without progress is still discovery's job.

## Schedule and path

1. `SetupLifecycleService.apply_to_run_result` evaluates discovery, then `monitor_tracking_obligations` inside the same `BEGIN IMMEDIATE` transaction. Missing symbols are left uncovered so the caller can fetch them. A gap is not written for a symbol the scan simply did not include.
2. `scripts/run_scan.py` calls `_continue_owned_plan_monitoring` after lifecycle on one-shot scans and watch iterations. The exchange client is created only if a symbol still needs candles.
3. Universe or ranking failure (`UniverseResolutionError` in the exception cause chain) runs that same continuation, then re-raises the discovery failure.

Empty discovery candle payloads are not treated as coverage. The continuation fetches them before recording a gap.

## Universe independence

An owned plan is not forced back into the new-opportunity universe. If its symbol leaves the ranking intersection and exchange klines are still available, tracking continues through the fetch path.

## Ranking failure

Ranking-provider failure fails new discovery closed. Existing obligations are still evaluated when `get_klines` succeeds. No prices are synthesized.

## Cursor semantics

Unchanged evaluation policy:

- closed candles only
- cursor advances monotonically
- the same candle does not create a second economic event
- several unseen candles catch up
- a continuity hole or stale window sets `INTEGRITY_UNVERIFIED` and does not skip ahead
- a terminal plan stops further economic progression

## Restart semantics

The cursor is the persisted `evaluation_cursor_open_at` / `evaluation_cursor_close_at`. A later process resumes after that cursor. Repeating the same window is idempotent.

## Plan-version binding

- A new progress row stores `plan_version_id` when `proven_progress_plan_version_id` remints it.
- A row created while the plan is unlocked is marked `plan_version_binding=awaiting_proven_plan_version`.
- When that same lifecycle later remints the locked id and the plan identity still matches, the row binds forward.
- A stored id is immutable. A different id, a different lifecycle, changed economics, geometry that does not remint, or missing provenance is rejected.
- Reconstructed NULL rows have no marker and stay NULL. There is no historical backfill and no guess from symbol, direction, or price.

## Gap behavior

Existing progress integrity is the gap contract. `record_closed_candle_evidence_gap` writes `INTEGRITY_UNVERIFIED`, restores the cursor, and does not emit TP, SL, INVALIDATED, or EXPIRED.

Gap diagnostics:

- `required_closed_candle_unavailable`
- `exchange_market_data_unavailable:<Exception>`
- `market_unsupported_or_delisted`
- `irrecoverable_cursor_interval:<detail>` for a continuity hole

No new gap table.

## Stale-owner diagnostic

`diagnose_tracking_cursor_lag` reports owner, plan version, last processed close, latest available close, lag seconds, and a gap reason when one is already known. It does not write progress and does not change eligibility. Each monitoring pass includes the same fields in `owner_monitoring.lags`.

## Database and index

`SCHEMA_VERSION` stays 26. `initialize_database` creates:

`ix_lifecycle_records_epoch_locked_plan_state` on `(runtime_epoch_id, current_state, lifecycle_id) WHERE plan_version_id IS NOT NULL`.

The monitoring query reads latched plans through that index, plus progress rows joined to unlocked lifecycles that already have non-terminal progress. It does not scan historical rejections.

## Tests

Adversarial coverage is `tests/test_p5a_active_owner_monitoring.py`:

- CAKE-like REJECTED `direction=N/A` cannot starve an entered owner
- INJ-like opposite-direction discovery still advances the owner
- different-mode discovery cannot starve the owner
- scalp and swing obligations are both monitored
- noncurrent unresolved owner continues
- symbol leaving the discovery universe still tracks
- ranking-provider failure still monitors when market data exists
- exchange failure and unsupported market are explicit gaps and are not terminal
- restart, repeated candle, catch-up, out-of-order invocation, continuity hole
- entry, TP, and SL idempotency
- pre-lock bind-forward, wrong plan version, changed economics, different lifecycle
- legacy NULL cannot be guessed
- public confirmed owner keeps receiving evaluation
- two monitoring passes do not duplicate events
- cursor-lag diagnostic does not change eligibility
- rejected, cooldown, and unlocked TRIGGERED rows are not obligations
- public quality 88, grade A, and public RR 3 stay unchanged
- the locked-plan index is created on an existing schema-26 database without a version bump
- empty discovery candles still fetch

`tests/test_outcome_plan_attribution_p3b1.py` now expects same-lifecycle bind-forward. Reconstructed unbound progress stays NULL.

## Full regression

- Baseline on unmodified `637e1208`: `python -m pytest`, exit 0, about 1221 seconds, Python 3.11.9, one pre-existing Starlette deprecation warning.
- This branch: `python -m pytest`, exit 0, 475.7 seconds, 2724 passed, 0 failed, 0 skipped, the same Starlette warning.
- `python -m compileall -q app scripts src tests`: exit 0.
- `git diff --check`: clean.
- No separate lint, typecheck, or formatter is configured. CI runs `compileall` and `pytest`.

## Strategy non-regression

Discovery classification, rejection, confirmation, quality, and RR gates were not edited. The public constants remain quality 88, grade A, and RR 3. Monitoring can advance an already-owned plan. It does not admit a new setup.

## Public delivery non-regression

No Telegram formatter, risk-warning, or delivery-rule change. A confirmed public owner continues closed-candle evaluation because it is an outcome-eligible latched plan.

## Known limitations

- One-shot continuation swallows an unexpected monitoring exception so the scan report still returns. Watch mode records `owner_monitoring=PARTIAL`. Evidence gaps themselves are persisted.
- `last_seen_at` stays a discovery timestamp. Freshness for an owned plan is the outcome cursor and the lag diagnostic.
- Historical production NULL progress is not backfilled.
- Gap state lives on the existing progress integrity fields. There is no separate gap table.

## Deferred

- F03 research-loader isolation
- F04 durable source replay
- F05 remains discovery fail-closed only; owned-plan monitoring during a ranking outage is in this phase
- F06 disk
- F08 Telegram risk-warning text
- paper execution, fees, slippage, and funding simulation

## Safety

Runtime database not opened. Runtime scanner not started. Runtime Telegram listener not started. No Telegram message sent. No exchange order executed. No secrets added.

`RUNTIME_DB_TOUCHED = FALSE`
`RUNTIME_DEPLOYED = FALSE`
`RUNTIME_RESTARTED = FALSE`
`TELEGRAM_SENT = FALSE`
`ORDER_EXECUTION = FALSE`
`STRATEGY_GATES_CHANGED = FALSE`
`AUTO_MERGE = FALSE`

## Rollback

Revert the branch. The partial index is `CREATE INDEX IF NOT EXISTS` and is unused if the monitoring query is removed. No production rows are rewritten by a migration.
