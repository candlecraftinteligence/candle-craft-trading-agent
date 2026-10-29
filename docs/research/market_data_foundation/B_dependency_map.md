# B. Source-to-decision map

Path shape: fetcher, normalizer, cache, consumer, gate or research output.
Optional flags defaulting to false are not Runtime proof. `runtime_enabled` is unverified throughout.

| metric | cache / missing behavior | decision role |
| --- | --- | --- |
| ohlcv_binance_usdm | CachedMarketDataClient.get_klines data_type=candles; closed 4h/12h/1d stay cached until the next boundary when no explicit TTL is set; freshness missing or unverified; missing bars are not zero volume | required for configured strategy timeframes |
| ohlcv_bybit_linear | same candle cache key family, exchange name bybit_linear; integrity rejects unordered or gapped series; derived close is not a publication time | required only on the Bybit exchange path |
| ohlcv_synthetic_2d | cached as candles when the caller stores the result; incomplete pair emits nothing for that bucket | required only when the configured interval is 2d |
| rolling_24h_ticker | data_type=ticker; missing quote volume excludes the ticker row; it is not treated as zero | required for market-cap discovery liquidity intersection; optional as a mark source |
| mark_index_last | ticker or funding; N/A on the DTO | optional |
| funding_settled_history | data_type funding; N/A funding context | optional enrichment |
| open_interest | open_interest and open_interest_history; N/A; never zero | optional |
| long_short_account_ratio | long_short_ratio; caller-specific; a missing ratio must stay N/A | optional |
| taker_flow | not the market-data file cache; disabled or stale is unavailable, not zero | optional; default disabled |
| order_book_binance_usdm | in-memory book; unverified or unavailable; a crossed book is not a valid sync | optional; default disabled |
| liquidations_binance_force_order | in-memory aggregator; disconnected or gapped coverage is unavailable, not zero. A covered empty window on SymbolLiquidationAggregator reports event_count 0. liquidation_observation(None) is a separate helper and is not that aggregator. | optional; default disabled |
| btc_directional_context | candle cache; component unavailable with a reason | default enabled in Settings; runtime activation unverified |
| eth_regime_candles | candle cache; ETHUSDT_candles: N/A | optional; ScannerRunConfig.market_regime_enabled defaults false. There is no standalone ETH context service. |
| btc_dominance | BtcDominanceContextService memory cache; context unavailable; not zero | default btc_d_context_enabled true; runtime unverified |
| market_regime | candle and BTC.D caches; missing inputs are named N/A | optional; default off |
| sector_strength | none of its own; UNAVAILABLE or UNCLASSIFIED; not a gate pass | research only |
| global_market_cap_rank | not used for the strict ranking path; universe resolution error; discovery failure does not stop owner monitoring | required for market-cap universe mode |
| macro_calendar | macro event cache; source status unavailable or unverified | default macro_event_context_enabled true; runtime unverified. Not a setup-generation authority. |
| essential_candle_freshness | n/a; stale or missing blocks new confirmation; owner preservation is a separate rule | required for essential discovery roles |
| public_quality_and_rr_gates | n/a; unevaluated quality is not a pass | required for public output |
| watch_cadence | n/a; n/a | operational; this phase does not start it |

Neutral defaults that can hide missingness:

- Open-interest `unit` defaults to N/A, while the numeric field can still be present. A present number with unit N/A is not a verified unit.
- Bybit candle `close_timestamp` stays N/A. Freshness derives the scheduled close from the open. That derived close is not a venue publication time.
- Bybit ticker timestamp copies the response envelope time.
- An empty liquidation stream is not zero.
- Cache TTL does not extend the freshness allowance. The allowance is zero.
- USDT turnover is not USD market cap.

ETH has no standalone context service. Regime fetches ETHUSDT only when `market_regime_enabled` is true, and that flag defaults to false.
USDT dominance is an explicit N/A input in the regime classifier.
Sector context is research-only and does not recalculate a trading decision.
