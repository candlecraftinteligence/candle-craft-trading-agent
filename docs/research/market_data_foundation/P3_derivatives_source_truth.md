# P3 — Derivatives source truth (shadow)

Decision C: instrument/unit semantics and source/acquisition timestamp integrity, together.
This increment does not correct live derivatives decisions. It adds an evidence contract, a deterministic admissibility evaluator, and a scanner-connected shadow classification.

Acceptance claim: the new source-truth contract rejects inadmissible evidence and cannot manufacture a validated derivatives confirmation.

Not claimed: existing live derivatives decisions are causally corrected. Live enforcement is a later increment. Runtime was not accessed or deployed.

## Base

- Approved baseline: `e753fedfe5aa97866668b393214b765fd8465cf3` (ancestor of main; P2 is included).
- Implementation base: `6b1fa7017cb73ee8ae3ec78e34c7c361eb2535bb` (`origin/main` at branch creation).
- Delta since the approved baseline: `c4e780d` / documentation-only multi-environment rules in `AGENTS.md`. No incompatible P2 change. Main was not reset.
- Source schema version stays 27. No migration and no Runtime writer.

## A. Source-to-decision matrix

Capability is not activation. "Scanner-wired" means the scanner calls the method. "Configuration-enabled" means a default or flag can select it. "Runtime activation" is unverified: this increment did not read Runtime configuration, incident logs, or any production database.

