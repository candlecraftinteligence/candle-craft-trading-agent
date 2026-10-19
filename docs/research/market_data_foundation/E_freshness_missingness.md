# E. Freshness, provenance, and original availability

## MDF-TIME-001

- classification: code_contract
- severity: medium
- production incident: not established
- summary: Production integer timestamps are milliseconds, and a seconds spacing is not rewritten.
- expected: Callers declare seconds or milliseconds. Acquisition time is not a substitute producer time.
- observed: sibling spacing classification=spacing_matches_seconds; historical availability of a bar fetched after the cutoff is incompatible
- consumers: app.data.candle_integrity.normalize_utc_timestamp, freshness and replay cutoffs
- evidence: synthetic:seconds-spacing, synthetic:late-fetch
- follow-up: Keep second-based provider timestamps off the millisecond normalizer.
- reproduction: timeutil.classify_unit_confusion and originally_available

## MDF-CANDLE-001

- classification: code_contract
- severity: medium
- production incident: not established
- summary: Shared candle-sequence validation does not reject impossible OHLC or negative volume.
- expected: A shared integrity boundary rejects impossible prices and negative volume before features.
- observed: validate_candle_sequence checks order, anchors, and continuity. OHLC and volume checks exist later in selected consumers.
- consumers: app.data.candle_integrity.validate_candle_sequence, app.analytics.target_intelligence, app.agents.technical_structure
- evidence: code:candle_integrity.py
- follow-up: A later phase can fail closed at the shared boundary without weakening freshness or RR.
- reproduction: ohlc_problems on high<low and volume<0; validate_candle_sequence does not raise for those fields

Essential freshness remains `essential-candle-freshness-v1` with a zero allowance and a Unix-epoch UTC anchor, including synthetic 2d candles.

A candle open is not its close. A response envelope time is not a measurement time. A cache insertion time is not a producer time. A regime computed now is stale when its candle input is stale.

Historical reconstruction requires both an admissible measurement time and proof CCI had the input at the decision cutoff. A later fetch can support a structural check. It cannot prove the earlier decision saw that bar.

Cache kinds used by the existing cache are miss, hit, and expiry refetch. A failed fetch raises and is not stored. The cache key includes the exchange name, so Binance and Bybit entries do not share a key when callers pass that name.
