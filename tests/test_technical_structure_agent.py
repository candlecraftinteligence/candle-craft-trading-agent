from __future__ import annotations

from decimal import Decimal

from app.agents.technical_structure import (
    TechnicalAnalysisStatus,
    TechnicalStructureAgent,
    calculate_ema,
)
from app.data.dtos import NA


def _flat_candles(count: int = 220, *, include_volume: bool = True) -> list[dict[str, Decimal | int]]:
    candles: list[dict[str, Decimal | int]] = []
    for index in range(count):
        candle: dict[str, Decimal | int] = {
            "timestamp": index,
            "open": Decimal("100"),
            "high": Decimal("101"),
            "low": Decimal("99"),
            "close": Decimal("100"),
        }
        if include_volume:
            candle["volume"] = Decimal("100")
        candles.append(candle)
    return candles


def _trend_candles(count: int = 220, *, start: int = 1, step: int = 1) -> list[dict[str, Decimal | int]]:
    candles: list[dict[str, Decimal | int]] = []
    for index in range(count):
        price = Decimal(start + index * step)
        candles.append(
            {
                "timestamp": index,
                "open": price,
                "high": price + Decimal("1"),
                "low": price - Decimal("1"),
                "close": price,
                "volume": Decimal("100"),
            }
        )
    return candles


def test_atr_calculation() -> None:
    candles = _flat_candles()
    for candle in candles:
        candle["high"] = Decimal("105")
        candle["low"] = Decimal("95")

    result = TechnicalStructureAgent().analyze(candles)

    assert result.is_valid
    assert result.atr == Decimal("10.00000000")