| Metric | Exchange | Endpoint and request | Identity and units | Time | Cache | Consumers and decision effect |
| --- | --- | --- | --- | --- | --- | --- |
| Current open interest | Binance USD-M | `GET /fapi/v1/openInterest`, `symbol` | Response `symbol` when present. Field `openInterest`. Counting `open_contract_quantity`. Unit stays unknown until a verified non-inverse instrument with multiplier applicability `not_applicable` upgrades it to `base_asset_quantity` of `baseAsset`. `contractSize` / `contractMultiplier` other than absent or 1 is unverified and is not applied. Lot size is not a multiplier. | `time` is the snapshot time. No exchange response time on this payload. Acquisition is the successful adapter response, from the injected UTC clock. | In-memory TTL 30 seconds. Retention only. | Legacy DTO `unit` remains `N/A`. Scanner stores the number. Enrichment and rejection use the number with no source time. Shadow can classify the evidence and cannot confirm. |
| Historical open interest | Binance USD-M | `GET /futures/data/openInterestHist`, `symbol`, `period`, `limit` | `sumOpenInterest` is the quantity candidate. `sumOpenInterestValue` is quote notional and is never position growth. Same upgrade rule as current OI. Current `openInterest` and historical `sumOpenInterest` may be paired only when instrument, unit, counting, and venue match. | `timestamp` is the series period time, not "a 5-minute change" unless the two selected times are 300000 ms apart. | TTL 30 seconds. | Legacy previous OI remains `history[-2]`. Shadow selects by timestamp. |
| Funding history, also used as the current funding helper | Binance USD-M | `GET /fapi/v1/fundingRate`, `symbol`, `limit` | `fundingRate` is a fraction. `0.0001 = 0.01 percent = 1 basis point`. Values with absolute value `>= 1` are unverified and are not divided by 100. The current Binance market-data reference documents `rateType=Regular` for ordinary funding and `rateType=Special` for stock-dividend-related additional funding. Absence of `rateType` and `fundingRateType` stays ordinary `settled_funding`. A case-insensitive `regular` token is `settled_funding` and records `regular_funding`. `Special` keeps its raw token, is `unsupported_funding_subtype`, and is never relabeled ordinary settled funding. Any other token, including `settled`, `predicted`, and `unexpected`, keeps the raw token and is `unexpected_funding_subtype`. This is not predicted, next-settlement, or annualized funding. | `fundingTime` is the settlement time. Response time is absent on this payload and that absence does not block. | TTL 60 seconds. | Legacy path keeps the rate only. Shadow current-context freshness needs a verified cadence. `exchangeInfo` does not supply one. `GET /fapi/v1/fundingInfo` documents `fundingIntervalHours` and is not called. Unknown cadence leaves the rate historically checkable and the current context unverified. |
| Account long/short ratio | Binance USD-M | `GET /futures/data/globalLongShortAccountRatio`, `symbol`, `period`, `limit` | `longAccount` and `shortAccount` are holder shares. Each supplied share must be finite and inside `[0, 1]`. A zero short account is `invalid_denominator` even when `longShortRatio` is present. Shares that are both in domain must sum to 1 within one fractional unit in the last place of the coarser share. Integer-form text is exact and does not grant a tolerance of one population. An explicit quotient is admitted only when its own half-unit interval overlaps the interval implied by each share's half-unit uncertainty. An absolute share quantum is not the quotient's uncertainty. If that uncertainty includes a non-positive short share, the row is `account_share_precision_unjustified` rather than a proven component conflict. The published row `0.6442 / 0.3558 / 1.8105` is consistent. Independently rounded shares `0.9900 / 0.0100` with quotient `99.4016` are consistent with 99004 long holders and 996 short holders. Shares `1` / `0.2` and `0` / `0.2` are not a holder partition in either integer or padded decimal text. Ratio `4` with `shortAccount` `0` is not. Ratio `9` against shares `0.8` / `0.2` or `0.7` / `0.3` is a component conflict. This is an account population, not position size or taker flow. | Row `timestamp` is retained on the evidence. The legacy return is a `Decimal` and still drops that timestamp. | TTL 60 seconds. | Legacy crowding can still see the explicit number. Shadow uses the ratio only when the row, identity, and share semantics are admissible. An invalid optional ratio does not change an otherwise admissible funding-plus-OI conclusion. |
| Instrument | Binance USD-M | `GET /fapi/v1/exchangeInfo`, `symbol` | One matching symbol. `baseAsset`, `quoteAsset`, `marginAsset`, `contractType`, `deliveryDate`. `marginAsset` different from `quoteAsset` is unsupported inverse. Digits in `1000SHIB` are not a multiplier. A `fundingIntervalHours` field on this payload is ignored. It does not unverifiy the instrument and it does not establish cadence. | Lookup acquisition is separate. A lookup after the cutoff cannot upgrade units. | TTL 300 seconds. File reload cannot resurrect verified provenance. | Shadow only. Failure does not enter production `missing_data`. |
| Open interest | Bybit linear | `GET /v5/market/open-interest`, `category=linear`, `symbol`, `intervalTime`, `limit` | `openInterest` is both sides. `singleOpenInterest` is one side. Both are retained. The validated pair uses both sides and does not halve, double, average, or fall through to the single-side field. Unequal fields are not a conflict and are not required to be 2:1. Unit upgrades to base asset only from verified linear instrument metadata. | Row `timestamp` is the sample time. Envelope `time` is response time only. A missing row timestamp stays missing. | Same 30-second retention. Current and history are this same endpoint. | Legacy DTO still reads `openInterest` only, unit `N/A`. |
| Funding history | Bybit linear | `GET /v5/market/funding/history`, `category=linear`, `symbol`, `limit` | Settled `fundingRate` fraction, same conversion rule. Cadence comes only from `fundingInterval` minutes on instruments-info. | `fundingRateTimestamp` is settlement time. Envelope `time` is response time. | TTL 60 seconds. | Same shadow boundary as Binance funding. |
| Account ratio | Bybit linear | `GET /v5/market/account-ratio`, `category=linear`, `symbol`, `period`, `limit` | `buyRatio` and `sellRatio` are holder shares in `[0, 1]` and must sum to 1 within the same fractional share precision. Integer text such as `1` and `0` does not widen that tolerance. Published examples are `0.49` / `0.51` and `0.4927` / `0.5073`. `buyRatio` `2` is outside that domain and stays unverified. Shares `1` / `0.2` and `0` / `0.2` are `account_share_inconsistent` and do not produce a quotient. A valid pair may produce a long/short quotient above 1. The evidence keeps the row timestamp, both shares, and the derived quotient. The legacy return remains a `Decimal`. | Row `timestamp`. Envelope `time` is response time only. | TTL 60 seconds. | Same shadow boundary as the Binance ratio. |
| Instrument | Bybit linear | `GET /v5/market/instruments-info`, `category=linear`, `symbol` | `LinearPerpetual` or `LinearFutures`. `baseCoin`, `quoteCoin`, `settleCoin`. `deliveryTime` `0` means no dated expiry. `lotSizeFilter` is ignored. A contract multiplier/size field is unverified. Inverse names are unverified. | Lookup acquisition is cutoff-gated. | TTL 300 seconds. | Shadow only. |

