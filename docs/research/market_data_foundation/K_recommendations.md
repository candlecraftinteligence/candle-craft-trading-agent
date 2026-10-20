# K. Recommendations

These are future tests, not production changes from this phase.

| measurement | authority to test later | why it is not changed now | smallest next validation |
| --- | --- | --- | --- |
| Binance or Bybit contract candles, volume, mark, index, funding, OI, book | that same venue | no sample here shows a vendor is more correct for that contract | exact-timestamp same-venue comparison with units |
| BTC and ETH direction | same-venue closed candles already used by CCI | regime and BTC context already have lineage; ETH context is only the regime fetch | confirm Runtime actually enables the flags before relying on them |
| Global rank and dominance | keep CoinPaprika in production | CMC IDs and dominance are candidates, not a proven replacement | aligned-window overlap with identity checks, then a separate adoption review |
| Liquidations | observed venue events with coverage limits | aggregates and heatmaps are not observations | a no-overage historical sample only after rights and retention are known |
| Macro | original source with vintage | already present and not a setup authority | no gate change |

Central question, by consumer:

| consumer | verdict | evidence |
| --- | --- | --- |
| Essential candle freshness policy | supported within tested scope | zero allowance, epoch anchor, and existing freshness tests; this phase does not widen it |
| Shared candle OHLC and negative-volume boundary | verified code gap, not a proven production incident | `validate_candle_sequence` does not check those fields; later consumers do some of the checks |
| Bybit OI unit and both-side versus single-side | insufficient evidence | DTO unit stays N/A; differing fields are not halved and stay unverified |
| Funding as last settled history | supported within tested scope for that meaning | client reads funding history, not premium index |
| Predicted funding | insufficient evidence | endpoint is not called |
| Public score 88, grade A, public RR 3 | supported within tested scope | constants reasserted; unevaluated quality does not pass |
| Top-100 boundary in the strict universe | supported within tested scope of the current builder and the shadow counter | rank above N is excluded and not refilled |
| Runtime data correctness on 2026-10-18 | insufficient evidence | user-reported cycles were not re-observed and are not treated as proof |
| Signal expectancy | insufficient evidence | this phase is not an expectancy study |

No source comparison here establishes a signal-performance improvement.
