from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.backtesting.strategy_replay import ReplayConfig, _strategy_input
from app.data.candle_integrity import (
    CandleIntegrityError,
    CandleIntegrityReason,
    closed_candles_as_of,
)
from app.data.normalizers.binance import normalize_binance_klines
from app.data.timeframes import resample_ohlcv_candles
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunner
from app.regime.classifier import evaluate_market_regime
from app.regime.models import MarketRegimeInput
from app.strategies.liquidity_grab_pullback import LiquidityGrabEngine
from tests.test_scanner_runner import (
    FakeExchangeClient,
    _config,
    _flat_candles,
    _strategy_pullback_candles,
    run,
)
from tests.test_strategy_replay import _causal_dataset, _full_bullish_setup_candles

BASE_MS = 1_704_067_200_000
MINUTE_MS = 60_000
FIFTEEN_MINUTES_MS = 15 * MINUTE_MS
TWELVE_HOURS_MS = 12 * 60 * MINUTE_MS
DAY_MS = 24 * 60 * MINUTE_MS


def _bar(timestamp: int, *, close: str = "100") -> dict[str, Decimal | int]:
    close_price = Decimal(close)
    return {
        "timestamp": timestamp,
        "open": close_price,
        "high": close_price + Decimal("1"),
        "low": close_price - Decimal("1"),
        "close": close_price,
        "volume": Decimal("100"),
    }


def _strategy_result_as_of(candles_by_timeframe: dict[str, list[dict]], decision_timestamp: int):
    closed = {
        timeframe: closed_candles_as_of(
            candles,
            timeframe=timeframe,
            decision_timestamp=decision_timestamp,
            minimum_closed_history=0,
        ).candles
        for timeframe, candles in candles_by_timeframe.items()
    }
    execution = closed["15m"]
    current_price = Decimal(str(execution[-1]["close"]))
    payload = _strategy_input(
        symbol="BTCUSDT",
        candles_by_timeframe=closed,
        config=ReplayConfig(modes=("swing",)),
        current_price=current_price,
        timeframe_context={},
    )
    return LiquidityGrabEngine().analyze(payload).swing


def test_binance_future_close_is_excluded_before_analysis() -> None:
    payload = [
        [BASE_MS, "100", "101", "99", "100", "10", BASE_MS + FIFTEEN_MINUTES_MS - 1, "1000", 10],
        [
            BASE_MS + FIFTEEN_MINUTES_MS,
            "100",
            "150",
            "80",
            "140",
            "20",
            BASE_MS + (2 * FIFTEEN_MINUTES_MS) - 1,
            "2000",
            20,
        ],
    ]

    normalized = normalize_binance_klines("BTCUSDT", "15m", payload)
    window = closed_candles_as_of(
        normalized,
        timeframe="15m",
        decision_timestamp=BASE_MS + FIFTEEN_MINUTES_MS,
        minimum_closed_history=1,
    )

    assert window.candles == (normalized[0],)
    assert window.excluded_unclosed_count == 1
    assert normalized[1].close_timestamp == BASE_MS + (2 * FIFTEEN_MINUTES_MS) - 1


def test_open_fake_bos_is_ineligible_until_its_real_close() -> None:
    dataset = _causal_dataset(_full_bullish_setup_candles())
    final_execution_open = int(dataset["15m"][-1]["timestamp"])
    final_execution_close = final_execution_open + FIFTEEN_MINUTES_MS

    before_close = _strategy_result_as_of(dataset, final_execution_close - 1)
    at_close = _strategy_result_as_of(dataset, final_execution_close)

    assert before_close.is_valid is False
    assert before_close.confirmation_structure_shift_status == "failed"
    assert at_close.is_valid is True
    assert at_close.bias == "long"
    assert at_close.entry == Decimal("97.00000000")
    assert at_close.stop == Decimal("83.87142857")


@pytest.mark.parametrize(
    ("candles", "reason"),
    [
        ([_bar(BASE_MS), _bar(BASE_MS)], CandleIntegrityReason.DUPLICATE_TIMESTAMP),
        (
            [_bar(BASE_MS), _bar(BASE_MS + FIFTEEN_MINUTES_MS), _bar(BASE_MS)],
            CandleIntegrityReason.OUT_OF_ORDER,
        ),
        ([_bar(BASE_MS), _bar(BASE_MS + (2 * FIFTEEN_MINUTES_MS))], CandleIntegrityReason.CONTINUITY_GAP),
    ],
)
def test_invalid_candle_timeline_is_rejected_explicitly(candles, reason) -> None:
    with pytest.raises(CandleIntegrityError) as exc_info:
        closed_candles_as_of(
            candles,
            timeframe="15m",
            decision_timestamp=BASE_MS + (4 * FIFTEEN_MINUTES_MS),
        )

    assert exc_info.value.reason == reason
    assert f"candle_integrity:{reason.value}" in str(exc_info.value)


