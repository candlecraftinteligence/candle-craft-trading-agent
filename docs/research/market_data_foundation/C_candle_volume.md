# Candle and volume integrity

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


No-trade bars are present intervals with zero volume and flat prices. A missing interval is a continuity gap. Those are different dispositions.

12h and 1d comparisons use native candles. The production resampler accepts only 2d. A 2d result is fresh only when its daily inputs are admissible at the cutoff.

USDT is not converted to USD. A `1000` contract prefix is a different instrument and is not stripped.
