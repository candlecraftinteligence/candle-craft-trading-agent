"""Regression suite for CCI Market Data Integrity P2 — Candle & Volume Validation."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.agents.technical_structure import TechnicalAnalysisStatus, TechnicalStructureAgent
from app.alerts.telegram_lifecycle import PUBLIC_SIGNAL_MIN_RR
from app.analytics.market_regime import MarketRegimeInput, RegimeState, evaluate_market_regime
from app.analytics.public_signal_quality import (
    MIN_PUBLIC_SETUP_QUALITY_SCORE,
    MIN_PUBLIC_SIGNAL_GRADE,
    canonical_public_setup_quality_decision,
)
from app.analytics.pullback_zones import analyze_pullback_zone
from app.analytics.setup_quality import SetupQualityGrade
from app.analytics.target_intelligence import TargetFailureType, build_target_intelligence
from app.analytics.volume_profile import VolumeProfileInput, calculate_volume_profile
from app.analytics.wick_close_structure import analyze_wick_close_structure
from app.backtesting.strategy_replay import ReplayConfig, StrategyReplayEngine
from app.cache.market_data_cache import CachedMarketDataClient, MarketDataCache
from app.context.sector_rotation import SectorMemberStatus, build_sector_member_feature
from app.core.minimum_rr import (
    CHALLENGE_HARD_MINIMUM_RR,
    DEFAULT_CONFIGURED_MINIMUM_RR,
    SCALP_HARD_MINIMUM_RR,
    SWING_HARD_MINIMUM_RR,
)
from app.data.candle_freshness import (
    POLICY_VERSION,
    PUBLICATION_ALLOWANCE,
    CandleFreshnessStatus,
    assess_candle_freshness,
    assess_essential_discovery,
)
from app.data.candle_integrity import (
    SHADOW_RULE_VERSION,
    CandleIntegrityError,
    CandleIntegrityReason,
    closed_candles_as_of,
    evaluate_candle_shadow_diagnostics,
    get_recorded_candle_shadow_diagnostics,
    normalize_utc_timestamp,
    validate_candle_sequence,
    validate_candle_values,
)
from app.data.dtos import NA, CandleDTO
from app.data.exceptions import ExchangeMissingFieldError, ExchangeResponseError
from app.data.exchange_clients.base import BaseExchangeClient
from app.data.normalizers.binance import normalize_binance_klines
from app.data.normalizers.bybit import normalize_bybit_klines
from app.data.timeframes import resample_ohlcv_candles
from app.lifecycle.models import (
    SetupLifecycleRecord,
    SetupLifecycleState,
)
from app.lifecycle.outcome_policy import candle_range
from app.lifecycle.outcomes import (
    INTEGRITY_UNVERIFIED,
    INTEGRITY_VERIFIED,
    evaluate_closed_candle_outcomes,
)
from app.lifecycle.owner_monitoring import (
    SymbolMonitoringEvidence,
    _latest_close,
    classify_market_data_failure,
    monitor_obligations_with_market_data,
)
from app.lifecycle.prefix_disposition_evidence import DISPOSITION_POST_FILTER_BLOCKED
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunner
from app.storage.database import SCHEMA_VERSION
from app.strategies.liquidity_grab_pullback import LiquidityGrabEngine, LiquidityGrabInput
from tests.runtime_epoch_support import (
    SYNTHETIC_IDENTITY,
    attach_synthetic_origin_to_record,
    bootstrap_operational_test_database,
)

BASE_MS = 1704067200000  # 2024-01-01T00:00:00Z
FIFTEEN_MIN_MS = 15 * 60 * 1000
DAY_MS = 86_400_000


def _control_candle(
    timestamp: int = BASE_MS,
    **overrides: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "timestamp": timestamp,
        "open": "100",
        "high": "101",
        "low": "99",
        "close": "100",
        "volume": "10",
    }
    row.update(overrides)
    return row


def _series(count: int = 35, *, step_ms: int = FIFTEEN_MIN_MS) -> list[dict[str, Any]]:
    candles: list[dict[str, Any]] = []
    for idx in range(count):
        base = Decimal("100") + Decimal(idx % 5)
        candles.append(
            {
                "timestamp": BASE_MS + idx * step_ms,
                "open": str(base),
                "high": str(base + Decimal("2")),
                "low": str(base - Decimal("2")),
                "close": str(base + Decimal("0.5")),
                "volume": str(Decimal("10") + Decimal(idx)),
            }
        )
    return candles


# ---------------------------------------------------------------------------
# 1. Valid ordinary, flat, doji, tiny-price, zero-volume, and numeric inputs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"open": "100", "high": "100", "low": "100", "close": "100", "volume": "10"},  # flat
        {"open": "100", "high": "100", "low": "100", "close": "100", "volume": "0"},  # flat + zero vol
        {"open": "100", "high": "105", "low": "95", "close": "100", "volume": "12.5"},  # doji
        {
            "open": "0.00000001",
            "high": "0.00000002",
            "low": "0.00000001",
            "close": "0.00000002",
            "volume": "1000000",
        },  # tiny price
        {
            "open": Decimal("100.12345678"),
            "high": Decimal("101.87654321"),
            "low": Decimal("99.00000001"),
            "close": Decimal("100.55555555"),
            "volume": Decimal("0"),
            "quote_volume": Decimal("0"),
            "trade_count": 0,
        },
        {"open": 100, "high": 102, "low": 99, "close": 101, "volume": 0},
        {"open": 100.5, "high": 101.5, "low": 99.5, "close": 100.5, "volume": 5.25},
    ],
)
def test_valid_candle_shapes_and_precision_preserved(overrides: dict[str, Any]) -> None:
    candle = _control_candle(**overrides)
    validated = validate_candle_values(candle, timeframe="15m", require_volume=True)
    assert validated.low <= validated.open <= validated.high
    assert validated.low <= validated.close <= validated.high
    assert validated.low > 0
    assert validated.volume is not None and validated.volume >= 0

    seq = validate_candle_sequence((candle,), timeframe="15m")
    assert len(seq) == 1
    window = closed_candles_as_of(
        (candle,),
        timeframe="15m",
        decision_timestamp=BASE_MS + FIFTEEN_MIN_MS,
        minimum_closed_history=1,
    )
    assert len(window.candles) == 1


def test_zero_volume_window_does_not_manufacture_volume_confirmation() -> None:
    zero_series = [
        _control_candle(
            BASE_MS + idx * FIFTEEN_MIN_MS,
            open="100",
            high="101",
            low="99",
            close="100.5",
            volume="0",
        )
        for idx in range(35)
    ]
    tech = TechnicalStructureAgent(
        ema_fast_period=10,
        ema_slow_period=20,
        range_window=20,
    ).analyze(zero_series, timeframe="15m")
    assert tech.is_valid is True
    assert tech.volume_z_score == Decimal("0E-8")
    assert tech.volume_anomaly.is_present is False
    assert tech.volume_anomaly.status == "none"

    vp = calculate_volume_profile(VolumeProfileInput(symbol="BTCUSDT", timeframe="15m", candles=zero_series))
    assert vp.poc == NA
    assert vp.total_volume == NA
    assert "total candle volume is zero" in vp.warnings[0]


# ---------------------------------------------------------------------------
# 2. OHLC geometry violations and non-positive prices
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    [
        ({"open": "100", "high": "99", "low": "98", "close": "99"}, CandleIntegrityReason.INVALID_OHLC_GEOMETRY),
        ({"open": "99", "high": "99", "low": "98", "close": "100"}, CandleIntegrityReason.INVALID_OHLC_GEOMETRY),
        ({"open": "97", "high": "101", "low": "98", "close": "100"}, CandleIntegrityReason.INVALID_OHLC_GEOMETRY),
        ({"open": "100", "high": "101", "low": "98", "close": "97"}, CandleIntegrityReason.INVALID_OHLC_GEOMETRY),
        ({"open": "100", "high": "95", "low": "98", "close": "96"}, CandleIntegrityReason.INVALID_OHLC_GEOMETRY),
        ({"low": "0"}, CandleIntegrityReason.INVALID_PRICE),
        ({"open": "0", "high": "10", "low": "0", "close": "5"}, CandleIntegrityReason.INVALID_PRICE),
        ({"open": "0", "high": "0", "low": "0", "close": "0"}, CandleIntegrityReason.INVALID_PRICE),
        ({"low": "-1"}, CandleIntegrityReason.INVALID_PRICE),
        ({"open": "-100", "high": "-90", "low": "-110", "close": "-95"}, CandleIntegrityReason.INVALID_PRICE),
    ],
)
def test_ohlc_geometry_and_non_positive_prices_rejected(
    overrides: dict[str, Any],
    expected_reason: CandleIntegrityReason,
) -> None:
    bad = _control_candle(**overrides)
    with pytest.raises(CandleIntegrityError) as exc_info:
        validate_candle_values(bad, timeframe="15m")
    assert exc_info.value.reason == expected_reason

    with pytest.raises(CandleIntegrityError) as seq_exc:
        validate_candle_sequence((bad,), timeframe="15m")
    assert seq_exc.value.reason == expected_reason

    with pytest.raises(CandleIntegrityError) as closed_exc:
        closed_candles_as_of(
            (bad,),
            timeframe="15m",
            decision_timestamp=BASE_MS + FIFTEEN_MIN_MS,
        )
    assert closed_exc.value.reason == expected_reason


# ---------------------------------------------------------------------------
# 3. Missing, malformed, negative, non-finite, and boolean numeric fields
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("price_field", ["open", "high", "low", "close"])
@pytest.mark.parametrize("missing_value", [None, "", "   ", NA])
def test_missing_required_price_fields_rejected(price_field: str, missing_value: Any) -> None:
    candle = _control_candle(**{price_field: missing_value})
    with pytest.raises(CandleIntegrityError) as exc_info:
        validate_candle_values(candle, timeframe="15m")
    assert exc_info.value.reason == CandleIntegrityReason.MISSING_PRICE

    omitted = _control_candle()
    omitted.pop(price_field)
    with pytest.raises(CandleIntegrityError) as omitted_exc:
        validate_candle_values(omitted, timeframe="15m")
    assert omitted_exc.value.reason == CandleIntegrityReason.MISSING_PRICE


@pytest.mark.parametrize("field_name", ["open", "high", "low", "close", "volume"])
@pytest.mark.parametrize(
    "bad_value",
    [
        True,
        False,
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        float("nan"),
        float("inf"),
        float("-inf"),
        "not-a-number",
        [100],
        {"value": 100},
    ],
)
def test_boolean_non_finite_and_malformed_numeric_fields_rejected(
    field_name: str,
    bad_value: Any,
) -> None:
    candle = _control_candle(**{field_name: bad_value})
    with pytest.raises(CandleIntegrityError) as exc_info:
        validate_candle_values(candle, timeframe="15m")
    expected = (
        CandleIntegrityReason.INVALID_VOLUME
        if field_name == "volume"
        else CandleIntegrityReason.INVALID_PRICE
    )
    assert exc_info.value.reason == expected


def test_missing_and_negative_volume_and_optional_volume_fields() -> None:
    no_vol = _control_candle()
    no_vol.pop("volume")
    with pytest.raises(CandleIntegrityError) as exc_info:
        validate_candle_values(no_vol, timeframe="15m", require_volume=True)
    assert exc_info.value.reason == CandleIntegrityReason.MISSING_VOLUME

    # Price-only contract preserves missing volume as None without fabricating 0
    price_only = validate_candle_values(no_vol, timeframe="15m", require_volume=False)
    assert price_only.volume is None

    for bad_vol in ("-1", "-0.00000001", Decimal("-5"), ""):
        with pytest.raises(CandleIntegrityError) as vol_exc:
            validate_candle_values(_control_candle(volume=bad_vol), timeframe="15m", require_volume=False)
        assert vol_exc.value.reason == CandleIntegrityReason.INVALID_VOLUME

    for optional_override in (
        {"quote_volume": "-1"},
        {"quote_volume": "NaN"},
        {"quote_volume": True},
        {"turnover": "-0.1"},
        {"trade_count": -1},
        {"trade_count": "1.5"},
        {"trade_count": True},
    ):
        with pytest.raises(CandleIntegrityError) as opt_exc:
            validate_candle_values(_control_candle(**optional_override), timeframe="15m")
        assert opt_exc.value.reason == CandleIntegrityReason.INVALID_VOLUME


@pytest.mark.parametrize("bad_container", [None, True, "candle", 123, [1, 2, 3]])
def test_malformed_candle_containers_rejected(bad_container: Any) -> None:
    with pytest.raises(CandleIntegrityError) as exc_info:
        validate_candle_sequence([bad_container], timeframe="15m")
    assert exc_info.value.reason == CandleIntegrityReason.INVALID_CANDLE


# ---------------------------------------------------------------------------
# 4. Provider normalizers (Binance & Bybit) and CandleDTO contracts
# ---------------------------------------------------------------------------


def test_binance_and_bybit_normalizers_enforce_ohlcv_and_ordering() -> None:
    valid_binance = [
        [BASE_MS, "100", "105", "99", "102", "15.5", BASE_MS + FIFTEEN_MIN_MS - 1, "1565.5", 42]
    ]
    dto_list = normalize_binance_klines("BTCUSDT", "15m", valid_binance)
    assert len(dto_list) == 1
    assert dto_list[0].close_timestamp == BASE_MS + FIFTEEN_MIN_MS - 1
    assert dto_list[0].volume == Decimal("15.5")
    assert dto_list[0].quote_volume == Decimal("1565.5")

    # Parse-level malformed fields remain rejected at the normalizer.
    for mutated_row, expected_exc in (
        ([BASE_MS, "100", "101", "99", "100", "NaN", BASE_MS + FIFTEEN_MIN_MS - 1, "100", 5], ExchangeResponseError),
        ([True, "100", "101", "99", "100", "10", BASE_MS + FIFTEEN_MIN_MS - 1, "100", 5], ExchangeResponseError),
        ([BASE_MS, "100", "101", "99"], ExchangeMissingFieldError),
    ):
        with pytest.raises(expected_exc):
            normalize_binance_klines("BTCUSDT", "15m", [mutated_row])

    # OHLC/volume geometry is preserved through DTO construction so forming rows
    # can be delivered, then rejected at the eligible closed-window boundary.
    for mutated_row, expected_reason in (
        ([BASE_MS, "100", "99", "98", "99", "10", BASE_MS + FIFTEEN_MIN_MS - 1, "100", 5], CandleIntegrityReason.INVALID_OHLC_GEOMETRY),
        ([BASE_MS, "100", "101", "0", "100", "10", BASE_MS + FIFTEEN_MIN_MS - 1, "100", 5], CandleIntegrityReason.INVALID_PRICE),
        ([BASE_MS, "100", "101", "99", "100", "-1", BASE_MS + FIFTEEN_MIN_MS - 1, "100", 5], CandleIntegrityReason.INVALID_VOLUME),
    ):
        delivered = normalize_binance_klines("BTCUSDT", "15m", [mutated_row])
        with pytest.raises(CandleIntegrityError) as seq_exc:
            validate_candle_sequence(delivered, timeframe="15m")
        assert seq_exc.value.reason == expected_reason

    # Bybit newest-first valid payload reversed; mixed order is NOT sorted away
    bybit_newest_first = {
        "result": {
            "list": [
                [str(BASE_MS + FIFTEEN_MIN_MS), "102", "106", "101", "105", "20", "2080"],
                [str(BASE_MS), "100", "103", "99", "102", "10", "1010"],
            ]
        }
    }
    bybit_dtos = normalize_bybit_klines("BTCUSDT", "15m", bybit_newest_first)
    assert [c.timestamp for c in bybit_dtos] == [BASE_MS, BASE_MS + FIFTEEN_MIN_MS]
    assert bybit_dtos[0].close_timestamp == NA
    assert bybit_dtos[0].volume == Decimal("10")
    assert bybit_dtos[0].quote_volume == Decimal("1010")

    bybit_mixed_order = {
        "result": {
            "list": [
                [str(BASE_MS), "100", "103", "99", "102", "10", "1010"],
                [str(BASE_MS + 2 * FIFTEEN_MIN_MS), "104", "108", "103", "107", "15", "1500"],
                [str(BASE_MS + FIFTEEN_MIN_MS), "102", "106", "101", "105", "20", "2080"],
            ]
        }
    }
    mixed_dtos = normalize_bybit_klines("BTCUSDT", "15m", bybit_mixed_order)
    with pytest.raises(CandleIntegrityError) as mixed_gap_exc:
        validate_candle_sequence(mixed_dtos, timeframe="15m", require_continuity=True)
    assert mixed_gap_exc.value.reason == CandleIntegrityReason.CONTINUITY_GAP

    with pytest.raises(CandleIntegrityError) as mixed_exc:
        validate_candle_sequence(mixed_dtos, timeframe="15m", require_continuity=False)
    assert mixed_exc.value.reason == CandleIntegrityReason.OUT_OF_ORDER

    bybit_negative = normalize_bybit_klines(
        "BTCUSDT",
        "15m",
        {"result": {"list": [[str(BASE_MS), "100", "103", "99", "102", "-5", "500"]]}},
    )
    with pytest.raises(CandleIntegrityError) as bybit_vol_exc:
        validate_candle_sequence(bybit_negative, timeframe="15m")
    assert bybit_vol_exc.value.reason == CandleIntegrityReason.INVALID_VOLUME


# ---------------------------------------------------------------------------
# 5. Gaps vs overlaps, continuity modes, timestamp bounds, and cutoff causality
# ---------------------------------------------------------------------------


def test_continuity_modes_reject_overlaps_and_preserve_future_causality() -> None:
    first = _control_candle(BASE_MS)
    overlapping = _control_candle(BASE_MS + 300_000)  # 5m after a 15m open
    gapped = _control_candle(BASE_MS + 2 * FIFTEEN_MIN_MS)

    with pytest.raises(CandleIntegrityError) as gap_exc:
        validate_candle_sequence((first, gapped), timeframe="15m", require_continuity=True)
    assert gap_exc.value.reason == CandleIntegrityReason.CONTINUITY_GAP

    # require_continuity=False allows genuine gaps but NEVER overlaps
    assert len(validate_candle_sequence((first, gapped), timeframe="15m", require_continuity=False)) == 2
    with pytest.raises(CandleIntegrityError) as overlap_exc:
        validate_candle_sequence((first, overlapping), timeframe="15m", require_continuity=False)
    assert overlap_exc.value.reason == CandleIntegrityReason.OVERLAPPING_INTERVAL

    # Timestamp bounds & fractional rejection
    for bad_ts in (True, False, 1704067200000.5, Decimal("1704067200000.5"), 10**20, -10**20, "not-a-ts"):
        with pytest.raises(ValueError):
            normalize_utc_timestamp(bad_ts)

    # Forming/future bar with corrupt OHLCV does not poison an earlier valid decision
    forming_corrupt = _control_candle(
        BASE_MS + FIFTEEN_MIN_MS,
        open="100",
        high="90",  # invalid geometry in future/forming bar
        low="95",
        close="92",
        volume="-10",
    )
    window = closed_candles_as_of(
        (first, forming_corrupt),
        timeframe="15m",
        decision_timestamp=BASE_MS + FIFTEEN_MIN_MS,  # forming_corrupt closes at BASE_MS + 2*FIFTEEN_MIN_MS
        minimum_closed_history=1,
    )
    assert len(window.candles) == 1
    assert window.excluded_unclosed_count == 1

    # Once decision_timestamp reaches forming_corrupt's close, its corrupt OHLCV is rejected
    with pytest.raises(CandleIntegrityError) as eligible_exc:
        closed_candles_as_of(
            (first, forming_corrupt),
            timeframe="15m",
            decision_timestamp=BASE_MS + 2 * FIFTEEN_MIN_MS,
            minimum_closed_history=1,
        )
    assert eligible_exc.value.reason == CandleIntegrityReason.INVALID_OHLC_GEOMETRY
    assert eligible_exc.value.index == 1


# ---------------------------------------------------------------------------
# 6. Freshness policy preservation and cache delivery parity
# ---------------------------------------------------------------------------


def test_freshness_policy_and_cache_delivery_parity(tmp_path: Path) -> None:
    assert POLICY_VERSION == "essential-candle-freshness-v1"
    assert PUBLICATION_ALLOWANCE == timedelta(0)

    decision_at = datetime.fromtimestamp((BASE_MS + FIFTEEN_MIN_MS) / 1000, tz=UTC)
    c0 = CandleDTO(
        exchange="binance_futures",
        symbol="BTCUSDT",
        timestamp=BASE_MS,
        interval="15m",
        open=Decimal("100"),
        high=Decimal("101"),
        low=Decimal("99"),
        close=Decimal("100.5"),
        volume=Decimal("10"),
        close_timestamp=BASE_MS + FIFTEEN_MIN_MS - 1,
    )
    fresh = assess_candle_freshness(
        (c0,),
        symbol="BTCUSDT",
        timeframe="15m",
        role="execution",
        decision_timestamp=decision_at,
    )
    assert fresh.status == CandleFreshnessStatus.PASS

    # Malformed essential candle fails closed in assess_candle_freshness / assess_essential_discovery
    bad_essential = _control_candle(BASE_MS, open="100", high="99", low="98", close="99")
    bad_fresh = assess_candle_freshness(
        (bad_essential,),
        symbol="BTCUSDT",
        timeframe="15m",
        role="execution",
        decision_timestamp=decision_at,
    )
    assert bad_fresh.status == CandleFreshnessStatus.UNVERIFIED
    from app.data.candle_freshness import essential_discovery_roles

    roles = essential_discovery_roles(
        strategy_enabled=True,
        primary_timeframe="15m",
        execution_timeframe="15m",
        confirmation_timeframe="5m",
        bias_timeframe="12h",
        structure_timeframe="12h",
        htf_timeframe="2d",
    )
    assert ("4h", "optional") not in roles
    discovery_check = assess_essential_discovery(
        symbol="BTCUSDT",
        decision_timestamp=decision_at,
        roles=(("15m", "execution"), ("5m", "confirmation")),
        candles_by_timeframe={
            "15m": (bad_essential,),
            "5m": (c0,),
            "4h": (bad_essential,),
        },
    )
    assert discovery_check.passed is False
    assert "execution" in [item.role for item in discovery_check.assessments if not item.passed]
    assert "4h" not in [item.timeframe for item in discovery_check.assessments]

    cache_file = tmp_path / "market_cache.json"
    clock = {"now": 1000.0}

    class _StubClient(BaseExchangeClient):
        exchange_name = "binance_futures"

        def __init__(self) -> None:
            self.calls = 0

        async def get_klines(self, symbol: str, interval: str, limit: int) -> list[CandleDTO]:
            self.calls += 1
            return [c0]

        async def get_ticker(self, symbol: str) -> Any:
            raise NotImplementedError

        async def get_funding_rate(self, symbol: str) -> Any:
            raise NotImplementedError

        async def get_open_interest(self, symbol: str) -> Any:
            raise NotImplementedError

    stub = _StubClient()
    cache = MarketDataCache(file_path=cache_file, ttl_seconds=60, now=lambda: clock["now"])
    cached_client = CachedMarketDataClient(stub, cache)

    direct = asyncio.run(stub.get_klines("BTCUSDT", "15m", 1))
    miss_delivery = asyncio.run(cached_client.get_klines("BTCUSDT", "15m", 1))
    hit_delivery = asyncio.run(cached_client.get_klines("BTCUSDT", "15m", 1))
    reloaded_cache = MarketDataCache(file_path=cache_file, ttl_seconds=60, now=lambda: clock["now"])
    reloaded_client = CachedMarketDataClient(stub, reloaded_cache)
    file_reload_delivery = asyncio.run(reloaded_client.get_klines("BTCUSDT", "15m", 1))
    clock["now"] = 1061.0
    expiry_refetch_delivery = asyncio.run(reloaded_client.get_klines("BTCUSDT", "15m", 1))

    for delivered in (direct, miss_delivery, hit_delivery, file_reload_delivery, expiry_refetch_delivery):
        window = closed_candles_as_of(delivered, timeframe="15m", decision_timestamp=decision_at)
        assert window.candles == (c0,)

    # Tamper with the serialized CandleDTO inside the cache file (inject negative volume)
    payload = json.loads(cache_file.read_text(encoding="utf-8"))
    for entry in payload["entries"].values():
        entry["expires_at"] = 2000.0
        entry["value"]["items"][0]["data"]["volume"] = "-5"
    payload["entries"]["corrupt_envelope"] = {"version": 1, "expires_at": "not-a-float", "value": 1}
    cache_file.write_text(json.dumps(payload), encoding="utf-8")

    corrupt_cache = MarketDataCache(file_path=cache_file, ttl_seconds=60, now=lambda: 1500.0)
    assert corrupt_cache.errors == 1  # corrupt_envelope skipped and counted
    corrupt_client = CachedMarketDataClient(stub, corrupt_cache)
    tampered = asyncio.run(corrupt_client.get_klines("BTCUSDT", "15m", 1))
    with pytest.raises(CandleIntegrityError) as cache_exc:
        closed_candles_as_of(tampered, timeframe="15m", decision_timestamp=decision_at)
    assert cache_exc.value.reason == CandleIntegrityReason.INVALID_VOLUME


# ---------------------------------------------------------------------------
# 7. Synthetic 2D resampling constituents and concealed-defect rejection
# ---------------------------------------------------------------------------


def test_synthetic_2d_resampling_rejects_concealed_constituent_defects() -> None:
    day0 = _control_candle(0, open="100", high="110", low="95", close="105", volume="10")
    day1 = _control_candle(DAY_MS, open="105", high="120", low="100", close="115", volume="20")
    cutoff = 2 * DAY_MS

    merged = resample_ohlcv_candles((day0, day1), "2d", decision_timestamp=cutoff)
    assert len(merged) == 1
    assert merged[0]["open"] == Decimal("100")
    assert merged[0]["high"] == Decimal("120")
    assert merged[0]["low"] == Decimal("95")
    assert merged[0]["close"] == Decimal("115")
    assert merged[0]["volume"] == Decimal("30")

    # Day 0 has high=90 < open=100, which Day 1's high=120 would conceal under naive max(h0, h1)
    bad_geom_day0 = _control_candle(0, open="100", high="90", low="85", close="88", volume="10")
    with pytest.raises(CandleIntegrityError) as geom_exc:
        resample_ohlcv_candles((bad_geom_day0, day1), "2d", decision_timestamp=cutoff)
    assert geom_exc.value.reason == CandleIntegrityReason.INVALID_OHLC_GEOMETRY

    # Day 0 has volume=-5 offset by Day 1 volume=+20 (sum = +15)
    bad_vol_day0 = _control_candle(0, open="100", high="110", low="95", close="105", volume="-5")
    with pytest.raises(CandleIntegrityError) as vol_exc:
        resample_ohlcv_candles((bad_vol_day0, day1), "2d", decision_timestamp=cutoff)
    assert vol_exc.value.reason == CandleIntegrityReason.INVALID_VOLUME


# ---------------------------------------------------------------------------
# 8. Downstream consumer boundaries (TechnicalStructure, LiquidityGrab, VP,
#    PullbackZones, WickClose, TargetIntelligence, Regime, Sector, Replay)
# ---------------------------------------------------------------------------


def test_downstream_consumers_reject_zero_low_and_interior_corruption() -> None:
    valid_bars = _series(35)
    zero_low_bars = [dict(bar) for bar in valid_bars]
    zero_low_bars[-2]["low"] = "0"

    # TechnicalStructureAgent rejects zero low
    tech_bad = TechnicalStructureAgent().analyze(zero_low_bars, timeframe="15m")
    assert tech_bad.is_valid is False
    assert tech_bad.analysis_status == TechnicalAnalysisStatus.DATA_ERROR

    # LiquidityGrabEngine rejects zero low (does not fabricate a sweep at wick_price=0)
    with pytest.raises(ValueError, match="OHLC prices must be positive"):
        LiquidityGrabEngine().analyze(
            LiquidityGrabInput(
                symbol="BTCUSDT",
                candles_2d=valid_bars,
                candles_12h=valid_bars,
                candles_15m=zero_low_bars,
                candles_5m=valid_bars,
            )
        )

    # VolumeProfile, PullbackZones, WickCloseStructure reject zero low
    with pytest.raises(ValueError):
        calculate_volume_profile(VolumeProfileInput(symbol="BTCUSDT", timeframe="15m", candles=zero_low_bars))

    with pytest.raises(ValueError, match="OHLC prices must be positive"):
        analyze_pullback_zone(
            {
                "symbol": "BTCUSDT",
                "direction": "long",
                "execution_timeframe": "15m",
                "confirmation_timeframe": "5m",
                "candles_15m": zero_low_bars,
                "candles_5m": valid_bars,
                "sweep_candle_index": 5,
                "bos_choch_candle_index": 10,
            }
        )

    with pytest.raises(ValueError, match="Malformed wick-close candle OHLC geometry"):
        analyze_wick_close_structure(
            zero_low_bars,
            direction="long",
            bos_index=10,
            sweep_price=Decimal("98"),
            bos_price=Decimal("105"),
            fib_786=Decimal("99"),
        )

    # TargetIntelligence fails closed on interior corruption rather than silently dropping the bar
    corrupt_interior = [
        _control_candle(0, open="100", high="100", low="90", close="95"),
        _control_candle(1, open="100", high="112", low="95", close="110"),
        _control_candle(2, open="100", high="90", low="95", close="92"),  # high < low interior bar
        _control_candle(3, open="100", high="120", low="96", close="115"),
        _control_candle(4, open="100", high="110", low="97", close="105"),
    ]
    ti = build_target_intelligence(
        symbol="BTCUSDT",
        direction="long",
        entry=Decimal("100"),
        stop=Decimal("95"),
        minimum_rr=Decimal("2.5"),
        swing_lookback=1,
        candles=corrupt_interior,
    )
    assert ti.target_failure_type == TargetFailureType.NO_CLEAR_TARGET
    assert ti.tp1_candidate == NA

    # MarketRegime fails closed on interior corruption in direct candle series
    regime_bars = _series(90, step_ms=12 * 3600 * 1000)
    regime_bars[40]["high"] = "80"  # < low
    regime_res = evaluate_market_regime(
        MarketRegimeInput(btc_candles=regime_bars, eth_candles=_series(90, step_ms=12 * 3600 * 1000))
    )
    assert regime_res.state == RegimeState.MIXED
    assert "BTCUSDT_candles: N/A" in regime_res.missing_data
    assert any("candle_integrity:invalid_ohlc_geometry" in item for item in regime_res.missing_data)

    # SectorMemberFeature fails closed on corrupt candle
    sector_feat = build_sector_member_feature(
        symbol="SOLUSDT",
        candles=zero_low_bars,
        timeframe="15m",
        decision_timestamp=datetime.fromtimestamp((BASE_MS + 35 * FIFTEEN_MIN_MS) / 1000, tz=UTC),
    )
    assert sector_feat.status == SectorMemberStatus.ERROR

    # StrategyReplayEngine rejects corrupt candle
    with pytest.raises(CandleIntegrityError):
        StrategyReplayEngine().replay_symbol(
            "BTCUSDT",
            {"15m": zero_low_bars, "5m": valid_bars, "12h": valid_bars, "2d": valid_bars},
            ReplayConfig(),
        )


# ---------------------------------------------------------------------------
# 9. Shadow diagnostics contract
# ---------------------------------------------------------------------------


def test_shadow_diagnostics_are_deterministic_and_non_gating() -> None:
    candles = [
        _control_candle(BASE_MS, open="100", high="100", low="100", close="100", volume="0"),
        _control_candle(BASE_MS + FIFTEEN_MIN_MS, open="100", high="102", low="99", close="101", volume="0"),
    ]
    diag = evaluate_candle_shadow_diagnostics(
        candles,
        timeframe="15m",
        consumer="unit_test_probe",
        source="binance_futures",
        synthetic_fixture=True,
    )
    assert diag.rule_version == SHADOW_RULE_VERSION
    assert diag.observation_kind == "synthetic_fixture"
    assert diag.old_decision == "pass"
    assert diag.new_decision == "shadow_flag"
    assert diag.would_flag_under_strict_shadow is True
    assert diag.disagreement_count == 2
    assert diag.total_flag_count == 2
    assert diag.reason_counts == {
        "flat_zero_volume_bar": 1,
        "positive_range_zero_volume_bar": 1,
    }

    stacked = [
        _control_candle(
            BASE_MS,
            open="100",
            high="102",
            low="99",
            close="101",
            volume="0",
            quote_volume="1000",
        )
    ]
    stacked_diag = evaluate_candle_shadow_diagnostics(
        stacked,
        timeframe="15m",
        consumer="unit_test_probe",
        synthetic_fixture=True,
    )
    assert stacked_diag.disagreement_count == 1
    assert stacked_diag.total_flag_count == 2
    assert stacked_diag.reason_counts == {
        "positive_range_zero_volume_bar": 1,
        "quote_volume_positive_on_zero_base_volume": 1,
    }

    extreme = [
        _control_candle(BASE_MS, open="100", high="2000", low="100", close="100", volume="10"),
        _control_candle(
            BASE_MS + FIFTEEN_MIN_MS,
            close_timestamp=BASE_MS + 3 * FIFTEEN_MIN_MS,
        ),
    ]
    extra = evaluate_candle_shadow_diagnostics(
        extreme,
        timeframe="15m",
        consumer="unit_test_probe",
        synthetic_fixture=True,
    )
    assert extra.reason_counts["extreme_range_to_low_ratio"] == 1
    assert extra.reason_counts["close_timestamp_exceeds_scheduled_close"] == 1

    from app.data.candle_integrity import clear_recorded_candle_shadow_diagnostics

    clear_recorded_candle_shadow_diagnostics()
    recorded = evaluate_candle_shadow_diagnostics(
        candles,
        timeframe="15m",
        consumer="unit_test_probe",
        synthetic_fixture=True,
        record=True,
    )
    assert recorded in get_recorded_candle_shadow_diagnostics()


# ---------------------------------------------------------------------------
# 10. Existing-owner monitoring interruption, cursor safety, TP1/BREAKEVEN
#     preservation, price-only outcome evidence, and recovery
# ---------------------------------------------------------------------------


def _owned_executing_record() -> SetupLifecycleRecord:
    base_iso = "2024-01-01T00:00:00+00:00"
    record = SetupLifecycleRecord(
        lifecycle_id="lc-p2-owner-1",
        symbol="BTCUSDT",
        mode="swing",
        direction="long",
        current_state=SetupLifecycleState.ACTIONABLE_A_GRADE,
        first_seen_at=base_iso,
        last_seen_at=base_iso,
        last_transition_at=base_iso,
        confirmed_at=base_iso,
        invalidation_reason="Invalid below 95",
        invalidation_logic="Invalid below 95",
        setup_identity="lc-p2-owner-1-setup",
        structural_anchor="sweep-p2",
        entry_low="99",
        entry_high="101",
        stop_loss="95",
        tp1="105",
        tp2="112",
        tp3="120",
    )
    from app.lifecycle.economic_identity import latch_economic_identities

    return latch_economic_identities(record, instrument_venue="binance", plan_locked=True)


def test_owner_monitoring_ignores_invalid_geometry_and_recovers_without_losing_tp1(
    tmp_path: Path,
) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "owner_p2.sqlite")
    identity = SYNTHETIC_IDENTITY

    with SQLiteSetupLifecycleRepository(db_path, expected_identity=identity) as repo:
        assert repo._connection is not None
        record = attach_synthetic_origin_to_record(repo._connection, _owned_executing_record())
        repo.upsert_record(record)

        # Step 1: Boundary bar 0, valid entry fill on bar 1, and TP1 hit on bar 2
        bar0_boundary = {
            "timestamp": BASE_MS,
            "open": "97",
            "high": "98",
            "low": "96",
            "close": "97.5",
            "volume": "10",
        }
        bar1_entry = {
            "timestamp": BASE_MS + FIFTEEN_MIN_MS,
            "open": "100",
            "high": "101",
            "low": "99.5",
            "close": "100.5",
            "volume": "10",
        }
        bar2_tp1 = {
            "timestamp": BASE_MS + 2 * FIFTEEN_MIN_MS,
            "open": "100.5",
            "high": "106",
            "low": "100",
            "close": "105.5",
            "volume": "15",
        }
        step1 = evaluate_closed_candle_outcomes(
            record,
            execution_candles=(bar0_boundary, bar1_entry, bar2_tp1),
            execution_timeframe="15m",
            decision_timestamp=BASE_MS + 3 * FIFTEEN_MIN_MS,
            evaluated_at="2024-01-01T00:45:00+00:00",
            repository=repo,
        )
        assert step1.progress is not None
        assert step1.progress.tp1_at is not None
        assert step1.progress.terminal_outcome == NA
        saved_cursor = step1.progress.evaluation_cursor_open_at

        # Step 2: Bar 3 has low="90" (would trigger SL/breakeven) BUT invalid geometry high="98" < open="105"!
        bar3_corrupt_sl = {
            "timestamp": BASE_MS + 3 * FIFTEEN_MIN_MS,
            "open": "105",
            "high": "98",
            "low": "90",
            "close": "92",
            "volume": "20",
        }
        step2 = evaluate_closed_candle_outcomes(
            step1.record,
            execution_candles=(bar0_boundary, bar1_entry, bar2_tp1, bar3_corrupt_sl),
            execution_timeframe="15m",
            decision_timestamp=BASE_MS + 4 * FIFTEEN_MIN_MS,
            evaluated_at="2024-01-01T01:00:00+00:00",
            repository=repo,
        )
        assert step2.progress is not None
        assert step2.progress.integrity_status == INTEGRITY_UNVERIFIED
        assert "invalid_ohlc_range" in step2.progress.diagnostic
        assert step2.progress.terminal_outcome == NA
        assert step2.progress.tp1_at == step1.progress.tp1_at
        assert step2.progress.evaluation_cursor_open_at == saved_cursor
        assert step2.progress.last_eligibility_prefix_evidence_json is not None
        envelope = json.loads(step2.progress.last_eligibility_prefix_evidence_json)
        assert envelope["disposition"] == DISPOSITION_POST_FILTER_BLOCKED

        # Interrupted monitoring is not counted as a completed observation in research ownership projection
        from app.analytics.outcome_ownership import project_outcome_ownership

        projected_interrupted = project_outcome_ownership(
            lifecycle_records=[step2.record],
            progress_rows=[step2.progress],
            provenance={
                "synthetic": True,
                "label": "p2-candle-integrity-synthetic",
                "source_namespace": "synthetic-p2",
                "coverage_complete": False,
            },
        )
        eval_ctx = projected_interrupted["evaluations"][0]["evaluation_context"]
        assert eval_ctx["prefix_disposition"] == DISPOSITION_POST_FILTER_BLOCKED
        assert eval_ctx["complete"] is False
        assert projected_interrupted["canonical_outcome_per_plan_version"]["established"] is False

        # Step 3: Valid price-only bar 3 (volume omitted) reaches TP3=120; recovers and completes cleanly
        bar3_valid_price_only = {
            "timestamp": BASE_MS + 3 * FIFTEEN_MIN_MS,
            "open": "105.5",
            "high": "121",
            "low": "105",
            "close": "120.5",
        }
        step3 = evaluate_closed_candle_outcomes(
            step2.record,
            execution_candles=(bar0_boundary, bar1_entry, bar2_tp1, bar3_valid_price_only),
            execution_timeframe="15m",
            decision_timestamp=BASE_MS + 4 * FIFTEEN_MIN_MS,
            evaluated_at="2024-01-01T01:05:00+00:00",
            repository=repo,
        )
        assert step3.progress is not None
        assert step3.progress.integrity_status == INTEGRITY_VERIFIED
        assert step3.progress.terminal_outcome != NA
        assert step3.record.current_state == SetupLifecycleState.TP_HIT

        # Step 4: Repeated evaluation on terminal state is idempotent
        step4 = evaluate_closed_candle_outcomes(
            step3.record,
            execution_candles=(bar0_boundary, bar1_entry, bar2_tp1, bar3_valid_price_only),
            execution_timeframe="15m",
            decision_timestamp=BASE_MS + 4 * FIFTEEN_MIN_MS,
            evaluated_at="2024-01-01T01:10:00+00:00",
            repository=repo,
        )
        assert step4.progress is not None
        assert step4.progress.terminal_outcome == step3.progress.terminal_outcome
        assert step4.record.current_state == SetupLifecycleState.TP_HIT


def test_owner_monitoring_market_data_failure_classification_for_new_reasons(tmp_path: Path) -> None:
    err = CandleIntegrityError(
        CandleIntegrityReason.INVALID_OHLC_GEOMETRY,
        "high < open",
        timeframe="15m",
        index=0,
    )
    assert classify_market_data_failure(err) == "exchange_market_data_unavailable:CandleIntegrityError"

    db_path = bootstrap_operational_test_database(tmp_path / "owner_fetch_gap.sqlite")
    identity = SYNTHETIC_IDENTITY
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=identity) as repo:
        assert repo._connection is not None
        record = attach_synthetic_origin_to_record(repo._connection, _owned_executing_record())
        repo.upsert_record(record)

    async def failing_fetcher(symbol: str, timeframe: str, limit: int) -> list[Any]:
        raise err

    result = asyncio.run(
        monitor_obligations_with_market_data(
            db_path,
            evidence_by_key={},
            fetch_candles=failing_fetcher,
            execution_timeframe="15m",
            candle_limit=50,
            evaluated_at="2024-01-01T01:00:00+00:00",
            scan_run_id="scan-p2",
            expected_identity=identity,
        )
    )
    assert result.errors == ()
    assert result.gap_lifecycle_ids == ("lc-p2-owner-1",)
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=identity) as repo:
        reloaded = repo.get_record_by_lifecycle_id("lc-p2-owner-1")
        assert reloaded is not None
        assert reloaded.current_state == SetupLifecycleState.ACTIONABLE_A_GRADE


# ---------------------------------------------------------------------------
# 11. Public gate invariants and legacy RR exception boundaries
# ---------------------------------------------------------------------------


def test_public_gate_and_schema_invariants_preserved() -> None:
    assert MIN_PUBLIC_SETUP_QUALITY_SCORE == Decimal("88")
    assert MIN_PUBLIC_SIGNAL_GRADE == SetupQualityGrade.A
    assert PUBLIC_SIGNAL_MIN_RR == Decimal("3")
    assert DEFAULT_CONFIGURED_MINIMUM_RR == Decimal("2.5")
    assert SCALP_HARD_MINIMUM_RR == Decimal("2.5")
    assert SWING_HARD_MINIMUM_RR == Decimal("2.5")
    assert CHALLENGE_HARD_MINIMUM_RR == Decimal("3.0")
    assert SCHEMA_VERSION >= 20


# ---------------------------------------------------------------------------
# 12. Multi-symbol control: one malformed symbol does not disrupt valid symbol
# ---------------------------------------------------------------------------


def test_multi_symbol_scanner_isolates_malformed_symbol_from_valid_symbol() -> None:
    from tests.test_scanner_runner import (
        FakeExchangeClient,
        _config,
        _flat_candles,
        _strategy_pullback_candles,
        run,
    )

    bad = _flat_candles()
    bad[5]["low"] = Decimal("0")  # invalid zero low price on BADUSDT only
    client = FakeExchangeClient(
        {
            "BADUSDT": bad,
            "BTCUSDT": _strategy_pullback_candles(),
        },
        failing_timeframes={"2d"},
    )

    runner = ScannerRunner(exchange_client=client)
    run_result = run(runner.run(_config(["BADUSDT", "BTCUSDT"])))

    assert run_result.scanned_symbols == 2
    assert run_result.results[0].status == ScannerPipelineStatus.REJECTED_BY_TECHNICAL
    assert run_result.results[0].technical_status == "DATA_ERROR"
    assert run_result.results[0].rejection_stage == "technical_data_error"
    assert run_result.results[1].status != ScannerPipelineStatus.SCAN_ERROR
    assert run_result.results[1].strategy_diagnostics["challenge"]["confirmation_structure_shift_status"] == "passed"

    # Also verify that a CandleIntegrityError raised during 1d/2d resampling on BADUSDT
    # becomes an isolated SCAN_ERROR on BADUSDT without stopping BTCUSDT
    class _Bad1dClient(FakeExchangeClient):
        async def get_klines(self, symbol: str, interval: str = "15m", limit: int = 200):
            rows = await super().get_klines(symbol, interval, limit)
            if symbol == "BADUSDT" and interval == "1d":
                mutated = [dict(r) for r in rows]
                mutated[0]["high"] = Decimal("90")  # high < open on 1d constituent for 2D resampling
                return mutated
            return rows

    client_2d = _Bad1dClient(
        {
            "BADUSDT": _flat_candles(),
            "BTCUSDT": _strategy_pullback_candles(),
        },
        failing_timeframes={"2d"},
    )
    run_result_2d = run(ScannerRunner(exchange_client=client_2d).run(_config(["BADUSDT", "BTCUSDT"])))
    assert run_result_2d.scanned_symbols == 2
    assert run_result_2d.results[0].status == ScannerPipelineStatus.SCAN_ERROR
    assert "candle_integrity:invalid_ohlc_geometry" in str(run_result_2d.results[0].error_message)
    assert run_result_2d.results[1].status != ScannerPipelineStatus.SCAN_ERROR
    recorded = runner.latest_candle_shadow_diagnostics()
    assert recorded
    assert all(item.old_decision in {"pass", "reject"} for item in recorded)
    assert all(item.rule_version == SHADOW_RULE_VERSION for item in recorded)


# ---------------------------------------------------------------------------
# Astra R1–R4 rereview regressions
# ---------------------------------------------------------------------------


def _run_immediate(coro: Any) -> Any:
    # Shared acquisitions now run as shielded asyncio tasks. Preserve every
    # integrity assertion under the cache's actual event-loop contract.
    return asyncio.run(coro)


def test_r1_cutoff_parity_across_direct_cache_and_provider_paths() -> None:
    closed = _control_candle(BASE_MS)
    forming_bad = _control_candle(
        BASE_MS + FIFTEEN_MIN_MS,
        open="100",
        high="90",
        low="99",
        close="100",
        volume="10",
    )
    cutoff = BASE_MS + FIFTEEN_MIN_MS
    eligible_cutoff = BASE_MS + 2 * FIFTEEN_MIN_MS
    rows = [closed, forming_bad]

    direct = closed_candles_as_of(rows, timeframe="15m", decision_timestamp=cutoff)
    assert len(direct.candles) == 1
    assert direct.excluded_unclosed_count == 1

    cache = MarketDataCache(now=lambda: 100.0)

    async def fetch() -> list[dict[str, Any]]:
        return rows

    miss = _run_immediate(cache.get_or_fetch(data_type="candles", key_parts={"symbol": "BTCUSDT"}, fetch=fetch))
    hit = _run_immediate(cache.get_or_fetch(data_type="candles", key_parts={"symbol": "BTCUSDT"}, fetch=fetch))
    miss_window = closed_candles_as_of(miss, timeframe="15m", decision_timestamp=cutoff)
    hit_window = closed_candles_as_of(hit, timeframe="15m", decision_timestamp=cutoff)
    assert len(miss_window.candles) == 1
    assert len(hit_window.candles) == 1

    provider_rows = [
        [BASE_MS, "100", "101", "99", "100", "10", BASE_MS + FIFTEEN_MIN_MS - 1],
        [BASE_MS + FIFTEEN_MIN_MS, "100", "90", "99", "100", "10", BASE_MS + 2 * FIFTEEN_MIN_MS - 1],
    ]
    provider = normalize_binance_klines("BTCUSDT", "15m", provider_rows)
    provider_window = closed_candles_as_of(provider, timeframe="15m", decision_timestamp=cutoff)
    assert len(provider_window.candles) == 1

    with pytest.raises(CandleIntegrityError) as eligible_exc:
        closed_candles_as_of(rows, timeframe="15m", decision_timestamp=eligible_cutoff)
    assert eligible_exc.value.reason == CandleIntegrityReason.INVALID_OHLC_GEOMETRY
    with pytest.raises(CandleIntegrityError) as provider_eligible_exc:
        closed_candles_as_of(provider, timeframe="15m", decision_timestamp=eligible_cutoff)
    assert provider_eligible_exc.value.reason == CandleIntegrityReason.INVALID_OHLC_GEOMETRY


@pytest.mark.parametrize(
    "bad_volume",
    [
        Decimal("NaN"),
        Decimal("sNaN"),
        Decimal("Infinity"),
        Decimal("-Infinity"),
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
    ],
)
def test_r1_non_finite_future_volume_has_cache_delivery_parity(
    bad_volume: Any,
    tmp_path: Path,
) -> None:
    closed = _control_candle(BASE_MS, volume=Decimal("10"))
    forming = _control_candle(BASE_MS + FIFTEEN_MIN_MS, volume=bad_volume)
    rows = [closed, forming]
    before_close = BASE_MS + FIFTEEN_MIN_MS
    after_close = BASE_MS + 2 * FIFTEEN_MIN_MS

    def eligible_count(delivered: Any, cutoff: int) -> int:
        window = closed_candles_as_of(delivered, timeframe="15m", decision_timestamp=cutoff)
        return len(window.candles)

    direct_before = eligible_count(rows, before_close)
    assert direct_before == 1
    with pytest.raises(CandleIntegrityError) as direct_after:
        eligible_count(rows, after_close)
    assert direct_after.value.reason == CandleIntegrityReason.INVALID_VOLUME
    assert direct_after.value.index == 1

    cache_file = tmp_path / "non_finite_volume.json"
    clock = {"now": 100.0}
    cache = MarketDataCache(file_path=cache_file, ttl_seconds=60, now=lambda: clock["now"])

    async def fetch() -> list[dict[str, Any]]:
        return rows

    key = {"symbol": "BTCUSDT", "case": str(bad_volume)}
    miss = _run_immediate(cache.get_or_fetch(data_type="candles", key_parts=key, fetch=fetch))
    hit = _run_immediate(cache.get_or_fetch(data_type="candles", key_parts=key, fetch=fetch))
    repeat_hit = _run_immediate(cache.get_or_fetch(data_type="candles", key_parts=key, fetch=fetch))
    reloaded = MarketDataCache(file_path=cache_file, ttl_seconds=60, now=lambda: clock["now"])
    file_hit = _run_immediate(reloaded.get_or_fetch(data_type="candles", key_parts=key, fetch=fetch))
    clock["now"] = 161.0
    expiry = _run_immediate(reloaded.get_or_fetch(data_type="candles", key_parts=key, fetch=fetch))

    for delivered in (miss, hit, repeat_hit, file_hit, expiry):
        assert eligible_count(delivered, before_close) == 1
        with pytest.raises(CandleIntegrityError) as after_exc:
            eligible_count(delivered, after_close)
        assert after_exc.value.reason == CandleIntegrityReason.INVALID_VOLUME
        assert after_exc.value.index == 1


def test_r1_non_finite_eligible_interior_candle_is_not_skipped(tmp_path: Path) -> None:
    rows = [
        _control_candle(BASE_MS, volume=Decimal("NaN")),
        _control_candle(BASE_MS + FIFTEEN_MIN_MS, volume=Decimal("10")),
    ]
    cutoff = BASE_MS + 2 * FIFTEEN_MIN_MS
    cache_file = tmp_path / "interior_nan.json"
    cache = MarketDataCache(file_path=cache_file, ttl_seconds=60, now=lambda: 50.0)

    async def fetch() -> list[dict[str, Any]]:
        return rows

    miss = _run_immediate(
        cache.get_or_fetch(data_type="candles", key_parts={"symbol": "ETHUSDT"}, fetch=fetch)
    )
    hit = _run_immediate(
        cache.get_or_fetch(data_type="candles", key_parts={"symbol": "ETHUSDT"}, fetch=fetch)
    )
    reloaded = MarketDataCache(file_path=cache_file, ttl_seconds=60, now=lambda: 50.0)
    file_hit = _run_immediate(
        reloaded.get_or_fetch(data_type="candles", key_parts={"symbol": "ETHUSDT"}, fetch=fetch)
    )
    for delivered in (rows, miss, hit, file_hit):
        with pytest.raises(CandleIntegrityError) as exc_info:
            closed_candles_as_of(delivered, timeframe="15m", decision_timestamp=cutoff)
        assert exc_info.value.reason == CandleIntegrityReason.INVALID_VOLUME
        assert exc_info.value.index == 0


def test_r1_cache_preserves_zero_volume_and_rejects_unrelated_non_finite_decimals(tmp_path: Path) -> None:
    rows = [_control_candle(BASE_MS, volume=Decimal("0"))]
    cache = MarketDataCache(now=lambda: 10.0)

    async def fetch_candles() -> list[dict[str, Any]]:
        return rows

    miss = _run_immediate(
        cache.get_or_fetch(data_type="candles", key_parts={"symbol": "SOLUSDT"}, fetch=fetch_candles)
    )
    hit = _run_immediate(
        cache.get_or_fetch(data_type="candles", key_parts={"symbol": "SOLUSDT"}, fetch=fetch_candles)
    )
    for delivered in (miss, hit):
        window = closed_candles_as_of(
            delivered,
            timeframe="15m",
            decision_timestamp=BASE_MS + FIFTEEN_MIN_MS,
        )
        assert len(window.candles) == 1
        assert window.candles[0]["volume"] == Decimal("0")

    async def fetch_ticker() -> dict[str, Decimal]:
        return {"last_price": Decimal("NaN")}

    _run_immediate(cache.get_or_fetch(data_type="ticker", key_parts={"symbol": "SOLUSDT"}, fetch=fetch_ticker))
    with pytest.raises(ValueError, match="Non-finite cached decimal"):
        _run_immediate(cache.get_or_fetch(data_type="ticker", key_parts={"symbol": "SOLUSDT"}, fetch=fetch_ticker))


def test_r2_price_only_and_optional_volume_contracts_are_consistent() -> None:
    series = [_control_candle(BASE_MS + i * FIFTEEN_MIN_MS) for i in range(250)]
    price_only = [{k: v for k, v in bar.items() if k != "volume"} for bar in series]
    none_volume = [{**bar, "volume": None} for bar in series]

    replayed = StrategyReplayEngine().replay_symbol("BTCUSDT", {"15m": price_only})
    assert replayed.historical_candles == 250

    assert _latest_close(price_only[:1], "15m", "2024-01-01T00:15:00+00:00") == "2024-01-01T00:15:00+00:00"

    cache = MarketDataCache(now=lambda: 100.0)

    async def fetch() -> list[dict[str, Any]]:
        return none_volume

    miss = _run_immediate(cache.get_or_fetch(data_type="candles", key_parts={"symbol": "ETHUSDT"}, fetch=fetch))
    hit = _run_immediate(cache.get_or_fetch(data_type="candles", key_parts={"symbol": "ETHUSDT"}, fetch=fetch))
    miss_status = TechnicalStructureAgent().analyze(miss, timeframe="15m").analysis_status
    hit_status = TechnicalStructureAgent().analyze(hit, timeframe="15m").analysis_status
    assert miss_status == TechnicalAnalysisStatus.VALID
    assert hit_status == TechnicalAnalysisStatus.VALID

    absent_status = TechnicalStructureAgent().analyze(price_only, timeframe="15m").analysis_status
    assert absent_status == TechnicalAnalysisStatus.VALID


def test_r3_direct_analytical_timestamps_reject_malformed_supplied_values() -> None:
    series = [_control_candle(BASE_MS + i * FIFTEEN_MIN_MS) for i in range(250)]
    agent = TechnicalStructureAgent()
    assert agent.analyze(series, timeframe="15m").analysis_status == TechnicalAnalysisStatus.VALID

    missing_ts = [{k: v for k, v in bar.items() if k != "timestamp"} for bar in series]
    assert agent.analyze(missing_ts, timeframe="15m").analysis_status == TechnicalAnalysisStatus.VALID

    for bad_value in (BASE_MS + 0.5, "bad-timestamp", float("inf")):
        mutated = [dict(bar) for bar in series]
        mutated[50]["timestamp"] = bad_value
        result = agent.analyze(mutated, timeframe="15m")
        assert result.analysis_status == TechnicalAnalysisStatus.DATA_ERROR
        assert result.is_valid is False

    grab_mutated = [dict(bar) for bar in series]
    grab_mutated[10]["timestamp"] = "bad-timestamp"
    with pytest.raises(ValueError):
        LiquidityGrabEngine().analyze(
            LiquidityGrabInput(
                symbol="BTCUSDT",
                candles_2d=series,
                candles_12h=series,
                candles_15m=grab_mutated,
                candles_5m=series,
            )
        )


def test_r4_optional_volume_aliases_are_all_validated() -> None:
    with pytest.raises(CandleIntegrityError) as exc_info:
        validate_candle_values(_control_candle(quote_volume="1000", turnover="-1"))
    assert exc_info.value.reason == CandleIntegrityReason.INVALID_VOLUME
    assert "turnover" in str(exc_info.value)

    validated = validate_candle_values(_control_candle(quote_volume="1000", turnover="20"))
    assert validated.quote_volume == Decimal("1000")
    assert validated.turnover == Decimal("20")

    series = [_control_candle(BASE_MS + i * FIFTEEN_MIN_MS) for i in range(250)]
    agent = TechnicalStructureAgent()
    for field, value in (("quote_volume", "-1"), ("turnover", "NaN"), ("trade_count", True)):
        mutated = [dict(bar) for bar in series]
        mutated[50][field] = value
        result = agent.analyze(mutated, timeframe="15m")
        assert result.analysis_status == TechnicalAnalysisStatus.DATA_ERROR

    causal = closed_candles_as_of(
        (_control_candle(trade_count=-1),),
        timeframe="15m",
        decision_timestamp=BASE_MS + FIFTEEN_MIN_MS,
        require_volume=False,
        validate_values=False,
    ).timeline[0]
    from app.data.candle_integrity import CausalCandle

    assert isinstance(causal, CausalCandle)
    high, low = candle_range(causal)
    assert high == Decimal("101")
    assert low == Decimal("99")
