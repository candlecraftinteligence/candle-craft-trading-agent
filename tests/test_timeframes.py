from __future__ import annotations

from decimal import Decimal

from app.data.dtos import NA, CandleDTO
from app.data.timeframes import resample_ohlcv_candles

DAY_MS = 86_400_000


def test_resample_ohlcv_candles_merges_complete_daily_pairs() -> None:
    candles = [
        {"timestamp": 0, "open": Decimal("100"), "high": Decimal("110"), "low": Decimal("95"), "close": Decimal("105"), "volume": Decimal("10")},
        {"timestamp": DAY_MS, "open": Decimal("105"), "high": Decimal("115"), "low": Decimal("101"), "close": Decimal("112"), "volume": Decimal("20")},
        {"timestamp": 2 * DAY_MS, "open": Decimal("112"), "high": Decimal("120"), "low": Decimal("108"), "close": Decimal("118"), "volume": Decimal("30")},
        {"timestamp": 3 * DAY_MS, "open": Decimal("118"), "high": Decimal("125"), "low": Decimal("111"), "close": Decimal("113"), "volume": Decimal("40")},
        {"timestamp": 4 * DAY_MS, "open": Decimal("113"), "high": Decimal("130"), "low": Decimal("109"), "close": Decimal("129"), "volume": Decimal("50")},
    ]

    result = resample_ohlcv_candles(candles, decision_timestamp=5 * DAY_MS)

    assert result == [
        {
            "timestamp": 0,
            "close_timestamp": 2 * DAY_MS,
            "interval": "2d",
            "open": Decimal("100"),
            "high": Decimal("115"),
            "low": Decimal("95"),
            "close": Decimal("112"),
            "volume": Decimal("30"),
        },
        {
            "timestamp": 2 * DAY_MS,
            "close_timestamp": 4 * DAY_MS,
            "interval": "2d",
            "open": Decimal("112"),
            "high": Decimal("125"),
            "low": Decimal("108"),
            "close": Decimal("113"),
            "volume": Decimal("70"),
        },
    ]


def test_resample_ohlcv_candles_preserves_candle_dto_compatibility() -> None:
    candles = [
        CandleDTO(
            exchange="binance_futures",
            symbol="BTCUSDT",
            timestamp=0,
            interval="1d",
            open=Decimal("100"),
            high=Decimal("110"),
            low=Decimal("95"),
            close=Decimal("105"),
            volume=Decimal("10"),
            close_timestamp=DAY_MS - 1,
            quote_volume=Decimal("1000"),
            trade_count=5,
        ),
        CandleDTO(
            exchange="binance_futures",
            symbol="BTCUSDT",
            timestamp=DAY_MS,
            interval="1d",
            open=Decimal("105"),
            high=Decimal("115"),
            low=Decimal("101"),
            close=Decimal("112"),
            volume=Decimal("20"),
            close_timestamp=(2 * DAY_MS) - 1,
            quote_volume=NA,
            trade_count=7,
        ),
    ]

    result = resample_ohlcv_candles(candles, decision_timestamp=2 * DAY_MS)

    assert len(result) == 1
    assert isinstance(result[0], CandleDTO)
    assert result[0].exchange == "binance_futures"
    assert result[0].symbol == "BTCUSDT"
    assert result[0].interval == "2d"
    assert result[0].timestamp == 0
    assert result[0].open == Decimal("100")
    assert result[0].high == Decimal("115")
    assert result[0].low == Decimal("95")
    assert result[0].close == Decimal("112")
    assert result[0].volume == Decimal("30")
    assert result[0].close_timestamp == (2 * DAY_MS) - 1
    assert result[0].quote_volume == NA
    assert result[0].trade_count == 12
