# P5A — Active Owner Monitoring Continuity and Immutable Progress Binding

## Authoritative inputs

- `CCI_POST_MIGRATION_RUNTIME_EVIDENCE_AUDIT_2026-09-26.md`
- `CCI_NEXT_DEVELOPMENT_RECOMMENDATIONS_2026-09-26.md`
- Original P5: independent outcome tracking and reconciliation

This phase repairs F01 owner starvation and F02 prospective `plan_version_id` binding. It does not redesign `setup_id` or `plan_version_id` generation.

## Delivery

- Branch: `fix/p5a-active-owner-monitoring-continuity`
- Base SHA: `637e1208d830b3886317b8fd5df6eb7aaa571bee`
- Draft PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/126
- Schema version: remains 26

Commits, in order:

- `a0d45ec6d2787faa337a115f6b32c04696da8769` — implementation. Owner monitoring continuity and prospective `plan_version_id` binding.
- `c891efa016b334a8eceea8059cd91dd903ee5fa0` — documentation only. Pins the implementation SHA above. No production or test code.
- `6046970aecb97deb241d7cf23735801c59e116bf` — implementation repair. Unexpected owner-monitoring failures are reported instead of discarded.
- `9cc9c647efa7182d8beb4d3ff72769e44b939541` — documentation only. Records the repair SHA and an older CI link. No production or test code.
- `69e46d431f4439a6ee4e44e6b1f4558cace01dc4` — documentation only. Separates acceptance evidence from older SHAs. No production or test code.
- `84bc91a6fb7816ae8554fa50d4000792f78165f0` — repairs shared-owner evidence, persistence binding, and candidate-query scope.
- The local-setup failure-visibility repair that contains this wording. It does not name its own hash. The acceptance-review SHA is published in the PR description after that commit is pushed.

This document is not an acceptance verdict. Review of `84bc91a6fb7816ae8554fa50d4000792f78165f0` returned `CHANGES_REQUIRED_LOCAL_SETUP_FAILURE_VISIBILITY`. P5A is not independently approved and is not approved for merge or Runtime rollout.

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
2. `scripts/run_scan.py` calls `_continue_owned_plan_monitoring` after lifecycle on one-shot scans and watch iterations. Discovery candles stay available for every owner of that symbol and timeframe. A successful sibling does not mark the key consumed for an owner whose evaluation failed. The exchange client is created only if a symbol still needs candles.
3. Universe or ranking failure (`UniverseResolutionError` in the exception cause chain) runs that same continuation, then re-raises the discovery failure. A monitoring failure is attached beside the discovery failure.

Empty discovery candle payloads are not treated as coverage. The continuation fetches them before recording a gap. An evaluator exception is not rewritten as `required_closed_candle_unavailable`. First-pass monitoring errors stay on the operational result when the continuation later succeeds or returns no new error.

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
- Reconstructed NULL rows have no marker and stay NULL. A later write that supplies the lifecycle's proven id, or that adds the awaiting marker, does not create provenance. There is no historical backfill and no guess from symbol, direction, or price.
- A new row may keep the proven id only when its plan identity matches the lifecycle economics. A progress identity that describes different economics cannot accept that id.
- An unlocked row that already has the awaiting marker keeps that marker across later pre-lock updates. It binds only after the same lifecycle remints a matching plan version.

## Gap behavior

Existing progress integrity is the gap contract. `record_closed_candle_evidence_gap` writes `INTEGRITY_UNVERIFIED`, restores the cursor, and does not emit TP, SL, INVALIDATED, or EXPIRED.

Gap diagnostics:

- `required_closed_candle_unavailable`
- `exchange_market_data_unavailable:<Exception>`
- `market_unsupported_or_delisted`
- `irrecoverable_cursor_interval:<detail>` for a continuity hole

No new gap table.

