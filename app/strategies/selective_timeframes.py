"""Fail-closed candle contract for prospective selective strategy observations."""
from collections.abc import Mapping
from typing import Any

from app.data.candle_freshness import assess_essential_discovery
from app.data.candle_integrity import closed_candles_as_of, normalize_utc_timestamp, timeframe_duration

HIERARCHY = ("1d", "12h", "2h", "15m", "15m")


def closed_selective_timeframes(data: Any) -> dict[str, tuple[Any, ...]]:
    actual = (data.htf_timeframe, data.bias_timeframe, data.structure_timeframe,
              data.execution_timeframe, data.confirmation_timeframe)
    if actual != HIERARCHY:
        raise ValueError("selective_timeframe_hierarchy_mismatch: Unverified")
    if not data.structure_analysis_required:
        raise ValueError("selective_structure_analysis_required: Unverified")
    if data.decision_timestamp is None or data.exchange not in {"binance", "bybit"}:
        raise ValueError("selective_candle_provenance: Unverified")
    output = {}
    for timeframe, raw in (("1d", data.candles_1d), ("12h", data.candles_12h),
                           ("2h", data.structure_candles), ("15m", data.candles_15m)):
        if not raw:
            raise ValueError(f"candles_{timeframe}: N/A")
        for candle in raw:
            get = candle.get if isinstance(candle, Mapping) else lambda key: getattr(candle, key, None)
            if (get("interval") != timeframe or get("symbol") != data.symbol or get("exchange") != data.exchange):
                raise ValueError(f"candles_{timeframe}_source_identity: Unverified")
            duration_ms = int(timeframe_duration(timeframe).total_seconds() * 1000)
            timestamp_ms = int(normalize_utc_timestamp(get("timestamp")).timestamp() * 1000)
            if timestamp_ms % duration_ms:
                raise ValueError(f"candles_{timeframe}_utc_alignment: Unverified")
            source = get("raw_source")
            if isinstance(source, Mapping) and (source.get("synthetic") or source.get("source_interval") == "2d"):
                raise ValueError(f"candles_{timeframe}_synthetic_source: Unverified")
        output[timeframe] = closed_candles_as_of(
            raw, timeframe=timeframe, decision_timestamp=data.decision_timestamp,
            minimum_closed_history=20,
        ).candles
    receipt = assess_essential_discovery(symbol=data.symbol, decision_timestamp=data.decision_timestamp,
        roles=(("1d", "htf"), ("12h", "bias"), ("2h", "structure"), ("15m", "execution")),
        candles_by_timeframe=output)
    if not receipt.passed:
        raise ValueError("selective_timeframes_stale_or_unverified: Unverified")
    return output
