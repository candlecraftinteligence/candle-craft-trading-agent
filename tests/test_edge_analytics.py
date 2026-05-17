from __future__ import annotations

from decimal import Decimal

from app.analytics.edge_analytics import (
    EdgeAnalyticsRecord,
    EdgeConditionKey,
    build_edge_analytics_report,
    condition_key_from_diagnostics,
    confidence_label,
    expectancy_metrics,
    match_historical_condition,
)
from app.data.dtos import NA


def _key(symbol: str = "BTCUSDT", *, mode: str = "swing", rr_bucket: str = "rr_3_to_3_99") -> EdgeConditionKey:
    return EdgeConditionKey(
        symbol=symbol,
        mode=mode,
        htf_direction_alignment="aligned",
        derivatives_state="supportive",
        volume_profile_alignment="entry_overlaps_poc",
        rr_bucket=rr_bucket,
        readiness_score_bucket="readiness_85_plus",
        sweep_quality="solid",
        pullback_quality="clean",
        ob_fvg_quality="ob_selected",
        trend_alignment="aligned",
        crowding_state="low",
    )


def test_expectancy_metrics_calculate_rates_r_and_drawdown() -> None:
    metrics = expectancy_metrics(
        (
            EdgeAnalyticsRecord(condition_key=_key(), filled=True, tp1_hit=True, tp2_hit=False, r_multiple=Decimal("1.5"), candles_held=4),
            EdgeAnalyticsRecord(condition_key=_key(), filled=True, tp1_hit=False, tp2_hit=False, r_multiple=Decimal("-1"), candles_held=2),
            EdgeAnalyticsRecord(condition_key=_key(), filled=True, tp1_hit=True, tp2_hit=True, r_multiple=Decimal("3"), candles_held=6),
            EdgeAnalyticsRecord(condition_key=_key(), filled=False, r_multiple=NA),
        )
    )

    assert metrics.setups == 4
    assert metrics.fills == 3
    assert metrics.tp1_hit_rate == Decimal("66.67")
    assert metrics.tp2_hit_rate == Decimal("33.33")
    assert metrics.average_r == Decimal("1.16666667")
    assert metrics.median_r == Decimal("1.50000000")
    assert metrics.max_drawdown == Decimal("1.00000000")
    assert metrics.expectancy == Decimal("1.16666667")
    assert metrics.average_hold_time == Decimal("4.00000000")


def test_confidence_label_respects_low_sample_and_negative_edge() -> None:
    low_sample = expectancy_metrics(
        (
            EdgeAnalyticsRecord(condition_key=_key(), filled=True, tp1_hit=True, r_multiple=Decimal("2")),
        )
    )
    negative = expectancy_metrics(
        (
            EdgeAnalyticsRecord(condition_key=_key(), filled=True, r_multiple=Decimal("-1")),
            EdgeAnalyticsRecord(condition_key=_key(), filled=True, r_multiple=Decimal("-0.5")),
        )
    )

    assert confidence_label(low_sample, min_sample=2) == "LOW SAMPLE"
    assert confidence_label(negative, min_sample=2) == "NEGATIVE EDGE"


def test_edge_report_groups_by_full_condition_and_dimensions() -> None:
    report = build_edge_analytics_report(
        (
            EdgeAnalyticsRecord(condition_key=_key("BTCUSDT", mode="swing"), filled=True, tp1_hit=True, r_multiple=Decimal("2")),
            EdgeAnalyticsRecord(condition_key=_key("BTCUSDT", mode="swing"), filled=True, tp1_hit=True, r_multiple=Decimal("1")),
            EdgeAnalyticsRecord(condition_key=_key("ETHUSDT", mode="scalp", rr_bucket="rr_2_to_2_49"), filled=True, r_multiple=Decimal("-1")),
            EdgeAnalyticsRecord(condition_key=_key("ETHUSDT", mode="scalp", rr_bucket="rr_2_to_2_49"), filled=True, r_multiple=Decimal("-1")),
        ),
        min_sample=2,
    )

    assert report.total_groups == 2
    assert report.strongest_conditions[0].condition_key.symbol == "BTCUSDT"
    assert report.strongest_conditions[0].confidence_label == "HIGH CONFIDENCE"
    assert report.weakest_conditions[0].condition_key.symbol == "ETHUSDT"
    assert report.dimension_breakdowns["mode"][0].group_value in {"swing", "scalp"}


def test_condition_key_from_live_diagnostics_and_historical_match() -> None:
    diagnostics = {
        "mode": "swing",
        "bias": "long",
        "htf_2d_trend": "bullish",
        "mtf_12h_trend": "bullish",
        "trend": "bullish",
        "derivatives_supports_trade": True,
        "poc": Decimal("100"),
        "entry_low": Decimal("99"),
        "entry_high": Decimal("101"),
        "rr_to_tp2": Decimal("3.2"),
        "trust_percentage": 88,
        "execution_sweep_status": "passed",
        "sweep_magnitude_atr": Decimal("0.8"),
        "pullback_zone_status": "valid",
        "fib_alignment_status": "aligned",
        "selected_zone_type": "OB",
        "crowding_risk": "low",
        "gates_passed": ("sweep", "bos_choch", "pullback_zone", "fib_alignment"),
    }
    condition_key = condition_key_from_diagnostics(
        symbol="BTCUSDT",
        mode="swing",
        diagnostics=diagnostics,
        readiness_score=91,
    )
    report = build_edge_analytics_report(
        (
            EdgeAnalyticsRecord(condition_key=condition_key, filled=True, tp1_hit=True, r_multiple=Decimal("1")),
            EdgeAnalyticsRecord(condition_key=condition_key, filled=True, tp1_hit=True, r_multiple=Decimal("2")),
        ),
        min_sample=2,
    )
    match = match_historical_condition(report, condition_key)
    missing = match_historical_condition(report, _key("SOLUSDT"))

    assert condition_key.htf_direction_alignment == "aligned"
    assert condition_key.volume_profile_alignment == "entry_overlaps_poc"
    assert match.matched is True
    assert match.matching_sample_size == 2
    assert match.expectancy_metrics.expectancy == Decimal("1.50000000")
    assert missing.matched is False
    assert missing.confidence_label == "LOW SAMPLE"
