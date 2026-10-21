# P4 — Causal derivatives evidence and shadow observability

P4 records what derivatives evidence a completed per-symbol bundle can support at two labeled cutoffs. It does not change a live decision, score, grade, confirmation, rejection, or lifecycle transition.

The reproduced limitation is causal, not a bad threshold. `ScannerRunner.run` resolves `decision_timestamp` before market-data collection and does not move it. Optional derivatives and the instrument lookup are acquired after that cutoff. P3 therefore reports `acquisition_after_cutoff` (classification `acquired_after_cutoff`) for evidence whose source time is already in the past. That result stays the strategy-cutoff audit. A later research observation can admit the same frozen bundle only as later research, with `validated_confirmation` false.

Not claimed: improved signal accuracy, profitability, or positive expectancy. Shadow mode stays `shadow`, enforcement stays `not_active`, and `validated_confirmation` stays false.

## Baseline

- Documented P3 Runtime release and `origin/main` at branch creation: `95f4f352509b43708c263635d8401e7058398736`.
- No commits exist between that SHA and the branch point. Main was not reset.
- Schema version stays 27. No migration, no production writer, and no Runtime database access.
- `derivatives_source_truth_shadow` still defaults to false.
- The supplied October 21, 2026 scanner/listener report is operational evidence only. This phase did not inspect or operate Runtime.

## Astra review corrections

Reviewed head `29b1c720ac3e7890284f2b1fbae6bb56c33e3ba8` requested changes. The corrections stay inside the original shadow boundary: no new endpoint, no background service, no longer production timeout, no second validator, and no change to strategy thresholds or lifecycle.

- Production isolation: do not collect shadow instrument metadata while it can share the production client or extend a symbol deadline. Record the metadata as unavailable.
- Conflict counts: every retained identity and time group is checked with the selectors' conflict rules. A conflicting older group is not admissible. Latest-pair selection is unchanged. Keep the raw-row denominator and the P3 `duplicate_conflict` result.
- Research pairs: count a research quantity change only when the selected OI pair was delivered by the research observation.
- Construction isolation: a malformed instrument or a bundle-construction failure remains shadow evidence and leaves the production scan status unchanged.

## Source-to-consumer timing

| Label | Fact | Where it is taken | What it does not prove |
| --- | --- | --- | --- |
| A | Source measurement or event time | Payload field (`time`, `timestamp`, `fundingTime`, row timestamp) | That any component had fetched it |
| B | Exchange response or envelope time | Present only when the payload has it. Binance OI, funding, and ratio payloads usually omit it. Bybit envelope `time` is response time, not metric time | A simultaneous snapshot |
| C | Successful adapter acquisition | Injected capture clock at the successful response, stored on `AcquisitionFact` | Cache insertion or consumer visibility |
| D | Cache provenance, retention, delivery | Original acquisition is preserved on a hit. Delivery is a separate clock sample. File reload and uninstrumented entries do not restore acquisition. Retention instant is not on the evidence clock. TTL is retention only | That the strategy consumed the item by its cutoff |
| E | Logical production strategy decision cutoff | `config.decision_timestamp`, resolved before collection | A research observation |
| F | Historical replay decision cutoff | The cutoff recorded in the artifact inputs | Replay wall time, file modification time, or a generated cutoff |
| G | Actual P4 research observation | One injected capture-clock sample after derivative deliveries and the instrument attempt finish or are cancelled | The original strategy decision |

Asynchronous responses keep their own timestamps. The record reports the acquisition span and sets `simultaneous_exchange_snapshot` false. One cutoff applies to the whole bundle.

Global-context BTC optional fetch withholds instrument metadata. It runs beside the BTC candle requests, so a metadata lookup would share their client. Its bundle is not a symbol-iteration study row. The symbol scan that follows still uses the original decision timestamp.

## Architecture

One immutable `FrozenDerivativesBundle`, then two calls to the existing pure evaluator `evaluate_derivatives_source_truth`. The evaluator parameter remains `decision_cutoff`. Serialized assessments label `cutoff_role` as `original_strategy_decision_cutoff` or `research_observation_cutoff`, and `evaluator_parameter` as `decision_cutoff`.

Assessment A is the P3 result at cutoff E. The scanner still calls `evaluate_derivatives_source_truth` from `scanner_runner` for that result, so an evaluation failure stays an `evaluation_error` on the strategy audit. Assessment B runs only when the observation clock is valid and the observation is at or after E. Clock failure, a malformed cutoff, or an observation before E yields an unavailable research assessment. There is no fallback clock and no clamping.

The research admission count adds one requirement the P3 evaluator does not have: included deliveries must have been delivered to this consumer at or before G. That count does not rewrite the embedded P3 classification. A cache hit after E can show earlier collector acquisition and later retrieval. It does not show that the original strategy consumed the item by E. Cache retention is `not_recorded` because bookkeeping `created_at` is not an evidence-clock timestamp.