Scanner-wired: the methods above are called from `_fetch_optional_market_data` when the corresponding client method exists. Fast mode skips funding history, OI history, and the account ratio; those skips are explicit `fast_mode_skipped` evidence, not zeros.

Configuration-enabled: `ScannerRunConfig.derivatives_source_truth_shadow` defaults to false. It only requests the companion evidence and an in-memory diagnostic. It is not an enforcement switch. There is no setting that turns the shadow verdict into eligibility.

Runtime activation: unverified. Method availability and Dev defaults do not establish what the Runtime process has enabled.

## Disposition of the eight priority concerns

1. Unknown OI unit and counting. Reproduced code-contract defect. Both normalizers leave `OpenInterestDTO.unit` at `N/A`. Shadow keeps the native field unknown until verified instrument metadata supports one interpretation.
2. Notional used as position growth. Reproduced risk, not an automatic production conversion. History keeps `notional_value` separate. No enabled cross-venue OI aggregation was found. Shadow growth uses native quantity only. A notional-only series cannot produce a percentage.
3. Scalar lineage loss. Reproduced code-contract defect. Funding and OI DTOs retain row timestamps; account-ratio normalizers return `Decimal` and drop them. Enrichment input has rates and sizes, not those timestamps. Shadow keeps the row, response, and acquisition facts beside the legacy value.
4. `history[-2]` predecessor. Reproduced code-contract defect on the legacy path, which is intentionally unchanged. An out-of-order three-row series selects index `-2` even when that row is not the predecessor of the newest observation. The shadow path orders by timestamp, validates the newest group and the immediate predecessor group, and documents the actual interval in milliseconds. Conflicting values or provenance in either group exclude the pair. Row order does not choose between 10 percent and 120 percent. A conflicting predecessor is not replaced by an older row. A different instrument at the same timestamp is not a value conflict of the requested series.
5. Acquisition after the decision cutoff. Reproduced structural risk. The scanner resolves `decision_timestamp` before the derivatives fetch. A row that describes an earlier event is still inadmissible when the adapter acquired it after the cutoff. This is not a proven production incident. Shadow reports `acquired_after_cutoff` and does not move the cutoff.
6. Settled funding unlabeled, with fraction and cadence ambiguity. Reproduced labeling gap. The wired endpoint is funding history, but the legacy consumer treats the number as a current rate and does not record cadence. Shadow labels an unlabeled history row `settled_funding`, preserves the fraction, and refuses an assumed eight-hour cadence. Documented `rateType=Regular` is ordinary settled funding. Documented `rateType=Special` stays unsupported and unverified. Unknown tokens stay unverified. Binance cadence stays unknown: `exchangeInfo.fundingIntervalHours` is ignored, and `/fapi/v1/fundingInfo` is not called. Bybit cadence remains `fundingInterval` minutes from instruments-info.
7. Price-only directional support. Reproduced code-contract behavior, still used by strategy diagnostics, setup quality, display, and scores. With funding and OI missing, a real price direction makes `_directional_support` return `True` for both directions. That production rule is unchanged. Shadow support is never `True` for price alone.
8. Cache TTL treated as freshness, and file entries without provenance. Reproduced risk. OI/history TTL is 30 seconds and funding/ratio TTL is 60 seconds. Those are retention policies. Shadow freshness is `cadence_or_verified_period_v1`. File or uninstrumented cache hits do not mint an acquisition time.

Additional traced findings:

- The foundation `derivatives_timestamp_probe` builds a rejection result by hand. The new test drives `enrich_derivatives` and `_derivatives_rejection` on normalized DTOs. The engine still rejects from the numbers while shadow does not confirm.
- Bybit ticker normalization can copy envelope time. That finding is not applied to OI or funding. A missing OI row timestamp stays missing while envelope time remains response time.
- Liquidation coverage already distinguishes disconnected or gapped coverage from a connected empty window. Strategy context remains `research_only`. Unavailable coverage is not zero activity. A covered empty window is zero events on that feed, not exchange-wide zero.
- Regime reads scanner breadth and diagnostics. No direct raw-OI regime input was found. Indirect influence remains possible through scan outcomes. Shadow diagnostics are excluded from the regime input.
- No permitted production evidence was used. None of these items is labeled a proven production defect, and none is a profitability or signal-count claim.

