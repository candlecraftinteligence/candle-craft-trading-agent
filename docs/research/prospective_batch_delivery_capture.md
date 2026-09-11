# CCI PROSPECTIVE_BATCH_DELIVERY_CAPTURE — In-Memory Candle-Batch Observation and Handoff

**Status:** Capture-only instrumentation on the ordinary candle path. In-memory association of local producer observations with the actual sequence reaching the lifecycle evaluator handoff.
**Does not** persist capture bytes, mint source/admission/episode IDs, bind evaluator policy, authenticate a venue, prove possession at cutoff, or change strategy/evaluator/public behavior.
**Base commit:** `8485f033abad3333269a8f26554abade4bae5ef5` (`main`, merge of PR #119 / SOURCE_EVIDENCE_BOUNDARY).
**Schema:** application `SCHEMA_VERSION` / SQLite `PRAGMA user_version` = **25** unchanged. This phase adds zero durable capture columns, tables, cache-file fields, or scan-payload fields.

Predecessor: [Source-evidence boundary](source_evidence_boundary.md) defined the unused unbound descriptor and proved where acquisition evidence disappeared. This phase implements the in-memory envelope at those seams. It does not consume or upgrade that descriptor.

Fixtures in `tests/test_prospective_batch_delivery_capture.py` are labeled synthetic. No live or existing scan database was read. Local timestamps and occurrence tokens are process-local observations, not venue publication time or durable identity.

## 1. Purpose and original P5 / P6 / P14

Original P6 required capturing causal source inputs. A later historical download cannot establish what runtime possessed earlier. Bounded capture may begin before full P5.

Original P5 still requires explicit admission, tracking obligation, coverage, and episode-owned reconciliation. This phase does not create that unit.

Original P14 (compatible rollout, capacity, retention) remains a gate before any durable capture writer. This phase adds no durable capture bytes, so current runtime disk runway is not a prerequisite for the DEV instrumentation. Unknown runway remains an explicit future gate.

R0's unit remains one explicitly admitted plan-evaluation episode. Capture repairs loss of locally observable batch evidence on the path toward that unit. It does not create the unit.

## 2. Semantic contract

Module: `app/data/candle_batch_evidence.py`. Format version: `cci-candle-batch-delivery-v1`.

### 2.1 Snapshot

The snapshot is a deeply isolated projection of supported candle input fields used to identify the data actually filtered, resampled, and supplied for evaluation:

Included when present: `exchange`, `symbol`, `interval`, `timestamp`, `open_timestamp`, `opened_at`, `close_timestamp`, `closed_at`, `open`, `high`, `low`, `close`, `volume`, `quote_volume`, `trade_count`.

Excluded: `raw_source`, HTTP bodies, retry history, headers, request bodies. This is not a lossless raw-response archive. Projection equality is not raw-response equality.

Decimals are preserved. Missing required identity/OHLCV fields are listed explicitly. Unsupported shapes yield `unavailable` capture while existing market-data validation is unchanged. Nested caller mutation of excluded `raw_source` or of the original mapping after snapshot does not change the frozen projection.

A SHA-256 fingerprint covers this declared projection only. It is not source authentication, a delivery identifier, or a same-opportunity key.

### 2.2 Occurrence

Each local return gets an ephemeral `occurrence_token` (`secrets.token_hex(16)`). Equal OHLC and equal wall-clock samples do not merge deliveries. The token is not a `source_id`, `source_namespace`, `admission_id`, `evaluation_episode_id`, or market event ID. It is never persisted.

### 2.3 Adapter observation

`BinanceFuturesClient.get_klines` still returns `list[CandleDTO]`. The shared `_get_klines_delivery` observes after `/fapi/v1/klines` and `normalize_binance_klines`, immediately before return.

Recorded: requested vs effective (clamped) symbol/interval/limit, endpoint path, adapter class label, local completion after normalize, normalized-batch snapshot, injected-transport label, explicit limitations.

Not recorded as authority: venue publication time, first-ever candle receipt, caller receipt, HTTP retry ledger, fixture-vs-live inference.

`get_klines_with_delivery` is used only when the instance `get_klines` is the same-path method as the capture owner. An overridden `get_klines` yields the weaker client-return observation. Bybit and arbitrary injected clients are not required to implement the capability.

### 2.4 Cache delivery

`MarketDataCache.get_or_fetch` remains the generic list/value return. Candle capture uses `get_or_fetch_with_local_delivery` and `CachedMarketDataClient.get_klines_with_delivery`.

| Path | Local fact | Upstream acquisition |
| --- | --- | --- |
| Cache disabled | Current client return; cache `not_applicable` | Preserve instrumented upstream if present |
| Miss | This invocation fetched | This fetch only |
| Expiry-refetch | Expired entry then fetched | New fetch; never inherit expired acquisition |
| Hit on exact in-process captured entry | New hit observation | May retain that entry's acquisition |
| File-cache hit after restart | Known hit | Acquisition unavailable; `created_at` is not a substitute |
| Hit on uninstrumented insertion | Known hit | Acquisition unavailable |
| Unwitnessed history | Delivery history unknown | No fabricated classification |

`created_at` remains the pre-fetch TTL bookkeeping sample. Capture completion is sampled after fetch/deserialize on a separate capture clock. Capture metadata never enters `_entries`, file-cache JSON, scan JSON, SQLite, or logs.

Ephemeral association is keyed by `id(entry)` and bounded by currently retained entries. Concurrent same-key misses each receive evidence for their own returned value. Later replacement cannot rewrite already-returned carriers. No single-flight or new locking was added.

### 2.5 Time semantics

Scanner `clock`, cache TTL `_now`, and capture clocks are separate. Wall-clock samples are not a trusted total order; regression is preserved. `possession_at_logical_cutoff` is never established.

Distinct clocks: candle event time, scanner logical cutoff, cache bookkeeping, adapter completion, cache delivery, transform/filter observation, evaluator-handoff observation.

### 2.6 Filtering and 2d lineage

Closed-candle selection records supplied cutoff, ordered **exact** membership indices, and the output snapshot. Exact membership requires object identity with the parent returned sequence (`closed_candles_as_of` preserves `item.source`). Projection equality of a replacement/copy is recorded only as `projection_equivalent_indices` plus `closed_selection_projection_equivalent_not_exact_membership`; it does **not** set `membership_complete=True` or `lineage_complete=True`. Slicing is not identity-preserving merely because some prices match.

Both 2d routes are instrumented:

1. `_fetch_primary_candles` when primary interval is `2d`
2. `_fetch_strategy_timeframe_candles` when HTF is `2d`, including reuse of an already-closed primary `1d` batch

`resample_ohlcv_candles` is not rewritten. A derived 2d batch is a transformation label, not a new exchange acquisition and not proof the parent was a synthetic fixture. Incomplete pairs remain dropped by existing resample semantics; lineage is explicitly incomplete when parent mapping cannot be verified.

### 2.7 Scanner and lifecycle handoff

Carriers travel through `_StrategyExecution.execution_batch_delivery` and `ScannerSymbolResult.lifecycle_execution_batch_delivery` (`exclude=True`). Ordinary `model_dump` / `store_scan_result` omit them.

At `SetupLifecycleService._apply_to_symbol_result_with_meta`, immediately before `evaluate_closed_candle_outcomes` when `final_record` and `execution_candles is not None`, the service associates the carrier with the actual execution tuple, timeframe, and cutoff. The frozen `delivery.snapshot` remains the captured projection. Handoff always projects the **current** execution candles and compares that projection to the captured one. Object identity may help identify the same batch; it does **not** certify a match after in-place mutation of an included field. Reorder of two or more candles with the same timeframe/cutoff is `mismatched` (`execution_projection_does_not_match_captured_batch`). Mutation of excluded `raw_source` alone does not create a projected mismatch. Missing or mismatched carriers yield `mismatched` / `unavailable`. The evaluator signature and semantics are unchanged. Direct-supplied `ScannerSymbolResult` objects cannot invent adapter/cache history.

None is not an empty tuple. `not_run` is not a successful evaluation. Capture runs before setup outcome selection and includes rejected/no-setup flows. That is not a research denominator.

### 2.8 Failures

Capture is not a quality gate. Representation/clock failure returns explicit unavailable metadata and preserves the business path. Upstream fetch, normalize, integrity, timeout, and cancellation exceptions still propagate. `BaseException` is not caught to manufacture a return.

## 3. Evidence classes

| Case | Class |
| --- | --- |
| Binance `get_klines` / `get_klines_with_delivery` through mocked HTTP | Ordinary producer-reachable, synthetic payload |
| `CachedMarketDataClient` miss/hit/expiry/disabled/concurrent/file reload | Ordinary producer-reachable, synthetic client |
| ScannerRunner closed filter, 2d routes, no-strategy paths, rejected/no-setup | Ordinary producer-reachable, `FakeExchangeClient` or mocked HTTP |
| ScannerRunner → `SetupLifecycleService.apply_to_run_result` | Ordinary producer-reachable orchestration, synthetic market data |
| Direct-constructed `ScannerSymbolResult` | Direct-writer / supplied state |
| Mutated mappings, reversed candles, mismatched TF | Malformed / adversarial |
| Unused `source_evidence` / `evaluation_policy` production imports | Source-inspection-only plus existing contract tests |

A hand-built `ScannerSymbolResult` does not prove ordinary scanner production. Mocked `/fapi/v1/klines` proves routing with synthetic data, not authentic Binance receipt.

## 4. Memory and storage

- Zero application schema delta (v25)
- Zero persisted capture/source/policy/admission fields
- Unchanged cache-file representation (`version` + `entries` with existing keys)
- Unchanged logical scan payload and v25 reference reconstruction
- Additional memory is per currently retained cache entry plus in-flight carriers; discarded runs drop carriers with the result objects
- Synthetic association-size checks: `capture_association_size() <= entries` after repeated hits, expiry-refetch, and replacement

These measurements are not runtime HDD throughput, live scan p95, or free-space evidence.

## 5. Compatibility and rollback

Public `get_klines` still returns a list. Generic cache consumers still use `get_or_fetch`. Evaluator policy IDs are unchanged for unchanged effective parameters. `SourceEvidenceDescriptor` remains unused by production modules and always `binding_status=unbound`; assessment authority/possession fields remain unavailable.

Rollback is revert of this branch's production instrumentation, tests, and this document. No database rewrite, version downgrade, or live DB copy is required. `SCAN_RAW_PAYLOAD_INLINE_ONLY=true` remains a decoder-capable-release tool for payload encoding, unrelated to this in-memory capture.

Next operational checkpoint before any durable capture writer: deployed SHA, decoder-capable readers/writers, operator-approved configuration, current DB/WAL/archive footprint and free-space trend, retention/restore, compatible rollback. Live evidence was not collected from DEV.

## 6. Tests

`tests/test_prospective_batch_delivery_capture.py` covers adapter clamping, cache delivery kinds, concurrency, restart, both 2d routes, closed filtering, no-strategy TF match/mismatch, ordinary handoff, direct/None/empty/`not_run`/fetch failure, snapshot isolation, same-object included-field mutation (rejected), excluded-field mutation (still matched), multi-candle reorder with identical timeframe/cutoff, replacement-copy membership incompleteness, ordinary closed-path exact membership, capture-clock failure, unbound source/policy, override-safe weaker observation, accepted and rejected flows, and bounded association lifetime.

## 7. Updated A–W matrix

RESOLVED always has the stated prospective/scoped meaning.

| ID | Status after this phase | Scope / remaining limitation |
| --- | --- | --- |
| A | PARTIALLY RESOLVED | Local batch observations captured on scoped candle paths; no retained as-run history, external authenticity, or original possession proof |
| B | FOUNDATION PRESENT, NOT FULLY CONSUMED | Unchanged; no episode/analytics consumption |
| C | RESOLVED at P2A scoped semantics | Unchanged; not unique actual fills |
| D | RESOLVED at P2B boundary | Unchanged; not episode-owned tracking |
| E | RESOLVED prospectively | Unchanged; historical rows preserved |
| F | PARTIALLY RESOLVED | No new authoritative owner |
| G | PARTIALLY RESOLVED | Prospective first-INSERT scope; no history/episode binding |
| H | PARTIALLY RESOLVED | Actual batch-to-handoff local association added; policy/source authority binding, admission, and interval coverage absent |
| I | RESOLVED for latest accepted qualifying application | Unchanged; not full historical as-of or possession evidence |
| J | PARTIALLY RESOLVED | Latest qualifying application only; not coverage |
| K | PARTIALLY RESOLVED | Ordinary same-known-anchor defect not demonstrated; import ambiguity remains |
| L | STILL OPEN | No episode-owned consumer |
| M | STILL OPEN / UNAVAILABLE | Capture is not a fill |
| N | STILL OPEN | No admission/context/coverage authority |
| O | FOUNDATION PRESENT; SEMANTIC NON-EQUIVALENCE PROVED | Local capture prerequisite improved; policy families remain distinct; retained causal dataset and same-opportunity identity absent; no runtime outcome authority |
| P | STILL OPEN | No admitted population, tracking obligations, or authoritative reconciliation |
| Q | DEFERRED INTENTIONALLY | Unchanged |
| R | PARTIALLY RESOLVED PROSPECTIVELY | No new durable capture bytes; historical duplication/allocation, WAL/archive, live runway, and rollout remain open |
| S | DEFERRED INTENTIONALLY | No strategy influence added |
| T | DEFERRED INTENTIONALLY | Unchanged |
| U | DEFERRED INTENTIONALLY | Unchanged |
| V | DEFERRED FROM AUTHORITATIVE LEARNING | Unchanged |
| W | DEFERRED | Unchanged |

## 8. Invariants preserved

- No evaluator, strategy, RR/quality gate, confirmation ownership, public `event_key` / `message_hash`, Telegram, symbol health, CMC, performance memory, or execution-boundary change
- No schema migration, source/policy backfill, or historical attribution
- P0–SOURCE_EVIDENCE_BOUNDARY protections remain the existing suites
- `ORDER_EXECUTION_ENABLED=false` default; no withdrawals; Dev-PC Telegram dry-run; no live DB access
- No merge and no deploy

This phase does not complete original P5 or P6.

**CORRECTNESS > COMPLETION**
