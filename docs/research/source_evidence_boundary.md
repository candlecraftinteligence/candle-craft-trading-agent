# CCI SOURCE_EVIDENCE_BOUNDARY — Executable Source-Evidence Contract and Producer Boundary Proof

**Status:** Unused contract library + synthetic producer-boundary proofs. No production consumer of this descriptor.
**Successor:** [Prospective batch-delivery capture](prospective_batch_delivery_capture.md) transports local in-memory observations on the candle path. It does not bind this descriptor, upgrade assessment fields, persist source evidence, or create admission.
**Does not** capture acquisition, persist source evidence, mint source IDs/namespaces, bind policy IDs, admit episodes, change evaluators, or alter scanner/cache/lifecycle behavior.
**Base commit:** `7455e36b3988bc50a8f0974204e3c43394688310` (`main`, merge of PR #118 / EVALUATION_POLICY_MANIFEST).
**Schema:** application `SCHEMA_VERSION` / SQLite `PRAGMA user_version` = **25** unchanged. Fresh synthetic `PRAGMA schema_version` is a distinct SQLite catalog revision. No admission, episode, policy-hash, or source-namespace columns exist.

PR **#117** was closed without merge and is not implementation or architectural authority.

Fixtures in `tests/test_source_evidence_boundary.py` are labeled synthetic. No live or existing scan database was read. Harness-observed fetch times are synthetic execution evidence, not an owned acquisition instrument.

This phase makes CCI's source-evidence limits executable before any producer is allowed to bind source context to a research episode. A validated descriptor is always **UNBOUND**. Neither a validated descriptor nor an evaluator-family name supplies source authority.

## 1. Purpose and original P5 / P6 / storage dependency

Original P5 required independently following admitted open plans, or accounting for tracking gaps, then reconciling mature covered unambiguous observations.

Original P6 required capturing causal inputs and reproducing decisions/outcomes under explicit semantics. Historical data downloaded later can support a disclosed reconstruction; it cannot establish what CCI originally possessed.

The original storage objective required lossless prospective deduplication with compatible readers. Capacity and rollout remain separately controlled. The historical ~81.21 GiB observation is not current disk-runway evidence. Live DB size was not measured here.

R0 defined the future unit as one admitted plan-evaluation episode under a declared source context and fixed evaluation-policy semantics. EVAL_SEMANTICS specified current evaluator meaning. EVALUATION_POLICY_MANIFEST made current evaluator semantics an immutable hashed invocation contract. This phase answers the missing source questions:

1. What is the declared purpose of an evaluation?
2. Where did its supplied market data purportedly originate?
3. How was that data delivered on this invocation?
4. Which times describe candle events, logical cutoffs, cache bookkeeping, or processing?
5. What evidence would a future producer need to establish actual acquisition and possession?
6. Where does that evidence currently disappear before evaluation and storage?

These remain separate questions. Policy identity is unchanged by source declarations.

## 2. Contract-only scope

Delivered:

- `app/research/source_evidence.py` — builders, validation, canonical UTF-8 JSON, factual-support assessment
- `tests/test_source_evidence_boundary.py`
- this document

Not delivered and not authorized:

- runtime imports or call-site wiring
- acquisition receipt capture, envelope transport, sidecar storage
- source ID / namespace minting
- policy binding, admission, tracking obligation, episode identity
- schema/migration/CI/dependency/evaluator/strategy/public changes

Canonical bytes exist so a descriptor is inspectable and deeply immutable. They are **not** a source ID, opportunity identity, or possession identity. Equal declared fields do not equalize receipt histories.

## 3. Semantic contract

Every successful parse/build returns `binding_status=unbound`. There is no caller-controlled `authoritative`, `source_verified`, `complete`, `witnessed`, or `bound` flag. Those keys are rejected.

Validation certifies structure. `assess_source_evidence()` reports factual support and never upgrades a declaration into witnessed provenance.

### 3.1 Declared evaluation purpose

Invocation purpose is not evaluator family.

| Token | Meaning |
| --- | --- |
| `scanner_observation` | Ordinary scanner/lifecycle observation path |
| `replay_experiment` | Replay/simulation experiment |
| `direct_evaluation_reconstruction` | Direct evaluator input or disclosed reconstruction |
| `unspecified` | Purpose not declared |

Replay may consume recorded runtime data. A runtime evaluator may consume synthetic candles. Purpose does not imply origin.

### 3.2 Declared input origin

| Token | Meaning |
| --- | --- |
| `externally_supplied` | Caller supplied the batch without claiming CCI acquired it |
| `synthetic_fixture` | Test/harness fixture |
| `historical_reconstruction` | Later-downloaded or reconstructed history |
| `acquisition_claim` | Caller claims an acquisition occurred, with required limitations |

`acquisition_claim` requires the limitation tokens:

- `local_completion_is_not_venue_publication_time`
- `class_identity_is_not_venue_proof`
- `config_exchange_is_not_provenance`
- `cutoff_is_not_possession`

Optional additional tokens may name injected transport, cache reuse, normalized exchange fields, or legacy cache entries. Limitations are declarations that the claim is incomplete. They do not create an evidence chain.

### 3.3 Delivery path

| Token | Meaning |
| --- | --- |
| `direct_supplied_input` | Evaluator/service received the batch as an argument |
| `adapter_return_path` | Returned from an exchange-adapter method |
| `cache_return_path` | Returned from `MarketDataCache.get_or_fetch` |
| `transformed_resampled_input` | Derived batch (currently synthetic 2d resample) |
| `unknown` | Delivery not declared |

Synthetic 2d resampling is a transformation of a 1d source batch. It is not automatically a fictitious market fixture, and it is not a new venue observation.

Cache delivery kind, when `delivery_path=cache_return_path`:

| Token | Meaning |
| --- | --- |
| `miss` | This invocation fetched |
| `hit` | Returned a stored batch without a new fetch |
| `expiry_refetch` | Prior entry expired; this invocation fetched again |
| `unavailable` | Hit/miss/expiry not known (including legacy entries) |
| `not_applicable` | Required when delivery is not the cache path |

Equal returned OHLC does not make miss, hit, and expiry-refetch the same receipt history. A cache hit is not a fresh exchange observation.

### 3.4 Temporal claims

Each of the following must be present as `declared`, `unavailable`, or `not_applicable`:

| Kind | What it may name | What it is not |
| --- | --- | --- |
| `candle_open_time` | Candle open event time | Receipt or possession |
| `candle_close_time` | Candle close event time | Pre-cutoff possession |
| `logical_decision_cutoff` | Scanner/evaluator as-of boundary | Acquisition or receipt |
| `processing_time` | Local evaluation/processing clock | Cutoff or possession |
| `cache_bookkeeping_time` | `created_at` sampled before `await fetch()` | Response completion |
| `declared_acquisition_observation` | Caller-declared local completion | Venue publication, first receipt of every candle, original historical availability, or owned chain |

Declared values are UTC ISO-8601 strings or non-negative epoch milliseconds. Naive datetimes are rejected. The contract does not consult the wall clock, environment, pytest module names, or network.

`cache_bookkeeping_time` is `not_applicable` off the cache path. On the cache path it may be declared or unavailable; it may not be treated as not-applicable.

### 3.5 Labels versus provenance

`venue_label`, `adapter_class_label`, and `config_exchange_label` are optional audit associations. They cannot supply provenance authority. Python class paths are insufficient: constructors accept injected transports and clients. Configured `exchange=binance` can coexist with an injected fixture client.

If venue and config-exchange labels are both declared and differ, the descriptor stays valid, the conflict is recorded, and neither label is chosen as authentic venue provenance.

Script names, DB filenames, `scan_run_id`, git SHA, policy IDs, and `app.storage.run_provenance` safe-config hashes are also not source authority. Policy IDs offered as payload fields are rejected.

### 3.6 Assessment (always)

| Field | Value |
| --- | --- |
| `binding_status` | `unbound` |
| `witnessed_acquisition_chain` | `unavailable` |
| `possession_at_logical_cutoff` | `unavailable` |
| `authentic_venue_provenance` | `unavailable` |
| `admission_authority` | `unavailable` |
| `original_historical_availability` | `unavailable` |

A positive test may prove a declaration is well formed and immutable. It may not certify historical truth.

## 4. Producer / evidence-class matrix

Evidence classes:

1. Ordinary producer-reachable state, synthetic/mocked external data
2. Direct evaluator / import-supported supplied state
3. Malformed / adversarial state
4. Source-inspection-only

| Source anchor | Established fact | Evidence | Consequence |
| --- | --- | --- | --- |
| `ScannerRunner.run` | Missing `decision_timestamp` is resolved from the runner clock **before** subsequent `get_klines` completes | Class 1: fake clock + advancing client | Logical cutoff, not receipt or original possession |
| Closed candles `close <= cutoff` | Eligible as-of the cutoff | Class 1 | Does not prove the process possessed those candles before cutoff |
| `ScannerRunner._exchange_client_for` | Injected `exchange_client` precedes `config.exchange`; cache may wrap | Class 1: inject fixture client with `exchange=binance` | Configured venue and wrapper names do not authenticate data |
| `BinanceFuturesClient.get_klines` | Default route `/fapi/v1/klines` + `normalize_binance_klines` | Class 1: mocked HTTP | Selected endpoint is known; payload remains synthetic; `exchange=binance_futures` is a normalizer field |
| `MarketDataCache.get_or_fetch` | `created_at` sampled before `await fetch()` | Class 1: clock advanced during fetch | Bookkeeping, not completion/receipt |
| Cache miss / hit / expiry | First fetch; hit without fetch; expiry is a new attempt | Class 1 | Equal OHLC ≠ equal delivery history |
| File cache restart | Hit can return a prior serialized batch | Class 1 | Legacy entries have `created_at` and no acquisition observation |
| `CachedMarketDataClient.get_klines` | Returns candle data only | Class 1 / 4 | Hit/miss provenance is not an evaluator-bound envelope |
| Synthetic 2d resample | `resample_ohlcv_candles` derives 2d from closed 1d | Class 1 | Transformation, not a new venue product |
| `_StrategyExecution` / `ScannerSymbolResult` | Execution candles, TF, and cutoff flow toward lifecycle; those fields are `exclude=True` | Class 1 dump + stored payload | v25 lossless decoding cannot recreate evidence never serialized |
| `SetupLifecycleService._apply_to_symbol_result_with_meta` | Transitions/plan handling precede `evaluate_closed_candle_outcomes`; evaluation requires non-`None` execution candles | Class 1/mixed | Future evaluation/admission seam, not a source owner |
| `apply_to_run_result` | `not_run` is skipped | Class 1 | No fabricated source observation |
| Fetch failure / empty list | Scanner error; empty list → integrity unverified | Class 1/2 | Failure is not successful acquisition |
| `StrategyReplayEngine.run` / `_simulate_trade` | Caller-supplied candles; distinct discovery/simulation semantics | Class 1/2 | Replay purpose ≠ original runtime acquisition |
| `app.storage.run_provenance` | Safe config/code provenance for runs | Class 4 | Reproducible code/config ≠ witnessed market-data acquisition |
| Policy IDs | Unchanged when source declarations differ | Class 2 | Source is not policy identity |

Reproduce these facts with synthetic traces. Do not promote them into claims about current live behavior, deployed configuration, or actual historical receipt times.

## 5. Where evidence currently disappears

| Boundary | What is owned locally | What is dropped |
| --- | --- | --- |
| Adapter return | HTTP path, normalized DTO batch, local await completion in a harness | Completion time, request/response identity, transport authenticity |
| Cache return | hit/miss/expiry counters, `created_at`, deserialized value | Envelope tying delivery kind + completion to **this** batch |
| 2d transform | Derived OHLC | Link to the 1d source batch and its delivery |
| Scanner symbol result | In-memory `lifecycle_execution_*` | Omitted from `model_dump()` and therefore from `store_scan_result` |
| Lifecycle evaluator | Supplied candles + cutoff + `evaluated_at` | Any acquisition envelope (none exists) |
| Replay | Caller candle map | Runtime opportunity/source binding |

Current producers can own limited execution facts at a witnessed boundary. They cannot, through this contract, certify original possession at the decision cutoff.

## 6. Future capture seam (not implemented)

The smallest truthful next interface is **not** a global latest-source value, run-level venue label, or shared cache counter. Concurrent symbols and timeframes mix hits, misses, and transforms.

Exact seam, in call order:

1. **Adapter return:** `BinanceFuturesClient.get_klines` after `_get_json("/fapi/v1/klines")` and `normalize_binance_klines`, immediately before return. Witnessable: this method, this symbol/interval/limit, local await completion, the returned list object. Not witnessable: venue publication time, first receipt of every historical candle, authenticity of an injected `http_client`/`base_url`.
2. **Cache delivery:** `MarketDataCache.get_or_fetch` after `value = await fetch()` (or on hit, after deserializing the stored value), before return. Witnessable: miss vs hit vs expiry-refetch for this cache key, `created_at` as bookkeeping, a **new** local completion timestamp sampled after fetch returns, the `value` object. Current `created_at` must not be relabeled. Legacy file entries without an acquisition observation remain unverified.
3. **Transformation:** `ScannerRunner._fetch_primary_candles` when `exchange=binance` and primary/HTF interval is `2d`. Witnessable: that the returned 2d batch was resampled from a named 1d source batch. The 2d list is not a second venue observation.
4. **Evaluator handoff:** `SetupLifecycleService._apply_to_symbol_result_with_meta` at the `execution_candles is not None` guard, immediately before `evaluate_closed_candle_outcomes`. This component could consume an envelope that traveled with the batch. It cannot invent one. Ordinary serialization still omits the candles.

Evidence that must travel **with the actual returned input batch**:

- symbol, interval, request limit/params
- delivery path and cache kind for this batch
- local completion observation for this fetch attempt, if a fetch occurred
- explicit limitations (injected transport, cache reuse, local-only clock)
- separately named logical cutoff for the evaluation that will consume the batch

A new acquisition/attempt record on retry or restart would **not** create a new admission. Admission remains STOP.

Future consumer of such an envelope: a later source-binding step that could accompany or precede admission. It would still lack: an explicit pre-outcome admission decision, obligation owner, continuation rules, coverage ledger, and episode identity.

This phase does not implement the envelope, receipt capture, sidecar, scheduler, or migration. If capture is later authorized, it must stay confined to the seams above.

## 7. Admission, policy, replay, and expectancy gates (unchanged)

Admission implementation remains STOP.

R0 unit remains: one explicitly admitted plan-evaluation episode from an evidenced admission boundary under a declared source context and fixed evaluation-policy semantics.

- A successful validator is not source context.
- A caller string is not source context.
- Current policy IDs remain unused and unbound.
- Source declarations must not churn an unchanged evaluator-policy ID.
- Replay may be an explicitly disclosed simulation; it is not runtime outcome authority.
- Unique real trade/fill occurrence remains unavailable.
- Canonical episode outcome remains unavailable.
- Trustworthy expectancy denominator remains unavailable.
- No global `UNIQUE(plan_version_id)` outcome constraint.

## 8. Storage checkpoint

Application schema remains **v25**. Fresh synthetic `PRAGMA user_version=25`. SQLite `PRAGMA schema_version` remains a distinct catalog revision. Ownership constraints unchanged:

- `setup_lifecycle_outcome_progress UNIQUE(lifecycle_id, plan_identity)`
- `setup_outcome_analytics UNIQUE(lifecycle_id, final_outcome)`

No source/policy/admission columns or tables. STORAGE_SINGLE_COPY decoder constraints remain: an old v24 reader must not coexist with reference-format writers. No deploy. Live DB size/runway was not measured.

Rollback is removal/revert of the unused module, tests, and this document on the development branch. It does not require rewriting a database or reverting a deployed reader.

## 9. Updated original-audit A–W matrix

RESOLVED always has the stated prospective/scoped meaning. This contract does not upgrade H, N, O, or P because a descriptor can be constructed.

| ID | Status | Scope / remaining limitation |
| --- | --- | --- |
| A | PARTIALLY RESOLVED | Contracts and fixtures exist; original as-run input/possession history remains incomplete. This phase makes the missing possession facts executable, not present. |
| B | FOUNDATION PRESENT, NOT FULLY CONSUMED | P1 IDs exist; episode/analytics ownership absent. |
| C | RESOLVED at P2A scope | Event accounting, not unique fills. |
| D | RESOLVED at P2B ownership boundary | Not independent episode tracking. |
| E | RESOLVED prospectively | Historical oscillation rows unchanged. |
| F | PARTIALLY RESOLVED | Physical progress ownership, not authoritative episode outcomes. |
| G | PARTIALLY RESOLVED | First-INSERT `plan_version_id` only. |
| H | PARTIALLY RESOLVED | Evaluator identity available; source/admission binding and interval evidence absent. Validated source descriptors remain unbound. |
| I | RESOLVED for last accepted qualifying application | Not complete historical as-of evidence. |
| J | PARTIALLY RESOLVED | Last qualifying supplied-prefix only. |
| K | PARTIALLY RESOLVED | Ordinary same-known-anchor fan-out not demonstrated; import ambiguity remains. |
| L | STILL OPEN | Analytics is not episode-owned. |
| M | STILL OPEN / UNAVAILABLE | Unique actual trade/fill occurrence. |
| N | STILL OPEN | No authoritative episode-scoped outcome. |
| O | FOUNDATION PRESENT; SEMANTIC NON-EQUIVALENCE PROVED | Policy IDs distinguish families; same-opportunity identity and runtime outcome authority remain absent. Source declarations are orthogonal to policy identity. |
| P | STILL OPEN | R0 unit defined; no production admission population; source binding not implemented. |
| Q | DEFERRED INTENTIONALLY | TARGET_INSIDE_CHOP / target integrity research. |
| R | PARTIALLY RESOLVED PROSPECTIVELY | v25 single-copy on eligible new writes; physical space, history, nested duplication, capacity, rollout remain open. Unchanged by this phase. |
| S | DEFERRED INTENTIONALLY | Regime/context strategy influence. |
| T | DEFERRED INTENTIONALLY | CMC. |
| U | DEFERRED INTENTIONALLY | Setup-family / target / strategy redesign. |
| V | DEFERRED FROM AUTHORITATIVE LEARNING | Performance memory. |
| W | DEFERRED | Champion/challenger. |

## 10. Invariants preserved

- No evaluator, strategy, RR/quality gate, confirmation ownership, public `event_key` / `message_hash`, Telegram, symbol health, CMC, performance memory, or execution-boundary change
- No schema migration, source/policy backfill, or historical attribution
- P0–EVALUATION_POLICY_MANIFEST protections remain the existing suites
- `ORDER_EXECUTION_ENABLED=false` default; no withdrawals; Dev-PC Telegram dry-run; no live DB access
- No merge and no deploy

**CORRECTNESS > COMPLETION**
