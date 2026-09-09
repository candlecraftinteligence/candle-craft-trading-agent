# CCI EVAL_POLICY — Evaluation Policy Manifest (Unbound)

**Status:** Research manifest + synthetic/source proofs. Diagnostic Python only.
**Does not** persist admissions, mint episode/trade/policy IDs, bind source/policy, add a tracker, authorize replay as runtime outcome authority, or compute expectancy.
**Audited base commit:** `afbc4ae75cbc886daea09cb536ae577661b96ada` (`main`, merge of PR #116 / STORAGE_SINGLE_COPY).
**Schema:** application `SCHEMA_VERSION` / SQLite `PRAGMA user_version` = **25** unchanged. No admission, episode, policy-hash, or source-namespace columns exist.

Machine-readable catalog: `app/analytics/evaluation_policy.py` (`cci-evaluation-policy-manifest-v1`).
Fixtures in `tests/test_evaluation_policy_manifest.py` are labeled synthetic. No live or existing scan database was read.

This phase is a finite successor to EVAL_SEMANTICS. EVAL_SEMANTICS specified **what current evaluators mean** and proved paired non-equivalence. This phase names that policy surface as an inspectable **manifest** and proves it is **not** an immutable evaluation-policy binding.

## 1. Purpose

R0 required evaluation-policy semantics covering entry, boundary, price basis/timeframe, same-candle ambiguity, target/stop/termination, and expiry/horizon, and forbade hashing a fictional historical policy version.

EVAL_SEMANTICS listed the nine required future-binding dimensions and forbade completing that checklist by hashing current source or assuming historical defaults.

This phase answers the next finite question:

**What is the current implemented evaluation-policy surface, which tokens are forbidden surrogates for a binding, and is that surface complete and persisted?**

The answers are: the nine-dimension catalog below; none of the listed surrogates; **no**.

Engine disagreement remains evidence. This phase does not change runtime or replay to produce matching labels.

## 2. Verified base, schema, and evidence scope

| Fact | Evidence |
| --- | --- |
| Fresh `origin/main` | `afbc4ae75cbc886daea09cb536ae577661b96ada` |
| PR #115 EVAL_SEMANTICS | Merged; implementation head `b47b184a3a60af6f468de0b55bf746966fb2efa7` is an ancestor |
| PR #116 STORAGE_SINGLE_COPY | Merged at the audited SHA; application schema **v25** |
| Absent | `admission_id`, `evaluation_episode_id`, `evaluation_policy_id`, `policy_hash`, durable `source_namespace`, `admitted_evaluation_episodes`, `evaluation_policies` |
| Present, not a policy | `scan_runs.raw_payload_format` (`inline_v1` / `symbol_refs_v1`) |

Evidence labels used in tests (not application enums):

- **direct evaluator / import-supported supplied state**
- **source inspection only, not executed here**
- **malformed / adversarial supplied state**

The diagnostic module does not open a database. A supplied `ReplayConfig` is a declared experiment envelope, not a persisted policy.

## 3. Manifest object (unbound)

`evaluation_policy_manifest()` is deterministic JSON. Every required dimension is present. Completeness is false for both engines.

Binding statuses used in the catalog (test/report concepts, not production enums, not persisted):

| Status | Meaning |
| --- | --- |
| `implemented_unbound` | Runtime code does this. No durable policy object. |
| `hardcoded_unbound` | Replay/runtime behavior is in source and is not a caller-versioned binding. |
| `caller_declared_unbound` | A `ReplayConfig` field exists. It is not `evaluation_policy_id`. |
| `externally_controlled` | Absent here. Recorded as `N/A`, not zero. |
| `not_a_policy_binding` | A marker or surrogate that must not complete the checklist. |

`complete_immutable_binding` is false on every dimension. `evaluation_policy_id` and `policy_hash` are `unavailable` wherever those keys appear.

Helpers:

- `classify_policy_surrogate(kind, value=None)` — acknowledges that a value was supplied and **never** copies it into a policy id.
- `declared_replay_experiment_envelope(config)` — names caller-declared replay fields; unset fill/hold windows stay unset (`unset_uses_mode_timeframe_default`) instead of inventing historical defaults.
- `implemented_runtime_policy_snapshot()` — names current runtime implementations without versioning them.

There is no `mint_evaluation_policy_id`, `bind_policy`, or source-hash helper.

## 4. Required dimensions (implemented, not bound)

Declared comparison classes remain those of EVAL_SEMANTICS. This table is the binding view of that matrix.

| Dimension | Runtime (unbound) | Replay (unbound) | EVAL_SEMANTICS proof |
| --- | --- | --- | --- |
| Entry model | Zone overlap `entry_touched` | Point touch `_price_touched`; `entry_low`/`entry_high` unused by `_simulate_trade` | `test_zone_overlap_without_point_touch_is_non_equivalent` |
| Boundary alignment | Confirmation or material-plan `first_evaluated_at`; fully-post-boundary; exact-open vs partial | Detection index + 1; detection candle never fills | `test_causal_boundary_exact_partial_later_processing_and_as_of_mutation` |
| Execution timeframe | Progress row must match | `ReplayConfig.execution_timeframe` default `15m` | existing lifecycle timeframe mismatch tests |
| Price basis | Supplied OHLC; Binance `/fapi/v1/klines` is ordinary scanner selection, not proven possession | Supplied OHLC after `_normalize_candles`; no fetch inside `_simulate_trade` | source-authority map; receipt time unavailable |
| Same-candle precedence | Conservative stop-wins; local `ambiguity_policy` metadata | `ReplayConfig.same_candle_policy`; `_evaluate_exit_candle` unused | `test_same_candle_entry_stop_and_post_entry_stop_target` |
| Fill rules | First fully post-boundary closed bar; no fill window | Detection+1 inside fill window; stop-before-fill `INVALIDATED` | horizon/fill tests |
| Termination | Evaluator SL/TP3; lifecycle shortcut may copy EXPIRED/INVALIDATED without a candle path | `ReplayOutcome` enum + modeled R | `test_unsafe_authority_terminal_shortcut_is_not_a_comparable_episode` |
| Expiry / horizon | No hold window; end of input is not an economic terminal | Fill/hold windows; `NOT_FILLED` / hold `EXPIRED` | `test_horizon_unfilled_filled_unresolved_and_input_exhaustion` |
| Target semantics | Entry candle cannot credit targets; TP1/TP2 milestones retained through later `SL_HIT`; `TP_HIT` only after TP3 | Fill candle can credit targets; prior TP then stop can return that TP with `sl_hit=False` | `test_earlier_tp_then_stop_is_milestone_not_a_winning_exit`; `test_entry_candle_target_is_credited_by_replay_not_runtime` |

This phase re-executes the zone-vs-point predicate difference and source-inspects the other predicates. It does not re-run the full EVAL_SEMANTICS paired candle matrix.

## 5. Forbidden surrogates and local markers

Do not elevate any of the following into `evaluation_policy_id` or `policy_hash`:

`source_sha256`, `pinned_git_sha`, `schema_version`, `pragma_user_version`, `scan_run_id`, `database_filename`, `script_name`, `caller_name`, `safe_configuration_hash`, `algorithm_marker`, `document_revision`, `fixture_label`, `raw_payload_format`, `ambiguity_policy_metadata`, `entry_causality_contract`, `replay_candidate_key`, `plan_version_id`, `plan_identity`, `lifecycle_id`.

Local markers that remain **not** complete bindings:

| Marker | Value | Why it is not a binding |
| --- | --- | --- |
| `metadata_json.ambiguity_policy` | `conservative_stop_wins` | Same-candle note on a progress row. Incomplete and unversioned. |
| `ENTRY_CAUSALITY_CONTRACT` | `fully_post_boundary_closed_candle_v1` | Algorithm label for start alignment only. |
| progress `source` | `canonical_lifecycle_closed_execution_candles` | Producer marker. |
| `scan_runs.raw_payload_format` | `inline_v1` / `symbol_refs_v1` | STORAGE_SINGLE_COPY representation. |

Hashing `app/lifecycle/outcomes.py` (or any other source) does not complete the checklist. `classify_policy_surrogate("source_sha256", digest)` returns `unavailable` ids and does not echo the digest as an identifier.

Unknown kinds are also refused (`unknown_surrogate_is_not_evaluation_policy_binding`). There is no kind that returns a real policy id.

## 6. ReplayConfig is a declared experiment, not a complete binding

`declared_replay_experiment_envelope` may differ when `same_candle_policy`, timeframe, or fill/hold windows differ. That difference is a different experiment description.

It does **not** bind:

- entry model (still point touch)
- boundary alignment (still detection+1)
- price basis / possession
- termination enum vs lifecycle terminals
- target/milestone vs economic-exit semantics
- entry-candle target eligibility

Unset `max_fill_candles` / `max_hold_candles` stay `unset_uses_mode_timeframe_default`. Current scalp/challenge 12-or-6 fill defaults and 48/80 hold defaults are code, not a historical policy for past rows.

Default `same_candle_policy="conservative"` is the current ReplayConfig default. It is not a historical default for every past runtime or replay row.

## 7. Absent / externally controlled

Fees, slippage, queue position, partial fills, stop movement, quantity fractions, live order-book fills, original market-data possession at decision time, admission cause/time, continuation vs re-entry, and dependence groups remain **N/A** / unavailable. They are not inferred as zero.

## 8. Admission STOP, tracking, storage, strategy gate

**Prospective admission persistence remains STOP.**

Plan locking is not admission. Outcome eligibility is not a research population. Unique trade count remains unavailable (`evidence_contract_payload`). P3A `canonical_plan_outcome_established` remains false on an empty supplied snapshot.

Tracking qualification from EVAL_SEMANTICS is unchanged: the only normal `app/`/`scripts/` caller of `evaluate_closed_candle_outcomes` is `app/lifecycle/service.py`. This phase does not add a tracker.

STORAGE_SINGLE_COPY (schema v25) may omit a nested `results[]` copy on new runs. That is a payload representation change. It is not source authority and not a policy binding. Historical rows keep their duplicate copy. Live size/runway was not measured (live DB not accessed).

No regime, CMC, target, directional veto, setup-family, performance-memory, or champion/challenger work is authorized. Replay R is not approved expectancy. Public RR/quality gates, confirmation ownership, and execution prohibitions are unchanged.

## 9. Updated original-audit A–W matrix

RESOLVED always has the stated scope. This manifest does not mark a finding solved merely because the catalog exists.

| ID | Status | Scope / remaining limitation |
| --- | --- | --- |
| A | PARTIALLY RESOLVED | Unchanged. Manifest proofs are synthetic/source. |
| B | FOUNDATION PRESENT, NOT FULLY CONSUMED | Unchanged. |
| C | RESOLVED at P2A scope | Unchanged. |
| D | RESOLVED at P2B ownership boundary | Unchanged. |
| E | RESOLVED prospectively | Unchanged. |
| F | PARTIALLY RESOLVED | Unchanged. |
| G | PARTIALLY RESOLVED | Unchanged. |
| H | PARTIALLY RESOLVED | Policy **surface** is now an inspectable unbound manifest. Source-context binding, admission, and full coverage remain missing. Local markers are not a complete binding. |
| I | RESOLVED for last accepted qualifying application | Unchanged. |
| J | PARTIALLY RESOLVED | Unchanged. |
| K | PARTIALLY RESOLVED | Unchanged. |
| L | STILL OPEN | Unchanged. |
| M | STILL OPEN / UNAVAILABLE | Unchanged. |
| N | STILL OPEN | Unchanged. Global `UNIQUE(plan_version_id)` still unjustified. |
| O | FOUNDATION PRESENT, SEMANTIC NON-EQUIVALENCE PROVED | Unchanged. This phase names the unbound dimensions that cause non-equivalence. Replay is still not runtime outcome authority. |
| P | STILL OPEN | Admission unit unresolved. |
| Q | DEFERRED INTENTIONALLY | Unchanged. |
| R | PARTIALLY RESOLVED | STORAGE_SINGLE_COPY prospective compact encoding (v25). Historical duplicate `results[]` remains. Live size unmeasured. Not a policy binding. |
| S–W | DEFERRED INTENTIONALLY | Unchanged. |

## 10. Future binding (not implemented)

If ASTRA later authorizes an immutable evaluation-policy binding, it must name all nine required dimensions, refuse the forbidden surrogates, leave absent external controls unavailable, and still not treat replay as runtime outcome authority.

This document names that future object. It does not create it.

Remaining blockers unchanged in kind:

1. Explicit prospective pre-outcome admission and complete population accounting.
2. Immutable economics **plus** authoritative source-context binding **plus** complete evaluation-policy binding.
3. Continuation / retry / restart versus new admission / re-entry.
4. Episode-owned tracking attempts or explicit gaps, plus interval coverage.
5. Legal terminal, unfilled, censored, unresolved, and conflicting-evidence handling without outcome-based attrition.
6. Episode-scoped outcome reconciliation and analytics/consumer migration.
7. Dependence-aware validation; episode identity ≠ independent samples.

## Invariants preserved

- No evaluator, schema, migration, settings, CI, or public formatting change.
- Diagnostic module only: `app/analytics/evaluation_policy.py`.
- RR and setup-quality gates, P2B confirmation ownership, public `event_key` / `message_hash`, Telegram routing, symbol health, CMC, performance memory, and execution prohibitions unchanged.
- P3A canonical outcome unproven; unique trade counts unavailable.
- No admission table, policy/source binding table, pass ledger, tracker, or replay authority.
- No migration or backfill.
- `ORDER_EXECUTION_ENABLED=false` default; no withdrawals; Dev-PC Telegram dry-run; no live DB access.

**CORRECTNESS > COMPLETION**
