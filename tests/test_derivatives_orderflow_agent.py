from __future__ import annotations

from decimal import Decimal

from app.agents.derivatives_orderflow import DerivativesOrderflowAgent
from app.data.dtos import NA


def _agent() -> DerivativesOrderflowAgent:
    return DerivativesOrderflowAgent()


def _base_data() -> dict[str, Decimal]:
    return {
        "price_change_pct": Decimal("1.00"),
        "current_open_interest": Decimal("105"),
        "previous_open_interest": Decimal("100"),
        "funding_rate": Decimal("0.0001"),
        "volume_z_score": Decimal("2.50"),
    }


def test_positive_funding_classification() -> None:
    result = _agent().analyze(_base_data())

    assert result.funding.raw_funding_rate == Decimal("0.00010000")
    assert result.funding.direction == "positive"
    assert result.funding.severity == "normal"
    assert result.funding.z_score == NA


def test_negative_funding_classification() -> None:
    data = _base_data()
    data["funding_rate"] = Decimal("-0.0006")

    result = _agent().analyze(data)

    assert result.funding.raw_funding_rate == Decimal("-0.00060000")
    assert result.funding.direction == "negative"
    assert result.funding.severity == "elevated"


def test_funding_z_score_calculation() -> None:
    data = _base_data()
    data["funding_rate"] = Decimal("0.0004")

    result = _agent().analyze(
        data,
        historical_funding_rates=[Decimal("0.0001"), Decimal("0.0002"), Decimal("0.0003")],
    )

    assert result.funding.historical_sample_size == 3
    assert result.funding.z_score == Decimal("2.44948974")


def test_oi_percentage_change() -> None:
    data = _base_data()
    data["current_open_interest"] = Decimal("110")

    result = _agent().analyze(data)

    assert result.open_interest.oi_change_percentage == Decimal("10.00000000")
    assert result.open_interest.direction == "increasing"


def test_price_up_oi_up_classification() -> None:
    result = _agent().analyze(_base_data())

    assert result.price_oi_relationship.price_direction == "up"
    assert result.price_oi_relationship.oi_direction == "increasing"
    assert result.price_oi_relationship.classification == "new participation / trend participation"


def test_price_up_oi_down_classification() -> None:
    data = _base_data()
    data["current_open_interest"] = Decimal("90")

    result = _agent().analyze(data)

    assert result.price_oi_relationship.classification == "short covering / weaker continuation"


def test_price_down_oi_up_classification() -> None:
    data = _base_data()
    data["price_change_pct"] = Decimal("-1.00")

    result = _agent().analyze(data)

    assert result.price_oi_relationship.classification == "new shorts / possible short crowding"


def test_price_down_oi_down_classification() -> None:
    data = _base_data()
    data["price_change_pct"] = Decimal("-1.00")
    data["current_open_interest"] = Decimal("90")

    result = _agent().analyze(data)

    assert result.price_oi_relationship.classification == "long liquidation / de-risking"


def test_crowded_long_risk() -> None:
    data = _base_data()
    data["funding_rate"] = Decimal("0.0006")

    result = _agent().analyze(data)

    assert result.crowding_risk.crowded_long_risk is True
    assert result.crowding_risk.risk_direction == "long"
    assert result.risk_flags.crowded_long_risk is True
    assert "crowded_long_risk" in result.active_risk_flags


def test_crowded_short_risk() -> None:
    data = _base_data()
    data["price_change_pct"] = Decimal("-1.00")
    data["funding_rate"] = Decimal("-0.0006")

    result = _agent().analyze(data)

    assert result.crowding_risk.crowded_short_risk is True
    assert result.crowding_risk.risk_direction == "short"
    assert result.risk_flags.crowded_short_risk is True
    assert "crowded_short_risk" in result.active_risk_flags


def test_missing_funding_handling() -> None:
    data = _base_data()
    del data["funding_rate"]

    result = _agent().analyze(data)

    assert result.funding.raw_funding_rate == NA
    assert result.funding.direction == NA
    assert result.funding.severity == NA
    assert result.funding.z_score == NA
    assert result.risk_flags.missing_funding is True


def test_missing_oi_handling() -> None:
    data = _base_data()
    del data["current_open_interest"]

    result = _agent().analyze(data)

    assert result.is_valid is False
    assert result.data_quality.status == "partial"
    assert result.open_interest.current_open_interest == NA
    assert result.open_interest.oi_change_percentage == NA
    assert result.risk_flags.missing_open_interest is True


def test_missing_volume_handling() -> None:
    data = _base_data()
    del data["volume_z_score"]

    result = _agent().analyze(data)

    assert result.is_valid is True
    assert result.volume_confirmation.volume_z_score == NA
    assert result.volume_confirmation.confirmation == NA
    assert result.risk_flags.missing_volume is True
    assert result.data_quality.reliability == "Unverified"


def test_derivatives_score_calculation() -> None:
    result = _agent().analyze(_base_data())

    assert result.score_components == {
        "price_oi_clarity": 25,
        "funding_context": 20,
        "oi_change_significance": 20,
        "volume_confirmation": 15,
        "crowding_risk_adjustment": 10,
        "data_quality": 10,
    }
    assert result.derivatives_score == 100


def test_data_quality_invalid_case() -> None:
    data = _base_data()
    del data["price_change_pct"]

    result = _agent().analyze(data)

    assert result.is_valid is False
    assert result.data_quality.status == "invalid"
    assert result.derivatives_score == 0
    assert "price_change_percentage" in result.data_quality.missing_fields


def test_no_cvd_or_liquidation_invention() -> None:
    result = _agent().analyze(_base_data())

    assert result.cvd == NA
    assert result.liquidation_heatmap == NA
    assert result.data_quality.cvd == NA
    assert result.data_quality.liquidation_heatmap == NA
    assert "CVD" in result.data_quality.unverified_fields
    assert "liquidation_heatmap" in result.data_quality.unverified_fields
