# CCI EVALUATION_POLICY_MANIFEST — Immutable Executable Evaluator Policy Contracts

**Status:** Unused contract library + synthetic coupling tests. No production consumer.
**Does not** persist policy IDs, bind progress/analytics/scan/replay rows, admit episodes, change either evaluator, or create source/admission/tracking authority.
**Base commit:** `afbc4ae75cbc886daea09cb536ae577661b96ada` (`main`, merge of PR #116 / STORAGE_SINGLE_COPY).
**Schema:** application `SCHEMA_VERSION` / SQLite `PRAGMA user_version` = **25** unchanged. Fresh synthetic `PRAGMA schema_version` is a distinct SQLite catalog revision. No admission, episode, policy-hash, or source-namespace columns exist.

PR **#117** was closed without merge and is not implementation or architectural authority.

Fixtures in `tests/test_evaluation_policy_manifest.py` are labeled synthetic. No live or existing scan database was read.

This phase extends EVAL_SEMANTICS from prose and paired examples into a reusable, content-addressed invocation contract. Equal policy IDs mean the same declared evaluator contract under the same normalized parameters. They do not prove shared source, same opportunity, equivalent accumulated history, statistical independence, profitability, or a complete episode policy.

## 1. Purpose and original P5 / P6 dependency

Original P5 required continuing evaluation of admitted open plans, or explicit gap evidence, then reconciling mature covered unambiguous outcomes.

Original P6 required capturing causal inputs and reproducing decisions/outcomes under explicit entry/exit assumptions, separating exact-as-run evidence from reconstruction.

R0 defined the future accounting unit as one admitted plan-evaluation episode under declared source context and fixed evaluation-policy semantics. EVAL_SEMANTICS specified what the current runtime and replay evaluators actually mean. This phase makes those current-evaluator semantics an immutable, hashed contract at the declared call boundary.

It is not a complete research-episode policy binding. A later producer may bind this ID only when source, admission, tracking, and continuation rules are independently owned. Setting `evaluation_context.complete=true` cannot erase the exclusions below.

## 2. Contract-only scope

Delivered:

- `app/research/evaluation_policy.py` — builders, validation, canonical UTF-8 JSON, domain-separated SHA-256 IDs
- `tests/test_evaluation_policy_manifest.py`
- this document

Not delivered and not authorized:

- runtime imports or call-site wiring
- persistence, backfill, or attribution onto continuing progress history
- schema/migration/CI/dependency/evaluator/strategy/public changes
- one policy for an entire multi-mode replay run
- use of unused helper `_evaluate_exit_candle` as active policy

“No persistence/binding” does **not** mean “no content-derived policy ID”. The ID is minted in-process from the validated semantic payload and is not written to SQLite.

## 3. Identity, canonicalization, and immutability

Identity payload fields, and only these, are hashed:

| Field | Role |
| --- | --- |
| `manifest_format_version` | `cci-evaluation-policy-manifest-v1` |
| `evaluator_family` | `runtime_lifecycle_closed_candle` or `replay_trade_simulation` |
| `scope` | semantic call-boundary token, includes, excludes — not Python source paths |
| `rule_vocabulary_version` | family-specific rule corpus |
| `rules` | operational definitions of in-scope predicates |
| `effective_parameters` | normalized evaluator parameters |

Canonical bytes:

- UTF-8 JSON object
- `sort_keys=True`
- separators `("," , ":")`
- `ensure_ascii=False`
- `allow_nan=False`
- supported values: object, array, string, int, bool, and JSON `null` only where a declared rule explicitly uses “no horizon”
- bool is not accepted as an integer
- unknown versions, unknown fields, invented rule strings, non-finite numbers, unresolved placeholders (`unknown`, `n/a`, …), and unsupported timeframes/modes fail without an ID

Policy ID:

- domain `cci.evaluation_policy_manifest.v1`
- `SHA-256(domain || 0x1F || canonical_bytes)`
- prefix `eval-policy-` plus the full 64-hex digest

Outside identity (audit only, not hashed): code SHA, source paths, line numbers, test names, inspection time, human commentary. Run IDs, plan IDs, candle arrays, actual prices, cutoffs, outcomes, and DB paths are context/economics/evidence, not policy identity.

Deep immutability: nested `MappingProxyType` / tuples; `to_canonical_dict()` is a JSON round-trip copy. Mutating caller-owned inputs or exported copies cannot change `canonical_bytes` / `policy_id`.

Call-boundary identity is semantic, not a Python source path:

| Family | Hashed `scope.call_boundary` |
| --- | --- |
| runtime | `runtime_closed_candle_outcome_evaluation` |
| replay | `replay_trade_simulation` |

Audit/provenance, via `evaluation_policy_provenance()` and module constants, remains **outside** `canonical_bytes` and `policy_id`:

- runtime source: `app.lifecycle.outcomes.evaluate_closed_candle_outcomes`
- replay source: `app.backtesting.strategy_replay._simulate_trade`
- unused helper (excluded from policy): `app.backtesting.strategy_replay._evaluate_exit_candle`

A pure refactor or module/function move with identical evaluator semantics must not churn the policy ID. A changed semantic call-boundary, rule vocabulary, or effective parameter must change or reject identity.

A runtime evaluator invoked on synthetic or reconstructed candles remains the runtime family. Family name does not assert live observation.

## 4. Effective parameter resolution

### Runtime

The only in-scope configuration at the call boundary is the normalized supported `execution_timeframe`. Rules are otherwise fixed in code. Unsupported timeframes are rejected (`timeframe_duration`).

### Replay

`build_replay_evaluation_policy(config: ReplayConfig, mode=...)` resolves **one** candidate mode. Do not emit one ID for `ReplayConfig.modes` when those modes resolve different fill/hold limits.

Identity parameters:

| Parameter | Resolution |
| --- | --- |
| `execution_timeframe` | `ReplayConfig.execution_timeframe` (normalized, must be a supported candle TF) |
| `same_candle_policy` | `conservative` or `optimistic` |
| `fill_window_candles` | `_fill_window`: explicit `max_fill_candles`, else challenge/scalp `12` if `confirmation_timeframe == "5m"` else `6`; swing uses the effective hold limit |
| `max_hold_candles` | `_max_hold_candles`: explicit `max_hold_candles`, else `48` for challenge/scalp, else `80` for swing. The `1h`/`4h` branch currently also returns `80`. |

An explicit limit equal to the resolved default has the same identity. `confirmation_timeframe` affects challenge/scalp fill defaults only until `max_fill_candles` is set; it is not itself an identity field.

Not identity (discovery / experiment envelope, not `_simulate_trade` rules): `max_setups`, `replay_candles`, `aggressive_toggle`, `edge_min_sample`, HTF/bias/structure timeframes, `config.modes` membership.

Hold loop (rule, not a separate numeric identity field): inclusive `fill_index` through `fill_index + max_hold_candles`, clipped to input end. `max_hold_candles=1` therefore evaluates the fill candle and the next candle.

## 5. Semantic dimensions, owners, and tests

Evidence classes:

1. Ordinary producer-reachable state, synthetic inputs
2. Direct evaluator / import-supported supplied state
3. Malformed / adversarial state
4. Source-inspection-only

| Dimension | Runtime | Replay | Evidence | Manifest treatment |
| --- | --- | --- | --- | --- |
| Entry | Zone overlap `high >= entry_low and low <= entry_high` (`entry_touched`) | Point containment `low <= entry <= high` (`_price_touched`); `entry_low`/`entry_high` unused by `_simulate_trade` | Class 2: existing EVAL_SEMANTICS zone fixture reused | Separate families; IDs differ |
| Stop / target touch | Directional inequality / threshold; jump beyond a level still counts | Inclusive range containment; jump that does not contain the level does not count | Class 2 helpers **and** full evaluators with continuous 5m bars (`test_price_jumps_beyond_stop_and_target_through_full_evaluators`) | Declared `requires_level_inside_inclusive_range` |
| Execution TF | Progress mismatch → integrity failed | `ReplayConfig.execution_timeframe`; numeric hold default currently independent of the 1h/4h branch | Class 2 / 4 | Parameter + mismatch rule |
| Candle eligibility | `closed_candles_as_of`: continuity then `close <= decision_timestamp`; `minimum_closed_history=0` | `_normalize_candles` / `validate_candle_sequence`; no decision cutoff inside `_simulate_trade` | Class 2/3: post-cutoff mutation vs whole-input continuity failure | Source availability not implied |
| Causal start | Confirmation / CONFIRMED event; material-plan `first_evaluated_at` when other identities exist; first fully post-boundary open; exact-open vs partial; legacy cursor | Fill search `detected_at_index + 1` | Class 2: exact-open case | Rules in vocabulary |
| Cutoff vs processing | `decision_timestamp` ≠ `evaluated_at` | Trade sim may consume later bars after detection | Class 2 | Runtime cutoff is an argument, not persisted policy |
| Entry-candle targets | Fill and stop allowed; targets start next bar | Target/exit loop includes fill index | Class 2 | Rule |
| Pre-entry stop | No invalidation; cursor advances; later fill allowed | `INVALIDATED` if stop without entry | Class 2 full evaluators (`test_pre_entry_stop_invalidates_replay_not_runtime`); existing `test_tp_and_stop_before_entry_do_not_count` | Do not assume both invalidate |
| Same-candle | Conservative stop wins; not configurable | conservative vs optimistic, including already achieved targets | Class 2 | Runtime fixed; replay parameter |
| Milestone vs terminal | TP1/TP2 timestamps; TP3 → `TP_HIT`; later stop retains milestones (`record_stop` does not clear TP fields) | Prior TP then stop → target result, `sl_hit=False`, positive R | Class 2 | Replay is not a milestone ledger |
| Target ladder | Stored TP1–TP3 required | Optional TP3; `_final_target_number` 3 else 2 | Class 2 | Conditional rule; supplied prices are plan input |
| Horizons | None; end of input is not an economic terminal | Fill/hold as resolved; unfilled = `missed_entry` | Class 2 | Runtime `null` windows mean none, not unknown |
| Expiry / shortcuts | Incoming lifecycle terminal copied without completing the candle filter; existing terminal no-op | Hold/input-end `EXPIRED` with mark-to-market R | Class 2 mixed planted terminal | Naming the shortcut is not causal economic support |
| Continuation / failure | Missing history/TF, integrity, no-new-candles, `not_run` skip, `execution_candles is None` skip | Normalize/engine unavailable notes | Class 2/3/4 | Not admission or coverage |
| Output / valuation | Timestamps, labels, integrity, prefix disposition | Outcome enum, flags, modeled R | Class 2 | No real-fill or episode-outcome authority |
| Price consumption | Supplied OHLC ranges | Same | Class 4 for venue: Binance adapter often uses `/fapi/v1/klines`; injected candles do not inherit that provenance | No Binance trade-price assertion |

`_evaluate_exit_candle` is defined and unused (single definition, no callers). It is excluded from policy. Its stop-with-prior-TP behavior is not `_simulate_trade`.

## 6. Reachability, admission, source, tracking

| Path | Class | Note |
| --- | --- | --- |
| `evaluate_closed_candle_outcomes` with constructed record (audit owner; not identity) | 2 | Not scanner discovery |
| `SetupLifecycleService.apply_to_symbol_result` after planted record | mixed 1+2 | Ordinary service evaluation; dump excludes execution candles/TF/decision timestamp |
| `StrategyReplayEngine.run` bullish fixture | 1 | Not the same opportunity as runtime paired plans |
| Empty list / REJECTED / continuity gap | 2/3 | Unresolved, not invented outcomes |
| `apply_to_run_result` skips `not_run` | 4 + existing R0 | Missing queued results are not evaluated |
| `ScannerRunner._exchange_client_for` | 4 | Injected/cached clients; selecting Binance is not receipt history |
| Watch `active_lifecycle_symbols` / priority | 4 | Incomplete tracking; `not_run` and omitted candles remain |

**Admission STOP.** `PLAN_LOCK_STATES` ≠ `OUTCOME_ELIGIBLE_STATES`. TRIGGERED is outcome-eligible and unlocked. A progress INSERT is not a pre-outcome admission. No row may receive an inferred policy ID. A current progress history must not be labeled as wholly evaluated under this policy.

**Source** remains unbound. Invocation purpose ≠ market-data acquisition provenance.

**Tracking** obligation and gap/censoring treatment are still required before admission persistence. Last-pass envelopes are not interval coverage.

## 7. Storage checkpoint

Application schema remains **v25**. STORAGE_SINGLE_COPY decoder constraints remain: an old v24 reader must not coexist with reference-format writers. This phase adds no writes, no payload fields, and no migration. Live DB size/runway was not measured (live runtime DB not accessed). Unknown capacity is not proven safe capacity. No deploy.

## 8. What this ID is not

Equal IDs do not mean: same source, same opportunity, same accumulated history, independent samples, profitability, complete episode context, or that any existing row was produced under the policy.

`evaluation_context.complete` remains false where previously unproven. `unique_trade_count` remains UNAVAILABLE.

## 9. Updated original-audit A–W matrix

RESOLVED always has the stated prospective/scoped meaning. This contract does not mark H, N, or P resolved because an ID exists.

| ID | Status | Scope / remaining limitation |
| --- | --- | --- |
| A | PARTIALLY RESOLVED | Contracts and fixtures exist; exact-as-run market input/decision history does not. |
| B | FOUNDATION PRESENT, NOT FULLY CONSUMED | P1 IDs exist; episode/analytics ownership absent. Policy ID is not economic identity. |
| C | RESOLVED at P2A scope | Event accounting, not unique fills. |
| D | RESOLVED at P2B ownership boundary | Not independent episode tracking. |
| E | RESOLVED prospectively | Historical oscillation rows unchanged. |
| F | PARTIALLY RESOLVED | Physical progress ownership, not authoritative episode outcomes. |
| G | PARTIALLY RESOLVED | First-INSERT `plan_version_id` only. |
| H | PARTIALLY RESOLVED | Current-evaluator policy contract exists and is ID-addressable; admission, source binding, and interval coverage are missing. Do not relabel history. |
| I | RESOLVED for last accepted qualifying application | Not complete historical as-of evidence. |
| J | PARTIALLY RESOLVED | Last qualifying supplied-prefix only. |
| K | PARTIALLY RESOLVED | Ordinary same-known-anchor fan-out not demonstrated; import ambiguity remains. |
| L | STILL OPEN | Analytics is not episode-owned. |
| M | STILL OPEN / UNAVAILABLE | Unique actual trade/fill occurrence. |
| N | STILL OPEN | No authoritative episode-scoped outcome. |
| O | FOUNDATION PRESENT; SEMANTIC NON-EQUIVALENCE PROVED | Policy IDs distinguish families/parameters; same-opportunity identity and runtime outcome authority remain absent. Newly executed: price-jump and pre-entry evaluator differences. |
| P | STILL OPEN | R0 unit defined; no production admission population. |
| Q | DEFERRED INTENTIONALLY | TARGET_INSIDE_CHOP / target integrity research. |
| R | PARTIALLY RESOLVED PROSPECTIVELY | v25 single-copy on eligible new writes; physical space, history, nested duplication, capacity, rollout remain open. Unchanged by this phase. |
| S | DEFERRED INTENTIONALLY | Regime/context strategy influence. |
| T | DEFERRED INTENTIONALLY | CMC. |
| U | DEFERRED INTENTIONALLY | Setup-family / target / strategy redesign. |
| V | DEFERRED FROM AUTHORITATIVE LEARNING | Performance memory. |
| W | DEFERRED | Champion/challenger. |

## 10. Invariants preserved

- No evaluator, strategy, RR/quality gate, confirmation ownership, public `event_key` / `message_hash`, Telegram, symbol health, CMC, performance memory, or execution-boundary change
- No schema migration or policy backfill
- P0–P3_PREFIX, R0, EVAL_SEMANTICS, and STORAGE_SINGLE_COPY protections remain the existing suites
- `ORDER_EXECUTION_ENABLED=false` default; no withdrawals; Dev-PC Telegram dry-run; no live DB access
- No merge and no deploy

**CORRECTNESS > COMPLETION**
