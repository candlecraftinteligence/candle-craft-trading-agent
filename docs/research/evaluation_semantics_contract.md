# CCI EVAL_SEMANTICS — Executable Runtime/Replay Evaluation Contract and Differential Proof

**Status:** Research contract + synthetic paired proofs. No production Python change.
**Does not** persist admissions, mint episode/trade/policy IDs, bind source/policy, add a tracker, authorize replay as runtime outcome authority, or compute expectancy.
**Audited base commit:** `1ff5b8db984dbb0b196e02c2cb702af52c9efa42` (`main`, merge of PR #114 / R0).
**Schema:** application `SCHEMA_VERSION` / SQLite `PRAGMA user_version` = **24** unchanged. Fresh synthetic `PRAGMA schema_version` = 46 (SQLite catalog revision, not the application version). No admission, episode, policy-hash, or source-namespace columns exist.

Fixtures in `tests/test_evaluation_semantics_contract.py` are labeled synthetic. No live or existing scan database was read.

This phase is a finite prerequisite to original-audit P5/P6. It specifies **what current runtime and replay evaluators actually mean**, the conditions under which their outputs may be compared, and the missing authority that still blocks prospective admitted research episodes.

## 1. Purpose and original P5 / P6 dependency

Original P5 required continuing evaluation of admitted open plans, or explicit gap evidence, then reconciling mature covered unambiguous outcomes without join fan-out.

Original P6 required capturing causal inputs and reproducing decisions/outcomes under explicit entry/exit assumptions, separating exact-as-run evidence from reconstructed simulations.

R0 defined the future accounting unit and proved that current keys do not recover it. This phase does not implement that unit. It answers the next finite question:

**When two evaluations see the same declared prices and candle path, which output facts are comparable, which differ because implemented policy differs, and which are not comparable because required timing, source, coverage, or economics are unavailable?**

Engine disagreement is evidence. This phase does not change runtime or replay to produce matching labels.

## 2. Verified base, schema, and evidence scope

| Fact | Evidence |
| --- | --- |
| Fresh `origin/main` | `1ff5b8db984dbb0b196e02c2cb702af52c9efa42` |
| PR #114 | Merged; implementation head `4586c5b9865e1ebd56ec93cadf40ee8ffe512bce` is an ancestor |
| Commits after ASTRA's reviewed SHA | None |
| Schema | v24; progress `UNIQUE(lifecycle_id, plan_identity)`; analytics `UNIQUE(lifecycle_id, final_outcome)` |
| Absent | `admission_id`, `evaluation_episode_id`, `evaluation_policy_id`, `policy_hash`, durable `source_namespace` |
| Local baseline | `C:\CandleCraftDev\.venv\Scripts\python.exe` 3.11.9; `python -m pytest -q` at the SHA above → **2380 passed**, 1 Starlette/httpx warning, ~180s |
| CI at reviewed SHA | Actions run 34244049908; job 102121327911 checked out `1ff5b8db984dbb0b196e02c2cb702af52c9efa42`; **2380 passed**, 2 warnings |

Evidence labels used in tests (not application enums):

- **normal production path reached with synthetic inputs**
- **direct evaluator / import-supported supplied state**
- **malformed / adversarial supplied state**
- **source inspection only, not executed here**

Comparison classes are test/report concepts only. They are not production enums and are not persisted:

- **comparable_for_named_dimensions** — named facts under declared common assumptions
- **non_equivalent_implemented_policy_differs** — both evaluators ran; policy produces different meaning
- **not_comparable_missing_or_conflicting_evidence** — required cutoff, coverage, source, or episode facts are absent/conflicting

A supplied `SetupLifecycleRecord` / `ReplaySetupCandidate` proves the evaluator that received it, not scanner discovery or admission. `StrategyReplayEngine.run` discovery geometry is not the same opportunity as the constructed runtime plan used in paired tests. True same-opportunity producer parity is **not established**.

## 3. Runtime / replay semantic matrix

Declared common fixture economics unless a test says otherwise: zone `[100, 102]`, replay point `101`, long stop `90` / short stop `112`, TP1/TP2/TP3 as in `_levels`, 5m closed candles, confirmation exactly at candle-0 open, replay `detected_at_index=0` so fill search starts at index 1, runtime `decision_timestamp` = close of the last supplied bar. Replay `ReplayConfig.same_candle_policy` defaults to `"conservative"`.

| Dimension | Runtime lifecycle evaluation | Existing strategy replay | Evidence grade | Durable availability | Binding implication |
| --- | --- | --- | --- | --- | --- |
| Entry predicate | Zone overlap: `high >= entry_low and low <= entry_high` (`entry_touched` in `app/lifecycle/outcome_policy.py`) | Point touch: `low <= entry <= high` (`_price_touched` in `app/backtesting/strategy_replay.py`) | Executed: `test_zone_overlap_without_point_touch_is_non_equivalent` | Runtime zone is stored on the lifecycle record. Replay `entry` is a candidate field; `entry_low`/`entry_high` exist on the candidate but are **not** used by `_simulate_trade`. | Future policy must name zone vs point. Matching prices on a fixture is not enough. |
| Start / fill search | `_entry_tracking_boundary` then `_first_fully_post_boundary_open`; first fully post-boundary closed bar is eligible (`outcomes.py`) | Fill loop is `range(detected_at_index + 1, max_fill_index + 1)` (`_simulate_trade`) | Executed: `test_causal_boundary_exact_partial_later_processing_and_as_of_mutation` | Runtime confirmation/`first_evaluated_at` can persist; original decision clock is lost after the pass (P3B2A). Replay detection index is in-memory / `replay_results`, not bound to a lifecycle row. | A shared candle list does not imply a shared start. |
| Exact-open vs partial bar | Confirmation exactly at candle open → that bar is entry-eligible. Confirmation inside a bar → that bar is not entry-eligible (`partially_overlapping_boundary`). Exact close of bar N is the open of bar N+1. | Detection candle is never the fill candle. Partial vs exact confirmation is not a replay fill rule. | Executed in the causal-boundary test | Runtime metadata `boundary_candle_eligibility_decision` on the progress row. Not a complete history. | Exact-open, exact-close, and partial overlap must be bound separately. |
| Cutoff / knowledge boundary | `closed_candles_as_of(..., close <= decision_timestamp)` (`app/data/candle_integrity.py`). Later `evaluated_at` does not admit later closes (`test_later_processing_clock_does_not_admit_future_candles` reused conceptually; paired in this module). | Detection uses a closed prefix (`StrategyReplayEngine.replay_symbol` via `_prefix_by_timeframe`). `_simulate_trade` then receives the **full** execution series, not `decision_timestamp`. | Executed: causal-boundary test; `test_replay_engine_looks_ahead_on_execution_series_after_causal_detection` | Runtime cutoff is an in-memory argument; last applied cutoff is P3B2B last-pass only. Replay has no durable as-of on the trade row. | Comparable as-of fill requires slicing replay input to the same closed window. Identical lists are not a shared cutoff. |
| Execution timeframe | `execution_timeframe` must match the progress row; mismatch is integrity failure. Continuity required. | `ReplayConfig.execution_timeframe` (default `"15m"`). `_normalize_candles` validates continuity. | Runtime empty-list path executed (`test_ineligible_and_missing_data_do_not_invent_comparable_outcomes`). Timeframe mismatch covered by existing `tests/test_lifecycle_outcomes.py`. | Runtime persists timeframe on progress. Replay config is caller-chosen. | Bind timeframe. Do not infer it from a filename. |
| Price basis | Evaluators consume OHLC already on the candle object. Ordinary runtime scanner for `exchange=binance` uses `BinanceFuturesClient.get_klines` → `/fapi/v1/klines` (`scanner_runner._exchange_client_for`, `binance_futures.py`). Normalization retains `exchange`, `symbol`, `interval`, `raw_source` (`normalize_binance_klines`). | Same OHLC fields after `_normalize_candles`. No exchange fetch inside `_simulate_trade`. | Source inspection for adapter selection; executed fixtures use synthetic dicts | An endpoint in source does not prove the provenance of every imported candle or historical progress row. `lifecycle_execution_candles` and `lifecycle_decision_timestamp` are `exclude=True` on `ScannerSymbolResult`. | Receipt/availability time is unavailable. Do not mint it. |
| Entry-candle targets | After fill (and optional same-candle stop), targets begin with the **next** closed bar. Comment and `continue` in `evaluate_closed_candle_outcomes`. | Exit/target loop starts at `fill_index`, so the fill candle can credit targets. | Executed: `test_entry_candle_target_is_credited_by_replay_not_runtime` | Runtime: no `tp*_at` on the entry candle. Replay: outcome may already be a TP on that index. | Bind entry-candle target eligibility. |
| Same-candle stop vs target | Conservative stop-wins. Entry+stop: `entry_and_stop_same_candle_stop_wins`. Post-entry stop+target: `post_entry_stop_and_target_same_candle_stop_wins`. Metadata `ambiguity_policy=conservative_stop_wins`. | `same_candle_policy` conservative vs optimistic. Conservative with no prior TP → `STOPPED`. Optimistic post-entry TP1+stop returns `TP1_HIT`. `_evaluate_exit_candle` is unused (source inspection only). | Executed: `test_same_candle_entry_stop_and_post_entry_stop_target` | Runtime policy is code, not a versioned binding. Replay policy is `ReplayConfig`. | Do not treat conservative as a historical default for every past row. |
| Earlier TP then later stop | Milestones retained (`tp1_at` stays); terminal is `SL_HIT`. `_record_stop` does not clear TP timestamps. | If `highest_tp > 0` then later stop returns `_target_result` for that TP (`TP1_HIT`), `sl_hit=False`, positive R. | Executed: `test_earlier_tp_then_stop_is_milestone_not_a_winning_exit` | Runtime progress has both facts. Replay trade row does not. | Matching a replay TP1 to a runtime TP1 milestone conceals later stop information. |
| Target ladder vs economic exit | TP1/TP2 are timestamps. `TP_HIT` only after TP3. Missing TP3 is not TP3. No quantity, fees, or P&L. | Optional `tp3`; `_final_target_number` is 3 if `tp3 != NA` else 2. Outcome is `tp1_hit`/`tp2_hit`/`tp3_hit` plus `r_multiple`. | Executed ladder vs stop subset | Runtime stored geometry requires TP1–TP3 (`stored_plan_geometry`). Replay TP3 may be `N/A`. | Do not infer position size or expectancy from milestones. |
| Horizon / end of input | No `ReplayConfig` hold/fill window. Pending-suffix exhaustion leaves `MANAGING` / `terminal_outcome=N/A`. Lifecycle `EXPIRED`/`INVALIDATED` can terminalize via `_terminal_progress_for_record` without a candle path. | `max_fill_candles` / `_fill_window`; `max_hold_candles` / `_max_hold_candles` (swing default 80 when unset). Unfilled → `NOT_FILLED` (`missed_entry`). Hold/input end after fill → `EXPIRED` with mark-to-market R if no TP. | Executed: `test_horizon_unfilled_filled_unresolved_and_input_exhaustion` | Replay windows are config. Runtime expiry is a lifecycle terminal, not an evaluator horizon. | “No data left” is not a supported economic terminal for runtime. |
| No-op / missing data / rejected | Empty candle list: integrity `missing_execution_candle_history`. `execution_candles is None` skips evaluation in the service. `not_run` skips the service. Rejected/non-eligible states do not create progress. | Missing execution candles → replay unavailable note. Fill window miss → `NOT_FILLED`. | Executed ineligible/empty test; service skip is existing R0/service path | Last-pass P3_PREFIX envelope is not interval coverage. | Last-pass evidence must not become whole-interval coverage. |
| Output meaning | Progress, milestones, raw terminals, integrity/cursor, prefix disposition | `ReplayTradeResult` outcome, filled flag, modeled R/statistics | Executed throughout | Different tables (`setup_lifecycle_outcome_progress` vs `replay_results`). No join key that is an episode. | Labels are not interchangeable. |

Relevant functions: `entry_touched`, `evaluate_closed_candle_outcomes`, `_entry_tracking_boundary`, `_terminal_progress_for_record`, `_simulate_trade`, `_fill_window`, `_max_hold_candles`, `StrategyReplayEngine.replay_symbol`, `SetupLifecycleService._apply_to_symbol_result_with_meta`.

## 4. Paired-fixture manifest

Declared inputs shared unless noted: UTC `BASE=2026-01-01T00:00:00+00:00`, 5m bars, long unless parametrized, replay entry `101`.

| Test | Assumptions | Runtime actual | Replay actual | Comparison | Reachability |
| --- | --- | --- | --- | --- | --- |
| `test_positive_comparable_subset_fill_ladder_and_separate_stop` (long/short) | C0 no-touch; C1 zone+point; C2–C4 TP1–TP3. Separate path C2=stop. Replay fill starts at 1. | Ladder: `entry_at` C1 close, `tp1/2/3_at` C2/C3/C4 closes, `terminal_outcome=TP_HIT`, no stop. Stop path: `SL_HIT` at C2, no TP1. | Ladder: `filled`, `fill_index=1`, `TP3_HIT`, `highest_tp_hit=3`. Stop path: `STOPPED`, `r_multiple=-1`. | Comparable: fill index 1; TP ladder touches; stop without TP. Non-equivalent: `TP_HIT` vs `tp3_hit`; `SL_HIT` vs `stopped`; close-ISO vs open-ms. | Supplied-state |
| `test_zone_overlap_without_point_touch_is_non_equivalent` | C1 high=100.5, low=99.5; replay entry stays 101 | `entry_at` C1; no stop | `NOT_FILLED` | Non-equivalent entry predicate | Supplied-state |
| `test_entry_candle_target_is_credited_by_replay_not_runtime` | C1 high=111, low=99 (entry+TP1, no stop); C2 stop | Fill C1, no `tp1_at`, `SL_HIT` on C2 | `TP1_HIT` on fill candle; later stop not applied | Non-equivalent entry-candle targets; later path is discriminating | Supplied-state |
| `test_same_candle_entry_stop_and_post_entry_stop_target` | C1 entry+stop; separate post-entry C2 TP1+stop; conservative vs optimistic | Both paths `SL_HIT`, no `tp1_at` | Entry+stop: both policies `STOPPED` (TP1 not touched). Post-entry conservative `STOPPED`; optimistic `TP1_HIT` | Non-equivalent same-candle precedence once a target is present | Supplied-state |
| `test_earlier_tp_then_stop_is_milestone_not_a_winning_exit` | C1 fill, C2 TP1, C3 stop | `tp1_at` C2 and `SL_HIT` C3 | `TP1_HIT`, `sl_hit=False`, positive R | Non-equivalent: milestone vs winning economic exit | Supplied-state |
| `test_horizon_unfilled_filled_unresolved_and_input_exhaustion` | Delayed fill with `max_fill_candles=1`; hold=1 with later TP1; input ends after fill | Delayed fill at C2; unresolved remains `MANAGING`; exhaustion has no terminal | Missed fill; `EXPIRED` inside hold despite later TP1; `EXPIRED` at input end | Non-equivalent horizon. End of input is not a runtime economic terminal. | Supplied-state |
| `test_causal_boundary_exact_partial_later_processing_and_as_of_mutation` | Exact-open entry on detection bar; confirmation +1m; cutoff=C1 close with later clock; mutate C2 after cutoff | Detection bar fills runtime, not replay. Partial confirmation fills at C1. Cutoff admits entry, not TP1. Mutation after cutoff does not change `entry_at`. | Detection bar not filled (`max_fill=1`). Full list credits `TP1_HIT`. Sliced list `EXPIRED`. | Comparable fill only after declaring the same closed window. Mutating post-as-of data must not change a claimed pre-as-of fact. A later full-trade outcome is a different question. | Supplied-state |
| `test_unsafe_authority_terminal_shortcut_is_not_a_comparable_episode` | Planted `EXPIRED`; caller `coverage_complete=True` | Shortcut copies `EXPIRED`, no tracking start, integrity Unverified | Hold-window `EXPIRED` after fill | Not comparable completed episode. P3A `evaluation_context.complete` stays false. | Direct writer + evaluator; P3A diagnostic API |
| `test_lifecycle_service_forwards_closed_candle_evaluation` | Planted ACTIONABLE record, then `SetupLifecycleService.apply_to_symbol_result` | `entry_at` on second pass; state `MANAGING` | Not paired | Forwards to `evaluate_closed_candle_outcomes`. Dump excludes execution candles and decision timestamp. | Mixed: planted record + ordinary service evaluation |
| `test_strategy_replay_engine_entrypoint_simulates_fill_from_detection` | Existing bullish discovery fixture from `tests/test_strategy_replay.py` | Not paired | `filled`, `fill_index=36`, `time_to_entry=1` | Engine entrypoint uses `_simulate_trade` after detection. Not the same opportunity as runtime paired fixtures. | Normal replay producer, synthetic candles |
| `test_replay_engine_looks_ahead_on_execution_series_after_causal_detection` | Same engine; TP1 after fill | Not paired | Detected at 35; fill then `TP1_HIT` on a later bar | Detection prefix is causal; trade simulation uses later execution bars | Normal replay producer |
| `test_ineligible_and_missing_data_do_not_invent_comparable_outcomes` | `REJECTED`; empty candle list | No progress; empty list integrity unverified | Not used | Missing/ineligible data stays unresolved | Malformed / non-eligible supplied state |
| `test_schema_remains_v24_without_admission_policy_or_source_binding` | Fresh synthetic DB | user_version 24; no admission/policy columns | n/a | Schema unchanged | Synthetic initializer |
| `test_claim_preservation_admission_and_replay_authority_remain_unproven` | Evidence contract + lock vs eligible sets | `unique_trade_count` UNAVAILABLE; `TRIGGERED` unlocked but outcome-eligible; `WATCHLISTED` in both lock and eligible sets | Default same-candle conservative; `NOT_FILLED is MISSED_ENTRY` | Plan lock ≠ outcome eligibility ≠ admission | Source + contract payload |

No CCI expectancy is computed. No quantity fractions, stop movement, fees, slippage, or realistic exchange fills are inferred.

## 5. Source-authority map and future immutable-policy-binding checklist

### Authority map (runtime observation vs replay experiment vs synthetic fixture)

| Fact | Known at concrete caller | Forwarded into evaluator | Persisted on evaluated object | Excluded / missing |
| --- | --- | --- | --- | --- |
| OHLC + candle open/close | Yes, on supplied candles | Yes | Last cursor pair / metadata, not full path | Receipt time; original availability |
| `decision_timestamp` | Scanner `ScannerRunConfig.decision_timestamp` or service `now` | Yes, as cutoff | Last applied cutoff only (P3B2B); not reconstructable as the original clock from ordinary dumps | `lifecycle_decision_timestamp` excluded from `ScannerSymbolResult.model_dump()` |
| `evaluated_at` / processing time | Service `now` | Yes | `first_evaluated_at` INSERT-only; `last_evaluated_at` updated | Not a cutoff |
| Execution timeframe | Symbol result / replay config | Yes | Runtime progress column | Replay config not bound to runtime rows |
| Exchange adapter | `config.exchange == "binance"` → `BinanceFuturesClient` else Bybit | Not as a policy object | CandleDTO `exchange` if that DTO was stored; synthetic dicts have none | Historical imported rows; cache vs live fetch |
| Scan / script / DB filename / `scan_run_id` | Caller | Optional event association | Not an evaluation owner (R0/P3B2A) | Not source authority |
| Replay `same_candle_policy`, fill/hold windows | `ReplayConfig` / CLI `--same-candle-policy` | Yes, into `_simulate_trade` | Replay result rows, not lifecycle progress | Not a runtime binding |
| Coverage complete | Caller assertion to P3A | Diagnostic only | Not row-level completeness | Still false after P3B2A |
| Fixture `source_namespace` | Test provenance | P3A only | Not minted here | Not live/replay authority |

Do not elevate `scan_run_id`, script name, database filename, caller name, safe-configuration hash, algorithm marker `canonical_lifecycle_closed_execution_candles`, document revision, fixture label, or pinned SHA into source authority or a production policy binding.

### Future required-binding checklist (not implemented)

A later immutable evaluation-policy binding, if ASTRA authorizes one, must name at least:

1. **Entry model** — zone overlap vs point touch vs another declared model; which price is the replay `entry`.
2. **Boundary alignment** — confirmation vs material-plan `first_evaluated_at`; exact-open, exact-close, partial overlap; fully-post-boundary rule.
3. **Execution timeframe** — the interval whose closed bars are eligible.
4. **Price basis** — which OHLC stream, venue, and whether synthetic/reconstructed/imported; honesty that receipt time is usually unavailable.
5. **Same-candle precedence** — stop-wins vs optimistic vs another declared rule; entry-candle vs post-entry.
6. **Fill rules** — first eligible bar vs detection+1; fill window; invalidation-before-fill.
7. **Termination** — lifecycle terminal shortcuts vs evaluator SL/TP3 vs replay outcome enum.
8. **Expiry / horizon** — runtime has no hold window; replay fill/hold defaults; end-of-input ≠ economic terminal.
9. **Target semantics** — milestones vs economic exit; TP3 required vs optional; prior TP then stop.

Absent or externally controlled: fees, slippage, queue position, partial fills, stop movement, quantity fractions, live order-book fills, original market-data possession at decision time, admission cause/time, continuation vs re-entry, dependence groups.

Do not complete this checklist by hashing current source or assuming historical defaults.

## 6. Tracking qualification, storage decision, and strategy-research gate

### Tracking (qualification only; no scheduler change)

- `active_lifecycle_symbols` reconstructs monitoring symbols from `get_records_for_states` (`is_current=1` only).
- `_watchlist_with_lifecycle_priority` can reserve capacity for active symbols and exceed the ordinary cap; strict universe membership still filters outsiders.
- `_watchlist_for_watch_iteration` reapplies that priority.
- Missing queued results become `not_run`; `SetupLifecycleService.apply_to_run_result` skips `not_run`.
- Evaluator runs only when `lifecycle_execution_candles is not None`. Empty list is a different integrity path.
- The only normal `app/`/`scripts/` caller of `evaluate_closed_candle_outcomes` remains the lifecycle service.

Do not claim that leaving discovery always stops tracking. Do not claim that active-symbol priority is complete episode tracking. Operational diagnostics can exist before admission. An authoritative obligation for every admitted open episode still requires an episode owner, source/policy semantics, and durable attempt/gap accounting. Not authorized here.

### Storage

`store_scan_result` still writes `scan_runs.raw_payload_json` from `_storage_payload` (or a supplied `raw_payload`) and separately `symbol_results.raw_result_json` from `_symbol_result_record`. The supplied-raw route is not assumed identical to the generated route. Original ~81.21 GiB / ~3.65 GB/day figures remain historical audit estimates, not current measurements. This phase adds no runtime payload. Current live size/runway was not measured (live DB not accessed). Storage remains a mandatory checkpoint before high-volume capture, pass ledgers, or long-duration replay expansion. Prospective lossless removal of the nested `results[]` copy is specified separately in `docs/research/storage_single_copy.md` (application schema v25); this EVAL_SEMANTICS document does not implement that change.

### Strategy-research gate

No regime, CMC, target, directional veto, setup-family, performance-memory, or champion/challenger work is authorized. A 75%+ success aspiration is not a benchmark. Replay R and partial-exit policy are not approved. Public RR/quality gates, confirmation ownership, and execution prohibitions are unchanged.

## 7. Admission STOP and remaining denominator / outcome / replay blockers

**Prospective admission persistence is STOP in this PR.**

Plan locking is not admission: `PLAN_LOCK_STATES` includes WATCHLISTED/STALKING and others; `TRIGGERED` is outcome-eligible but unlocked (`test_claim_preservation_admission_and_replay_authority_remain_unproven`). Outcome eligibility and plan lock have different memberships. Neither set is a research population.

The candidate future seam remains `SetupLifecycleService._apply_to_symbol_result_with_meta` after transition and before/around `evaluate_closed_candle_outcomes`. Naming it does not create an admission decision.

Remaining blockers (unchanged in kind; this phase only specifies evaluation semantics):

1. Explicit prospective pre-outcome admission and complete population accounting.
2. Immutable economics **plus** authoritative source-context binding **plus** complete evaluation-policy binding.
3. Continuation / retry / restart versus new admission / re-entry.
4. Episode-owned tracking attempts or explicit gaps, plus interval coverage.
5. Legal terminal, unfilled, censored, unresolved, and conflicting-evidence handling without outcome-based attrition.
6. Episode-scoped outcome reconciliation and analytics/consumer migration.
7. Dependence-aware validation; episode identity ≠ independent samples.

Unique real trade/fill identity remains required for actual execution statistics and is not a prerequisite for every explicitly controlled setup-simulation question.

Canonical episode outcomes remain blocked. Never add `UNIQUE(plan_version_id)` as an outcome rule. Replay is not runtime outcome authority. A historical reconstruction acquired now can support a disclosed simulation; it cannot establish exactly what runtime possessed at original decision time.

## 8. Updated original-audit A–W matrix

RESOLVED always has the stated scope. This contract does not mark a finding solved merely because the document exists.

| ID | Status | Scope / remaining limitation |
| --- | --- | --- |
| A | PARTIALLY RESOLVED | Reproducibility contracts and exact-main CI exist; complete exact-as-run data does not follow. Paired fixtures are synthetic. |
| B | FOUNDATION PRESENT, NOT FULLY CONSUMED | P1 identities exist; downstream episode/analytics ownership absent. |
| C | RESOLVED at P2A scope | Truthful event-accounting semantics, not unique economic fills. |
| D | RESOLVED at P2B ownership boundary | Does not establish independent tracking or admission. |
| E | RESOLVED prospectively | Historical oscillation records are not rewritten. |
| F | PARTIALLY RESOLVED | Physical progress ownership and diagnostics, not authoritative episode outcome ownership. |
| G | PARTIALLY RESOLVED | Proven `plan_version_id` first-INSERT only; no legacy backfill. |
| H | PARTIALLY RESOLVED | Source, complete policy binding, admission, and full coverage still missing. This phase specifies the policy surface; it does not persist it. |
| I | RESOLVED for last accepted qualifying application | Not complete historical as-of evidence. |
| J | PARTIALLY RESOLVED | Last qualifying supplied-prefix disposition only. |
| K | PARTIALLY RESOLVED | Ordinary same-known-anchor fan-out not demonstrated; import/storage ambiguity remains. |
| L | STILL OPEN | Analytics is lifecycle/final-outcome keyed, not episode keyed. |
| M | STILL OPEN / UNAVAILABLE | Unique real trade/fill occurrence identity. |
| N | STILL OPEN | Authoritative episode-scoped outcome absent; global plan-version uniqueness unjustified. |
| O | FOUNDATION PRESENT, SEMANTIC NON-EQUIVALENCE PROVED | Replay is not runtime outcome authority. Paired tests in `tests/test_evaluation_semantics_contract.py` prove concrete policy differences. Engine parity is not a universal prerequisite to a later separately declared runtime policy. |
| P | STILL OPEN | Unit is defined semantically (R0); prospective population, handling, and denominator remain absent. |
| Q | DEFERRED INTENTIONALLY | TARGET_INSIDE_CHOP / target-quality research. |
| R | STILL OPEN, CURRENT WRITER DUPLICATION CONFIRMED | Current live size/runway unmeasured; no storage change in this phase. |
| S | DEFERRED INTENTIONALLY | Regime/context strategy influence. |
| T | DEFERRED INTENTIONALLY | CMC integration. |
| U | DEFERRED INTENTIONALLY | Setup-family / target / directional redesign. |
| V | DEFERRED FROM SERIOUS LEARNING | Existing memory machinery is not validated learning evidence. |
| W | DEFERRED | Champion/challenger validation/promotion. |

## Invariants preserved

- No production Python, schema, migration, dependencies, settings, CI, or public formatting change.
- RR and setup-quality gates, P2B confirmation ownership, public `event_key` / `message_hash`, Telegram routing, symbol health, CMC, performance memory, and execution prohibitions unchanged.
- P3B1 INSERT-only attribution and legacy NULL handling retained (not re-tested as new logic; existing suites remain the proof).
- P2A scoped counts and P2B ownership/confirmation protections unchanged.
- P3A canonical outcome unproven; unique trade counts unavailable.
- Last accepted cutoff / last qualifying prefix limitations retained.
- Missing anchors, contradictory identity, and unsupported generation fan-out remain unresolved (R0).
- No admission table, policy/source binding table, pass ledger, tracker, or replay authority.
- No migration or backfill.
- `ORDER_EXECUTION_ENABLED=false` default; no withdrawals; Dev-PC Telegram dry-run; no live DB access.

**CORRECTNESS > COMPLETION**
