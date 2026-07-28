from __future__ import annotations

from decimal import Decimal

from app.backtesting.strategy_replay import (
    ReplayConfig,
    ReplayOutcome,
    StrategyReplayEngine,
    backtest_json_payload,
)

BASE_TIMESTAMP_MS = 1_704_067_200_000
FIVE_MINUTES_MS = 5 * 60_000
FIFTEEN_MINUTES_MS = 15 * 60_000
TWO_DAYS_MS = 2 * 24 * 60 * 60_000
CONFIRMATION_START_MS = BASE_TIMESTAMP_MS + (36 * (FIFTEEN_MINUTES_MS - FIVE_MINUTES_MS))


def test_replay_default_confirmation_timeframe_matches_live_scanner() -> None:
    assert ReplayConfig().confirmation_timeframe == "15m"


def _time_aligned(
    candles: list[dict[str, Decimal | int]],
    *,
    duration_ms: int,
    start_ms: int,
) -> list[dict[str, Decimal | int]]:
    return [
        {**candle, "timestamp": start_ms + (index * duration_ms)}
        for index, candle in enumerate(candles)
    ]


def _confirmation_candles(candles: list[dict[str, Decimal | int]]) -> list[dict[str, Decimal | int]]:
    confirmation = _base_candles(36)
    confirmation[8]["low"] = Decimal("90")
    confirmation[12]["high"] = Decimal("110")
    confirmation[18]["low"] = Decimal("85")
    confirmation[18]["close"] = Decimal("91")
    confirmation[18]["volume"] = Decimal("200")
    confirmation[33]["open"] = Decimal("99")
    confirmation[33]["close"] = Decimal("97")
    confirmation[33]["low"] = Decimal("95")
    confirmation[33]["high"] = Decimal("100")
    confirmation[35]["open"] = Decimal("104")
    confirmation[35]["high"] = Decimal("114")
    confirmation[35]["low"] = Decimal("101")
    confirmation[35]["close"] = Decimal("112")
    return confirmation + [dict(candle) for candle in candles[36:]]


def _causal_dataset(candles: list[dict[str, Decimal | int]]) -> dict[str, list[dict[str, Decimal | int]]]:
    confirmation = _confirmation_candles(candles)
    htf = _trend_candles()
    return {
        "15m": _time_aligned(candles, duration_ms=FIFTEEN_MINUTES_MS, start_ms=BASE_TIMESTAMP_MS),
        "5m": _time_aligned(confirmation, duration_ms=FIVE_MINUTES_MS, start_ms=CONFIRMATION_START_MS),
        "2d": _time_aligned(htf, duration_ms=TWO_DAYS_MS, start_ms=BASE_TIMESTAMP_MS - (len(htf) * TWO_DAYS_MS)),
    }


def _base_candles(count: int = 45, *, volume: Decimal = Decimal("100")) -> list[dict[str, Decimal | int]]:
    return [
        {
            "timestamp": index,
            "open": Decimal("100"),
            "high": Decimal("105"),
            "low": Decimal("95"),
            "close": Decimal("100"),
            "volume": volume,
        }
        for index in range(count)
    ]


def _full_bullish_setup_candles(*, sweep_volume: Decimal = Decimal("200")) -> list[dict[str, Decimal | int]]:
    candles = _base_candles(36)
    candles[20]["low"] = Decimal("90")
    candles[24]["high"] = Decimal("110")
    candles[30]["low"] = Decimal("85")
    candles[30]["close"] = Decimal("91")
    candles[30]["volume"] = sweep_volume
    candles[33]["open"] = Decimal("99")
    candles[33]["close"] = Decimal("97")
    candles[33]["low"] = Decimal("95")
    candles[33]["high"] = Decimal("100")
    candles[35]["open"] = Decimal("104")
    candles[35]["high"] = Decimal("114")
    candles[35]["low"] = Decimal("101")
    candles[35]["close"] = Decimal("112")
    return candles