def test_ema_calculation() -> None:
    assert calculate_ema([Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"), Decimal("5")], 3) == Decimal(
        "4.00000000"
    )


def test_swing_high_detection_confirms_after_lookback_window() -> None:
    candles = _flat_candles()
    candles[100]["high"] = Decimal("110")

    result = TechnicalStructureAgent(lookback=2).analyze(candles)

    swing = next(point for point in result.swing_highs if point.index == 100)
    assert swing.price == Decimal("110")
    assert swing.confirmed_at_index == 102


def test_swing_low_detection_confirms_after_lookback_window() -> None:
    candles = _flat_candles()
    candles[100]["low"] = Decimal("90")

    result = TechnicalStructureAgent(lookback=2).analyze(candles)

    swing = next(point for point in result.swing_lows if point.index == 100)
    assert swing.price == Decimal("90")
    assert swing.confirmed_at_index == 102


def test_bullish_sweep_detection() -> None:
    candles = _flat_candles()
    candles[180]["low"] = Decimal("90")
    candles[-1]["low"] = Decimal("89")
    candles[-1]["close"] = Decimal("91")

    result = TechnicalStructureAgent(lookback=2).analyze(candles)

    assert result.sweep.is_present
    assert result.sweep.direction == "bullish"
    assert result.sweep.level == Decimal("90")


def test_bearish_sweep_detection() -> None:
    candles = _flat_candles()
    candles[180]["high"] = Decimal("110")
    candles[-1]["high"] = Decimal("111")
    candles[-1]["close"] = Decimal("109")

    result = TechnicalStructureAgent(lookback=2).analyze(candles)

    assert result.sweep.is_present
    assert result.sweep.direction == "bearish"
    assert result.sweep.level == Decimal("110")


def test_bullish_bos_detection() -> None:
    candles = _flat_candles()
    candles[180]["high"] = Decimal("110")
    candles[-1]["high"] = Decimal("112")
    candles[-1]["close"] = Decimal("111")

    result = TechnicalStructureAgent(lookback=2).analyze(candles)

    assert result.bos.is_present
    assert result.bos.direction == "bullish"
    assert result.bos.level == Decimal("110")


def test_bearish_bos_detection() -> None:
    candles = _flat_candles()
    candles[180]["low"] = Decimal("90")
    candles[-1]["low"] = Decimal("88")
    candles[-1]["close"] = Decimal("89")

    result = TechnicalStructureAgent(lookback=2).analyze(candles)

    assert result.bos.is_present
    assert result.bos.direction == "bearish"
    assert result.bos.level == Decimal("90")


def test_bullish_choch_detection_after_bearish_context() -> None:
    candles = _trend_candles(start=300, step=-1)
    candles[180]["high"] = Decimal("130")
    candles[-1]["high"] = Decimal("132")
    candles[-1]["low"] = Decimal("80")
    candles[-1]["close"] = Decimal("131")

    result = TechnicalStructureAgent(lookback=2).analyze(candles)

    assert result.choch.is_present
    assert result.choch.direction == "bullish"
    assert result.choch.prior_context == "bearish"
    assert result.choch.level == Decimal("130")


def test_bearish_choch_detection_after_bullish_context() -> None:
    candles = _trend_candles(start=100, step=1)
    candles[180]["low"] = Decimal("250")
    candles[-1]["high"] = Decimal("320")
    candles[-1]["low"] = Decimal("248")
    candles[-1]["close"] = Decimal("249")

    result = TechnicalStructureAgent(lookback=2).analyze(candles)

    assert result.choch.is_present
    assert result.choch.direction == "bearish"
    assert result.choch.prior_context == "bullish"
    assert result.choch.level == Decimal("250")


def test_trend_context() -> None:
    bullish = TechnicalStructureAgent().analyze(_trend_candles(start=1, step=1))
    bearish = TechnicalStructureAgent().analyze(_trend_candles(start=300, step=-1))
    neutral = TechnicalStructureAgent().analyze(_flat_candles())

    assert bullish.trend_context == "bullish"
    assert bearish.trend_context == "bearish"
    assert neutral.trend_context == "neutral"


def test_volume_z_score_detects_anomaly() -> None:
    candles = _flat_candles()
    candles[-1]["volume"] = Decimal("200")

    result = TechnicalStructureAgent().analyze(candles)

    assert result.volume_anomaly.is_present
    assert result.volume_anomaly.status == "confirmed"
    assert result.volume_z_score != NA


def test_missing_volume_handling_marks_volume_anomaly_na() -> None:
    result = TechnicalStructureAgent().analyze(_flat_candles(include_volume=False))

    assert result.is_valid
    assert result.volume_z_score == NA
    assert result.volume_anomaly.status == NA
    assert result.volume_anomaly.is_present is False
    volume_component = next(item for item in result.component_availability if item.component == "volume_anomaly")
    assert volume_component.required is False
    assert volume_component.status == "unavailable"


def test_not_enough_candles_handling() -> None:
    result = TechnicalStructureAgent().analyze(_flat_candles(50))

    assert result.is_valid is False
    assert result.analysis_status == TechnicalAnalysisStatus.INSUFFICIENT_DATA
    assert result.data_quality == "insufficient_data"
    assert result.structure_score == NA
    assert result.required_candles == 200
    assert result.available_candles == 50
    assert "Not enough candles" in result.errors[0]


def test_malformed_candle_handling() -> None:
    candles = _flat_candles()
    del candles[10]["high"]

    result = TechnicalStructureAgent().analyze(candles)

    assert result.is_valid is False
    assert result.analysis_status == TechnicalAnalysisStatus.DATA_ERROR
    assert result.data_quality == "data_error"
    assert result.structure_score == NA
    assert result.errors == ("Missing required OHLC field candles[10].high.",)


def test_range_detection_and_nearest_levels() -> None:
    candles = _flat_candles()
    candles[170]["low"] = Decimal("94")
    candles[180]["high"] = Decimal("108")
    candles[-1]["close"] = Decimal("107")
    candles[-1]["high"] = Decimal("108")

    result = TechnicalStructureAgent(lookback=2, range_window=50).analyze(candles)

    assert result.recent_range_high == Decimal("108.00000000")
    assert result.recent_range_low == Decimal("94.00000000")
    assert result.nearest_support == Decimal("94.00000000")
    assert result.nearest_resistance == Decimal("108.00000000")
    assert result.range_position == "upper"


def test_zero_candles_are_a_data_error() -> None:
    result = TechnicalStructureAgent().analyze([], timeframe="15m")

    assert result.analysis_status == TechnicalAnalysisStatus.DATA_ERROR
    assert result.data_quality == "data_error"
    assert result.available_candles == 0
    assert result.structure_score == NA


def test_199_bars_preserve_partial_component_availability_without_scoring() -> None:
    result = TechnicalStructureAgent().analyze(_flat_candles(199), timeframe="15m")

    assert result.analysis_status == TechnicalAnalysisStatus.INSUFFICIENT_DATA
    assert result.is_valid is False
    assert result.required_candles == 200
    assert result.available_candles == 199
    assert result.structure_score == NA
    components = {item.component: item for item in result.component_availability}
    assert components["ema_50"].status == "available"
    assert components["ema_200"].status == "unavailable"


def test_exactly_200_bars_runs_normal_analysis() -> None:
    result = TechnicalStructureAgent().analyze(_flat_candles(200), timeframe="15m")

    assert result.analysis_status == TechnicalAnalysisStatus.VALID
    assert result.is_valid is True
    assert result.available_candles == 200


def test_201_bars_runs_normal_analysis() -> None:
    result = TechnicalStructureAgent().analyze(_flat_candles(201), timeframe="15m")

    assert result.analysis_status == TechnicalAnalysisStatus.VALID
    assert result.is_valid is True
    assert result.available_candles == 201


def test_nan_ohlc_is_a_data_error() -> None:
    candles = _flat_candles()
    candles[10]["close"] = Decimal("NaN")
    result = TechnicalStructureAgent().analyze(candles)

    assert result.analysis_status == TechnicalAnalysisStatus.DATA_ERROR
    assert result.structure_score == NA
    assert any("invalid decimal" in error for error in result.errors)


def test_impossible_ohlc_geometry_is_a_data_error() -> None:
    candles = _flat_candles()
    candles[10]["close"] = Decimal("105")
    result = TechnicalStructureAgent().analyze(candles)

    assert result.analysis_status == TechnicalAnalysisStatus.DATA_ERROR
    assert result.structure_score == NA
    assert result.errors == ("Malformed candle candles[10]: high is below open or close.",)


def test_wick_only_penetration_does_not_count_as_bos() -> None:
    candles = _flat_candles()
    candles[180]["high"] = Decimal("110")
    candles[-1]["high"] = Decimal("112")
    candles[-1]["close"] = Decimal("109")
    result = TechnicalStructureAgent().analyze(candles)

    assert result.bos.is_present is False
    assert result.sweep.is_present is True


def test_bos_on_previous_candle_is_not_persisted_by_secondary_analyzer() -> None:
    candles = _flat_candles()
    candles[180]["high"] = Decimal("110")
    candles[-2]["high"] = Decimal("112")
    candles[-2]["close"] = Decimal("111")
    result = TechnicalStructureAgent().analyze(candles)

    assert result.bos.is_present is False


def test_unconfirmed_future_dependent_pivot_is_not_detected() -> None:
    candles = _flat_candles()
    candles[-2]["high"] = Decimal("110")
    result = TechnicalStructureAgent(lookback=2).analyze(candles)

    assert all(point.index != len(candles) - 2 for point in result.swing_points)


def test_same_data_produces_same_technical_classification() -> None:
    candles = _flat_candles(199)
    agent = TechnicalStructureAgent()
    first = agent.analyze(candles, timeframe="15m")
    second = agent.analyze(candles, timeframe="15m")

    assert first.model_dump(mode="json") == second.model_dump(mode="json")
