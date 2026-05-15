from __future__ import annotations

from decimal import Decimal

from app.analytics.derivatives_enrichment import DerivativesEnrichmentEngine, enrich_derivatives
from app.data.dtos import NA


def _candles(first: Decimal = Decimal("100"), last: Decimal = Decimal("102")) -> list[dict[str, Decimal | int]]:
    return [
        {"timestamp": 1, "open": first, "high": first, "low": first, "close": first, "volume": Decimal("100")},
        {"timestamp": 2, "open": last, "high": last, "low": last, "close": last, "volume": Decimal("100")},
    ]


def _base_input(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "symbol": "BTCUSDT",
        "exchange": "binance",
        "latest_price": Decimal("102"),
        "current_funding_rate": Decimal("0.0001"),
        "current_open_interest": Decimal("110"),
        "previous_open_interest": Decimal("100"),
        "candles_15m": _candles(),
        "funding_history": [Decimal("0.0001"), Decimal("0.0002"), Decimal("0.0001")],
        "open_interest_history": [Decimal("100"), Decimal("110")],
        "long_short_ratio": Decimal("1.10"),
    }
    data.update(overrides)
    return data


def test_funding_status_positive_normal_elevated_extreme() -> None:
    engine = DerivativesEnrichmentEngine()

    assert engine.analyze(_base_input(current_funding_rate=Decimal("0.0001"))).funding_status == "normal"
    assert engine.analyze(_base_input(current_funding_rate=Decimal("0.0005"))).funding_status == "elevated_positive"
    assert engine.analyze(_base_input(current_funding_rate=Decimal("0.0010"))).funding_status == "extreme_positive"


def test_funding_status_negative_normal_elevated_extreme() -> None:
    engine = DerivativesEnrichmentEngine()

    assert engine.analyze(_base_input(current_funding_rate=Decimal("-0.0001"))).funding_status == "normal"
    assert engine.analyze(_base_input(current_funding_rate=Decimal("-0.0005"))).funding_status == "elevated_negative"
    assert engine.analyze(_base_input(current_funding_rate=Decimal("-0.0010"))).funding_status == "extreme_negative"


def test_oi_change_pct_calculation() -> None:
    result = enrich_derivatives(_base_input(current_open_interest=Decimal("125"), previous_open_interest=Decimal("100")))

    assert result.open_interest_change_pct == Decimal("25.00000000")
    assert result.oi_direction == "rising"


def test_price_up_oi_up_relationship() -> None:
    result = enrich_derivatives(_base_input(candles_15m=_candles(Decimal("100"), Decimal("102"))))

    assert result.price_direction == "up"
    assert result.price_oi_relationship == "long_building_or_breakout_participation"


def test_price_up_oi_down_relationship() -> None:
    result = enrich_derivatives(
        _base_input(
            current_open_interest=Decimal("90"),
            previous_open_interest=Decimal("100"),
            candles_15m=_candles(Decimal("100"), Decimal("102")),
            latest_price=Decimal("102"),
        )
    )

    assert result.oi_direction == "falling"
    assert result.price_oi_relationship == "short_covering_or_weak_participation"


def test_price_down_oi_up_relationship() -> None:
    result = enrich_derivatives(
        _base_input(candles_15m=_candles(Decimal("100"), Decimal("98")), latest_price=Decimal("98"))
    )

    assert result.price_direction == "down"
    assert result.price_oi_relationship == "short_building_or_long_trap_risk"


def test_price_down_oi_down_relationship() -> None:
    result = enrich_derivatives(
        _base_input(
            current_open_interest=Decimal("90"),
            previous_open_interest=Decimal("100"),
            candles_15m=_candles(Decimal("100"), Decimal("98")),
            latest_price=Decimal("98"),
        )
    )

    assert result.price_oi_relationship == "long_unwind_or_deleveraging"


def test_price_flat_oi_flat_relationship_is_neutral_not_na() -> None:
    result = enrich_derivatives(
        _base_input(
            current_open_interest=Decimal("100"),
            previous_open_interest=Decimal("100"),
            candles_15m=_candles(Decimal("100"), Decimal("100")),
            latest_price=Decimal("100"),
        )
    )

    assert result.price_direction == "flat"
    assert result.oi_direction == "flat"
    assert result.price_oi_relationship == "neutral_or_no_clear_positioning"
    assert "price_oi_relationship: N/A" not in result.missing_data


def test_missing_oi_returns_na() -> None:
    result = enrich_derivatives(
        _base_input(current_open_interest=NA, previous_open_interest=NA, open_interest_history=None)
    )

    assert result.open_interest == NA
    assert result.open_interest_change_pct == NA
    assert result.oi_direction == NA
    assert "open_interest: N/A" in result.missing_data


def test_missing_funding_returns_na() -> None:
    result = enrich_derivatives(_base_input(current_funding_rate=NA, funding_history=None))

    assert result.funding_rate == NA
    assert result.funding_status == NA
    assert result.funding_extreme == NA
    assert "funding_rate: N/A" in result.missing_data


def test_crowding_risk_high_when_funding_and_long_short_imbalance_extreme() -> None:
    result = enrich_derivatives(
        _base_input(
            current_funding_rate=Decimal("0.0015"),
            current_open_interest=Decimal("112"),
            previous_open_interest=Decimal("100"),
            long_short_ratio=Decimal("1.90"),
        )
    )

    assert result.crowding_risk == "high"
    assert result.crowding_context.risk_direction == "long"
    assert result.supports_long is False


def test_squeeze_risk_detection() -> None:
    result = enrich_derivatives(
        _base_input(
            current_funding_rate=Decimal("-0.0015"),
            current_open_interest=Decimal("112"),
            previous_open_interest=Decimal("100"),
            long_short_ratio=Decimal("0.50"),
            candles_15m=_candles(Decimal("100"), Decimal("102")),
        )
    )

    assert result.squeeze_risk == "short_squeeze_risk"
    assert result.supports_short is False


def test_derivatives_score_with_full_data() -> None:
    result = enrich_derivatives(_base_input())

    assert result.derivatives_score == 100


def test_derivatives_score_with_missing_data() -> None:
    result = enrich_derivatives({"symbol": "BTCUSDT", "exchange": "binance"})

    assert result.derivatives_score == 0
    assert result.funding_status == NA
    assert result.price_oi_relationship == NA
