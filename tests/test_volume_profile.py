from __future__ import annotations

from decimal import Decimal

import pytest

from app.analytics.volume_profile import VOLUME_PROFILE_SOURCE, VolumeProfileInput, calculate_volume_profile
from app.data.dtos import NA


def _profile_candle(low: str, high: str, volume: str | None, *, close: str | None = None) -> dict[str, Decimal | None]:
    close_value = Decimal(close) if close is not None else Decimal(high)
    return {
        "open": Decimal(low),
        "high": Decimal(high),
        "low": Decimal(low),
        "close": close_value,
        "volume": Decimal(volume) if volume is not None else None,
    }


def test_volume_profile_returns_na_when_no_candles() -> None:
    result = calculate_volume_profile({"symbol": "BTCUSDT", "timeframe": "15m", "candles": []})

    assert result.poc == NA
    assert result.value_area_high == NA
    assert result.value_area_low == NA
    assert result.total_volume == NA
    assert result.candles_used == 0
    assert "candles: N/A" in result.missing_data
    assert result.warnings


def test_volume_profile_returns_na_when_volume_missing() -> None:
    result = calculate_volume_profile(
        {
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "candles": [
                _profile_candle("100", "101", None),
                _profile_candle("101", "102", None),
            ],
        }
    )

    assert result.poc == NA
    assert result.total_volume == NA
    assert result.high_volume_nodes == ()
    assert result.low_volume_nodes == ()
    assert result.missing_data == ("volume: N/A",)
    assert result.warnings


def test_volume_profile_calculates_poc_from_mocked_candles() -> None:
    result = calculate_volume_profile(
        VolumeProfileInput(
            symbol="BTCUSDT",
            timeframe="15m",
            bucket_count=4,
            candles=[
                _profile_candle("100", "101", "100"),
                _profile_candle("101", "102", "200"),
                _profile_candle("102", "103", "300", close="102.5"),
                _profile_candle("103", "104", "50"),
            ],
        )
    )

    assert result.poc == Decimal("102.50000000")
    assert result.total_volume == Decimal("650.00000000")
    assert result.candles_used == 4


def test_volume_profile_calculates_value_area_deterministically() -> None:
    result = calculate_volume_profile(
        {
            "symbol": "BTCUSDT",
            "timeframe": "15m",
            "bucket_count": 4,
            "value_area_pct": Decimal("0.70"),
            "candles": [
                _profile_candle("100", "101", "100"),
                _profile_candle("101", "102", "200"),
                _profile_candle("102", "103", "300", close="102.5"),
                _profile_candle("103", "104", "50"),
            ],
        }
    )

    assert result.value_area_high == Decimal("103.00000000")
    assert result.value_area_low == Decimal("101.00000000")


def test_volume_profile_source_is_estimated_from_candles() -> None:
    result = calculate_volume_profile(
        {
            "symbol": "ETHUSDT",
            "timeframe": "15m",
            "candles": [_profile_candle("100", "101", "10")],
        }
    )

    assert result.source == VOLUME_PROFILE_SOURCE


def test_volume_profile_rejects_malformed_candles() -> None:
    with pytest.raises(ValueError, match="high is lower than low"):
        calculate_volume_profile(
            {
                "symbol": "BTCUSDT",
                "timeframe": "15m",
                "candles": [
                    {
                        "open": Decimal("100"),
                        "high": Decimal("99"),
                        "low": Decimal("100"),
                        "close": Decimal("100"),
                        "volume": Decimal("1"),
                    }
                ],
            }
        )
