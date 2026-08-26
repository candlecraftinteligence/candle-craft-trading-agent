from __future__ import annotations

from app.core.confirmed_data_health import classify_confirmed_data_health


PRODUCTION_OPTIONAL_FIELDS = (
    "liquidation_data",
    "liquidity_below",
    "liquidity_above",
    "orderflow_summary",
    "cvd",
    "btc_context",
    "btc_d_context",
    "event_risk_context",
    "weekend_filter",
    "sector_rotation",
    "narrative",
    "liquidation_heatmap",
)


def test_production_fields_are_optional_by_audited_domain() -> None:
    report = classify_confirmed_data_health(
        missing_values=(tuple(f"{field}: N/A" for field in PRODUCTION_OPTIONAL_FIELDS),),
        unverified_values=(("CVD: Unverified", "liquidation_heatmap: Unverified"),),
    )

    assert report.blocked is False
    assert report.required_missing == ()
    assert report.required_unverified == ()
    assert report.optional_missing == PRODUCTION_OPTIONAL_FIELDS
    assert report.optional_unverified == ("cvd", "liquidation_heatmap")
    assert report.blocking_reasons == ()
    assert report.diagnostic_reasons == (
        f"optional_data_missing:{','.join(PRODUCTION_OPTIONAL_FIELDS)}",
        "optional_data_unverified:cvd,liquidation_heatmap",
    )


def test_other_current_enrichment_and_scoring_domains_are_non_blocking() -> None:
    optional_fields = (
        "poc",
        "value_area_high",
        "value_area_low",
        "volume",
        "ticker",
        "funding_rate",
        "funding_history",
        "open_interest",
        "open_interest_history",
        "previous_open_interest",
        "current_open_interest",
        "open_interest_change_pct",
        "price_change_percentage",
        "price_direction",
        "price_oi_relationship",
        "long_short_ratio",
        "volume_z_score",
        "derivatives",
        "liquidity",
        "catalyst",
        "derivatives_summary",
    )

    report = classify_confirmed_data_health(
        missing_values=(tuple(f"{field}: N/A" for field in optional_fields),),
    )

    assert report.blocked is False
    assert report.optional_missing == optional_fields


def test_decision_critical_and_unknown_fields_fail_closed() -> None:
    required_fields = (
        "candles",
        "candles_15m",
        "candle_sufficiency_15m",
        "technical",
        "data_quality",
        "current_price",
        "candidate_direction",
        "structure_level",
        "entry_zone",
        "stop_loss",
        "invalidation",
        "take_profit_targets",
        "scanner",
        "future_unclassified_feed",
    )
    report = classify_confirmed_data_health(
        missing_values=(
            (
                "candles: N/A",
                "candles_15m: N/A",
                "candle_sufficiency[15m]: N/A (status=INSUFFICIENT_DATA)",
                "technical: N/A",
                "data_quality: N/A",
                "current_price: N/A",
                "candidate_direction: N/A",
                "structure_level: N/A",
                "entry_zone: N/A",
                "stop_loss: N/A",
                "invalidation: N/A",
                "take_profit_targets: N/A",
                "scanner: N/A",
                "future_unclassified_feed: N/A",
            ),
        ),
    )

    assert report.blocked is True
    assert report.required_missing == required_fields
    assert report.blocking_reasons == (
        f"required_data_missing:{','.join(required_fields)}",
    )


def test_required_unverified_and_optional_unverified_are_separated() -> None:
    report = classify_confirmed_data_health(
        unverified_values=(
            (
                "candles_15m: Unverified",
                "technical: Unverified",
                "long_short_ratio: Unverified",
                "CVD: Unverified",
            ),
        ),
    )

    assert report.blocked is True
    assert report.required_unverified == ("candles_15m", "technical")
    assert report.optional_unverified == ("long_short_ratio", "cvd")
    assert report.blocking_reasons == (
        "required_data_unverified:candles_15m,technical",
    )
    assert report.diagnostic_reasons == (
        "optional_data_unverified:long_short_ratio,cvd",
    )
