from __future__ import annotations

from decimal import Decimal

from app.backtesting.strategy_replay import ReplayConfig, ReplayOutcome, StrategyReplayEngine


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
        {symbol: {"15m": candles, "5m": candles, "2d": _trend_candles()}},
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
    return {"15m": candles, "5m": candles, "2d": _trend_candles()}


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
    assert trade.fill_index == 36


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
    assert trade.r_multiple == Decimal("-1.00000000")


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
    assert trade.filled is False
    assert summary.stats.filled_trades == 0


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
    assert summary.stats.win_rate == Decimal("50.00")
    assert summary.stats.tp1_rate == Decimal("50.00")
    assert summary.stats.max_win_streak == 1
    assert summary.stats.max_loss_streak == 1
    assert summary.sample_size_warning == "low_sample_size"
