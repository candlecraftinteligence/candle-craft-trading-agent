# D. Open interest, funding, positioning, book, and liquidations

## MDF-OI-001

- classification: code_contract
- severity: medium
- production incident: not established
- summary: Bybit openInterest is stored without a unit label. A differing singleOpenInterest is not halved.
- expected: The DTO records both-sides versus single-side and the unit. Differing fields are not silently averaged.
- observed: repository value 20 unit N/A; divided_by_two=False; disposition=unverified. Bybit's public open-interest page retrieved 2026-10-19 defines openInterest as the sum of both sides and singleOpenInterest as one side. For linear BTCUSDT the documented unit is BTC.
- consumers: app.data.normalizers.bybit.normalize_bybit_open_interest, derivatives enrichment
- evidence: synthetic:bybit-oi-both-fields
- follow-up: Record the applicable Bybit schema field and unit before any cross-venue OI comparison.
- reproduction: bybit_open_interest_fields

## MDF-PX-001

- classification: code_contract
- severity: low
- production incident: not established
- summary: Bybit ticker DTO timestamp copies the response envelope time when no measurement time exists.
- expected: Envelope time and measurement time stay separate. A missing measurement time stays missing.
- observed: copied_envelope_time=True measurement_timestamp=None
- consumers: app.data.normalizers.bybit.normalize_bybit_ticker
- evidence: synthetic:bybit-ticker-envelope
- follow-up: Store response time separately and leave measurement time N/A when the payload has none.
- reproduction: bybit_ticker_timestamp_source

## MDF-BOOK-001

- classification: methodology
- severity: info
- production incident: not established
- summary: A REST snapshot alone cannot prove diff-stream synchronization.
- expected: Synchronization is not_applicable without a sequenced diff stream.
- observed: synchronization=not_applicable crossed=True on a synthetic bid 100 / ask 99 snapshot
- consumers: app.microstructure.order_book.parse_binance_depth_snapshot
- evidence: synthetic:crossed-snapshot
- follow-up: Keep order-book research on the existing snapshot-plus-diff implementation.
- reproduction: order_book_snapshot_diagnosis

## MDF-LIQ-001

- classification: code_contract
- severity: high
- production incident: not established
- summary: No received liquidation events is missing coverage, not zero liquidations.
- expected: Empty or absent streams stay N/A unless coverage proves there were no events.
- observed: disposition=missing event_count=None
- consumers: app.microstructure.liquidation_service
- evidence: synthetic:empty-liquidation-stream
- follow-up: Do not let an unsubscribed or gapped stream satisfy a liquidation feature.
- reproduction: liquidation_observation(None)

## MDF-FUND-001

- classification: code_contract
- severity: low
- production incident: not established
- summary: CCI funding reads are last settled history, not the predicted premium-index rate.
- expected: Settled and predicted rates stay distinct.
- observed: BinanceFuturesClient.get_funding_rate uses /fapi/v1/fundingRate history. premiumIndex is not requested.
- consumers: app.analytics.derivatives_enrichment
- evidence: code:binance_futures.py:get_funding_rate
- follow-up: Name the consumer value last settled funding unless a predicted endpoint is added and tested.
- reproduction: inspect BinanceFuturesClient.get_funding_rate

## Numerical contracts

Linear example, synthetic: 2.5 base units at mark 100000 USDT is 250000 USDT notional. At mark 110000 the notional is 275000 USDT and the base position change is 0. That notional change is not position growth. The quote unit stays USDT.

Inverse example, synthetic: 1000 contracts with a 1 USD face at price 100000 is 0.01 coin. At price 50000 the coin exposure is 0.02 while the contract count is unchanged.

Funding example: fraction 0.0001 is 0.01 percent and 1 basis point. A fraction at or above 1 is incompatible with the usual per-interval fraction convention and is not divided by 100 automatically.

Bybit `openInterest` and `singleOpenInterest` are both retained in the diagnosis. The repository stores `openInterest` and does not divide by two. If the fields differ, or the DTO unit is N/A, the counting convention is unverified.

Binance `globalLongShortAccountRatio` and Bybit `account-ratio` are account populations. They are not top-trader position ratios and not taker flow.

Order-book synchronization is not applicable for a snapshot that has no diff sequence. A partial liquidation stream does not prove total liquidations. A modelled heatmap is not an observed event.

Production incident status for these code contracts: not established.
