from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from app.backtesting.strategy_replay import (
    ReplayConfig,
    ReplayDirection,
    StrategyReplayEngine,
    _normalize_candles,
    _prefix_by_timeframe,
    _slice_until,
    _strategy_input,
)
from app.data.candle_integrity import CandleIntegrityError, CandleIntegrityReason
from app.strategies.liquidity_grab_pullback import LiquidityGrabEngine
from tests.test_strategy_replay import (
    BASE_TIMESTAMP_MS,
    FIFTEEN_MINUTES_MS,
    TWO_DAYS_MS,
    _causal_dataset,
    _full_bullish_setup_candles,
)

TWELVE_HOURS_MS = 12 * 60 * 60_000


def _bar(timestamp: int, *, close: str = "100", volume: str = "100") -> dict[str, Decimal | int]:
    price = Decimal(close)
    return {
        "timestamp": timestamp,
        "open": price,
        "high": price + Decimal("2"),
        "low": price - Decimal("2"),
        "close": price,
        "volume": Decimal(volume),
    }


@pytest.mark.parametrize(
    ("timeframe", "duration"),
    [("12h", TWELVE_HOURS_MS), ("2d", TWO_DAYS_MS)],
)
def test_replay_htf_is_not_included_until_close_boundary(timeframe: str, duration: int) -> None:
    candle = _bar(BASE_TIMESTAMP_MS)

    assert _slice_until(
        [candle],
        BASE_TIMESTAMP_MS + duration - 1,
        timeframe=timeframe,
    ) == ()
    assert _slice_until(
        [candle],
        BASE_TIMESTAMP_MS + duration,
        timeframe=timeframe,
    ) == (candle,)


def test_future_unclosed_htf_ohlcv_mutation_cannot_change_earlier_replay_decision() -> None:
    baseline = _causal_dataset(_full_bullish_setup_candles())
    future_htf = _bar(BASE_TIMESTAMP_MS, close="500", volume="999")
    baseline["2d"].append(future_htf)
    mutated = deepcopy(baseline)
    mutated["2d"][-1].update(
        high=Decimal("9000"),
        low=Decimal("1"),
        close=Decimal("8000"),
        volume=Decimal("999999"),
    )

    execution = _normalize_candles(baseline["15m"], timeframe="15m")
    decision_timestamp = execution[35].close_timestamp
    baseline_prefix = _prefix_by_timeframe(
        baseline,
        execution_timeframe="15m",
        decision_timestamp=decision_timestamp,
        execution_prefix=baseline["15m"][:36],
    )
    mutated_prefix = _prefix_by_timeframe(
        mutated,
        execution_timeframe="15m",
        decision_timestamp=decision_timestamp,
        execution_prefix=mutated["15m"][:36],
    )
    config = ReplayConfig(modes=("swing",))
    baseline_result = LiquidityGrabEngine().analyze(
        _strategy_input(
            symbol="BTCUSDT",
            candles_by_timeframe=baseline_prefix,
            config=config,
            current_price=execution[35].close,
            timeframe_context={},
        )
    )
    mutated_result = LiquidityGrabEngine().analyze(
        _strategy_input(
            symbol="BTCUSDT",
            candles_by_timeframe=mutated_prefix,
            config=config,
            current_price=execution[35].close,
            timeframe_context={},
        )
    )

    assert baseline_prefix["2d"] == mutated_prefix["2d"]
    assert future_htf not in baseline_prefix["2d"]
    assert baseline_result.model_dump() == mutated_result.model_dump()
    assert baseline_result.swing.is_valid is True


def test_replay_missing_timing_metadata_cannot_use_proportional_fallback() -> None:
    malformed = {"BTCUSDT": {"15m": [{"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}]}}

    with pytest.raises(CandleIntegrityError) as exc_info:
        StrategyReplayEngine().run(malformed, ReplayConfig(modes=("swing",)))

    assert exc_info.value.reason == CandleIntegrityReason.MISSING_OPEN_TIMESTAMP
    assert "missing_open_timestamp" in str(exc_info.value)


def _mirrored_short_dataset():
    bullish = _causal_dataset(_full_bullish_setup_candles())
    midpoint = Decimal("200")
    return {
        timeframe: [
            {
                **candle,
                "open": midpoint - Decimal(str(candle["open"])),
                "high": midpoint - Decimal(str(candle["low"])),
                "low": midpoint - Decimal(str(candle["high"])),
                "close": midpoint - Decimal(str(candle["close"])),
            }
            for candle in candles
        ]
        for timeframe, candles in bullish.items()
    }


def test_closed_long_and_short_replay_geometry_remains_directional() -> None:
    long_dataset = _causal_dataset(_full_bullish_setup_candles())
    short_dataset = _mirrored_short_dataset()
    summary = StrategyReplayEngine().run(
        {"LONGUSDT": long_dataset, "SHORTUSDT": short_dataset},
        ReplayConfig(modes=("swing",)),
    )

    long_candidate = summary.symbols[0].trades[0].candidate
    short_candidate = summary.symbols[1].trades[0].candidate
    assert long_candidate.direction == ReplayDirection.LONG
    assert long_candidate.entry == Decimal("97.00000000")
    assert long_candidate.stop == Decimal("83.87142857")
    assert long_candidate.tp2 == Decimal("131.92200000")
    assert short_candidate.direction == ReplayDirection.SHORT
    assert short_candidate.entry == Decimal("103.00000000")
    assert short_candidate.stop == Decimal("116.12857143")
    assert short_candidate.tp2 == Decimal("68.07800000")
