"""Essential candle freshness at the scan decision cutoff.

Synthetic clocks and fake exchange clients only. These tests do not read a
live runtime database, send Telegram traffic, or place orders.
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.alerts.telegram_lifecycle import PUBLIC_SIGNAL_MIN_RR, PUBLIC_SIGNAL_TARGET_CAUTION_MIN_SCORE, TelegramLifecycleDeliveryService
from app.cache.market_data_cache import MarketDataCache
from app.core.config import Settings
from app.core.confirmed_data_health import confirmed_data_health_for_symbol
from app.core.minimum_rr import DEFAULT_CONFIGURED_MINIMUM_RR
from app.data.candle_freshness import (
    POLICY_VERSION,
    PUBLICATION_ALLOWANCE,
    CandleFreshnessStatus,
    assess_candle_freshness,
    assess_essential_discovery,
    essential_discovery_roles,
    freshness_failure_preserves_owner,
    observation_has_essential_freshness_failure,
    required_scheduled_close,
    required_scheduled_close_ms,
    utc_epoch_milliseconds,
)
from app.data.candle_integrity import closed_candles_as_of, timeframe_duration
from app.data.timeframes import resample_ohlcv_candles
from app.lifecycle.models import SetupLifecycleState
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import _generation_setup_observable
from app.lifecycle.state_machine import evaluate_lifecycle_transition
from app.pipeline.scanner_runner import (
    ESSENTIAL_CANDLE_FRESHNESS_REJECTION,
    ScannerPipelineStatus,
    ScannerRunner,
    _freshness_receipt,
)
from scripts import run_scan
from test_lifecycle import _confirmed_observation
from test_lifecycle_outcomes import _baseline, _entry, _evaluate, _record, _target
from test_telegram_lifecycle_delivery_phase42 import FakeSender
from tests.runtime_epoch_support import (
    SYNTHETIC_CUTOFF_AT,
    SYNTHETIC_EPOCH_ID,
    SYNTHETIC_GENERATION_BINDING,
    SYNTHETIC_RELEASE_SHA,
    bootstrap_operational_test_database,
)
from tests.test_prospective_runtime_epoch_isolation_repair2 import (
    FakeAdapterExchangeClient,
    _public_min_rr_pullback_candles,
)
from tests.test_scanner_runner import (
    FakeExchangeClient,
    _config,
    _flat_candles,
    _strategy_pullback_candles,
    run,
)


DECISION = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
DAY_MS = 86_400_000
SEVEN_DAYS_MS = 7 * DAY_MS


def _ms(value: datetime) -> int:
    return utc_epoch_milliseconds(value)


def _bar(open_ms: int, *, price: str = "100", close_timestamp: int | None = None) -> dict[str, Any]:
    candle: dict[str, Any] = {
        "timestamp": open_ms,
        "open": Decimal(price),
        "high": Decimal(price) + Decimal("1"),
        "low": Decimal(price) - Decimal("1"),
        "close": Decimal(price),
        "volume": Decimal("10"),
    }
    if close_timestamp is not None:
        candle["close_timestamp"] = close_timestamp
    return candle


def _step_ms(timeframe: str) -> int:
    delta = timeframe_duration(timeframe)
    return (delta.days * DAY_MS) + (delta.seconds * 1000) + (delta.microseconds // 1000)


def _series(timeframe: str, decision: datetime, *, count: int = 30, lag_intervals: int = 0) -> list[dict[str, Any]]:
    required = required_scheduled_close_ms(timeframe, decision)
    step = _step_ms(timeframe)
    last_close = required - (lag_intervals * step)
    last_open = last_close - step
    return [_bar(last_open - (count - 1 - index) * step) for index in range(count)]


class TimeframeShiftClient(FakeExchangeClient):
    def __init__(self, *args: Any, shifts: dict[str, int] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.shifts = {key.lower(): value for key, value in (shifts or {}).items()}

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Decimal | int]]:
        candles = await super().get_klines(symbol, interval, limit)
        shift = self.shifts.get(f"{symbol.upper()}:{interval.lower()}", self.shifts.get(interval.lower(), 0))
        if not shift:
            return candles
        return [{**candle, "timestamp": int(candle["timestamp"]) - int(shift)} for candle in candles]


def _scan(candles: list[dict[str, Any]], *, symbol: str = "BTCUSDT", decision: datetime = DECISION, shifts: dict[str, int] | None = None, **overrides: Any):
    client = TimeframeShiftClient({symbol: candles}, candle_as_of=decision, shifts=shifts)
    result = run(ScannerRunner(exchange_client=client).run(_config([symbol], decision_timestamp=decision, **overrides)))
    return result.results[0]


def _mirror_short(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    mirrored: list[dict[str, Any]] = []
    for candle in candles:
        high = Decimal("200") - Decimal(str(candle["low"]))
        low = Decimal("200") - Decimal(str(candle["high"]))
        mirrored.append(
            {
                **candle,
                "open": Decimal("200") - Decimal(str(candle["open"])),
                "high": high,
                "low": low,
                "close": Decimal("200") - Decimal(str(candle["close"])),
            }
        )
    return mirrored


def _roles() -> tuple[tuple[str, str], ...]:
    return essential_discovery_roles(
        strategy_enabled=True,
        primary_timeframe="15m",
        execution_timeframe="15m",
        confirmation_timeframe="15m",
        bias_timeframe="12h",
        structure_timeframe="2h",
        htf_timeframe="2d",
    )


def _bundle(decision: datetime = DECISION, *, lag: dict[str, int] | None = None, drop: set[str] | None = None):
    lag = lag or {}
    drop = drop or set()
    candles = {
        "15m": _series("15m", decision, lag_intervals=lag.get("15m", 0)),
        "12h": _series("12h", decision, lag_intervals=lag.get("12h", 0)),
        "2h": _series("2h", decision, lag_intervals=lag.get("2h", 0)),
        "1d": _series("1d", decision, count=40, lag_intervals=lag.get("1d", 0)),
    }
    candles["2d"] = resample_ohlcv_candles(candles["1d"], target_interval="2d", decision_timestamp=decision)
    for timeframe in drop:
        candles.pop(timeframe, None)
    return assess_essential_discovery(
        symbol="BTCUSDT",
        decision_timestamp=decision,
        roles=_roles(),
        candles_by_timeframe=candles,
        daily_source_candles=candles.get("1d", ()),
    )


def test_public_and_strategy_rr_floors_stay_distinct() -> None:
    assert DEFAULT_CONFIGURED_MINIMUM_RR == Decimal("2.5")
    assert PUBLIC_SIGNAL_MIN_RR == Decimal("3")
    assert PUBLIC_SIGNAL_TARGET_CAUTION_MIN_SCORE == Decimal("88")
    assert PUBLICATION_ALLOWANCE == timedelta(0)
    assert POLICY_VERSION == "essential-candle-freshness-v1"


def test_required_close_uses_each_timeframes_own_epoch_anchor() -> None:
    assert required_scheduled_close("15m", DECISION) == DECISION
    assert required_scheduled_close("2h", DECISION) == DECISION
    assert required_scheduled_close("1h", DECISION) == DECISION
    assert required_scheduled_close("12h", DECISION) == datetime(2026, 3, 1, 12, tzinfo=UTC)
    assert required_scheduled_close("4h", DECISION) == datetime(2026, 3, 1, 12, tzinfo=UTC)
    assert required_scheduled_close("1d", DECISION) == datetime(2026, 3, 1, tzinfo=UTC)
    two_day = required_scheduled_close_ms("2d", DECISION)
    assert two_day % (2 * DAY_MS) == 0
    assert two_day <= _ms(DECISION) < two_day + (2 * DAY_MS)
    naive = datetime(2026, 3, 1, 14, 0)
    assert required_scheduled_close_ms("15m", naive) == required_scheduled_close_ms("15m", DECISION)
    just_before = DECISION - timedelta(milliseconds=1)
    assert required_scheduled_close("15m", just_before) == DECISION - timedelta(minutes=15)
    assert required_scheduled_close("15m", DECISION + timedelta(milliseconds=1)) == DECISION


def test_closed_prefix_utility_still_accepts_an_outdated_tail() -> None:
    stale = _series("15m", DECISION, lag_intervals=1)
    window = closed_candles_as_of(stale, timeframe="15m", decision_timestamp=DECISION, minimum_closed_history=1)
    assert window.timeline
    assessment = assess_candle_freshness(stale, symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=DECISION)
    assert assessment.status is CandleFreshnessStatus.STALE
    assert assessment.reason == "missing_completed_interval"
    assert assessment.lag_intervals == 1
    assert assessment.diagnostic_line() is not None
    assert "N/A" in assessment.diagnostic_line()


def test_boundary_matrix_and_exchange_close_stamp() -> None:
    step = _step_ms("15m")
    required = required_scheduled_close_ms("15m", DECISION)
    current = [_bar(required - (2 * step)), _bar(required - step)]
    one_short = [_bar(required - (2 * step))]
    minus_one = [_bar(required - step, close_timestamp=required - 1)]
    before = DECISION - timedelta(milliseconds=1)
    after = DECISION + timedelta(milliseconds=1)
    assert assess_candle_freshness(current, symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=DECISION).passed
    assert assess_candle_freshness(minus_one, symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=DECISION).passed
    assert assess_candle_freshness(current, symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=before).passed
    assert not assess_candle_freshness(one_short, symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=DECISION).passed
    rolled = assess_candle_freshness(current, symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=DECISION + timedelta(minutes=15))
    assert rolled.status is CandleFreshnessStatus.STALE
    assert rolled.lag_intervals == 1
    same_bucket = assess_candle_freshness(current, symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=after)
    assert same_bucket.passed
    open_only = [_bar(required)]
    assert assess_candle_freshness(open_only, symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=DECISION).status is CandleFreshnessStatus.MISSING
    with_open_next = [_bar(required - step), _bar(required)]
    covered = assess_candle_freshness(with_open_next, symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=DECISION)
    assert covered.passed
    assert covered.latest_observed_close == DECISION


def test_integrity_failures_are_unverified_and_not_passes() -> None:
    step = _step_ms("15m")
    required = required_scheduled_close_ms("15m", DECISION)
    duplicate = [_bar(required - step), _bar(required - step)]
    gap = [_bar(required - (3 * step)), _bar(required - step)]
    unordered = [_bar(required - step), _bar(required - (2 * step))]
    off_anchor = [_bar(required - (2 * step) + 1)]
    for candles in (duplicate, gap, unordered, off_anchor):
        assessment = assess_candle_freshness(candles, symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=DECISION)
        assert assessment.status is CandleFreshnessStatus.UNVERIFIED
        assert assessment.passed is False
        assert "Unverified" in (assessment.diagnostic_line() or "")
    weekly = assess_candle_freshness([_bar(0)], symbol="BTCUSDT", timeframe="1w", role="structure", decision_timestamp=DECISION)
    assert weekly.status is CandleFreshnessStatus.UNVERIFIED
    assert weekly.reason == "unsupported_anchor"
    empty = assess_candle_freshness([], symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=DECISION)
    assert empty.status is CandleFreshnessStatus.MISSING
    assert empty.reason == "no_closed_candle"
    invalid_decision = assess_candle_freshness([_bar(0)], symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=None)
    assert invalid_decision.status is CandleFreshnessStatus.UNVERIFIED
    assert invalid_decision.passed is False


def test_short_current_history_passes_freshness_without_replacing_the_history_gate() -> None:
    short = _series("15m", DECISION, count=3)
    assessment = assess_candle_freshness(short, symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=DECISION)
    assert assessment.passed
    assert len(short) < 20


def test_timeframes_are_not_judged_by_one_universal_timeout() -> None:
    fifteen = _series("15m", DECISION)
    twelve = _series("12h", DECISION)
    fifteen_assessment = assess_candle_freshness(fifteen, symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=DECISION)
    twelve_assessment = assess_candle_freshness(twelve, symbol="BTCUSDT", timeframe="12h", role="bias", decision_timestamp=DECISION)
    assert fifteen_assessment.passed and twelve_assessment.passed
    assert twelve_assessment.latest_observed_close == datetime(2026, 3, 1, 12, tzinfo=UTC)
    assert fifteen_assessment.latest_observed_close == DECISION
    assert twelve_assessment.latest_observed_close < fifteen_assessment.latest_observed_close


def test_synthetic_2d_pair_tail_and_fetch_window() -> None:
    fresh = _bundle()
    assert fresh.passed
    daily = _series("1d", DECISION, count=40)
    older_daily = _series("1d", DECISION, count=80)
    short_window = resample_ohlcv_candles(daily, target_interval="2d", decision_timestamp=DECISION)
    long_window = resample_ohlcv_candles(older_daily, target_interval="2d", decision_timestamp=DECISION)
    assert short_window[-1]["timestamp"] == long_window[-1]["timestamp"]
    stale_2d = short_window[:-1]
    stale = assess_essential_discovery(
        symbol="BTCUSDT",
        decision_timestamp=DECISION,
        roles=_roles(),
        candles_by_timeframe={"15m": _series("15m", DECISION), "12h": _series("12h", DECISION), "2h": _series("2h", DECISION), "2d": stale_2d},
        daily_source_candles=daily,
    )
    two_day = next(item for item in stale.assessments if item.timeframe == "2d")
    assert two_day.status is CandleFreshnessStatus.STALE
    assert two_day.reason == "missing_completed_interval"
    pair_second_open = required_scheduled_close_ms("2d", DECISION) - DAY_MS
    incomplete_daily = [candle for candle in daily if int(candle["timestamp"]) < pair_second_open]
    incomplete = assess_essential_discovery(
        symbol="BTCUSDT",
        decision_timestamp=DECISION,
        roles=_roles(),
        candles_by_timeframe={
            "15m": _series("15m", DECISION),
            "12h": _series("12h", DECISION),
            "2h": _series("2h", DECISION),
            "2d": resample_ohlcv_candles(incomplete_daily, target_interval="2d", decision_timestamp=DECISION),
        },
        daily_source_candles=incomplete_daily,
    )
    assert any(item.reason == "incomplete_daily_pair" for item in incomplete.assessments)
    missing_source = assess_essential_discovery(
        symbol="BTCUSDT",
        decision_timestamp=DECISION,
        roles=_roles(),
        candles_by_timeframe={"15m": _series("15m", DECISION), "12h": _series("12h", DECISION), "2h": _series("2h", DECISION), "2d": short_window},
        daily_source_candles=None,
    )
    assert any(item.reason == "synthetic_2d_source_missing" for item in missing_source.assessments)


def test_prefix_causality_uses_the_supplied_cutoff_only() -> None:
    early = DECISION - timedelta(minutes=15)
    candles = _series("15m", early)
    before = assess_candle_freshness(candles, symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=early)
    with_future = [*candles, _bar(required_scheduled_close_ms("15m", early))]
    repeated = assess_candle_freshness(with_future, symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=early)
    assert before.as_dict() == repeated.as_dict()
    later = assess_candle_freshness(candles, symbol="BTCUSDT", timeframe="15m", role="execution", decision_timestamp=DECISION)
    assert later.status is CandleFreshnessStatus.STALE
    assert "2026-10-18" not in str(before.as_dict())


def test_optional_roles_are_not_globally_required_and_empty_assessment_is_not_a_pass() -> None:
    roles = _roles()
    assert all(timeframe not in {"4h", "1h"} for timeframe, _role in roles)
    empty = assess_essential_discovery(symbol="BTCUSDT", decision_timestamp=DECISION, roles=(), candles_by_timeframe={})
    assert empty.passed is False
    assert empty.diagnostic_lines()[1]


def test_fresh_long_control_keeps_its_economics_and_a_passing_receipt() -> None:
    first = _scan(_public_min_rr_pullback_candles())
    second = _scan(_public_min_rr_pullback_candles())
    assert first.trade_idea is not None
    assert first.rejection_reason != ESSENTIAL_CANDLE_FRESHNESS_REJECTION
    receipt = _freshness_receipt(first.strategy_diagnostics)
    assert receipt is not None and receipt["discovery_passed"] is True
    assert first.trade_idea.direction == second.trade_idea.direction
    assert first.trade_idea.entry_zone == second.trade_idea.entry_zone
    assert first.trade_idea.stop_loss == second.trade_idea.stop_loss
    assert first.trade_idea.take_profits == second.trade_idea.take_profits
    assert first.trade_idea.best_rr == second.trade_idea.best_rr
    assert first.trade_idea.best_rr >= PUBLIC_SIGNAL_MIN_RR
    health = confirmed_data_health_for_symbol(first)
    assert health.required_missing == ()
    assert health.required_unverified == ()


def test_flat_candles_remain_a_legitimate_no_setup() -> None:
    result = _scan(_flat_candles())
    assert result.trade_idea is None
    assert result.status == ScannerPipelineStatus.SCANNED_NO_SETUP
    assert result.rejection_reason != ESSENTIAL_CANDLE_FRESHNESS_REJECTION
    receipt = _freshness_receipt(result.strategy_diagnostics)
    assert receipt is not None and receipt["discovery_passed"] is True


def test_short_mirror_cannot_skip_freshness() -> None:
    result = _scan(_mirror_short(_public_min_rr_pullback_candles()))
    receipt = _freshness_receipt(result.strategy_diagnostics)
    assert receipt is not None and receipt["discovery_passed"] is True
    assert result.rejection_reason != ESSENTIAL_CANDLE_FRESHNESS_REJECTION
    if result.trade_idea is not None:
        assert result.trade_idea.direction == "short"
        assert result.trade_idea.best_rr >= PUBLIC_SIGNAL_MIN_RR


@pytest.mark.parametrize("timeframe", ["15m", "12h", "2h"])
def test_stale_essential_timeframe_creates_no_trade_idea(timeframe: str) -> None:
    result = _scan(_strategy_pullback_candles(), shifts={timeframe: SEVEN_DAYS_MS})
    assert result.trade_idea is None
    assert result.status == ScannerPipelineStatus.SCANNED_NO_SETUP
    assert result.rejection_reason == ESSENTIAL_CANDLE_FRESHNESS_REJECTION
    assert result.rejection_stage == "essential_candle_freshness"
    assert result.lifecycle_execution_candles
    health = confirmed_data_health_for_symbol(result)
    assert any(item.startswith("essential_candle_freshness") for item in health.required_missing)
    assert health.blocked is True
    receipt = _freshness_receipt(result.strategy_diagnostics)
    assert receipt is not None and receipt["discovery_passed"] is False
    assert receipt["allowance_ms"] == 0


def test_one_missing_15m_interval_is_enough_to_reject() -> None:
    result = _scan(_strategy_pullback_candles(), shifts={"15m": 15 * 60_000})
    assert result.trade_idea is None
    receipt = _freshness_receipt(result.strategy_diagnostics)
    assert receipt is not None
    execution = next(item for item in receipt["assessments"] if item["role"] == "execution")
    assert execution["status"] == "stale"
    assert execution["lag_intervals"] == 1


def test_fast_mode_does_not_bypass_essential_freshness() -> None:
    stale = _scan(_strategy_pullback_candles(), shifts={"15m": SEVEN_DAYS_MS}, fast_mode=True)
    fresh = _scan(_flat_candles(), fast_mode=True)
    assert stale.trade_idea is None
    assert stale.rejection_reason == ESSENTIAL_CANDLE_FRESHNESS_REJECTION
    assert fresh.rejection_reason != ESSENTIAL_CANDLE_FRESHNESS_REJECTION


def test_one_stale_symbol_does_not_stop_a_healthy_symbol() -> None:
    decision = DECISION
    candles = {
        "BADUSDT": _strategy_pullback_candles(),
        "BTCUSDT": _public_min_rr_pullback_candles(),
    }

    class OneBadClient(TimeframeShiftClient):
        async def get_klines(self, symbol: str, interval: str, limit: int):
            rows = await FakeExchangeClient.get_klines(self, symbol, interval, limit)
            if symbol.upper() == "BADUSDT" and interval.lower() == "15m":
                return [{**row, "timestamp": int(row["timestamp"]) - SEVEN_DAYS_MS} for row in rows]
            return rows

    result = run(
        ScannerRunner(exchange_client=OneBadClient(candles, candle_as_of=decision)).run(
            _config(["BADUSDT", "BTCUSDT"], decision_timestamp=decision)
        )
    )
    by_symbol = {item.symbol: item for item in result.results}
    assert set(by_symbol) == {"BADUSDT", "BTCUSDT"}
    assert by_symbol["BADUSDT"].status != ScannerPipelineStatus.SCAN_ERROR
    assert by_symbol["BADUSDT"].trade_idea is None
    assert by_symbol["BTCUSDT"].trade_idea is not None
    assert by_symbol["BTCUSDT"].trade_idea.best_rr >= PUBLIC_SIGNAL_MIN_RR


def test_cached_candles_fail_after_the_decision_crosses_the_next_close(tmp_path) -> None:
    decision = DECISION
    later = decision + timedelta(minutes=15)
    clock = {"now": 1_700_000_000.0}
    cache = MarketDataCache(enabled=True, ttl_seconds=3600, now=lambda: clock["now"], file_path=tmp_path / "candles.json")
    client = FakeExchangeClient({"BTCUSDT": _public_min_rr_pullback_candles()}, candle_as_of=decision)
    first = run(
        ScannerRunner(exchange_client=client, market_data_cache=cache).run(
            _config(["BTCUSDT"], decision_timestamp=decision, cache_ttl_seconds=3600, cache_file=tmp_path / "candles.json")
        )
    )
    assert first.results[0].trade_idea is not None
    restored = MarketDataCache(enabled=True, ttl_seconds=3600, now=lambda: clock["now"], file_path=tmp_path / "candles.json")
    fresh_client = FakeExchangeClient({"BTCUSDT": _public_min_rr_pullback_candles()}, candle_as_of=later)
    second = run(
        ScannerRunner(exchange_client=fresh_client, market_data_cache=restored).run(
            _config(["BTCUSDT"], decision_timestamp=later, cache_ttl_seconds=3600, cache_file=tmp_path / "candles.json")
        )
    )
    assert restored.hits > 0
    assert second.results[0].trade_idea is None
    assert second.results[0].rejection_reason == ESSENTIAL_CANDLE_FRESHNESS_REJECTION


def test_fresh_http_response_with_old_candles_is_not_current() -> None:
    result = _scan(_strategy_pullback_candles(), shifts={"15m": SEVEN_DAYS_MS, "12h": SEVEN_DAYS_MS})
    assert result.trade_idea is None
    assert result.status != ScannerPipelineStatus.SCAN_ERROR


def test_new_stale_observation_cannot_confirm_and_owned_plan_holds() -> None:
    fresh = _confirmed_observation()
    assert observation_has_essential_freshness_failure(fresh) is False
    first = evaluate_lifecycle_transition(None, fresh, lifecycle_id="life-fresh")
    second = evaluate_lifecycle_transition(first.record, fresh, lifecycle_id="life-fresh")
    assert second.record is not None
    assert second.record.current_state == SetupLifecycleState.CONFIRMED
    assert second.record.confirmation_count == 2
    stale = _confirmed_observation(
        required_data_missing=("essential_candle_freshness_15m",),
        data_health_failed=True,
        failed_gate="essential_candle_freshness",
        sweep_detected=True,
        valid_trade_idea=False,
    )
    assert freshness_failure_preserves_owner(stale) is True
    held = evaluate_lifecycle_transition(second.record, stale, lifecycle_id="life-fresh")
    assert held.transitioned is False
    assert held.record.current_state == SetupLifecycleState.CONFIRMED
    assert held.record.confirmation_count == 2
    assert held.record.entry_low == second.record.entry_low
    assert held.record.stop_loss == second.record.stop_loss
    assert held.record.tp1 == second.record.tp1
    assert held.record.plan_version_id == second.record.plan_version_id
    created = evaluate_lifecycle_transition(None, stale, lifecycle_id="life-stale")
    assert created.record.current_state == SetupLifecycleState.REJECTED
    assert created.record.confirmation_count == 0
    assert created.record.plan_version_id in (None, "")
    mixed = _confirmed_observation(required_data_missing=("essential_candle_freshness_15m", "technical_candles"))
    assert freshness_failure_preserves_owner(mixed) is False
    invalidated = evaluate_lifecycle_transition(second.record, mixed, lifecycle_id="life-fresh")
    assert invalidated.record.current_state == SetupLifecycleState.INVALIDATED
    assert _generation_setup_observable(stale) is False


def test_held_owner_still_evaluates_a_closed_candle_prefix(tmp_path) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "owner.sqlite") as repository:
        record = _record(state=SetupLifecycleState.CONFIRMED)
        repository.upsert_record(record)
        stale = _confirmed_observation(required_data_missing=("essential_candle_freshness_15m",))
        held = evaluate_lifecycle_transition(record, stale, lifecycle_id=record.lifecycle_id)
        assert held.record.current_state == SetupLifecycleState.CONFIRMED
        repository.upsert_record(held.record)
        candles = [_baseline("long"), _entry(1, "long"), _target(2, "long", 1)]
        evaluated = _evaluate(repository, held.record, candles)
    assert evaluated.progress is not None
    assert evaluated.progress.tp1_at is not None
    assert evaluated.record.entry_low == record.entry_low
    assert evaluated.record.stop_loss == record.stop_loss


def test_legacy_observation_without_a_freshness_receipt_is_not_backfilled() -> None:
    legacy = _confirmed_observation()
    assert observation_has_essential_freshness_failure(legacy) is False
    result = evaluate_lifecycle_transition(None, legacy, lifecycle_id="legacy")
    assert result.record.confirmation_count == 1
    assert result.record.current_state != SetupLifecycleState.REJECTED


class _SelectiveStaleAdapter(FakeAdapterExchangeClient):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.shift_timeframes: set[str] = set()

    async def get_klines(self, symbol: str, interval: str, limit: int):
        candles = await FakeExchangeClient.get_klines(self, symbol, interval, limit)
        if interval.lower() in self.shift_timeframes:
            return [{**candle, "timestamp": int(candle["timestamp"]) - SEVEN_DAYS_MS} for candle in candles]
        return candles

    async def get_klines_with_delivery(self, symbol: str, interval: str, limit: int, *, capture_clock=None, occurrence_factory=None):
        from app.data.candle_batch_evidence import new_capture_occurrence_token, observe_adapter_normalized_batch

        candles = await self.get_klines(symbol, interval, limit)
        clock = capture_clock or (lambda: datetime.now(UTC))
        token_factory = occurrence_factory or new_capture_occurrence_token
        return observe_adapter_normalized_batch(
            candles,
            requested_symbol=symbol,
            requested_interval=interval,
            requested_limit=limit,
            effective_symbol=symbol.upper(),
            effective_interval=interval,
            effective_limit=limit,
            endpoint_path="fake_exchange.get_klines",
            adapter_class_label=f"{type(self).__module__}.{type(self).__qualname__}",
            capture_clock=clock,
            occurrence_factory=token_factory,
            http_client_injected=True,
            base_url_label="fake://exchange",
        )


@pytest.mark.no_auto_epoch
def test_registered_scans_confirm_fresh_evidence_and_a_later_stale_scan_holds_the_owner(tmp_path, monkeypatch) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "freshness.sqlite")
    sender = FakeSender()
    clock = {"now": DECISION}

    def _tick() -> datetime:
        current = clock["now"]
        clock["now"] = current + timedelta(seconds=5)
        return current

    client = _SelectiveStaleAdapter(
        {"BTCUSDT": _public_min_rr_pullback_candles()},
        failing_timeframes={"2d"},
        candle_as_of=lambda: clock["now"],
    )

    def _runner(cache=None, **_kwargs):
        return ScannerRunner(exchange_client=client, market_data_cache=cache, clock=_tick, capture_clock=_tick)

    original_init = TelegramLifecycleDeliveryService.__init__

    def _init(self, *args, **kwargs):
        kwargs["sender"] = sender
        settings = kwargs.get("settings") or Settings(
            _env_file=None,
            telegram_dry_run=True,
            telegram_signals_enabled=False,
            local_manual_mode=True,
            order_execution_enabled=False,
        )
        kwargs["settings"] = settings.model_copy(update={"telegram_public_watchlist_enabled": True})
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(run_scan, "_scanner_runner", _runner)
    monkeypatch.setattr(run_scan, "_watch_iteration_timestamp", lambda: _tick().strftime("%Y-%m-%dT%H:%M:%SZ"))
    monkeypatch.setattr("app.lifecycle.service.now_utc_iso", lambda: clock["now"].strftime("%Y-%m-%dT%H:%M:%SZ"))
    monkeypatch.setattr(TelegramLifecycleDeliveryService, "__init__", _init)
    monkeypatch.setattr(run_scan, "WATCH_STATE_PATH", tmp_path / "watch_state.json")
    monkeypatch.setattr(run_scan, "WATCH_STATE_CANONICAL_BASE_DIR", tmp_path)
    argv = [
        "--strategy-version", "liquidity-grab-v1", "--htf-timeframe", "2d",
        "--symbols",
        "BTCUSDT",
        "--database-path",
        str(db_path),
        "--runtime-epoch-id",
        SYNTHETIC_EPOCH_ID,
        "--runtime-epoch-cutoff-at",
        SYNTHETIC_CUTOFF_AT,
        "--runtime-reviewed-release-sha",
        SYNTHETIC_RELEASE_SHA,
        "--runtime-generation-binding",
        SYNTHETIC_GENERATION_BINDING,
        "--lifecycle",
        "--store-scan",
        "--telegram-manual-signals",
        "--no-cache",
        "--candle-limit",
        "220",
    ]
    asyncio.run(run_scan.main(argv))
    asyncio.run(run_scan.main(argv))
    identity_sql = """
        SELECT lifecycle_id, mode, direction, current_state, confirmation_count,
               entry_low, stop_loss, tp1, plan_version_id, is_current
        FROM setup_lifecycle_records
        ORDER BY lifecycle_id
    """
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        before = [dict(row) for row in connection.execute(identity_sql)]
        blocked = connection.execute(
            "SELECT blocked_reason FROM telegram_alert_attempts WHERE new_state = 'CONFIRMED'"
        ).fetchone()
    confirmed = [row for row in before if row["current_state"] == SetupLifecycleState.CONFIRMED.value]
    assert confirmed
    assert all(int(row["confirmation_count"]) >= 2 for row in confirmed)
    calls_after_confirm = len(sender.calls)
    assert blocked is not None
    assert "public_setup_quality_score_below_min:86<88" in str(blocked["blocked_reason"])
    client.shift_timeframes.add("15m")
    asyncio.run(run_scan.main(argv))
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        after = [dict(row) for row in connection.execute(identity_sql)]
    assert after == before
    assert len(sender.calls) == calls_after_confirm


@pytest.mark.no_auto_epoch
def test_publicly_eligible_fresh_control_sends_and_a_later_stale_scan_does_not(tmp_path, monkeypatch) -> None:
    """An even Unix-epoch day scores 89, above the 88 public floor.

    The March 1 fixture scores 86 and is already blocked by that floor, so a
    later stale scan with zero sends does not isolate freshness.
    """

    decision = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
    db_path = bootstrap_operational_test_database(tmp_path / "public-fresh.sqlite")
    sender = FakeSender()
    clock = {"now": decision}

    def _tick() -> datetime:
        current = clock["now"]
        clock["now"] = current + timedelta(seconds=5)
        return current

    client = _SelectiveStaleAdapter(
        {"BTCUSDT": _public_min_rr_pullback_candles()},
        failing_timeframes={"2d"},
        candle_as_of=lambda: clock["now"],
    )

    def _runner(cache=None, **_kwargs):
        return ScannerRunner(exchange_client=client, market_data_cache=cache, clock=_tick, capture_clock=_tick)

    original_init = TelegramLifecycleDeliveryService.__init__

    def _init(self, *args, **kwargs):
        kwargs["sender"] = sender
        settings = kwargs.get("settings") or Settings(
            _env_file=None,
            telegram_dry_run=True,
            telegram_signals_enabled=False,
            local_manual_mode=True,
            order_execution_enabled=False,
        )
        kwargs["settings"] = settings.model_copy(update={"telegram_public_watchlist_enabled": True})
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(run_scan, "_scanner_runner", _runner)
    monkeypatch.setattr(run_scan, "_watch_iteration_timestamp", lambda: _tick().strftime("%Y-%m-%dT%H:%M:%SZ"))
    monkeypatch.setattr("app.lifecycle.service.now_utc_iso", lambda: clock["now"].strftime("%Y-%m-%dT%H:%M:%SZ"))
    monkeypatch.setattr(TelegramLifecycleDeliveryService, "__init__", _init)
    monkeypatch.setattr(run_scan, "WATCH_STATE_PATH", tmp_path / "watch_state.json")
    monkeypatch.setattr(run_scan, "WATCH_STATE_CANONICAL_BASE_DIR", tmp_path)
    argv = [
        "--strategy-version", "liquidity-grab-v1", "--htf-timeframe", "2d",
        "--symbols",
        "BTCUSDT",
        "--database-path",
        str(db_path),
        "--runtime-epoch-id",
        SYNTHETIC_EPOCH_ID,
        "--runtime-epoch-cutoff-at",
        SYNTHETIC_CUTOFF_AT,
        "--runtime-reviewed-release-sha",
        SYNTHETIC_RELEASE_SHA,
        "--runtime-generation-binding",
        SYNTHETIC_GENERATION_BINDING,
        "--lifecycle",
        "--store-scan",
        "--telegram-manual-signals",
        "--no-cache",
        "--candle-limit",
        "220",
    ]
    asyncio.run(run_scan.main(argv))
    asyncio.run(run_scan.main(argv))
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        confirmed = [
            dict(row)
            for row in connection.execute(
                """
                SELECT lifecycle_id, current_state, confirmation_count, plan_version_id
                FROM setup_lifecycle_records
                WHERE current_state = ?
                """,
                (SetupLifecycleState.CONFIRMED.value,),
            )
        ]
        public_before = connection.execute(
            "SELECT COUNT(*) FROM public_alert_events"
        ).fetchone()[0]
    assert confirmed
    assert all(int(row["confirmation_count"]) >= 2 for row in confirmed)
    calls_after_confirm = len(sender.calls)
    assert calls_after_confirm >= 1
    assert public_before >= 1
    client.shift_timeframes.add("15m")
    asyncio.run(run_scan.main(argv))
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        held = dict(
            connection.execute(
                """
                SELECT lifecycle_id, current_state, confirmation_count, plan_version_id
                FROM setup_lifecycle_records
                WHERE lifecycle_id = ?
                """,
                (confirmed[0]["lifecycle_id"],),
            ).fetchone()
        )
        public_after = connection.execute(
            "SELECT COUNT(*) FROM public_alert_events"
        ).fetchone()[0]
    assert held == confirmed[0]
    assert len(sender.calls) == calls_after_confirm
    assert public_after == public_before

