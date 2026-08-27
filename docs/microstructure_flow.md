# Binance USD-M microstructure flow

This phase consumes the public Binance USD-M aggregate-trade stream only. It does
not place orders, use credentials, read a runtime database, or persist the trade
firehose.

## Stream contract

The service connects to `wss://fstream.binance.com/market/stream` and manages
lower-case `{symbol}@aggTrade` subscriptions. The current USD-M payload fields
used are:

| Field | Meaning | Use |
| --- | --- | --- |
| `e` | event type | must be `aggTrade` |
| `E` | event time, milliseconds | validated diagnostic timestamp |
| `s` | symbol | per-symbol routing |
| `a` | aggregate trade ID | deduplication and gap detection |
| `p` | price | quote-notional and price return |
| `q` | total quantity, including RPI trades | primary flow quantity |
| `nq` | normal quantity, excluding RPI trades | separate RPI diagnostic |
| `f`, `l` | first and last underlying trade IDs | compact trade count |
| `T` | trade time, milliseconds | UTC minute bucketing |
| `m` | buyer is maker | aggressor-side classification |
| `st` | stream type | `1` USD-M accepted; `2` COIN-M rejected |

Binance defines `m=true` as buyer-maker, so the seller is the taker and the
observation is an aggressive sell. `m=false` is an aggressive buy.

Production parsing requires `st=1`. A deliberately named compatibility switch
may accept a missing `st` only for legacy recordings or fake payloads whose
USD-M transport is independently known. It is disabled by default. Detectable
COIN-M data is never accepted.

`q` is the primary quantity because it represents all market-trade quantity.
`nq` is never added to it. The service records normal quote notional and the
derived RPI-inclusive difference separately so research can compare them.

Official contract references:

- <https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market>
- <https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect>
- <https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Important-WebSocket-Change-Notice>

## Aggregation and CVD definition

Every accepted event updates one in-memory UTC minute bucket selected by Binance
trade time. Price and quantity are `Decimal` values. Quote notional is exactly
`price * q`; delta is aggressive-buy quote notional minus aggressive-sell quote
notional. Empty observed minutes inside continuous connection coverage contribute
zero delta, while an unobserved or compromised interval never masquerades as a
zero-volume verified window.

Snapshots use the last complete 1, 5, and 15 UTC minutes. For an N-minute window,
minute deltas are `d1...dN`. The rolling CVD ending value is anchored at zero for
that window and equals `sum(d1...dN)`. Its momentum is the ordinary least-squares
slope of the cumulative path `(0, 0), (1, d1), ...,
(N, sum(d1...dN))`, measured in quote currency per minute. It is not dependent on
process lifetime.

Price return uses the first accepted price in the window and the last accepted
price in the window. The factual alignment labels are:

- `ALIGNED_UP`: price return and CVD are positive.
- `ALIGNED_DOWN`: price return and CVD are negative.
- `PRICE_UP_CVD_DOWN`: price is positive and CVD is negative.
- `PRICE_DOWN_CVD_UP`: price is negative and CVD is positive.
- `MIXED_FLAT`: either measure is zero or otherwise flat.

These labels are research observations, not strategy gates.

## Coverage, reconnects, and freshness

A full window requires uninterrupted observed connection coverage beginning no
later than the window start, at least one valid event, and a last-event age within
the configured stale threshold. Startup warm-up, disconnects, reconnects,
aggregate-ID gaps, and trade-time regressions reset trustworthy coverage. Duplicate
or older aggregate IDs are discarded. Missing volume is never synthesized.

Snapshots use the shared context statuses `VERIFIED`, `STALE`, `UNAVAILABLE`, and
`ERROR`, plus an exact reason such as `insufficient_window_coverage`,
`connection_gap_in_window`, or `last_valid_event_stale`.

## Runtime and storage bounds

`run_scan.py --watch` owns one long-lived service and injects it into every
`ScannerRunner`. BTCUSDT is always selected. Universe additions and removals are
subscribed and unsubscribed in place. The configured cap is deterministic: BTC is
first, then symbols sort lexically. Overflow symbols receive an explicit
`subscription_limit_exceeded` unavailable snapshot; none are silently dropped.

The WebSocket library queue is capped at 32 messages and messages at 64 KiB. The
application adds no raw-event queue. Each selected symbol retains at most 16
minute buckets (15 complete minutes plus the current minute), and raw payloads are
discarded immediately after aggregation. Neither raw events nor minute buckets are
written to SQLite, JSON logs, audit logs, or scanner results. Only the compact
per-symbol snapshot is attached to existing scanner output.

The default cap of 100 symbols therefore bounds aggregation state at 1,600 minute
buckets. At the allowed configuration ceiling of 1,024 symbols it is 16,384
buckets. Exact representative serialized size and measured Python-state estimates
are reported by the test/PR verification.

## Research-only isolation

Verified CVD and orderflow inputs carry `usage=research_only`. Existing momentum,
absorption, derivatives-conflict, scoring, gate, grade, risk/reward, lifecycle, and
Telegram paths explicitly ignore those inputs. They remove only the truthful
optional `cvd: N/A` and `orderflow_summary: N/A` labels. Non-verified snapshots are
not injected. Unavailable, warming, overflow, disconnected, and error states remain
optional missing diagnostics; stale or gap-compromised observations remain optional
unverified diagnostics. `microstructure_flow` is explicitly optional at the final
CONFIRMED data-health boundary, while unknown future fields continue to fail closed.

## Optional manual smoke procedure

On a disposable development session, enable `MICROSTRUCTURE_FLOW_ENABLED=true`,
run a single bounded watch iteration, inspect the compact health/snapshot output,
and stop it immediately. This procedure is intentionally not run by the automated
test suite and was not run during implementation.