def test_insufficient_closed_history_has_precise_reason_and_accepts_naive_decision_time() -> None:
    naive_decision = datetime.fromtimestamp((BASE_MS + 5 * MINUTE_MS) / 1000, tz=UTC).replace(tzinfo=None)

    with pytest.raises(CandleIntegrityError) as exc_info:
        closed_candles_as_of(
            [_bar(BASE_MS)],
            timeframe="15m",
            decision_timestamp=naive_decision,
            minimum_closed_history=1,
        )

    assert exc_info.value.reason == CandleIntegrityReason.INSUFFICIENT_CLOSED_HISTORY
    assert "only 0 were closed" in str(exc_info.value)


def test_regime_excludes_unclosed_btc_and_eth_candles() -> None:
    btc = [_bar(BASE_MS + index * TWELVE_HOURS_MS, close=str(100 + index)) for index in range(41)]
    eth = [_bar(BASE_MS + index * TWELVE_HOURS_MS, close=str(80 + index)) for index in range(41)]
    decision_before_last_close = BASE_MS + (40 * TWELVE_HOURS_MS) + (6 * 60 * MINUTE_MS)

    before = evaluate_market_regime(
        MarketRegimeInput(
            btc_candles=btc,
            eth_candles=eth,
            candle_timeframe="12h",
            decision_timestamp=datetime.fromtimestamp(decision_before_last_close / 1000, tz=UTC),
        )
    )
    after = evaluate_market_regime(
        MarketRegimeInput(
            btc_candles=btc,
            eth_candles=eth,
            candle_timeframe="12h",
            decision_timestamp=datetime.fromtimestamp(
                (BASE_MS + (41 * TWELVE_HOURS_MS)) / 1000,
                tz=UTC,
            ),
        )
    )

    assert before.metrics["btc"]["candle_count"] == 40
    assert before.metrics["eth"]["candle_count"] == 40
    assert after.metrics["btc"]["candle_count"] == 41
    assert after.metrics["eth"]["candle_count"] == 41


def test_synthetic_2d_requires_closed_consecutive_daily_sources() -> None:
    candles = [_bar(index * DAY_MS, close=str(100 + index)) for index in range(3)]

    before_second_close = resample_ohlcv_candles(
        candles,
        decision_timestamp=(2 * DAY_MS) - 1,
    )
    at_second_close = resample_ohlcv_candles(
        candles,
        decision_timestamp=2 * DAY_MS,
    )

    assert before_second_close == []
    assert len(at_second_close) == 1
    assert at_second_close[0]["timestamp"] == 0
    assert at_second_close[0]["close_timestamp"] == 2 * DAY_MS


def test_synthetic_2d_bucket_boundaries_are_fetch_window_stable() -> None:
    full = [_bar(index * DAY_MS, close=str(100 + index)) for index in range(5)]
    shifted = full[1:]

    full_result = resample_ohlcv_candles(full, decision_timestamp=5 * DAY_MS)
    shifted_result = resample_ohlcv_candles(shifted, decision_timestamp=5 * DAY_MS)

    common_full = next(candle for candle in full_result if candle["timestamp"] == 2 * DAY_MS)
    assert shifted_result == [common_full]
    assert all(candle["timestamp"] != 4 * DAY_MS for candle in full_result)


def test_bad_symbol_candle_integrity_does_not_stop_other_symbols() -> None:
    bad = _flat_candles()
    bad[1]["timestamp"] = bad[0]["timestamp"]
    client = FakeExchangeClient(
        {
            "BADUSDT": bad,
            "BTCUSDT": _strategy_pullback_candles(),
        },
        failing_timeframes={"2d"},
    )

    result = run(ScannerRunner(exchange_client=client).run(_config(["BADUSDT", "BTCUSDT"])))

    assert result.scanned_symbols == 2
    assert result.results[0].status == ScannerPipelineStatus.SCAN_ERROR
    assert "candle_integrity:duplicate_timestamp" in str(result.results[0].error_message)
    assert result.results[1].status != ScannerPipelineStatus.SCAN_ERROR
    assert result.results[1].strategy_diagnostics["challenge"]["confirmation_structure_shift_status"] == "passed"