def _trend_candles(count: int = 30) -> list[dict[str, Decimal | int]]:
    candles: list[dict[str, Decimal | int]] = []
    for index in range(count):
        price = Decimal(100 + index)
        candles.append(
            {
                "timestamp": index,
                "open": price,
                "high": price + Decimal("2"),
                "low": price - Decimal("2"),
                "close": price + Decimal("1"),
                "volume": Decimal("100"),
            }
        )
    return candles


def _run_replay(
    future_candles: list[dict[str, Decimal | int]],
    *,
    symbol: str = "BTCUSDT",
    max_hold_candles: int = 10,
    max_fill_candles: int = 5,
    same_candle_policy: str = "conservative",
):
    candles = _candles_with_future(future_candles)
    return StrategyReplayEngine().run(
        {symbol: _causal_dataset(candles)},
        ReplayConfig(
            modes=("swing",),
            max_hold_candles=max_hold_candles,
            max_fill_candles=max_fill_candles,
            same_candle_policy=same_candle_policy,
        ),
    )


def _candles_with_future(future_candles: list[dict[str, Decimal | int]]) -> list[dict[str, Decimal | int]]:
    candles = _full_bullish_setup_candles()
    candles.extend(future_candles)
    return candles


def _dataset(future_candles: list[dict[str, Decimal | int]]) -> dict[str, list[dict[str, Decimal | int]]]:
    candles = _candles_with_future(future_candles)
    return _causal_dataset(candles)


def test_replay_detects_valid_historical_setup_and_limit_fill() -> None:
    summary = _run_replay(
        [
            {
                "timestamp": 36,
                "open": Decimal("112"),
                "high": Decimal("113"),
                "low": Decimal("97"),
                "close": Decimal("105"),
                "volume": Decimal("100"),
            }
        ]
    )

    trade = summary.symbols[0].trades[0]
    assert summary.stats.total_setups == 1
    assert trade.candidate.entry == Decimal("97.00000000")
    assert trade.filled is True
    assert trade.entry_filled is True
    assert trade.fill_index == 36
    assert trade.time_to_entry == 1


def test_replay_does_not_use_pre_confirmation_entry_touch() -> None:
    summary = _run_replay([], max_fill_candles=1)

    trade = summary.symbols[0].trades[0]
    assert trade.candidate.detected_at_index == 35
    assert trade.outcome == ReplayOutcome.NOT_FILLED
    assert trade.filled is False
    assert trade.fill_index == "N/A"


def test_replay_simulates_tp_hit() -> None:
    summary = _run_replay(
        [
            {
                "timestamp": 36,
                "open": Decimal("112"),
                "high": Decimal("113"),
                "low": Decimal("97"),
                "close": Decimal("105"),
                "volume": Decimal("100"),
            },
            {
                "timestamp": 37,
                "open": Decimal("105"),
                "high": Decimal("125"),
                "low": Decimal("104"),
                "close": Decimal("122"),
                "volume": Decimal("100"),
            },
        ]
    )

    trade = summary.symbols[0].trades[0]
    assert trade.outcome == ReplayOutcome.TP1_HIT
    assert trade.highest_tp_hit == 1
    assert trade.tp1_hit is True
    assert trade.time_to_tp1 == 1
    assert trade.r_multiple > 0


def test_replay_simulates_stop_hit() -> None:
    summary = _run_replay(
        [
            {
                "timestamp": 36,
                "open": Decimal("99"),
                "high": Decimal("100"),
                "low": Decimal("97"),
                "close": Decimal("98"),
                "volume": Decimal("100"),
            },
            {
                "timestamp": 37,
                "open": Decimal("98"),
                "high": Decimal("99"),
                "low": Decimal("83"),
                "close": Decimal("84"),
                "volume": Decimal("100"),
            },
        ]
    )

    trade = summary.symbols[0].trades[0]
    assert trade.outcome == ReplayOutcome.STOPPED
    assert trade.sl_hit is True
    assert trade.r_multiple == Decimal("-1.00000000")


