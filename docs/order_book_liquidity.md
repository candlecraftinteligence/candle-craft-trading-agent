# Binance USDⓈ-M visible order-book liquidity

Phase 4 adds a bounded, in-memory, research-only local order book for Binance
USDⓈ-M public depth. It does not change signal selection, entries, stops, targets,
risk/reward, grades, lifecycle, or Telegram eligibility.

## Official contract audited

Contract audit date: 2026-08-28.

- Public diff depth: `{symbol}@depth@{updateSpeed}` on
  `wss://fstream.binance.com/public/stream`.
- Supported update rates: 100 ms, 250 ms, and 500 ms.
- Required fields: `E`, `T`, `s`, `U`, `u`, `pu`, `b`, `a`, `ps`, and `st`.
- After the Binance CM migration, `st=1` identifies USDⓈ-M and `st=2`
  identifies COIN-M. CCI accepts only integer `st=1`.
- REST initialization: `GET https://fapi.binance.com/fapi/v1/depth` with valid
  limits 5, 10, 20, 50, 100, 500, and 1000.
- Current request weights by limit are 2, 2, 2, 2, 5, 10, and 20.
- Standard public depth excludes Retail Price Improvement (RPI) orders. This
  phase does not call or combine the separate RPI depth contract.

Official references:

- [USDⓈ-M public WebSocket streams](https://developers.binance.info/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/public)
- [Current local order-book procedure](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/How-to-manage-a-local-order-book-correctly)
- [USDⓈ-M REST market data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)

## Synchronization semantics

For each maintained symbol, CCI subscribes before requesting a REST snapshot and
buffers diff events in arrival order. After the snapshot arrives it:

1. discards buffered events whose `u < lastUpdateId`;
2. requires the first remaining event to satisfy
   `U <= lastUpdateId <= u`;
3. applies absolute price-level quantities from that bridging event;
4. requires each later forward event's `pu` to equal the previously applied
   event's `u`;
5. ignores already-applied duplicates or old events (`u <= current update id`)
   before checking forward continuity;
6. inserts or replaces a level for quantity greater than zero and removes it for
   quantity zero.

An invalid bridge, forward `pu` gap, buffer overflow, crossed book, or reconnect
immediately revokes synchronization. Trusted levels are cleared, VERIFIED output
stops, and a new snapshot bootstrap is scheduled. Previous local state is never
carried across reconnects and CCI never guesses through a gap.

## Default load contract

- Update speed: 500 ms. The scanner consumes research context at a five-minute
  cadence, so the slower current Binance rate avoids the 5x event load of 100 ms.
- Snapshot limit: 500 levels per side, request weight 10. This provides five times
  the initialization coverage of limit 100 for twice its weight, while using half
  the weight and approximate book memory of limit 1000.
- Symbol cap: 100. BTCUSDT is always first, followed by the current queued universe
  in deterministic lexical order. Overflow symbols are explicitly UNAVAILABLE.
- Bootstrap concurrency: 2, with snapshot starts spaced by at least 0.5 seconds.
  A 100-symbol cold start therefore consumes 1,000 request weight over roughly 50
  seconds rather than issuing a reconnect storm.
- Bootstrap attempts: 3 with exponential backoff, bounded jitter, and `Retry-After`
  support. Failures are isolated per symbol.
- Per-symbol pre-snapshot buffer: 256 events. Overflow fails synchronization closed.
- WebSocket transport queue: 32 messages; maximum message size: 64 KiB.

The feature and all load knobs are disabled or bounded in `.env.example`.

## Metrics and coverage

The canonical reference is the synchronized book:

- `best_bid`, `best_ask`;
- `mid_price = (best_bid + best_ask) / 2`;
- absolute spread and spread in basis points.

Visible quote notional is `price × quantity`, calculated with `Decimal`, inside
10, 25, 50, and 100 bps bands around mid. Band imbalance is:

`(visible bid quote - visible ask quote) / (visible bid quote + visible ask quote)`

Positive values mean more observed bid depth; negative values mean more observed
ask depth. A zero denominator produces N/A rather than an invented value.

The deepest initialized bid and ask are fixed coverage boundaries. Every side of
every band reports `coverage_complete`; a 500-level snapshot is never assumed to
cover a fixed percentage. Incomplete totals remain explicitly observed partial
depth and are not presented as complete depth.

Concentration metrics are the largest individual visible level inside the outer
100-bps band, its quote notional, distance from mid, and share of observed band
depth. There is no arbitrary wall threshold and no adjacent-level clustering.

`liquidity_below` means visible bids below book mid. `liquidity_above` means visible
asks above book mid. Both compact mappings carry `usage=research_only`.

## Freshness and data health

Synchronized fresh output is VERIFIED. Old output is STALE. Warming,
synchronizing, disconnected, overflowed, or resynchronizing books are unavailable;
snapshot failures are ERROR. A STALE compact context contains status/provenance but
does not publish trusted bands or concentrations to strategy input.

Verified mappings truthfully remove the two optional N/A labels. Warming or
resynchronizing state leaves them optional and missing. Stale context marks them
optional and unverified. The existing fail-closed treatment of unknown future
data-health fields is unchanged.

All existing consumers explicitly ignore `usage=research_only` mappings for sweep
confluence, provided-liquidity targets, pullback targets, and target intelligence.

## Measurement and persistence limits

The deterministic, network-free benchmark on CPython 3.11.9 with 100 symbols and
500 levels per side measured:

- initialized book memory: 247,479 bytes per symbol;
- 100-symbol book memory: 24,747,999 bytes;
- representative serialized compact snapshot: 1,641 bytes;
- synthetic snapshot computation: 1.9085 ms median, 3.3875 ms p95;
- synthetic sequential update processing: 5,010.85 events/second.

Timing is reported, not asserted in CI. Run the same benchmark with:

`python scripts/benchmark_order_book_liquidity.py`

Raw WebSocket messages, diff events, REST responses, price levels, complete books,
and level history are never persisted. Only the compact normalized snapshot in the
scanner result is eligible for existing research persistence.

Visible public orders can be cancelled before execution. These measurements are
observed visible depth, not true or guaranteed executable liquidity, support,
resistance, institutional activity, hidden liquidity, or spoofing detection.
