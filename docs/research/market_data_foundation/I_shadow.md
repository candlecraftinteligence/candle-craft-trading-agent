# I. Shadow comparison

Offline probes, all synthetic or pure-function:

- Public gate: score 88 and grade A passes; score 87 fails the score gate; grade B fails the grade gate; unevaluated quality fails.
- Freshness: a covered 1m series passes; removing the latest bar is stale. Allowance stays zero.
- Open interest: a price-only notional change is not position growth. Missing previous OI does not become zero.
- Cache: a failed fetch is not stored; a hit keeps the measurement time distinct from cache bookkeeping time; Binance and Bybit keys stay separate.
- Candle pairing uses exact open timestamps. Unmatched rows are kept.

Same-venue vendor comparison is not applicable unless a vendor payload identifies the venue and the sample summary says it was fetched. Cross-venue differences are context. Symbol-only CoinPaprika overlap is not validated identity.

The representative offline comparison below is synthetic. Captured prices are unavailable because raw bodies were not retained. Live comparison was not repeated.

```json
{
  "label": "synthetic",
  "captured": "unavailable_raw_bodies_were_not_retained",
  "live": "not_executed",
  "selected": [
    "BTCUSDT",
    "ETHUSDT",
    "S0USDT",
    "S1USDT",
    "S2USDT",
    "S3USDT",
    "S4USDT",
    "S5USDT"
  ],
  "metrics": [
    "close",
    "base_volume",
    "open_interest",
    "funding_fraction"
  ],
  "rows": [
    {
      "symbol": "BTCUSDT",
      "open_ms": 1700000000000,
      "label": "synthetic",
      "binance_close_usdt": "100",
      "bybit_close_usdt": "100.5",
      "close_delta_usdt": "0.5",
      "binance_base_volume": "10",
      "bybit_base_volume": "11",
      "binance_open_interest": "2.5",
      "bybit_open_interest": "2.5",
      "open_interest_unit": "unverified",
      "binance_funding_fraction": "0.0001",
      "bybit_funding_fraction": "0.0002",
      "usdt_converted_to_usd": false,
      "delta_is_error": false
    },
    {
      "symbol": "ETHUSDT",
      "open_ms": 1700000900000,
      "label": "synthetic",
      "binance_close_usdt": "101",
      "bybit_close_usdt": "101.5",
      "close_delta_usdt": "0.5",
      "binance_base_volume": "10",
      "bybit_base_volume": "11",
      "binance_open_interest": "2.5",
      "bybit_open_interest": "2.5",
      "open_interest_unit": "unverified",
      "binance_funding_fraction": "0.0001",
      "bybit_funding_fraction": "0.0002",
      "usdt_converted_to_usd": false,
      "delta_is_error": false
    },
    {
      "symbol": "S0USDT",
      "open_ms": 1700001800000,
      "label": "synthetic",
      "binance_close_usdt": "102",
      "bybit_close_usdt": "102.5",
      "close_delta_usdt": "0.5",
      "binance_base_volume": "10",
      "bybit_base_volume": "11",
      "binance_open_interest": "2.5",
      "bybit_open_interest": "2.5",
      "open_interest_unit": "unverified",
      "binance_funding_fraction": "0.0001",
      "bybit_funding_fraction": "0.0002",
      "usdt_converted_to_usd": false,
      "delta_is_error": false
    },
    {
      "symbol": "S1USDT",
      "open_ms": 1700002700000,
      "label": "synthetic",
      "binance_close_usdt": "103",
      "bybit_close_usdt": "103.5",
      "close_delta_usdt": "0.5",
      "binance_base_volume": "10",
      "bybit_base_volume": "11",
      "binance_open_interest": "2.5",
      "bybit_open_interest": "2.5",
      "open_interest_unit": "unverified",
      "binance_funding_fraction": "0.0001",
      "bybit_funding_fraction": "0.0002",
      "usdt_converted_to_usd": false,
      "delta_is_error": false
    },
    {
      "symbol": "S2USDT",
      "open_ms": 1700003600000,
      "label": "synthetic",
      "binance_close_usdt": "104",
      "bybit_close_usdt": "104.5",
      "close_delta_usdt": "0.5",
      "binance_base_volume": "10",
      "bybit_base_volume": "11",
      "binance_open_interest": "2.5",
      "bybit_open_interest": "2.5",
      "open_interest_unit": "unverified",
      "binance_funding_fraction": "0.0001",
      "bybit_funding_fraction": "0.0002",
      "usdt_converted_to_usd": false,
      "delta_is_error": false
    },
    {
      "symbol": "S3USDT",
      "open_ms": 1700004500000,
      "label": "synthetic",
      "binance_close_usdt": "105",
      "bybit_close_usdt": "105.5",
      "close_delta_usdt": "0.5",
      "binance_base_volume": "10",
      "bybit_base_volume": "11",
      "binance_open_interest": "2.5",
      "bybit_open_interest": "2.5",
      "open_interest_unit": "unverified",
      "binance_funding_fraction": "0.0001",
      "bybit_funding_fraction": "0.0002",
      "usdt_converted_to_usd": false,
      "delta_is_error": false
    },
    {
      "symbol": "S4USDT",
      "open_ms": 1700005400000,
      "label": "synthetic",
      "binance_close_usdt": "106",
      "bybit_close_usdt": "106.5",
      "close_delta_usdt": "0.5",
      "binance_base_volume": "10",
      "bybit_base_volume": "11",
      "binance_open_interest": "2.5",
      "bybit_open_interest": "2.5",
      "open_interest_unit": "unverified",
      "binance_funding_fraction": "0.0001",
      "bybit_funding_fraction": "0.0002",
      "usdt_converted_to_usd": false,
      "delta_is_error": false
    },
    {
      "symbol": "S5USDT",
      "open_ms": 1700006300000,
      "label": "synthetic",
      "binance_close_usdt": "107",
      "bybit_close_usdt": "107.5",
      "close_delta_usdt": "0.5",
      "binance_base_volume": "10",
      "bybit_base_volume": "11",
      "binance_open_interest": "2.5",
      "bybit_open_interest": "2.5",
      "open_interest_unit": "unverified",
      "binance_funding_fraction": "0.0001",
      "bybit_funding_fraction": "0.0002",
      "usdt_converted_to_usd": false,
      "delta_is_error": false
    }
  ],
  "same_venue_vendor_comparison": "not_applicable"
}
```

Live cross-venue sample: not attached.