## B. Architecture and shadow boundary

Evidence is an immutable companion. Adapters still return the legacy DTO or `Decimal` from the same response. `*_with_evidence` returns `MetricEvidenceDelivery(value, evidence)`. Acquisition is captured only at a successful adapter response, with an injected clock, and means `successful_adapter_response_boundary`. A failing capture clock returns a missing acquisition fact. It does not raise, and it does not fall through to another clock. A `describe_*` call without an acquisition fact does not sample a clock. Normalizers, cache reads, scorers, replay, and rejection do not stamp acquisition. Transport failures and cancellation still propagate. The successfully fetched legacy value is preserved when evidence attachment fails.

The cache stores the legacy value. In-memory entries also retain the original evidence batch. A hit copies that batch and records a separate cache delivery time. A file reload or an entry without upstream evidence is unverified and has no acquisition time. If cache-delivery attachment fails after the value is already recorded, the legacy value is returned and the evidence batch is unavailable. That failure does not refetch the value or mint a new acquisition.

`evaluate_derivatives_source_truth` is pure. `DerivativesSourceTruthResult` coerces its constants at construction, `dataclasses.replace`, and `from_dict`: `mode` is `shadow`, `enforcement` is `not_active`, and `validated_confirmation` is false. Support values outside `N/A`, `unverified`, `false`, and `eligible_no_conflict_detected` become `unverified`. The shadow feature flag stays configurable and defaults false. No constructor argument turns enforcement on.

The result carries a bounded `lineage` tuple of at most 32 facts and an explicit `lineage_omitted_count`. Selection keeps the instrument and the selected or excluded current OI, predecessor OI, funding, and account-ratio facts before optional context. Context is truncated newest-first, so omitted rows are the oldest context observations. Each fact records the source endpoint, requested and returned identity, native value, unit, counting convention, raw funding subtype, relevant component fields, and the metric, response, acquisition, and cache-delivery timestamps that actually exist. Selected current OI is distinct from the selected predecessor. Missing timestamps serialize as JSON null. Lineage stays on the shadow result and on the explicit DEV JSON artifact. It is absent from public signal dumps, persistence, and economic identity. An evaluation error keeps lineage for inputs that can be summarized. There is no new archive or automatic writer.

The scanner flag defaults false. When false, the legacy fetch path runs and the diagnostic is absent. When true, collection uses the companion methods and `ScannerSymbolResult.derivatives_source_truth_shadow` is `exclude=True`, so ordinary `model_dump` identity payloads omit it. An exception becomes `evaluation_error` and does not alter the legacy scan. DEV review JSON is written only by `write_dev_shadow_artifact` to an explicit `.json` path. The scanner does not call it.

Shadow diagnostics are not production derivatives inputs, `missing_data`, `unverified_data`, strategy or risk eligibility, setup quality, regime breadth, Telegram delivery, lifecycle persistence, or economic identity.

Freshness policy `cadence_or_verified_period_v1`: a point can be historically admissible when its metric meaning is verified and both the metric time and the genuine adapter acquisition are at or before the caller cutoff. Current-context freshness additionally requires a verified funding cadence or a verified series period, and the metric age must be within that allowance. Equal to the cutoff is admissible. Later than the cutoff is not. Response time after acquisition is `response_after_acquisition`. Cache delivery before acquisition is `delivery_before_acquisition`. A missing optional response time stays missing and does not block; Binance OI, funding, and account-ratio payloads omit it. Bybit envelope time is response time only. There is no clock-skew tolerance and no clamping. Cache TTL is not this allowance. Unknown cadence or period is `freshness_unjustified`: the observation can remain historically visible and still cannot support a current-context conclusion.

The scanner cutoff is resolved before the derivatives fetch and is not moved. A fresh response acquired after that cutoff is `acquired_after_cutoff` even when its metric time is earlier. That exclusion is a possession-time limit of this shadow design, not a claim that the exchange row was false.

## C. Before and after

Legacy enrichment, using real normalizers and `enrich_derivatives`:

