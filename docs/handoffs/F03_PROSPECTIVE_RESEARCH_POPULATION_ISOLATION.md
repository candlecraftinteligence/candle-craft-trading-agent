# F03 — Prospective Research Population Isolation

## Phase

F03_PROSPECTIVE_RESEARCH_POPULATION_ISOLATION

This phase repairs the research-query denominator. It does not change scanner discovery, lifecycle transitions, owner monitoring, Telegram delivery, quality gates, or RR gates. It does not implement F04 source replay and it does not prove expectancy.

## Delivery

- Branch: `fix/f03-prospective-research-isolation`
- Base SHA: `44777a3b4f53919875b66940f231ea542ca38bd4` (merged P5A, `origin/main` at the start of this phase)
- Implementation SHA: `557e992c25e3e11d9d209fc61cd04971ccecb35b`
- Handoff SHA that CI already passed: `cae19080a96c3fefeb08a9176922f1b940c11c1d`
- Draft PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/127
- Schema version: remains 26
- Runtime database: not opened, copied, or modified

Verified: local `main` was fast-forwarded from `637e1208d830b3886317b8fd5df6eb7aaa571bee` to the expected P5A SHA before the branch was created. The eight commits in between were the already-merged P5A owner-monitoring release. They were not rewritten.

## Origin-text repair

Reviewed head `776da0f9c0715853a19909d2183bc6bcd36870d0` treated stored `N/A` as a blank origin. `app/research/queries.py::_origin_text` returned None when the stripped text was empty or `text.upper() == "N/A"`. `_split_lifecycle_lineage` then admitted the row on the epoch column alone. Case and surrounding-whitespace variants took the same path, so a missing `N/A` origin and an `N/A` origin owned by another epoch both entered the prospective denominator.

The direct-epoch exception is now SQL NULL or whitespace-empty text only. Surrounding whitespace is stripped before lookup, and the remaining text is compared to `runtime_operational_origins.origin_id` exactly. A missing id and an id owned by another epoch are unresolved. Their events stay out of conversion numerators and denominators. `unresolved_within_requested_epoch` counts those filtered rows. Schema 26, historical default, public quality 88, grade A, and RR 3 are unchanged. No live database was opened. Runtime behavior, order execution, and F04 were not changed.

## Root cause

Verified from `app/research/queries.py` on the base SHA: `_load_research_data` selected `scan_runs`, `symbol_results`, `setup_candidates`, `replay_results`, `setup_lifecycle_records`, `setup_lifecycle_events`, and `symbol_health` without a Runtime epoch predicate. Symbol, mode, and regime filters ran afterward in Python. Those filters do not identify an epoch.

Legacy rows (`runtime_epoch_id` NULL, or scan runs absent from `runtime_operational_runs`) and rows from other prospective epochs can share symbol, mode, direction, regime, timestamp, and plan geometry. The unscoped loader therefore built one analytical population from all of them.

## Old research behavior

The only production caller is `scripts/run_scan.py` `--research`. It called `build_research_report` with symbol, mode, regime, limit, and stale-hours filters. There was no population label. A summary, quality cohort, lifecycle conversion, or replay expectancy figure was whatever those filters left in the whole database.

That command was not named prospective. Treating its output as a prospective Runtime cohort was the integrity failure.

## New research population contract

Two explicit scopes:

| Scope | Meaning |
| --- | --- |
| `HISTORICAL_MIXED_NON_PROSPECTIVE` | The previous unscoped read. It may contain legacy and every epoch. It cannot be labelled prospective. |
| `PROSPECTIVE_EPOCH_SCOPED_RESEARCH` | Rows proven to belong to one resolved Runtime epoch. |

A prospective request must name an epoch id, or set explicit active-epoch resolution. Active resolution reads `runtime_epoch_control` / `runtime_epochs` and prints the resolved id. It does not select the latest timestamp or the latest lifecycle.

If the epoch does not exist, no active epoch is configured, or the request is ambiguous, `ResearchPopulationError` is raised. There is no fallback to all rows. `fallback_to_all_history` is false on every report.

Existing `--research` stays historical so stored commands keep their previous counts. The text and JSON now say `HISTORICAL_MIXED_NON_PROSPECTIVE` and `prospective_claim=false`. Prospective research is `--research-population prospective` with `--research-epoch`, or without an epoch id when active resolution is intended.

This default is an engineering choice from the existing command semantics. Changing the default to the active epoch would change historical result counts. That was not required to stop a prospective claim, because the old command never made one.