Shadow metadata does not run on the production client while a symbol deadline or global-context collection is active. The scanner records `instrument_lookup_withheld`, with acquisition time missing, and does not start `get_instrument_metadata`. That avoids occupying a shared connection and avoids extending a production deadline. A direct caller with no deadline and no withhold flag may perform one lookup after the optional gather returns; both assessments reuse that result. No second instrument fetch and no new endpoint.

A malformed instrument object is replaced with `instrument_representation_invalid` before bundle construction reads its reason codes. If construction still fails, the scanner keeps the legacy values already fetched and emits `shadow_bundle_construction_failed` with a missing observation cutoff. It does not sample a replacement clock or fetch metadata again. `CancelledError` still propagates.

Study admission validates every retained identity and time group with the same open-interest collapse, funding conflict, ratio conflict, and provenance helpers the selectors use. That validation is separate from choosing the latest OI pair or the latest funding and ratio measurement. A `duplicate_conflict`, `conflicting_provenance`, or `contradictory_funding_evidence` mark removes that group from the admissible count. An older conflict does not remove a valid latest pair or its verified quantity change. Identical duplicates stay admissible. Another venue or instrument is not part of the conflicting group. Raw received rows and distinct unique observations stay in their denominators, and observations outside the conflicting group stay admissible. The embedded P3 classification is not rewritten and is not cleared to make the counters match.

Research valid-pair and verified-change counters increment only when the selector's current and previous OI observations, and the batches that carry them, were delivered at or before the research observation. Equality with the observation cutoff counts. A missing delivery stamp does not. Strategy-role pair counters follow the labeled P3 evaluation. Delivery after the observation can leave that P3 change in place while the research counter stays at zero.

`write_dev_causal_shadow_artifact` is an explicit DEV JSON route. The scanner does not call it. Scanner bundles do not retain inputs, so a scanner study is diagnostic: `replay_complete` is false and it cannot report complete verified coverage. The synthetic fixture retains inputs and can be replayed from the recorded cutoffs without sampling a clock.

Public symbol dumps exclude `derivatives_source_truth_shadow` and `derivatives_causal_observation`. Those objects are not inputs to regime, prioritization, lifecycle, identity hashes, or journal payloads.

### Rejected alternatives

- Moving `decision_timestamp` forward so the reproduction passes.
- A second validator with different admission rules.
- Streaming, polling, durable prefetch, or a new snapshot subsystem.
- New endpoints, including `fundingInfo`, or paid providers.
- Treating TTL or cache bookkeeping time as historical availability.
- Extending production deadlines or changing production collection to hide shadow work.
- Storing research time in a field named as the strategy decision cutoff.

## Contracts that stay fixed

- Reason code `acquisition_after_cutoff`. Classification text `acquired_after_cutoff`. Equal to a cutoff is admissible; one microsecond later is not.
- OI native quantity stays separate from quote notional. Changes require compatible venue, instrument, units, and counting. The reported interval is the actual elapsed time.
- Binance `exchangeInfo` does not establish funding cadence. A fixture that needs current-context eligibility sets `funding_interval_ms` and labels that assumption `synthetic_binance_funding_interval_ms`.
- Settled, predicted, special, and unknown funding subtypes stay distinct.
- Account ratio remains holder-account share semantics. An invalid optional ratio does not erase admissible OI or funding.
- Policy `p4_causal_shadow_observation_v1`. Freshness policy remains `cadence_or_verified_period_v1`.
- Observation role `later_research_observation`. Boundary `completed_per_symbol_derivatives_bundle_after_delivery_and_instrument_attempt`. Mode `injected_capture_clock_at_bundle_boundary`.
- Constructors, `dataclasses.replace`, and replay deserialization coerce mode, enforcement, validated confirmation, trading-performance claim, policy, and observation role back to the shadow constants.

## Availability claims

Each metric slot and the instrument metadata carry, for each cutoff role:

- collector acquired before the cutoff
- cache retained before the cutoff (`not_recorded`)
- consumer delivered before the cutoff
- later cache retrieval of an earlier acquisition

Direct adapter responses are not cache deliveries. A missing delivery stamp stays `missing_cache_delivery_capture`. Delivery after E is `consumer_delivery_after_strategy_cutoff`. Delivery after G is `delivery_after_observation`. File reload and shadow-off warm entries keep `legacy_cache_entry_lacks_acquisition_observation` or `uninstrumented_cache_entry_lacks_acquisition_observation` and `cache_value_does_not_restore_acquisition`. Instrument consumer delivery is `not_recorded`. Keys remain isolated by venue, symbol, endpoint, and request parameters. Production cache keys, TTLs, and eviction are unchanged.

## Study denominators

Populations are `requested_venue={venue}|scan_mode={fast|full}`. Rates are not pooled across venues, modes, or cutoff roles. An empty denominator is `N/A`. Overlapping reason counts set `percentages_are_not_additive` and are not a partition of 100%.