- Stale funding `0.01` at 2020-01-01 and future OI `110` at 2030-01-01 still reach the enrichment input as numbers. The input has no timestamp field.
- Previous OI from a two-row normalized history is `100`. The engine reports extreme positive funding and rising OI. `_derivatives_rejection("long", ...)` returns a conflict string.
- An unordered series `(50 at T-1h, 110 at T, 100 at T-7m)` makes `history[-2]` return `110`.

Shadow on the same facts:

- Acquisition after the cutoff classifies `acquired_after_cutoff`. `validated_confirmation` is false.
- The ordered pair of `100` then `110` across 420000 ms is a 10 percent native change labeled `420000ms`, not a five-minute change. It is still not a validated confirmation.
- Duplicate newest quantities `110` and `999` produce `duplicate_conflict` and no percentage. Identical `110` duplicates collapse. Predecessor quantities `100` and `50` at one timestamp, followed by `110`, produce `duplicate_conflict` in either row order and no percentage. Identical values with conflicting acquisition provenance also exclude the pair.
- Quantity `2.5` to `2.5` is `0.00000000` percent while quote notional at marks 100 and 110 is `250.00000000` and `275.00000000`.
- Quantity `100` to `110` is `10.00000000` percent.
- `1000SHIB` base quantity `1000` to `1100` is 10 percent of the exchange base asset. It is not divided by 1000.
- Funding `0.0001` displays fraction `0.00010000`, percent `0.01000000`, basis points `1.00000000`, label `settled_funding`.
- Funding `1.5` is `unexpected_funding_magnitude` and is not rewritten as `0.015`.
- A history payload with `rateType=Regular` or `fundingRateType=regular` is `settled_funding`. `rateType=Special` keeps the raw token `Special` and is `unsupported_funding_subtype`. `predicted`, `unexpected`, and the token `settled` keep that raw token and are `unexpected_funding_subtype`. None of those non-regular tokens are relabeled ordinary settled funding.
- Duplicate funding or ratio rows are judged before collapse. Every same-timestamp candidate is checked for identity, subtype, quantity, and temporal or provenance quality. A foreign identity is excluded and does not veto a valid row for the requested instrument. Contradictory essential funding in that local group is not eligible in either row order. An invalid optional ratio stays unused and does not change the funding-plus-OI conclusion. A rejected optional ratio's cutoff codes do not reclassify that conclusion.
- Account shares `1` / `0.2` and `0` / `0.2` are not admitted, including when the same numbers are written with extra decimal places. The rounded example `0.9900` / `0.0100` / `99.4016` is admitted.
- Notional-only rows produce `notional_not_position_growth` and no percentage.
- Inverse, unknown multiplier, wrong symbol, wrong venue, and wrong settlement stay unverified.
- Bybit both-side `100 -> 110` with single-side `50 -> 40` uses 10 percent. The single-side field is retained and is not a conflict.

A price-only legacy call with closes 100 and 110 and missing funding/OI returns `supports_long is True` and `supports_short is True`. The shadow result for verified price and no derivatives evidence is not support and not a confirmation.

## D. Dependencies and reason codes

A combined shadow conclusion needs admissible settled funding and an admissible current-context OI pair. Account ratio and price are used only when those inputs are themselves admissible. An invalid optional ratio is recorded and does not turn an admissible funding-plus-OI result into `conflict_detected` or `supports_long=false`. An essential funding identity failure blocks the combined conclusion and leaves an independently valid OI percentage visible. Request context may establish a missing returned symbol only when it matches the decision instrument, and that provenance stays explicit. Missing numbers stay `N/A`. Unreliable evidence stays unverified. Absence of a conflict is `eligible_no_conflict_detected`, which is not `validated_confirmation`.

Stable reason codes include:

