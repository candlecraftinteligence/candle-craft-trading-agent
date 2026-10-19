# I. Shadow comparison

Offline probes, all synthetic or pure-function:

- Public gate: score 88 and grade A passes; score 87 fails the score gate; grade B fails the grade gate; unevaluated quality fails.
- Freshness: a covered 1m series passes; removing the latest bar is stale. Allowance stays zero.
- Open interest: a price-only notional change is not position growth. Missing previous OI does not become zero.
- Cache: a failed fetch is not stored; a hit keeps the measurement time distinct from cache bookkeeping time; Binance and Bybit keys stay separate.
- Candle pairing uses exact open timestamps. Unmatched rows are kept.

Same-venue vendor comparison is not applicable unless a vendor payload identifies the venue and the sample summary says it was fetched. Cross-venue differences are context. Rank differences are methodology until a defect is shown.

Attached cross-venue BTC note:

```json
{
  "comparison_class": "cross_venue",
  "paired_opens": [
    1792400400000,
    1792401300000,
    1792402200000,
    1792403100000,
    1792404000000
  ],
  "same_venue_vendor_comparison": "not_applicable",
  "status": "observed",
  "tolerance_ms": 0,
  "unmatched_binance": [],
  "unmatched_bybit": []
}
```