| Denominator | Unit | Rule |
| --- | --- | --- |
| Study attempts | symbol iteration | Every targeted iteration, including errors and skips |
| Metric opportunities | metric slot | Five slots per attempt: open interest, OI history, funding rate, funding history, account ratio |
| Received source rows | source row | Rows in received slots before deduplication |
| Unique observations | unique observation | Identity, series, and time deduplication inside each attempt. Conflicting values stay distinct |
| Semantically verified observations | unique observation | After instrument visibility at that cutoff |
| Causally admissible observations | unique observation | Temporal and semantic gates, excluding every retained identity/time group the shared conflict rules reject; research also requires recorded consumer delivery at or before G |
| Fresh observations | admissible unique observation | Freshness among the admissible population only |
| OI pair opportunities | assessed attempt | One per attempt assessed at that cutoff. Strategy-role valid and verified changes follow the labeled P3 evaluation. Research-role changes also require delivery of the selected pair |
| Attempts assessed at both cutoffs | symbol iteration | Mutually exclusive with unassessed, research-clock unavailable, and evaluation error |

Metric slot dispositions are mutually exclusive: received, skipped, unsupported, failed, empty, unassessed. Early scan errors stay in the attempt denominator as `errored` rather than disappearing. `complete_verified_coverage` is true only when inputs are retained, every attempt is assessed at both cutoffs, every slot was received, and every unique observation is verified and causally admissible at both cutoffs. A conflicting duplicate remains a received row and a distinct unique observation, and it makes this flag false. The other admissible rows stay visible in the admissible numerator.

The synthetic study (`build_synthetic_causal_fixture`, code revision `synthetic-fixture-not-a-release`) has three attempts: one full BTC observation, one unassessed ETH scan error, and one fast BTC skip. Its full population is 2 attempts and 10 slots. Strategy-cutoff admissible observations are 0 of 5. Research-cutoff admissible observations are 5 of 5. The verified-coverage fixture acquires 30 seconds before E and observes 1 second after E, and it is the case that reports complete verified coverage. Rejected evidence is a data-quality finding, not a market-condition label.

## Measured overhead

Workload: the synthetic hero bundle, 6 observations, 5 metric slots, 30 in-process samples. Python 3.11 on the Dev PC. No network, no instrument fetch, and no background task. These are measured samples, not a pass/fail latency gate.

| Path | min | p50 | max |
| --- | ---: | ---: | ---: |
| P3 `evaluate_derivatives_source_truth` | 343 µs | 414 µs | 2,493 µs |
| P4 `assess_frozen_bundle` | 1,088 µs | 1,452 µs | 10,112 µs |

Serializing that one retained-input study allocated a tracemalloc peak of 421,025 bytes and an artifact of 60,331 bytes. P4 evaluates the same bundle twice and builds availability claims, so the median is higher than one P3 evaluation. The max includes ordinary timer noise on this machine.

A symbol deadline does not start a slow instrument lookup. The tested case uses a 30-second metadata sleep, a 2-second symbol timeout, and a 10-second request timeout. Shadow ON keeps the production status, does not call metadata, does not add an instrument miss to production `missing_data`, and does not add a cache miss. Kline request counts match shadow OFF. The same withholding applies to global context, which collects BTC candles and BTC derivatives on one client.

## Limitations

- Scanner studies omit retained inputs. Bounded evaluator lineage is not a complete replay record. Recomputation requires the fixture or another artifact with `replay_complete`.
- Research admissibility is stricter than P3 because it requires recorded consumer delivery and conflict marks for every retained identity and time group. The embedded P3 classification is not rewritten to match that count. Latest-pair selection still ignores an unused older conflict.
- Research pair counters are stricter than the embedded P3 change. Strategy-role pair counters still follow that labeled evaluation.
- Symbol scans and global context do not collect instrument metadata. A shadow bundle from those callers has `instrument_lookup_withheld` and no metadata acquisition time. Direct no-deadline callers can still perform one post-gather lookup.
- A malformed metadata object or a bundle-construction exception stays in the shadow record. It does not change the production status.
- Code revision on scanner records is `N/A`. It is not a live git SHA.
- No representative prospective coverage, latency distribution from Runtime, or outcome study exists yet.

## Before any production enforcement

Enforcement is not implemented and is not authorized by this phase. A later decision would need all of the following, separately approved:

- An approved production causal-availability contract.
- Representative prospective coverage across venues, instruments, and regimes.
- Measured missingness, staleness, provenance, and latency.
- Resolution of the open unit, identity, cadence, and adapter assumptions, including Binance funding cadence.
- Leakage-free replay and out-of-sample outcome analysis.
- Adequate samples, uncertainty, transaction-cost assumptions, and selection-bias analysis.
- A trial with rollback and explicit enforcement criteria.

A shadow study establishes evidence quality and observability. It does not establish a trading improvement.