- identity: `symbol_conflict`, `venue_mismatch`, `category_mismatch`, `identity_conflict`, `cross_instrument_not_paired`, `endpoint_incompatible`, `unsupported_inverse`, `multiplier_unverified`, `unit_unverified`, `unit_unknown`
- quantity: `malformed_quantity`, `non_finite_quantity`, `negative_open_interest`, `zero_baseline`, `notional_not_position_growth`, `single_side_not_substituted`, `duplicate_conflict`
- funding and ratio: `unexpected_funding_subtype`, `unsupported_funding_subtype`, `regular_funding`, `contradictory_funding_evidence`, `unexpected_funding_magnitude`, `funding_cadence_unknown`, `invalid_denominator`, `invalid_account_ratio`, `ratio_component_conflict`, `account_share_inconsistent`, `account_share_precision_unjustified`, `account_population`
- time: `missing_metric_timestamp`, `malformed_metric_timestamp`, `ambiguous_metric_timestamp`, `missing_acquisition_timestamp`, `description_without_acquisition_fact`, `acquisition_capture_failed`, `metric_after_cutoff`, `acquisition_after_cutoff`, `metric_after_acquisition`, `metric_after_response`, `response_after_acquisition`, `delivery_before_acquisition`, `malformed_exchange_response_time`, `freshness_unjustified`, `stale_for_current_context`, `instrument_acquired_after_cutoff`, `conflicting_provenance`
- identity binding: `request_context_identity_conflict`, `settlement_mismatch`, `contract_mismatch`
- availability: `fast_mode_skipped`, `adapter_has_no_source_evidence`, `evidence_representation_failed`, `legacy_cache_entry_lacks_acquisition_observation`, `uninstrumented_cache_entry_lacks_acquisition_observation`, `missing_open_interest`, `missing_previous_open_interest`, `missing_funding`, `missing_account_ratio`, `price_only_not_derivatives_support`, `evaluation_error`

Classification priority is `acquired_after_cutoff`, then `conflict_detected`, then `eligible_no_conflict_detected`, then `unverified`, otherwise `not_available`. `evaluation_error` is the fail-safe path.

## E. Cache provenance and instrument identity

- Miss, hit, expiry refetch, and disabled cache are distinct delivery kinds. Disabled cache is `not_applicable` and still keeps the adapter acquisition.
- The original acquisition survives a later hit. Delivery time changes. The same batch evaluated before and after its acquisition time produces `acquisition_after_cutoff` only for the earlier cutoff.
- File rehydration returns the legacy OI value and an unverified batch with no acquisition timestamp.
- Concurrent requests keep the clock passed to that call. A retried HTTP 500 then 200 stamps only the successful response.
- Missing response identity binds to the verified request and says so. A conflicting response symbol is not overwritten.
- `BTCUSDT` on Binance is not the same evidence identity as `BTCUSDT` on Bybit. USDT is not rewritten to USD. A perpetual is not a dated future. Scaled base assets stay the exchange base asset.

## F. Legacy preservation

With the shadow flag off and on, mocked scanner runs match on status, rejection, missing and unverified lists, funding, open interest, setup quality, enrichment, score, risk, regime state, trade-idea count, dry-run alert count, and journal count. The shadow object is absent from `model_dump`. Fast mode and an injected evaluation exception keep that public payload. Schema identification on a temporary database remains 27, and the new name is not in `sqlite_master`. `mint_plan_version_id` is unchanged. `observation_from_symbol_result` does not change when the excluded shadow field is attached.

The production `_directional_support` rule, RR gates, and public quality thresholds are not modified.

## G. BTCUSDT producer lineage

Verified code path: `producer_acquisition_from_symbol_result` reads `lifecycle_execution_batch_delivery`, then `lifecycle_batch_handoff.delivery`, then `producer_acquisition_from_delivery`. That function reads adapter `observed_completed_at`, walking `parent` and cache `upstream_acquisition`. It does not read derivatives OI or funding timestamps. `evaluate_symbol_origin` fail-closes when `producer_observed_at` is missing. This PR does not change `app/runtime_epoch/origin.py`, origin grants, or epoch repair.

Reproduced loss mechanisms, in code and fixtures, without a Runtime incident claim:

- No execution delivery and no handoff yields no producer time.
- `unavailable_delivery` can carry an observed time and still yields no producer time because it has no adapter ancestry.
- An empty execution without a parent yields no producer time. An empty execution that keeps its parent still resolves the parent adapter time, not the later empty-execution clock.
- A cache-shaped delivery with no `upstream_acquisition` yields no producer time. That is the file-cache / uninstrumented loss shape.
- A derivatives timestamp attached to the symbol result does not become `producer_observed_at`.