## Lineage rules by entity

Membership is not timestamp, symbol, mode, direction, `is_current`, or price geometry.

| Entity | Prospective membership |
| --- | --- |
| `scan_runs` | `run_id` is present in `runtime_operational_runs` for the resolved epoch. |
| `symbol_results` | Same run registration. Loaded with `run_id IN (...)` against `ix_symbol_results_run_id`. |
| `setup_candidates` | Same run registration. The symbol join is `run_id` plus `symbol` on that run, not symbol alone. |
| `replay_results` | Same run registration. |
| `setup_lifecycle_records` | `runtime_epoch_id` equals the resolved epoch. SQL NULL and whitespace-empty `creation_origin_id` keep that direct membership. Every other stored origin, including `N/A` and case or surrounding-whitespace variants, must exist in `runtime_operational_origins` for the same epoch. A missing or other-epoch origin is excluded, and its events are excluded. A NULL `runtime_epoch_id` is not admitted through an origin id. |
| `setup_lifecycle_events` | Joined only through an admitted lifecycle id. `scan_run_id` does not move an event into another epoch. |
| `symbol_health` | No epoch or run lineage. Excluded from prospective research. Still returned for explicit historical research. Operational symbol-health writes were not changed. |

`setup_lifecycle_outcome_progress` and `setup_outcome_analytics` are not read by this research loader. Lifecycle outcome rates in these reports come from the epoch-scoped lifecycle records and events.

## Callers reviewed

- `build_research_report` — attaches `population` and `fallback_to_all_history` after every query, including queries that return an error payload.
- `scripts/run_scan.py` `_handle_research_command` — the only production caller. Scope is passed from the CLI into the loader.
- Text formatting in `app/research/reports.py` — prepends the population banner.
- Direct callers in tests keep the historical default.

## Prospective scope semantics

Resolved metadata includes scope type, requested id (`ACTIVE` when active resolution was requested), resolved id, epoch cutoff as information rather than a membership rule, included entity counts, and exclusion flags. `expectancy_claim` is `prospective_population_only` only for this scope. Sample-size warnings are unchanged. This phase does not call a prospective expectancy trustworthy.

## Historical scope semantics

`expectancy_claim` is `not_a_prospective_claim`. Legacy rows are not deleted, rewritten, or backfilled. `legacy_excluded` is false because the caller asked for the mixed population.

## Unresolved-lineage behavior

Rows that carry the requested epoch id but whose `creation_origin_id` points at a missing origin, or at another epoch, are omitted. The omission count is `unresolved_within_requested_epoch`, limited to those candidate rows rather than a full-database census. Stored text is an origin id after surrounding whitespace is removed. Only SQL NULL and whitespace-empty text skip that lookup. `N/A`, `n/a`, and ` N/A ` do not.

A symbol-bounded lifecycle census, when `--research-symbol` is set, also reports `legacy_or_null_epoch` and `different_epoch` for that symbol. Without a symbol, those two counts are not enumerated. Scan-entity exclusion totals are not enumerated, because `symbol_results` has no symbol index and a census would scan history. The prospective read itself still starts from registered run ids.

## Denominator protection

Quality grades, regime expectancy, replay expectancy, and lifecycle conversion are computed from the same filtered `ResearchData`. Tests on one mixed database show epoch A confirmation and TP rates using only epoch A lifecycles, and epoch A expectancy using only the epoch A replay. Legacy and epoch B outcomes stay in the historical report and out of the prospective one.

## Performance / query plan

Verified on a synthetic schema-26 database: the prospective symbol query plan uses `ix_symbol_results_run_id` and does not scan `symbol_results`. The lifecycle and lifecycle-event plans use `ix_lifecycle_records_runtime_epoch`. Adding 2,500 unregistered symbol rows and 2,500 NULL-epoch lifecycle rows left the epoch A summary and lifecycle count unchanged. The historical summary count increased by those 2,500 symbol rows, which shows the rows were stored and were not silently dropped from historical research.

No schema version bump and no new index. Existing lineage indexes were sufficient.

## Tests added

`tests/test_f03_prospective_research_population.py`