def test_replay_keeps_sl_before_later_tp_as_stop() -> None:
    summary = _run_replay(
        [
            {
                "timestamp": 36,
                "open": Decimal("99"),
                "high": Decimal("100"),
                "low": Decimal("97"),
                "close": Decimal("98"),
                "volume": Decimal("100"),
            },
            {
                "timestamp": 37,
                "open": Decimal("98"),
                "high": Decimal("99"),
                "low": Decimal("83"),
                "close": Decimal("84"),
                "volume": Decimal("100"),
            },
            {
                "timestamp": 38,
                "open": Decimal("84"),
                "high": Decimal("130"),
                "low": Decimal("84"),
                "close": Decimal("125"),
                "volume": Decimal("100"),
            },
        ]
    )

    trade = summary.symbols[0].trades[0]
    assert trade.outcome == ReplayOutcome.STOPPED
    assert trade.tp1_hit is False
    assert trade.r_multiple == Decimal("-1.00000000")


def test_replay_keeps_tp_before_later_sl_as_tp() -> None:
    summary = _run_replay(
        [
            {
                "timestamp": 36,
                "open": Decimal("99"),
                "high": Decimal("100"),
                "low": Decimal("97"),
                "close": Decimal("98"),
                "volume": Decimal("100"),
            },
            {
                "timestamp": 37,
                "open": Decimal("98"),
                "high": Decimal("125"),
                "low": Decimal("98"),
                "close": Decimal("120"),
                "volume": Decimal("100"),
            },
            {
                "timestamp": 38,
                "open": Decimal("120"),
                "high": Decimal("121"),
                "low": Decimal("83"),
                "close": Decimal("85"),
                "volume": Decimal("100"),
            },
        ]
    )

    trade = summary.symbols[0].trades[0]
    assert trade.outcome == ReplayOutcome.TP1_HIT
    assert trade.tp1_hit is True
    assert trade.sl_hit is False


def test_same_candle_conservative_policy_chooses_stop_first() -> None:
    summary = _run_replay(
        [
            {
                "timestamp": 36,
                "open": Decimal("112"),
                "high": Decimal("125"),
                "low": Decimal("80"),
                "close": Decimal("90"),
                "volume": Decimal("100"),
            }
        ],
        same_candle_policy="conservative",
    )

    trade = summary.symbols[0].trades[0]
    assert trade.outcome == ReplayOutcome.STOPPED
    assert trade.r_multiple == Decimal("-1.00000000")


def test_not_filled_setup_expires_correctly() -> None:
    summary = _run_replay(
        [
            {
                "timestamp": 36,
                "open": Decimal("112"),
                "high": Decimal("116"),
                "low": Decimal("100"),
                "close": Decimal("111"),
                "volume": Decimal("100"),
            },
            {
                "timestamp": 37,
                "open": Decimal("111"),
                "high": Decimal("116"),
                "low": Decimal("100"),
                "close": Decimal("112"),
                "volume": Decimal("100"),
            },
        ],
        max_fill_candles=2,
    )

    trade = summary.symbols[0].trades[0]
    assert trade.outcome == ReplayOutcome.NOT_FILLED
    assert trade.outcome.value == "missed_entry"
    assert trade.filled is False
    assert trade.entry_filled is False
    assert summary.stats.filled_trades == 0
    assert summary.stats.missed_entries == 1


def test_filled_trade_expires_correctly() -> None:
    summary = _run_replay(
        [
            {
                "timestamp": 36,
                "open": Decimal("99"),
                "high": Decimal("100"),
                "low": Decimal("97"),
                "close": Decimal("98"),
                "volume": Decimal("100"),
            },
            {
                "timestamp": 37,
                "open": Decimal("98"),
                "high": Decimal("110"),
                "low": Decimal("96"),
                "close": Decimal("100"),
                "volume": Decimal("100"),
            },
            {
                "timestamp": 38,
                "open": Decimal("100"),
                "high": Decimal("110"),
                "low": Decimal("96"),
                "close": Decimal("101"),
                "volume": Decimal("100"),
            },
        ],
        max_hold_candles=2,
    )

    trade = summary.symbols[0].trades[0]
    assert trade.outcome == ReplayOutcome.EXPIRED
    assert trade.filled is True
    assert trade.candles_held == 2