Not confirmed: which of those mechanisms, if any, caused a recurring Runtime BTCUSDT incident. Distinguishing them needs permitted Runtime evidence that was not accessed: the symbol result's execution delivery and handoff, whether the candle came from an adapter observation or a file-cache hit, and the stored origin block reason. A derivatives timestamp must not be substituted for candle `producer_observed_at`.

## H. Residual risks before any live-enforcement decision

- Fresh fetches after the already chosen cutoff are `acquired_after_cutoff` by construction. Turning this check on without a possession-time design would change many decisions. That policy is not chosen here.
- Legacy `history[-2]`, unlabeled funding, and price-only `True` support still affect production scores and rejections.
- Binance current and historical OI are treated as quantity-compatible only after instrument verification. If exchange documentation later distinguishes those quantity definitions, the compatibility set must be revised before enforcement.
- Binance's current market-data reference is `https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data`. It documents funding-history `rateType=Regular` for ordinary funding, `rateType=Special` for stock-dividend-related additional funding, and `fundingIntervalHours` on `GET /fapi/v1/fundingInfo`. This implementation does not call `fundingInfo` and does not infer cadence from `exchangeInfo`. An earlier third-party OpenAPI snapshot did not list `rateType`; that snapshot is not the source for the subtype rule. `GET /fapi/v1/openInterest` returns `openInterest`, `symbol`, and `time`. The account-ratio route is outside the USD-M market-data page cited above; the share example `longAccount 0.6442`, `shortAccount 0.3558`, `longShortRatio 1.8105` remains covered by the share-precision rule. Binance current and historical OI quantity compatibility remains an adapter-contract assumption, not a newly proven exchange identity.
- Bybit field meanings follow the current public v5 pages: open interest `https://bybit-exchange.github.io/docs/v5/market/open-interest`, funding history `https://bybit-exchange.github.io/docs/v5/market/history-fund-rate`, instruments-info `https://bybit-exchange.github.io/docs/v5/market/instrument`, and account ratio `https://bybit-exchange.github.io/docs/v5/market/long-short-ratio` (the path used by the client is `/v5/market/account-ratio`; the account-ratio URL 404s). `openInterest` is both sides and `singleOpenInterest` is one side. Linear unit examples are base coin; inverse examples are quote coin. `fundingInterval` is minutes. Envelope `time` is response time. The funding-history payload does not carry cadence or a rate subtype. Both account-ratio shares are against total holders, and the shadow quotient is long share over short share.
- Runtime configuration and live derivatives behavior remain unverified.
- Enforcement must be a separate reviewed change. It must not be activated by renaming this shadow flag.

## Scope

Included: evidence and instrument models, Binance and Bybit companion reads, cache provenance, the shadow evaluator, scanner diagnostics, tests, and this note.

Excluded: order execution, a general derivatives platform, new indicators, cross-venue aggregation, inverse trading support, liquidation feed redesign, schedulers, storage migrations, historical research pipelines, predicted-funding endpoints, Runtime epoch repair, and any live eligibility change.

## I. Review corrections

Independent review of the first head required seven corrections. Each one is closed in this branch. None of them activates enforcement.

| ID | Closed behavior |
| --- | --- |
| R1 | A capture-clock failure and a cache-delivery attachment failure keep the already fetched legacy open interest for Binance and Bybit, with shadow off and on. Evidence becomes unavailable. The value is not refetched. HTTP 500 stays missing data. `CancelledError` still propagates. A missing evidence method and malformed instrument metadata keep the legacy value. |
| R2 | The immediate predecessor group is conflict-checked in either row order, including when the current point is unit-unverified. Conflicting provenance does not keep the convenient sibling. There is no fall-through to an older row and no `history[-2]` on the validated path. Notional growth is not position growth. |
| R3 | Venue, requested symbol, returned symbol, and known category, contract, and settlement metadata are validated together. An ETH ratio, a foreign-venue ratio, or funding bound to another instrument by request context cannot support the BTC conclusion. Upstream semantic failures are retained. |
| R4 | Description without an acquisition fact does not sample a clock. Response-after-acquisition and delivery-before-acquisition are rejected. Missing, malformed, and ambiguous timestamps stay missing. Binance's absent response time does not block. The pre-fetch scanner cutoff is unchanged, so a post-cutoff acquisition is `acquired_after_cutoff`. |
| R5 | Account shares must be a consistent partition of the holder population before a quotient is accepted. Unknown funding subtypes stay raw and unverified. `exchangeInfo.fundingIntervalHours` is not Binance cadence. The later C2 rule replaces the integer-scale tolerance from this correction. |
| R6 | `mode`, `enforcement`, and `validated_confirmation` are coerced at construction, replacement, and supported deserialization. The feature flag still defaults false. |
| R7 | Selected and excluded observation facts are bounded lineage on the shadow result and the explicit DEV JSON artifact. Missing timestamps are null. Public dumps, persistence, and economic identity do not include that result. The later C3 rule is what keeps the actual selected rows inside that bound. |

