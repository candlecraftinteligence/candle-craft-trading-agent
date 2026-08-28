from __future__ import annotations

import asyncio
import inspect
import json
import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.context.sector_rotation import (
    SectorAggregateStatus,
    SectorContextStatus,
    SectorMemberFeature,
    SectorMemberStatus,
    SectorRotationEngine,
    SectorRotationSnapshot,
    SectorRotationState,
    SectorSnapshotStatus,
    build_sector_member_feature,
    project_sector_context,
)
from app.context.sector_scanner_enrichment import apply_sector_context_to_symbol_result
from app.context.sector_taxonomy import (
    SECTOR_TAXONOMY_VERSION,
    SectorAssetType,
    classify_sector,
)
from app.core.confirmed_data_health import (
    classify_confirmed_data_health,
    confirmed_data_health_for_symbol,
)
from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerRunConfig,
    ScannerRunner,
    ScannerSymbolResult,
)
from app.strategies.liquidity_grab_pullback import LiquidityGrabEngine


OBSERVED_AT = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _candles(
    closes: list[Decimal],
    *,
    interval: timedelta = timedelta(minutes=15),
    start: datetime = datetime(2026, 8, 27, 11, 45, tzinfo=UTC),
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, close in enumerate(closes):
        opened_at = start + interval * index
        rows.append(
            {
                "timestamp": int(opened_at.timestamp() * 1000),
                "close_timestamp": int((opened_at + interval).timestamp() * 1000),
                "open": close,
                "high": close + Decimal("1"),
                "low": close - Decimal("1"),
                "close": close,
                "volume": Decimal("100"),
            }
        )
    return rows


def _exact_return_candles() -> list[dict[str, Any]]:
    closes = [Decimal("100") for _ in range(97)]
    closes[0] = Decimal("60")
    closes[80] = Decimal("80")
    closes[92] = Decimal("96")
    closes[95] = Decimal("120")
    closes[96] = Decimal("144")
    return _candles(closes)


def _feature(
    symbol: str,
    return_4h: str,
    *,
    structure: str = "bullish",
    observed_at: datetime = OBSERVED_AT,
    return_15m: str | None = None,
    return_1h: str | None = None,
    return_24h: str | None = None,
) -> SectorMemberFeature:
    classification = classify_sector(symbol)
    is_benchmark = classification.asset_type == SectorAssetType.BENCHMARK_ONLY
    return SectorMemberFeature(
        symbol=symbol,
        sector=classification.primary_sector,
        asset_type=classification.asset_type,
        status=(
            SectorMemberStatus.BENCHMARK_ONLY
            if is_benchmark
            else SectorMemberStatus.VERIFIED
        ),
        observed_at=observed_at,
        source_timeframe="15m",
        return_15m_pct=Decimal(return_15m or return_4h),
        return_1h_pct=Decimal(return_1h or return_4h),
        return_4h_pct=Decimal(return_4h),
        return_24h_pct=Decimal(return_24h or return_4h),
        structure_state=structure,
        technical_valid=True,
    )


def _snapshot(
    features: list[SectorMemberFeature],
    *,
    symbols: list[str] | None = None,
    generated_at: datetime = OBSERVED_AT,
) -> SectorRotationSnapshot:
    return SectorRotationEngine().build_snapshot(
        universe_symbols=symbols or [item.symbol for item in features],
        member_features=features,
        generated_at=generated_at,
    )


def _metrics(snapshot: SectorRotationSnapshot, sector: str):
    aggregate = snapshot.sector(sector)
    assert aggregate is not None
    metrics = aggregate.metrics_for("4h")
    assert metrics is not None
    return aggregate, metrics


def test_taxonomy_classification_is_deterministic_and_versioned() -> None:
    first = classify_sector("solusdt")
    second = classify_sector("SOLUSDT")

    assert first == second
    assert first.primary_sector == "L1"
    assert first.asset_type == SectorAssetType.DIRECTIONAL
    assert first.taxonomy_version == SECTOR_TAXONOMY_VERSION


def test_unknown_symbol_remains_unclassified() -> None:
    classification = classify_sector("MYSTERYUSDT")

    assert classification.primary_sector == "UNCLASSIFIED"
    assert classification.asset_type == SectorAssetType.UNCLASSIFIED
    assert classification.exclusion_reason == "unclassified_asset"


def test_taxonomy_has_no_llm_or_network_dependency(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network used")),
    )

    assert classify_sector("ETHUSDT").primary_sector == "L1"
    source = inspect.getsource(classify_sector).lower()
    assert "openai" not in source
    assert "http" not in source


@pytest.mark.parametrize(
    ("symbol", "reason"),
    (
        ("USDCUSDT", "stablecoin"),
        ("WBTCUSDT", "wrapped_or_receipt_asset"),
        ("PAXGUSDT", "commodity_backed_asset"),
        ("ETHBULLUSDT", "leveraged_token"),
    ),
)
def test_non_standard_directional_assets_are_explicitly_excluded(
    symbol: str, reason: str
) -> None:
    classification = classify_sector(symbol)

    assert classification.asset_type == SectorAssetType.NON_DIRECTIONAL
    assert classification.exclusion_reason == reason


def test_multiplier_contract_uses_underlying_primary_sector() -> None:
    assert classify_sector("1000PEPEUSDT").primary_sector == "MEME"


def test_exact_15m_1h_4h_and_24h_returns() -> None:
    feature = build_sector_member_feature(
        symbol="ETHUSDT",
        candles=_exact_return_candles(),
        timeframe="15m",
        decision_timestamp=OBSERVED_AT,
        structure_state="bullish",
        technical_valid=True,
    )

    assert feature.return_15m_pct == Decimal("20.00000000")
    assert feature.return_1h_pct == Decimal("50.00000000")
    assert feature.return_4h_pct == Decimal("80.00000000")
    assert feature.return_24h_pct == Decimal("140.00000000")
    assert feature.observed_at == OBSERVED_AT


def test_closed_candle_only_semantics_exclude_incomplete_extreme_candle() -> None:
    rows = _exact_return_candles()
    future_open = OBSERVED_AT
    rows.append(
        {
            "timestamp": int(future_open.timestamp() * 1000),
            "close_timestamp": int(
                (future_open + timedelta(minutes=15)).timestamp() * 1000
            ),
            "open": Decimal("144"),
            "high": Decimal("10000"),
            "low": Decimal("1"),
            "close": Decimal("9999"),
            "volume": Decimal("999999"),
        }
    )

    feature = build_sector_member_feature(
        symbol="ETHUSDT",
        candles=rows,
        timeframe="15m",
        decision_timestamp=OBSERVED_AT,
        structure_state="bullish",
        technical_valid=True,
    )

    assert feature.return_15m_pct == Decimal("20.00000000")
    assert feature.observed_at == OBSERVED_AT


def test_unsupported_horizon_is_unavailable_not_inferred() -> None:
    rows = _candles(
        [Decimal("100"), Decimal("102"), Decimal("104")],
        interval=timedelta(hours=1),
        start=OBSERVED_AT - timedelta(hours=3),
    )
    feature = build_sector_member_feature(
        symbol="ETHUSDT",
        candles=rows,
        timeframe="1h",
        decision_timestamp=OBSERVED_AT,
    )

    assert feature.return_15m_pct is None
    assert feature.return_1h_pct == Decimal("1.96078431")
    assert feature.return_4h_pct is None


def test_sector_equal_weight_median_and_return_breadth_are_exact() -> None:
    snapshot = _snapshot(
        [
            _feature("BTCUSDT", "1"),
            _feature("ETHUSDT", "10", structure="bullish"),
            _feature("SOLUSDT", "-1", structure="bearish"),
            _feature("ADAUSDT", "2", structure="neutral"),
        ]
    )
    aggregate, metrics = _metrics(snapshot, "L1")

    assert metrics.equal_weight_return_pct == Decimal("3.66666667")
    assert metrics.median_return_pct == Decimal("2.00000000")
    assert metrics.positive_breadth_pct == Decimal("66.66666667")
    assert metrics.negative_breadth_pct == Decimal("33.33333333")
    assert aggregate.bullish_structure_pct == Decimal("33.33333333")
    assert aggregate.bearish_structure_pct == Decimal("33.33333333")
    assert aggregate.neutral_structure_pct == Decimal("33.33333333")
    assert metrics.relative_strength_vs_btc_pct_points == Decimal("1.00000000")


def test_missing_btc_comparison_is_not_fabricated_and_sectors_are_not_ranked() -> None:
    snapshot = _snapshot(
        [_feature("ETHUSDT", "3"), _feature("SOLUSDT", "2"), _feature("ADAUSDT", "1")]
    )
    aggregate, metrics = _metrics(snapshot, "L1")

    assert aggregate.status == SectorAggregateStatus.VERIFIED
    assert metrics.relative_strength_vs_btc_pct_points is None
    assert aggregate.sector_rank is None
    assert snapshot.status == SectorSnapshotStatus.PARTIAL


def test_insufficient_constituent_count_cannot_be_verified() -> None:
    snapshot = _snapshot(
        [_feature("BTCUSDT", "0"), _feature("ONDOUSDT", "4"), _feature("OMUSDT", "3")]
    )
    aggregate, metrics = _metrics(snapshot, "RWA")

    assert metrics.coverage_pct == Decimal("100.00000000")
    assert aggregate.verified_constituent_count == 2
    assert aggregate.status == SectorAggregateStatus.INSUFFICIENT_DATA


def test_partial_unavailable_constituents_remain_in_coverage_denominator() -> None:
    features = [
        _feature("BTCUSDT", "0"),
        _feature("ETHUSDT", "4"),
        _feature("SOLUSDT", "3"),
        _feature("ADAUSDT", "2"),
    ]
    snapshot = _snapshot(
        features,
        symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT", "AVAXUSDT", "SUIUSDT"],
    )
    aggregate, metrics = _metrics(snapshot, "L1")

    assert aggregate.constituent_count == 5
    assert aggregate.verified_constituent_count == 3
    assert aggregate.unavailable_constituent_count == 2
    assert metrics.coverage_pct == Decimal("60.00000000")
    assert aggregate.status == SectorAggregateStatus.VERIFIED


def test_one_extreme_token_cannot_define_sector_median_or_breadth() -> None:
    snapshot = _snapshot(
        [
            _feature("BTCUSDT", "0"),
            _feature("DOGEUSDT", "100"),
            _feature("PEPEUSDT", "-1"),
            _feature("WIFUSDT", "-2"),
        ]
    )
    aggregate, metrics = _metrics(snapshot, "MEME")

    assert metrics.equal_weight_return_pct == Decimal("32.33333333")
    assert metrics.median_return_pct == Decimal("-1.00000000")
    assert metrics.positive_breadth_pct == Decimal("33.33333333")
    assert aggregate.top_constituent_4h == "DOGEUSDT"
    assert aggregate.rotation_state == SectorRotationState.UNDERPERFORMING_BROAD


def test_universe_membership_changes_do_not_retain_or_zero_disappearing_symbols() -> None:
    features = [
        _feature("BTCUSDT", "0"),
        _feature("ETHUSDT", "3"),
        _feature("SOLUSDT", "2"),
        _feature("ADAUSDT", "1"),
        _feature("AVAXUSDT", "9"),
    ]
    first = _snapshot(features)
    second = _snapshot(
        features[:-1],
        symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT", "ADAUSDT"],
    )

    first_aggregate, first_metrics = _metrics(first, "L1")
    second_aggregate, second_metrics = _metrics(second, "L1")
    assert first_aggregate.constituent_count == 4
    assert second_aggregate.constituent_count == 3
    assert first_metrics.equal_weight_return_pct == Decimal("3.75000000")
    assert second_metrics.equal_weight_return_pct == Decimal("2.00000000")


def test_previous_snapshot_preserves_observed_at_and_computes_age() -> None:
    observed = OBSERVED_AT - timedelta(minutes=15)
    features = [
        _feature("BTCUSDT", "0", observed_at=observed),
        _feature("ETHUSDT", "3", observed_at=observed),
        _feature("SOLUSDT", "2", observed_at=observed),
        _feature("ADAUSDT", "1", observed_at=observed),
    ]
    snapshot = _snapshot(features, generated_at=OBSERVED_AT)
    context = project_sector_context(
        symbol="ETHUSDT",
        snapshot=snapshot,
        member_feature=features[1],
        as_of=OBSERVED_AT,
    )

    assert context.observed_at == observed
    assert context.age_seconds == 900.0
    assert context.status == SectorContextStatus.VERIFIED


def test_stale_previous_snapshot_is_unverified() -> None:
    features = [
        _feature("BTCUSDT", "0"),
        _feature("ETHUSDT", "3"),
        _feature("SOLUSDT", "2"),
        _feature("ADAUSDT", "1"),
    ]
    snapshot = _snapshot(features)
    context = project_sector_context(
        symbol="ETHUSDT",
        snapshot=snapshot,
        member_feature=features[1],
        as_of=OBSERVED_AT + timedelta(minutes=31),
    )

    assert context.status == SectorContextStatus.STALE
    assert context.age_seconds == 1860.0
    assert context.reason == "sector_snapshot_stale"


@pytest.mark.parametrize(
    ("sector_symbols", "returns", "btc_return", "expected_state"),
    (
        (
            ["ETHUSDT", "SOLUSDT", "ADAUSDT"],
            ["4", "3", "2"],
            "1",
            SectorRotationState.OUTPERFORMING_BROAD,
        ),
        (
            ["DOGEUSDT", "PEPEUSDT", "WIFUSDT"],
            ["20", "0", "0"],
            "-1",
            SectorRotationState.OUTPERFORMING_NARROW,
        ),
        (
            ["UNIUSDT", "AAVEUSDT", "CRVUSDT"],
            ["-4", "-3", "-2"],
            "-1",
            SectorRotationState.UNDERPERFORMING_BROAD,
        ),
        (
            ["ARBUSDT", "OPUSDT", "POLUSDT"],
            ["2", "0", "-2"],
            "0",
            SectorRotationState.MIXED,
        ),
        (
            ["LINKUSDT", "PYTHUSDT", "BANDUSDT"],
            ["4", "3", "2"],
            "5",
            SectorRotationState.UNDERPERFORMING_NARROW,
        ),
        (
            ["FILUSDT", "ARUSDT", "STORJUSDT"],
            ["-3", "-2", "-1"],
            "-5",
            SectorRotationState.OUTPERFORMING_NARROW,
        ),
    ),
)
def test_synthetic_rotation_states_a_b_c_d_g_h(
    sector_symbols: list[str],
    returns: list[str],
    btc_return: str,
    expected_state: SectorRotationState,
) -> None:
    features = [_feature("BTCUSDT", btc_return)] + [
        _feature(symbol, value) for symbol, value in zip(sector_symbols, returns, strict=True)
    ]
    snapshot = _snapshot(features)
    sector = classify_sector(sector_symbols[0]).primary_sector
    aggregate, _ = _metrics(snapshot, sector)

    assert aggregate.rotation_state == expected_state


def test_unclassified_synthetic_symbol_f_has_optional_missing_context() -> None:
    snapshot = _snapshot([_feature("BTCUSDT", "0")], symbols=["BTCUSDT", "UNKNOWNUSDT"])
    context = project_sector_context(
        symbol="UNKNOWNUSDT",
        snapshot=snapshot,
        member_feature=None,
        as_of=OBSERVED_AT,
    )
    result = apply_sector_context_to_symbol_result(_symbol_result("UNKNOWNUSDT"), context)
    health = confirmed_data_health_for_symbol(result)

    assert context.status == SectorContextStatus.UNCLASSIFIED
    assert "sector_rotation" in health.optional_missing
    assert health.blocked is False


def test_verified_sector_removes_optional_na_and_preserves_strategy_fields() -> None:
    features = [
        _feature("BTCUSDT", "0"),
        _feature("ETHUSDT", "3"),
        _feature("SOLUSDT", "2"),
        _feature("ADAUSDT", "1"),
    ]
    snapshot = _snapshot(features)
    context = project_sector_context(
        symbol="ETHUSDT",
        snapshot=snapshot,
        member_feature=features[1],
        as_of=OBSERVED_AT,
    )
    before = _symbol_result("ETHUSDT")
    after = apply_sector_context_to_symbol_result(before, context)

    assert context.status == SectorContextStatus.VERIFIED
    assert all(not item.startswith("sector_rotation:") for item in after.missing_data)
    assert all(
        not item.startswith("sector_rotation:") for item in after.strategy_missing_data
    )
    assert confirmed_data_health_for_symbol(after).blocked is False
    assert after.status == before.status
    assert after.status_history == before.status_history
    assert after.score_result == before.score_result
    assert after.setup_quality == before.setup_quality
    assert after.lifecycle_state == before.lifecycle_state
    assert after.trade_idea == before.trade_idea
    assert after.alert_result == before.alert_result


def test_insufficient_sector_is_optional_missing() -> None:
    features = [_feature("BTCUSDT", "0"), _feature("ONDOUSDT", "3"), _feature("OMUSDT", "2")]
    snapshot = _snapshot(features)
    context = project_sector_context(
        symbol="ONDOUSDT",
        snapshot=snapshot,
        member_feature=features[1],
        as_of=OBSERVED_AT,
    )
    result = apply_sector_context_to_symbol_result(_symbol_result("ONDOUSDT"), context)
    health = confirmed_data_health_for_symbol(result)

    assert context.status == SectorContextStatus.INSUFFICIENT_DATA
    assert "sector_rotation" in health.optional_missing
    assert health.blocked is False


def test_stale_sector_is_optional_unverified() -> None:
    features = [
        _feature("BTCUSDT", "0"),
        _feature("ETHUSDT", "3"),
        _feature("SOLUSDT", "2"),
        _feature("ADAUSDT", "1"),
    ]
    snapshot = _snapshot(features)
    context = project_sector_context(
        symbol="ETHUSDT",
        snapshot=snapshot,
        member_feature=features[1],
        as_of=OBSERVED_AT + timedelta(hours=1),
    )
    result = apply_sector_context_to_symbol_result(_symbol_result("ETHUSDT"), context)
    health = confirmed_data_health_for_symbol(result)

    assert health.optional_unverified == ("sector_rotation",)
    assert health.blocked is False


def test_research_only_sector_cannot_change_strategy_gates_grade_rr_or_geometry() -> None:
    engine = LiquidityGrabEngine()
    baseline = engine.analyze({"symbol": "DOGEUSDT", "mode": "challenge"})
    research = engine.analyze(
        {
            "symbol": "DOGEUSDT",
            "mode": "challenge",
            "sector_rotation": {
                "usage": "research_only",
                "sector": "MEME",
                "status": "VERIFIED",
            },
        }
    )

    for mode in ("challenge", "swing", "scalp"):
        before = getattr(baseline, mode)
        after = getattr(research, mode)
        assert after.is_valid == before.is_valid
        assert after.status == before.status
        assert after.gates_passed == before.gates_passed
        assert after.gates_failed == before.gates_failed
        assert after.hard_rejection_reasons == before.hard_rejection_reasons
        assert after.trust_meter == before.trust_meter
        assert after.entry_low == before.entry_low
        assert after.entry_high == before.entry_high
        assert after.stop == before.stop
        assert after.tp1 == before.tp1
        assert after.tp2 == before.tp2
        assert after.tp3 == before.tp3
        assert after.rr_to_tp2 == before.rr_to_tp2


def test_unknown_future_data_health_field_still_fails_closed() -> None:
    report = classify_confirmed_data_health(
        missing_values=(("future_sector_magic: N/A",),)
    )

    assert report.required_missing == ("future_sector_magic",)
    assert report.blocked is True


def test_snapshot_and_per_symbol_context_persistence_footprints_are_bounded() -> None:
    features = [
        _feature("BTCUSDT", "0"),
        _feature("ETHUSDT", "3"),
        _feature("SOLUSDT", "2"),
        _feature("ADAUSDT", "1"),
        _feature("DOGEUSDT", "4"),
        _feature("PEPEUSDT", "3"),
        _feature("WIFUSDT", "2"),
        _feature("UNIUSDT", "1"),
        _feature("AAVEUSDT", "0"),
        _feature("CRVUSDT", "-1"),
    ]
    snapshot = _snapshot(features)
    context = project_sector_context(
        symbol="ETHUSDT",
        snapshot=snapshot,
        member_feature=features[1],
        as_of=OBSERVED_AT,
    )
    snapshot_bytes = len(
        json.dumps(snapshot.model_dump(mode="json"), separators=(",", ":")).encode()
    )
    context_payload = context.model_dump(mode="json")
    context_bytes = len(json.dumps(context_payload, separators=(",", ":")).encode())

    assert snapshot_bytes < 20_000
    assert context_bytes < 1_000
    assert "sectors" not in context_payload


class _NoMarketDataClient:
    cache_stats = None
    retry_events = None
    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        self.calls += 1
        raise AssertionError(f"sector aggregation made a market-data call: {name}")


class _StubScannerRunner(ScannerRunner):
    def __init__(self, features: dict[str, SectorMemberFeature], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.features = features

    async def _scan_symbol(self, symbol_config, config, client, **kwargs):
        return ScannerSymbolResult(
            symbol=symbol_config.symbol,
            status=ScannerPipelineStatus.SCANNED_NO_SETUP,
            status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
            rejection_reason="fixture_no_setup",
            missing_data=("sector_rotation: N/A",),
            strategy_missing_data=("sector_rotation: N/A",),
            sector_member_feature=self.features.get(symbol_config.symbol),
        )


class _ExplodingSectorEngine:
    def build_snapshot(self, **kwargs: Any) -> SectorRotationSnapshot:
        raise RuntimeError("synthetic sector engine failure")


def _scanner_config(symbols: list[str]) -> ScannerRunConfig:
    return ScannerRunConfig(
        symbols=symbols,
        exchange="binance",
        interval="15m",
        cache_enabled=False,
        account_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
        decision_timestamp=OBSERVED_AT,
    )


def test_same_scan_aggregation_adds_zero_additional_market_data_requests() -> None:
    features = {
        feature.symbol: feature
        for feature in (
            _feature("BTCUSDT", "0"),
            _feature("ETHUSDT", "3"),
            _feature("SOLUSDT", "2"),
            _feature("ADAUSDT", "1"),
        )
    }
    client = _NoMarketDataClient()
    result = _run(
        _StubScannerRunner(
            features,
            exchange_client=client,
        ).run(_scanner_config(list(features)))
    )

    assert client.calls == 0
    assert result.sector_rotation_snapshot is not None
    assert result.sector_rotation_snapshot.status == SectorSnapshotStatus.VERIFIED
    assert result.results[1].sector_rotation.status == SectorContextStatus.VERIFIED
    assert result.trade_ideas_created == 0
    assert result.dry_run_alerts_created == 0


def test_scanner_completes_when_entire_sector_engine_fails() -> None:
    features = {"ETHUSDT": _feature("ETHUSDT", "3")}
    result = _run(
        _StubScannerRunner(
            features,
            exchange_client=_NoMarketDataClient(),
            sector_rotation_engine=_ExplodingSectorEngine(),
        ).run(_scanner_config(["ETHUSDT"]))
    )

    assert result.scanned_symbols == 1
    assert result.failed_symbols == 0
    assert result.sector_rotation_snapshot.status == SectorSnapshotStatus.ERROR
    assert result.results[0].sector_rotation.status == SectorContextStatus.ERROR
    assert confirmed_data_health_for_symbol(result.results[0]).blocked is False


def _symbol_result(symbol: str) -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejection_reason="fixture_no_setup",
        missing_data=("sector_rotation: N/A", "narrative: N/A"),
        strategy_missing_data=("sector_rotation: N/A",),
    )