Expected market-data failures are exchange-client errors, `TimeoutError`, `ConnectionError`, and messages that identify an invalid, unknown, or delisted symbol. Those stay on the gap contract and do not advance the cursor. `OSError` is not an expected exchange outage: `FileNotFoundError`, `PermissionError`, and other local filesystem or configuration errors are subsystem failures. Client construction is classified separately from candle acquisition. A missing `SSL_CERT_FILE` is reported with `FileNotFoundError` and its message. It is not stored as `exchange_market_data_unavailable`.

## Stale-owner diagnostic

`diagnose_tracking_cursor_lag` reports owner, plan version, last processed close, latest available close, lag seconds, and a gap reason when one is already known. It does not write progress and does not change eligibility. Each monitoring pass includes the same fields in `owner_monitoring.lags`.

## Database and index

`SCHEMA_VERSION` stays 26. `initialize_database` creates:

`ix_lifecycle_records_epoch_locked_plan_state` on `(runtime_epoch_id, current_state, lifecycle_id) WHERE plan_version_id IS NOT NULL`.

The locked candidate branch reads that index with `INDEXED BY`. The unlocked branch starts at non-terminal `setup_lifecycle_outcome_progress` rows and `CROSS JOIN`s the lifecycle primary key, so SQLite cannot reorder the join onto `ix_lifecycle_records_runtime_epoch` and walk historical rejections.

`open_operational_database` does not create indexes. Installing this additive index on an existing schema-26 runtime database is a separate controlled step: `migrate_existing_database` (or another explicit `initialize_database`). That call uses `CREATE INDEX IF NOT EXISTS` and does not rewrite historical rows. Schema version stays 26. Until that step runs, the locked branch cannot execute because it names the index.

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

`tests/test_p5a_monitoring_binding_query_repair.py` covers the review counterexamples against the real monitoring layer:

- shared symbol and timeframe, one owner succeeds and another raises, without a false candle gap
- a first-pass failure remains visible when the continuation succeeds
- real `run_scan.main` one-shot and watch paths for that shared-owner failure
- unexpected client construction and fetch/adapter exceptions
- ranking failure together with monitoring success, an expected gap, and an unexpected failure
- an owned symbol excluded from strict discovery membership is still monitored
- legacy NULL rejects a supplied proven id and a rewritten marker
- mismatched economics cannot take the record's proven id
- valid creation, pre-lock bind-forward, immutable retry, and foreign-lifecycle rejection
- candidate query plan and work with 5,000 historical rejections
- concurrent discovery/monitor, bind retry, and terminal retry under `BEGIN IMMEDIATE`

`tests/test_p5a_local_setup_failure_visibility.py` uses the real exchange-client constructor with a nonexistent temporary `SSL_CERT_FILE`:

- continuation reports `owner_monitoring=PARTIAL` and does not persist an exchange gap
- one-shot `run_scan.main` exits with an `owner_monitoring` diagnostic that includes `FileNotFoundError`
- watch reports `owner_monitoring=PARTIAL` and keeps that setup error
- ranking failure plus the same setup failure keeps both diagnostics

`tests/test_p5a_monitoring_failure_visibility.py` covers the silent-exception repair:

- ranking failure with successful monitoring keeps only the discovery error
- ranking failure plus an unexpected monitoring exception keeps both
- startup `SystemExit` keeps the discovery text and appends the monitoring line
- a normal watch iteration records `owner_monitoring=PARTIAL` and a recoverable error
- a successful monitoring pass does not create an error
- one-shot monitoring failure raises `SystemExit` and is not discarded
- `scripts/run_scan.py` no longer contains `except Exception: pass` around owner monitoring

## Evidence by SHA

These runs belong only to the SHA named on each line. They are not evidence for a later HEAD.

