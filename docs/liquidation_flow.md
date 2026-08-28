# Binance USDⓈ-M liquidation flow

This research-only subsystem consumes Binance's public force-liquidation stream. It
uses no credentials, places no orders, reads no runtime database, and never stores
raw WebSocket messages or raw liquidation events.

## Audited stream contract

The official Binance USDⓈ-M market-stream contract currently provides both:

- `{symbol}@forceOrder`, a per-symbol snapshot stream updated at most every 1000 ms;
- `!forceOrder@arr`, an all-market snapshot stream updated at most every 1000 ms.

Both publish only the latest liquidation order for a symbol inside a 1000 ms
interval and publish nothing for that interval when no liquidation occurs. They are
therefore snapshot feeds, not lossless order-by-order liquidation tapes. Counts,
largest-event values, and notional totals in CCI are explicitly *observed stream*
metrics and may be lower than the exchange's underlying liquidation activity when
multiple orders occur for one symbol inside one publication interval.

CCI uses one `!forceOrder@arr` subscription on
`wss://fstream.binance.com/market/stream`. The all-market feed is sufficient because
it carries every market symbol in one connection; unrelated symbols are discarded
immediately. BTCUSDT and the current scanner universe are retained. This avoids one
subscription per scanner symbol.

After Binance's COIN-M migration, this all-market stream is a merged USDⓈ-M and
COIN-M universe. The production parser requires `st=1` (USDⓈ-M), rejects `st=2`
(COIN-M), and requires the new `ps` pair-symbol field. The audited payload fields
are:

| Field | Meaning | CCI use |
| --- | --- | --- |
| `e` | event type | must be `forceOrder` |
| `E` | event time in milliseconds | validated event timestamp and dedupe input |
| `o.s` | symbol | retention filter and per-symbol routing |
| `o.S` | forced order side | raw order side and position-side interpretation |
| `o.o` | order type | retained only in the transient normalized event and dedupe input |
| `o.f` | time in force | transient normalized event and dedupe input |
| `o.q` | original quantity | validation and dedupe input |
| `o.p` | order price | diagnostic validation and dedupe input |
| `o.ap` | average fill price | authoritative execution price for notional |
| `o.X` | order status | transient normalized event and dedupe input |
| `o.l` | last filled quantity | validation and dedupe input |
| `o.z` | accumulated filled quantity | authoritative executed quantity for notional |
| `o.T` | order trade time in milliseconds | UTC minute bucketing |
| `ps` | pair symbol | merged-stream validation and dedupe input |
| `st` | stream type | `1` accepted; `2` rejected |

Official contract: <https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market>

## Side and notional semantics

A forced `SELL` closes or reduces a long position, so CCI classifies it as
`long_liquidated`. A forced `BUY` closes or reduces a short position, so CCI
classifies it as `short_liquidated`. The transient parsed event exposes both the raw
order side and interpreted position side; persisted research snapshots use the
position-side names so the meaning cannot be confused with aggressive trade flow.

Quote notional is deterministic and precision-safe:

`observed_liquidation_quote_notional = o.z × o.ap`

Both operands are `Decimal`. CCI uses accumulated executed quantity and average
fill price because they describe actual filled execution, rather than original
quantity or limit/order price. Missing, zero, non-finite, inconsistent, or otherwise
unusable values reject the observation and increment a malformed diagnostic; they
never contribute a fabricated zero.

## Windows, imbalance, intensity, and acceleration

Every accepted observation updates a UTC minute bucket selected by `o.T`. Snapshots
use the last complete 1, 5, and 15 UTC minutes. Each window exposes observed long,
short, and total quote notional; observed event counts; largest long and short
events; quote and event rates per minute; largest-event share; and this imbalance:

`(short_liquidation_quote - long_liquidation_quote) / total_liquidation_quote`

Positive imbalance means more short-position liquidation notional was observed;
negative imbalance means more long-position liquidation notional was observed. It
is `N/A` when total notional is zero.

Acceleration is descriptive and threshold-free:

- 1 minute rate versus the preceding 4-minute average rate;
- 5 minute rate versus the preceding 10-minute average rate.

The label is `INCREASING`, `DECREASING`, or `STABLE` from exact rate comparison.
It is `INSUFFICIENT_DATA` until the prior baseline has full connection coverage.
The raw rates and ratio remain available; no percentile or trading interpretation
is applied.

## Coverage, silence, reconnects, and freshness

Connection coverage is tracked independently from event arrival. A connected,
fully warmed 5-minute interval with no messages is a truthful `VERIFIED` zero because
Binance specifies that the stream is silent when there is no liquidation. A
disconnected interval is never represented as verified zero.

Complete live coverage is required separately for each 1-, 5-, and 15-minute
window. Startup, disconnect, reconnect, or an excessively delayed event resets
trustworthy coverage. A bounded ping/pong timeout detects a connection that stops
responding without treating ordinary event silence as stale. Failures return an
`UNAVAILABLE`, `STALE`, or `ERROR` snapshot with the exact reason, while the scanner
continues. Reconnect delay uses bounded exponential backoff.

Binance exposes no liquidation order ID in this contract. Exact reconnect duplicates
are suppressed with a deterministic 128-bit BLAKE2 fingerprint over every documented
payload field used by the parser. This is intentionally exact-only: CCI does not
invent an order identity or attempt unsafe de-cumulation across differing snapshots.

## Runtime and storage bounds

`run_scan.py --watch` owns one long-lived `LiquidationFlowService` beside the
aggregate-trade service. The Telegram listener never owns it. Universe reconciliation
updates only the in-memory retention filter because the WebSocket subscription stays
all-market.

Each retained symbol is bounded to 16 minute buckets and 128 compact dedupe digests.
The configured symbol cap is bounded from 1 to 1024 and defaults to 100. BTCUSDT is
always selected first; remaining symbols are selected lexically, and overflow symbols
get an explicit unavailable reason. The WebSocket transport queue remains bounded at
32 messages with a 64 KiB message limit.

Raw events are parsed, aggregated, and discarded. No raw event table, force-order
JSON history, raw WebSocket log, or minute-bucket persistence exists. Only the compact
normalized snapshot is attached to the existing research scanner result.

## Research-only isolation and data health

The feature defaults to disabled:

- `LIQUIDATION_FLOW_ENABLED=false`
- `LIQUIDATION_FLOW_STALE_SEC=30.0`
- `LIQUIDATION_FLOW_MAX_SYMBOLS=100`

Injected strategy context always carries `usage=research_only`. Shared strategy
context helpers ignore it for absorption, momentum, scoring, gates, grade,
risk/reward, trade plans, lifecycle, and Telegram eligibility. A verified snapshot
truthfully removes optional `liquidation_data: N/A`; stale research remains optional
`Unverified`; warming, unavailable, disconnected, overflow, and error states remain
optional missing. Liquidation data is still optional at the final CONFIRMED boundary,
and unknown future data-health fields still fail closed.

No production scanner/watch loop or external test-network request is required for
verification; all service tests use deterministic synthetic payloads and fake
transports.