Covers mixed legacy plus two prospective epochs with shared symbol, mode, direction, regime, timestamp, and geometry; epoch A versus epoch B; NULL epoch and stolen-origin exclusion; timestamp-after-cutoff without registration; conflicting and missing origins; cross-wired `scan_run_id`; non-current epoch rows kept; historical labelling; empty epoch; unknown epoch; missing active epoch; explicit epoch without an active control row; symbol-health exclusion; run-id chunking; every `RESEARCH_QUERIES` entry; CLI default versus explicit prospective; query plan; legacy growth; strategy constants 88 / A / 3; schema 26.

The origin-text repair adds two tests. `test_na_origin_text_cannot_bypass_lineage` injects epoch A `TP_HIT` lifecycles whose origins are missing `N/A`, surrounding whitespace around `N/A`, and `n/a` / ` n/a ` while `n/a` is owned by epoch B. After `N/A` itself is registered to epoch B, those rows stay out. `build_research_report` and `scripts/run_scan.py --research --research-population prospective --research-epoch` both keep epoch A at 3 lifecycles, watchlisted 2, confirmed 1, TP rate 0, and 4 events. Unresolved within the epoch rises from 2 to 6. Epoch B stays at 1 lifecycle and a 100 TP rate. The historical summary sees all 14 lifecycle rows, so the injections were stored. `test_blank_and_matching_origins_still_admit` keeps SQL NULL, `""`, and whitespace-only origins, and admits `N/A` only when that origin id belongs to the requested epoch. A case-different `n/a` with no matching origin stays out and does not change the TP rate or the watchlisted-to-valid denominator.

## Focused test results

Verified before the origin-text repair: `python -m pytest tests/test_f03_prospective_research_population.py tests/test_research_queries.py tests/test_symbol_health.py` — 59 passed.

Verified after the origin-text repair: the same command — 61 passed in 8.99s.

## Full test results

- Baseline, before edits, at `44777a3b4f53919875b66940f231ea542ca38bd4`: `python -m pytest` — 2754 passed, 1 existing Starlette deprecation warning, 766.16s. `python -m compileall -q app scripts src tests` — exit 0.
- After the first implementation: `python -m pytest` — 2770 passed, the same warning, 556.45s. `compileall` — exit 0. `git diff --check` — clean.
- After the origin-text repair: `python -m pytest` — 2772 passed, the same warning, 557.20s. Collection count was 2772. `compileall` — exit 0. `git diff --check` — clean.

The F03 module now has 18 tests. No assertion was weakened and no test was marked xfail.

## CI result

Verified before this repair: GitHub Actions run [36852376342](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/36852376342) passed `Python 3.11 tests` on reviewed head `776da0f9c0715853a19909d2183bc6bcd36870d0`. Earlier run [36851959852](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/36851959852) passed on `cae19080a96c3fefeb08a9176922f1b940c11c1d`. The pull request check on the commit that introduces the origin-text repair is the CI result for the branch head.

## Strategy non-regression

Verified constants are unchanged: `MIN_PUBLIC_SETUP_QUALITY_SCORE` 88, `MIN_PUBLIC_SIGNAL_GRADE` A, `PUBLIC_SIGNAL_MIN_RR` 3, `PUBLIC_WATCHLIST_MIN_RR` 3. No strategy module was edited.

## Runtime non-regression

No edits to scanner discovery, lifecycle transition rules, owner monitoring, Telegram delivery, or order execution. Research isolation is a read-path contract. P5A monitoring tests remained in the full suite and passed.

## Known limitations

- `symbol_health` is an operational aggregate with one row per symbol. Prospective research excludes it instead of guessing an epoch. Historical symbol-health reports still work and are labelled non-prospective.
- Scan-table exclusion totals are not counted for the whole history. Inclusion is proven by the registered-run predicate.
- A lifecycle row with the requested `runtime_epoch_id` and a SQL NULL or whitespace-empty origin is included. The epoch column is the direct membership evidence. Any other stored origin is validated against `runtime_operational_origins`. `N/A` is not a blank origin.
- Timestamp is not used to admit or reject a row when an epoch lineage chain exists.
- Explicit historical mode still reads the full research tables. That is the labelled mixed contract, not a prospective fallback.
- Replay expectancy remains subject to the existing small-sample warning. A prospective label is not a claim of edge.

## Deferred F04

Exact decision replay and source evidence remain incomplete. A clean prospective denominator is not causal source replay. F04 is not implemented here.

## Next architectural decision

None is required to merge this research-query contract.

A later product choice, not a blocker: whether operators want `--research` to default to the active Runtime epoch. The compatible behavior shipped here is historical-by-default, prospective-by-flag, with both labels visible.