def test_replay_r_multiple_and_excursions_calculate_from_trade_levels() -> None:
    summary = _run_replay(
        [
            {
                "timestamp": 36,
                "open": Decimal("112"),
                "high": Decimal("113"),
                "low": Decimal("97"),
                "close": Decimal("105"),
                "volume": Decimal("100"),
            },
            {
                "timestamp": 37,
                "open": Decimal("105"),
                "high": Decimal("125"),
                "low": Decimal("104"),
                "close": Decimal("122"),
                "volume": Decimal("100"),
            },
        ]
    )

    trade = summary.symbols[0].trades[0]
    risk = abs(trade.entry - trade.stop)
    expected_r = ((trade.tp1 - trade.entry) / risk).quantize(Decimal("0.00000001"))
    assert trade.final_r_multiple == expected_r
    assert trade.max_favorable_excursion >= expected_r
    assert trade.max_adverse_excursion <= Decimal("0")


def test_summary_stats_calculate_correctly_and_low_sample_warning_appears() -> None:
    summary = StrategyReplayEngine().run(
        {
            "WINUSDT": _dataset(
                [
                    {
                        "timestamp": 36,
                        "open": Decimal("112"),
                        "high": Decimal("113"),
                        "low": Decimal("97"),
                        "close": Decimal("105"),
                        "volume": Decimal("100"),
                    },
                    {
                        "timestamp": 37,
                        "open": Decimal("105"),
                        "high": Decimal("125"),
                        "low": Decimal("104"),
                        "close": Decimal("122"),
                        "volume": Decimal("100"),
                    },
                ]
            ),
            "LOSSUSDT": _dataset(
                [
                    {
                        "timestamp": 36,
                        "open": Decimal("99"),
                        "high": Decimal("100"),
                        "low": Decimal("97"),
                        "close": Decimal("98"),
                        "volume": Decimal("100"),
                    },
                    {
                        "timestamp": 37,
                        "open": Decimal("98"),
                        "high": Decimal("99"),
                        "low": Decimal("83"),
                        "close": Decimal("84"),
                        "volume": Decimal("100"),
                    },
                ]
            ),
        },
        ReplayConfig(modes=("swing",), max_hold_candles=10, max_fill_candles=5),
    )

    assert summary.stats.total_setups == 2
    assert summary.stats.filled_trades == 2
    assert summary.stats.missed_entries == 0
    assert summary.stats.win_rate == Decimal("50.00")
    assert summary.stats.tp1_rate == Decimal("50.00")
    assert summary.stats.average_r != "N/A"
    assert summary.stats.median_r != "N/A"
    assert summary.stats.max_drawdown_r != "N/A"
    assert summary.stats.best_r != "N/A"
    assert summary.stats.worst_r != "N/A"
    assert summary.stats.max_win_streak == 1
    assert summary.stats.max_loss_streak == 1
    assert summary.sample_size_warning == "low_sample_size"
    assert summary.per_mode_stats["swing"].total_setups == 2


def test_backtest_json_payload_contains_phase_26_sections() -> None:
    summary = _run_replay(
        [
            {
                "timestamp": 36,
                "open": Decimal("112"),
                "high": Decimal("113"),
                "low": Decimal("97"),
                "close": Decimal("105"),
                "volume": Decimal("100"),
            }
        ]
    )

    payload = backtest_json_payload(summary)

    assert "backtest_summary" in payload
    assert "per_symbol_stats" in payload
    assert "per_mode_stats" in payload
    assert "individual_setup_results" in payload
    assert "edge_analytics" in payload
    assert "expectancy_metrics" in payload
    assert payload["confidence_label"] == "LOW SAMPLE"
    setup = payload["individual_setup_results"][0]
    assert setup["entry"] == "97"
    assert setup["entry_filled"] is True
    assert "risk_warning" in setup
    assert setup["condition_key"]["symbol"] == "BTCUSDT"
    assert setup["condition_key"]["mode"] == "swing"