## J. Second-review closure

Independent review of head `4c304874b07eb40dafd193a02985adc765f54588` required three further corrections. Each one is closed in shadow mode. None of them activates enforcement or changes schema 27.

| ID | Defect on the reviewed head | Closed behavior |
| --- | --- | --- |
| C1 | Equal-valued funding and ratio duplicates changed admissibility with row order. Only the first sorted sibling was checked for subtype or identity. | Every same-timestamp candidate is validated before selection. Foreign identities are excluded and do not veto a valid requested row. Contradictory essential funding, including an unknown subtype beside an ordinary row, is ineligible in either order. Invalid optional ratios stay unused. Optional-ratio cutoff codes do not reclassify an admissible funding-plus-OI result. Identical valid duplicates still collapse. OI predecessor conflicts stay order-independent. The Bybit BTC control, with `fundingInterval` 480 minutes and OI `100` to `110`, is the eligible positive control. |
| C2 | Integer-form shares `1` / `0.2` and `0` / `0.2` were accepted because an integer string granted a tolerance of 1. The same numbers with four decimal places were rejected. A compatible four-decimal rounding, `0.9900` / `0.0100` / `99.4016`, was labeled a component conflict. | Integer text contributes no population slack. Sum tolerance is one fractional unit of the coarser share. Quotient uncertainty is the half-unit interval of each component, not the share quantum itself. Impossible partitions are `account_share_inconsistent` and unused. Precision that cannot justify a quotient is `account_share_precision_unjustified`. No trading threshold was widened. |
| C3 | Lineage sorted by role and ascending time, then kept the first 32 facts. A 40-row OI series calculated `138` to `139` while retaining `100` through `129`. One hundred funding rows displaced the instrument and both selected OI facts. Both rows of a two-point pair were labeled selected current. Rejected subtype and ratio components were dropped. | The bound reserves instrument, selected current OI, selected predecessor OI, selected funding, and selected ratio, then excluded evidence, then newest context. `lineage_omitted_count` reports what the bound dropped. Facts keep endpoint, requested and returned identity, native quantity, components, unit, counting, normalization, raw subtype, and the timestamps that exist. Missing timestamps stay null. |

Preserved: R1 exception isolation, R2 OI predecessor behavior, R4 timestamp rules, R6 shadow constants, the pre-fetch cutoff, schema 27, public quality gates, and the explicit DEV artifact route. `mode` stays `shadow`, `enforcement` stays `not_active`, and `validated_confirmation` stays false. The shadow flag still defaults false.

Source rules used here: Binance market data at `https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data` for `Regular`, `Special`, and `fundingInfo`; Bybit open interest, funding history, instruments-info, and long-short ratio pages cited above. `fundingInfo` is not collected. Binance current-versus-history OI compatibility stays an unresolved adapter assumption. The separate BTCUSDT `producer_observed_at_missing` investigation is unchanged: no Runtime root cause is claimed and no epoch repair is implemented.

## Validation

Targeted and full commands are recorded in the PR. The R1–R7 regressions in `tests/test_derivatives_source_truth_corrections.py` remain passing. The C1–C3 acceptance cases in `tests/test_derivatives_source_truth_c1_c3.py` failed on reviewed head `4c304874b07eb40dafd193a02985adc765f54588` and pass on this head. Tests use injected clocks, mocked HTTP, and temporary databases. No live prices, production databases, or Telegram delivery were used. Runtime was not accessed or deployed. No profitability or signal-count claim is made. Live derivatives decisions are not claimed to be causally corrected.