- Base `637e1208d830b3886317b8fd5df6eb7aaa571bee`: `python -m pytest`, exit 0, about 1221 seconds, Python 3.11.9, one pre-existing Starlette deprecation warning. This was the unmodified main baseline.
- Sources of `a0d45ec6d2787faa337a115f6b32c04696da8769` / `c891efa016b334a8eceea8059cd91dd903ee5fa0`: `python -m pytest`, exit 0, 475.7 seconds, 2724 passed, 0 failed, 0 skipped, the same warning. `c891efa` changes only this handoff's implementation-SHA line relative to `a0d45ec`. CI for `c891efa`: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/36268114059
- Sources of `6046970aecb97deb241d7cf23735801c59e116bf`: `python -m pytest`, exit 0, 465.4 seconds, 2731 passed, 0 failed, 0 skipped, the same warning. CI for that exact commit: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/36300087110
- `9cc9c647efa7182d8beb4d3ff72769e44b939541`: documentation-only child of `6046970`. CI succeeded: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/36300673563. Pytest was not re-executed on this SHA. Do not treat the 2731 count as a run of `9cc9c64`.
- `69e46d431f4439a6ee4e44e6b1f4558cace01dc4`: documentation-only child of `9cc9c64`. CI succeeded: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/36302011988. Independent review of that SHA reproduced the four blockers. Its green tests did not cover those failures. Do not treat the 2731 count as evidence that the blockers were absent.
- `84bc91a6fb7816ae8554fa50d4000792f78165f0`: shared-owner, binding, and query-scope repair. Local pytest on that tree: exit 0, 2750 passed, 0 failed, 0 skipped, 438.5 seconds, one Starlette warning. CI: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/36304676453. Re-review of that SHA returned `CHANGES_REQUIRED_LOCAL_SETUP_FAILURE_VISIBILITY` because `OSError` classified a missing local CA file as an exchange gap. Do not treat the 2750 count as evidence that this setup failure was visible.

Pytest, `compileall`, and `git diff --check` for the local-setup repair HEAD are recorded in the PR description after that commit is pushed. This file does not copy those results forward and does not name that commit.

No separate lint, typecheck, or formatter is configured. CI runs `compileall` and `pytest`.

## Strategy non-regression

Discovery classification, rejection, confirmation, quality, and RR gates were not edited. The public constants remain quality 88, grade A, and RR 3. Monitoring can advance an already-owned plan. It does not admit a new setup.

## Public delivery non-regression

No Telegram formatter, risk-warning, or delivery-rule change. A confirmed public owner continues closed-candle evaluation because it is an outcome-eligible latched plan.

## Failure observability

Unexpected monitoring failures are not discarded. Expected per-symbol market-data problems stay on the existing gap contract and do not use this path.

- Normal watch iteration: `owner_monitoring=PARTIAL` plus a `recoverable_errors` line `owner_monitoring:<type>:<detail>`. A clean pass, including persisted candle gaps, stays `SUCCESS`.
- Ranking or universe failure: discovery still fails closed and remains the iteration error. Monitoring is still attempted. If it also fails, the failed-iteration summary keeps the discovery error and adds `phase_statuses.owner_monitoring=PARTIAL` plus the monitoring error. Startup `SystemExit` text keeps the original discovery message and appends the monitoring line.
- One-shot: an unexpected monitoring failure raises `SystemExit` with the `owner_monitoring:` diagnostic. The command does not return a successful scan report over that failure. The diagnostic includes a first-pass failure even when the continuation itself succeeds.
- Watch: the same aggregated failure sets `owner_monitoring=PARTIAL` and keeps the original iteration error when discovery also failed.

## Known limitations

- Independent architecture acceptance has not happened. Review of `84bc91a` required local client-setup failures to stay visible. This change is that repair, not an approval.
- The locked candidate query names `ix_lifecycle_records_epoch_locked_plan_state`. A schema-26 runtime file does not gain that index until an explicit `migrate_existing_database` / `initialize_database` step. The operational opener does not run it.
- `last_seen_at` stays a discovery timestamp. Freshness for an owned plan is the outcome cursor and the lag diagnostic.
- Historical production NULL progress is not backfilled.
- Gap state lives on the existing progress integrity fields. There is no separate gap table.
- Unexpected one-shot monitoring failures raise `SystemExit` with an `owner_monitoring:` diagnostic. They are not swallowed. Exchange timeouts and unsupported or delisted markets stay on the progress gap contract and do not use that exit.

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
