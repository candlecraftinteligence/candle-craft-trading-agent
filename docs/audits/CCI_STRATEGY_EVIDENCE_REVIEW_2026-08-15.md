# Candle Craft Intelligence Strategy Evidence Review

Audit date: 2026-08-15
Branch: `audit/candle-craft-reliability-validation`
Baseline: `aca528fe7a227eb43146120ffecb168cb439994f` (`origin/main`)
Reviewed source: `bf76aa6` plus the read-only checks recorded below

## 1. Decision

**NO_STRATEGY_CHANGE - WAITING_FOR_EMPIRICAL_OUTCOME_EVIDENCE**

The replay implementation has deterministic, leakage-resistant contracts and its focused test matrix passes repeatably. The repository does not contain a tracked empirical candle/outcome cohort, however, so it cannot support a defensible claim about profitability, expectancy, target quality, scoring calibration, or risk/reward thresholds.

No strategy, scoring, setup-gate, target, stop, or minimum-RR behavior is changed by this review. In particular, strict rejection of targets inside chop is retained. Weakening that gate without representative terminal outcomes would substitute intuition for evidence and conflict with the project rule to prefer rejecting weak setups.

## 2. Safety and scope

This review was offline and read-only with respect to runtime data:

- no exchange or market-data request;
- no Telegram delivery;
- no order, withdrawal, or transfer action;
- no access to or mutation of the protected live Runtime database;
- no invented candles, outcomes, or derivatives data;
- no performance-memory or lifecycle mutation.

The synthetic benchmark below is contract evidence only. It is not market evidence and must not be presented as live, historical, or forward performance.

## 3. Evidence inventory

Tracked repository inventory found no historical candle dataset and no terminal-outcome export suitable for strategy evaluation. `replay_reports/.gitkeep` and `replay_validation/.gitkeep` are placeholders; the replay tests use deterministic, hand-built fixtures.

| Evidence class | Available | What it can establish |
|---|---:|---|
| Replay engine and causal boundary tests | Yes | Closed-candle and decision-time behavior, deterministic ordering, conservative event rules |
| Dataset export, quality, coverage, readiness, and validation contracts | Yes | Required schema and failure semantics |
| Synthetic win/loss fixtures | Yes | Outcome accounting and metric arithmetic |
| Versioned empirical candles | No | N/A |
| Representative terminal lifecycle outcomes with `result_r` | No | N/A |
| Producer/strategy/timeframe cohort metadata at useful sample size | No | N/A |
| Funding, open interest, CVD, liquidation, or other derivatives history | No | N/A |

## 4. Leakage and determinism controls

The focused replay suite verifies these boundaries:

- each decision uses only candles closed as of its decision timestamp;
- higher-timeframe candles are unavailable until their close boundary;
- mutating future or still-open higher-timeframe data does not change an earlier decision;
- event ordering is deterministic;
- ambiguous same-candle paths are handled conservatively;
- missing lower-timeframe evidence does not trigger a proportional-outcome fallback;
- missing data stays `N/A`, and unreliable data stays `Unverified`.

The complete tracked replay matrix passed twice without source changes between runs:

| Run | Result | Duration |
|---|---:|---:|
| 1 | 184 passed | 3.33 s |
| 2 | 184 passed | 3.15 s |

The matrix covered:

- `test_strategy_replay.py`
- `test_replay_causality_phase2.py`
- replay dataset export, quality, and coverage
- event-sequence validation and failure taxonomy
- outcome readiness and lifecycle integration
- research reporting and validation scaffolding

## 5. Empty empirical-input audit

The repository's own read-only analyzers were run against the tracked empirical inventory, which contains zero eligible rows.

| Analyzer | Result |
|---|---|
| Dataset coverage | 0 rows; 0 replay-ready; 5 warnings; no symbol, trade-idea, terminal-outcome, or `result_r` coverage |
| Dataset quality | 0 rows; quality score 0.0; `no_rows` warning |
| Outcome readiness | 0 candidates; 0 outcome-ready |
| Validation plan | 0 candidates; `no_candidates` informational finding |

The analyzers are correctly valid as schema/audit operations while still reporting no usable evidence. `is_valid=true` means the empty input was processed correctly; it does not mean the dataset is adequate for performance research.

## 6. Synthetic contract benchmark

A bounded offline benchmark used the tracked deterministic fixtures from `tests/test_strategy_replay.py`: one fixture designed to reach TP1 and one designed to stop. The benchmark ran no network or runtime path.

| Metric | Synthetic result |
|---|---:|
| Setups | 2 |
| Filled / missed | 2 / 0 |
| TP1-or-better rate | 50% |
| TP2 rate | 0% |
| Expectancy / average R | 0.44785636 R |
| Best / worst | 1.89571273 R / -1 R |
| Maximum drawdown | 1 R |
| Profit factor | 1.89571273 |
| Rejected setups / near misses | 5 / 3 |
| Most common synthetic rejection | `missing_confirmation_structure_shift` |

The sample-size warning is `low_sample_size`, and the replay-edge classification is `mixed`. These figures prove deterministic accounting over those two constructed cases only. They provide no estimate of real win rate, edge, profit factor, or drawdown.

## 7. Strategy decision record

| Candidate change | Decision | Evidence basis |
|---|---|---|
| Relax target-inside-chop rejection | Do not change | Causal effect on terminal outcomes is N/A |
| Adjust confidence scoring or grade cutoffs | Do not change | No calibrated outcome cohort |
| Adjust minimum RR | Do not change | No representative expectancy distribution |
| Alter entries, stops, invalidation, or target construction | Do not change | No historical counterfactual comparison |
| Add derivatives-based gates | Do not change | Required historical inputs are N/A |

This is an evidence-based no-change decision, not a claim that the current strategy is optimal.

## 8. Evidence required before tuning

Strategy tuning may be reconsidered only after a versioned, read-only export is available with, at minimum:

1. stable run, scan, setup, trade-idea, alert, and journal identities where applicable;
2. decision timestamp and candle-close boundaries;
3. symbol, exchange, market type, timeframe, strategy name/mode, and producer version;
4. entry range, stop, invalidation, TP1, TP2, grade, confidence, and planned RR;
5. first failed gate and rejection reasons for negative examples;
6. terminal lifecycle status, outcome timestamp, and `result_r` for resolved setups;
7. explicit `N/A` and `Unverified` markers for unavailable or unreliable fields;
8. enough independent samples per cohort to avoid drawing conclusions from sparse buckets.

Any future comparison must remain time-ordered, closed-candle-only, cohort-aware, and out-of-sample. Threshold selection and evaluation must not use the same future outcomes. Until that evidence exists, empirical strategy quality and all causal tuning effects remain **N/A**.
