"""P5A regression: owned economic plans stay monitored independently of discovery."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.analytics.public_alert_funnel import DEFAULT_PUBLIC_RR_MIN
from app.analytics.public_signal_quality import (
    MIN_PUBLIC_SETUP_QUALITY_SCORE,
    MIN_PUBLIC_SIGNAL_GRADE,
)
from app.lifecycle.economic_identity import latch_economic_identities, proven_progress_plan_version_id
from app.lifecycle.models import (
    SetupLifecycleOutcomeProgress,
    SetupLifecycleRecord,
    SetupLifecycleState,
)
from app.lifecycle.outcome_policy import canonical_plan_identity
from app.lifecycle.outcomes import INTEGRITY_UNVERIFIED, evaluate_closed_candle_outcomes
from app.lifecycle.owner_monitoring import (
    GAP_EXCHANGE_MARKET_DATA,
    GAP_MARKET_UNSUPPORTED,
    GAP_REQUIRED_CLOSED_CANDLE,
    SymbolMonitoringEvidence,
    classify_market_data_failure,
    diagnose_tracking_cursor_lag,
    discovery_failure_continues_owner_monitoring,
    evidence_key,
    monitor_obligations_with_market_data,
    monitor_tracking_obligations,
    select_tracking_obligations,
)
from app.lifecycle.plan_version_binding import (
    PLAN_VERSION_BINDING_AWAITING,
    bind_progress_to_proven_plan_version,
    resolve_persisted_plan_version,
)
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import SetupLifecycleService
from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerRunConfig,
    ScannerRunResult,
    ScannerSymbolResult,
)
from app.storage.database import SCHEMA_VERSION, open_initialized_database
from app.universe.symbol_universe import UniverseResolutionError

BASE = datetime(2026, 3, 23, 6, 0, tzinfo=UTC)
TIMEFRAME = "5m"
INVALIDATION = "Closed structure beyond the stored stop invalidates the plan."


def _candle(index: int, *, high: str, low: str) -> dict[str, object]:
    opened = BASE + timedelta(minutes=5 * index)
    return {
        "timestamp": int(opened.timestamp() * 1000),
        "open": Decimal(low),
        "high": Decimal(high),
        "low": Decimal(low),
        "close": Decimal(high),
        "volume": Decimal("10"),
    }


def _decision(index: int) -> str:
    return (BASE + timedelta(minutes=5 * (index + 1))).isoformat()


def _record(
    *,
    lifecycle_id: str = "life-cake",
    symbol: str = "CAKEUSDT",
    mode: str = "scalp",
    direction: str = "short",
    state: SetupLifecycleState = SetupLifecycleState.CONFIRMED,
    **updates: object,
) -> SetupLifecycleRecord:
    levels = (
        {
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "90",
            "tp1": "110",
            "tp2": "120",
            "tp3": "130",
        }
        if direction == "long"
        else {
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "112",
            "tp1": "92",
            "tp2": "84",
            "tp3": "76",
        }
    )
    values: dict[str, object] = {
        "lifecycle_id": lifecycle_id,
        "symbol": symbol,
        "mode": mode,
        "direction": direction,
        "current_state": state,
        "first_seen_at": BASE.isoformat(),
        "last_seen_at": BASE.isoformat(),
        "last_transition_at": BASE.isoformat(),
        "confirmed_at": BASE.isoformat(),
        "invalidation_reason": INVALIDATION,
        "invalidation_logic": INVALIDATION,
        "setup_identity": f"{lifecycle_id}-setup",
        "structural_anchor": f"anchor-{lifecycle_id}",
        **levels,
    }
    values.update(updates)
    return SetupLifecycleRecord(**values)


def _latched(**updates: object) -> SetupLifecycleRecord:
    return latch_economic_identities(
        _record(**updates),
        instrument_venue="binance",
        plan_locked=True,
    )


def _evaluate(repository: SQLiteSetupLifecycleRepository, record: SetupLifecycleRecord, candles: list[dict[str, object]]):
    index = max(0, len(candles) - 1)
    return evaluate_closed_candle_outcomes(
        record,
        execution_candles=candles,
        execution_timeframe=TIMEFRAME,
        decision_timestamp=_decision(index),
        evaluated_at=_decision(index),
        repository=repository,
        scan_run_id="scan-seed",
    )


def _seed_entered(repository: SQLiteSetupLifecycleRepository, **updates: object) -> SetupLifecycleRecord:
    record = _latched(**updates)
    repository.upsert_record(record)
    candles = [
        _candle(0, high="107", low="103"),
        _candle(1, high="103", low="99"),
    ]
    result = _evaluate(repository, record, candles)
    assert result.progress is not None
    assert result.progress.entry_at is not None
    assert result.record.current_state == SetupLifecycleState.MANAGING
    return result.record


def _rejected_result(symbol: str, candles: tuple[dict[str, object], ...], *, mode: str, bias: str | None) -> ScannerSymbolResult:
    diagnostics: dict[str, object] = {
        "mode": mode,
        "rr_to_tp2": Decimal("0.8562"),
        "first_failed_gate": "rr_too_low",
        "gates_failed": ("rr_too_low",),
        "gates_passed": (),
    }
    if bias is not None:
        diagnostics["bias"] = bias
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejected_strategy_modes=(mode,),
        strategy_diagnostics={mode: diagnostics},
        rejection_stage="rr_too_low",
        lifecycle_execution_candles=candles,
        lifecycle_execution_timeframe=TIMEFRAME,
        lifecycle_decision_timestamp=_decision(len(candles) - 1),
    )


def _run(symbol_result: ScannerSymbolResult) -> ScannerRunResult:
    config = ScannerRunConfig.model_validate(
        {
            "symbols": [symbol_result.symbol],
            "exchange": "binance",
            "account_equity": Decimal("10000"),
            "risk_per_trade_pct": Decimal("1"),
            "execution_timeframe": TIMEFRAME,
        }
    )
    return ScannerRunResult(
        config=config,
        results=(symbol_result,),
        scanned_symbols=1,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )


def _apply(db_path: Path, symbol_result: ScannerSymbolResult) -> ScannerRunResult:
    return SetupLifecycleService(db_path).apply_to_run_result(
        _run(symbol_result),
        scan_run_id="scan-discovery",
        now=_decision(3),
    )


def _progress(repository: SQLiteSetupLifecycleRepository, lifecycle_id: str) -> SetupLifecycleOutcomeProgress:
    rows = repository.list_outcome_progress(lifecycle_id=lifecycle_id)
    assert len(rows) == 1
    return rows[0]


def _event_count(repository: SQLiteSetupLifecycleRepository, lifecycle_id: str) -> int:
    return len(repository.list_events(lifecycle_id=lifecycle_id))


def _continuation_candles() -> list[dict[str, object]]:
    return [
        _candle(0, high="107", low="103"),
        _candle(1, high="103", low="99"),
        _candle(2, high="99", low="91"),
    ]


def test_cake_like_rejected_na_observation_cannot_starve_entered_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "cake.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository, symbol="CAKEUSDT", mode="scalp", direction="short")
        before = _progress(repository, entered.lifecycle_id).evaluation_cursor_close_at
    candles = tuple(_continuation_candles())
    result = _apply(
        db_path,
        _rejected_result("CAKEUSDT", candles, mode="scalp", bias=None),
    )
    selected = result.results[0].lifecycle_state
    assert selected is not None
    assert selected.lifecycle_id != entered.lifecycle_id
    assert selected.direction == "N/A"
    assert selected.current_state == SetupLifecycleState.REJECTED
    assert selected.plan_version_id is None
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        progressed = _progress(repository, entered.lifecycle_id)
        stored = repository.get_record_by_lifecycle_id(entered.lifecycle_id)
    assert stored is not None
    assert stored.current_state == SetupLifecycleState.MANAGING
    assert progressed.tp1_at is not None
    assert progressed.evaluation_cursor_close_at != before
    assert progressed.plan_version_id == entered.plan_version_id


def test_inj_like_opposite_direction_discovery_still_advances_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "inj.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository, lifecycle_id="life-inj", symbol="INJUSDT", mode="scalp", direction="short")
    _apply(
        db_path,
        _rejected_result("INJUSDT", tuple(_continuation_candles()), mode="scalp", bias="long"),
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        progressed = _progress(repository, entered.lifecycle_id)
        rejection = repository.get_record(symbol="INJUSDT", mode="scalp", direction="long")
    assert progressed.tp1_at is not None
    assert rejection is None or rejection.lifecycle_id != entered.lifecycle_id
    assert progressed.plan_version_id == entered.plan_version_id


def test_different_mode_discovery_cannot_starve_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "mode.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository, lifecycle_id="life-kas", symbol="KASUSDT", mode="scalp", direction="short")
    _apply(
        db_path,
        _rejected_result("KASUSDT", tuple(_continuation_candles()), mode="swing", bias=None),
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        progressed = _progress(repository, entered.lifecycle_id)
    assert progressed.tp1_at is not None


def test_scalp_and_swing_obligations_are_both_monitored(tmp_path: Path) -> None:
    db_path = tmp_path / "both.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        scalp = _seed_entered(repository, lifecycle_id="life-scalp", symbol="POLUSDT", mode="scalp", direction="short")
        swing = _latched(lifecycle_id="life-swing", symbol="POLUSDT", mode="swing", direction="long")
        repository.upsert_record(swing)
        _evaluate(repository, swing, [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")])
    candles = (
        _candle(0, high="99", low="95"),
        _candle(1, high="103", low="99"),
        _candle(2, high="111", low="103"),
        _candle(3, high="99", low="91"),
    )
    _apply(db_path, _rejected_result("POLUSDT", candles, mode="scalp", bias=None))
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        scalp_progress = _progress(repository, scalp.lifecycle_id)
        swing_progress = _progress(repository, swing.lifecycle_id)
    assert scalp_progress.tp1_at is not None
    assert swing_progress.tp1_at is not None
    assert scalp_progress.plan_version_id == scalp.plan_version_id
    assert swing_progress.plan_version_id == swing.plan_version_id
    assert scalp_progress.plan_version_id != swing_progress.plan_version_id


def test_noncurrent_unresolved_owner_continues_monitoring(tmp_path: Path) -> None:
    db_path = tmp_path / "noncurrent.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository, lifecycle_id="life-jst", symbol="JSTUSDT")
        repository.supersede_record(entered.lifecycle_id)
        stored = repository.get_record_by_lifecycle_id(entered.lifecycle_id)
        assert stored is not None and stored.is_current is False
        obligations = select_tracking_obligations(repository)
    assert entered.lifecycle_id in {item.lifecycle_id for item in obligations}
    evidence = {
        evidence_key("JSTUSDT", TIMEFRAME): SymbolMonitoringEvidence(
            candles=tuple(_continuation_candles()),
            execution_timeframe=TIMEFRAME,
            decision_timestamp=_decision(2),
        )
    }
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        monitor_tracking_obligations(
            repository,
            evidence_by_key=evidence,
            evaluated_at=_decision(2),
            scan_run_id="scan-noncurrent",
            default_timeframe=TIMEFRAME,
        )
        progressed = _progress(repository, entered.lifecycle_id)
        stored = repository.get_record_by_lifecycle_id(entered.lifecycle_id)
    assert stored is not None and stored.is_current is False
    assert progressed.tp1_at is not None


def test_symbol_leaving_discovery_universe_still_tracks(tmp_path: Path) -> None:
    db_path = tmp_path / "universe.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository, lifecycle_id="life-atom", symbol="ATOMUSDT")

    async def fetch(symbol: str, timeframe: str, limit: int) -> tuple[dict[str, object], ...]:
        assert symbol == "ATOMUSDT"
        assert timeframe == TIMEFRAME
        assert limit == 50
        return tuple(_continuation_candles())

    import asyncio

    asyncio.run(
        monitor_obligations_with_market_data(
            db_path,
            evidence_by_key={},
            fetch_candles=fetch,
            execution_timeframe=TIMEFRAME,
            candle_limit=50,
            evaluated_at=_decision(2),
            scan_run_id="scan-universe",
        )
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        progressed = _progress(repository, entered.lifecycle_id)
    assert progressed.tp1_at is not None


def test_ranking_provider_failure_still_monitors_when_market_data_exists(tmp_path: Path) -> None:
    failure = SystemExit("universe failed")
    failure.__cause__ = UniverseResolutionError("market-cap source failed: HTTP 503")
    assert discovery_failure_continues_owner_monitoring(failure) is True
    assert discovery_failure_continues_owner_monitoring(SystemExit("bad args")) is False
    assert discovery_failure_continues_owner_monitoring(RuntimeError("scanner")) is False

    db_path = tmp_path / "ranking.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository, lifecycle_id="life-rank", symbol="BNBUSDT")

    async def fetch(symbol: str, timeframe: str, limit: int) -> tuple[dict[str, object], ...]:
        del symbol, timeframe, limit
        return tuple(_continuation_candles())

    import asyncio

    asyncio.run(
        monitor_obligations_with_market_data(
            db_path,
            evidence_by_key={},
            fetch_candles=fetch,
            execution_timeframe=TIMEFRAME,
            candle_limit=20,
            evaluated_at=_decision(2),
            scan_run_id="owner-monitor-ranking",
        )
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        assert _progress(repository, entered.lifecycle_id).tp1_at is not None


def test_exchange_candle_failure_is_explicit_and_not_terminal(tmp_path: Path) -> None:
    db_path = tmp_path / "exchange.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository, lifecycle_id="life-gap", symbol="WLDUSDT")
        before_cursor = _progress(repository, entered.lifecycle_id).evaluation_cursor_close_at
        before_events = _event_count(repository, entered.lifecycle_id)

    async def fetch(symbol: str, timeframe: str, limit: int) -> tuple[dict[str, object], ...]:
        del symbol, timeframe, limit
        raise TimeoutError("klines timed out")

    import asyncio

    asyncio.run(
        monitor_obligations_with_market_data(
            db_path,
            evidence_by_key={},
            fetch_candles=fetch,
            execution_timeframe=TIMEFRAME,
            candle_limit=20,
            evaluated_at=_decision(4),
            scan_run_id="owner-monitor-gap",
        )
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        progressed = _progress(repository, entered.lifecycle_id)
        stored = repository.get_record_by_lifecycle_id(entered.lifecycle_id)
        assert stored is not None
        assert stored.current_state == SetupLifecycleState.MANAGING
        assert progressed.evaluation_cursor_close_at == before_cursor
        assert progressed.tp1_at is None
        assert progressed.terminal_outcome == "N/A"
        assert progressed.integrity_status == INTEGRITY_UNVERIFIED
        assert GAP_EXCHANGE_MARKET_DATA in progressed.diagnostic
        assert _event_count(repository, entered.lifecycle_id) == before_events


def test_unsupported_market_gap_does_not_invent_outcome(tmp_path: Path) -> None:
    db_path = tmp_path / "delist.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository, lifecycle_id="life-delist", symbol="OLDUSDT")

    async def fetch(symbol: str, timeframe: str, limit: int) -> tuple[dict[str, object], ...]:
        del symbol, timeframe, limit
        raise RuntimeError("Invalid symbol")

    import asyncio

    result = asyncio.run(
        monitor_obligations_with_market_data(
            db_path,
            evidence_by_key={},
            fetch_candles=fetch,
            execution_timeframe=TIMEFRAME,
            candle_limit=20,
            evaluated_at=_decision(4),
            scan_run_id="owner-monitor-delist",
        )
    )
    assert result.lags[0].gap_reason == GAP_MARKET_UNSUPPORTED
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        stored = repository.get_record_by_lifecycle_id(entered.lifecycle_id)
        progressed = _progress(repository, entered.lifecycle_id)
    assert stored is not None and stored.current_state == SetupLifecycleState.MANAGING
    assert progressed.terminal_outcome == "N/A"


def test_restart_resumes_cursor_and_repeated_candle_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "restart.db"
    evidence = {
        evidence_key("CAKEUSDT", TIMEFRAME): SymbolMonitoringEvidence(
            candles=tuple(_continuation_candles()[:2]),
            execution_timeframe=TIMEFRAME,
            decision_timestamp=_decision(1),
        )
    }
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository)
        monitor_tracking_obligations(
            repository,
            evidence_by_key=evidence,
            evaluated_at=_decision(1),
            scan_run_id="scan-1",
            default_timeframe=TIMEFRAME,
        )
        once = _progress(repository, entered.lifecycle_id)
        events_after = _event_count(repository, entered.lifecycle_id)
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        monitor_tracking_obligations(
            repository,
            evidence_by_key=evidence,
            evaluated_at=_decision(1),
            scan_run_id="scan-2",
            default_timeframe=TIMEFRAME,
        )
        repeated = _progress(repository, entered.lifecycle_id)
        assert _event_count(repository, entered.lifecycle_id) == events_after
        assert repeated.entry_at == once.entry_at
        assert repeated.evaluation_cursor_close_at == once.evaluation_cursor_close_at
        catch_up = {
            evidence_key("CAKEUSDT", TIMEFRAME): SymbolMonitoringEvidence(
                candles=tuple(_continuation_candles() + [_candle(3, high="98", low="90")]),
                execution_timeframe=TIMEFRAME,
                decision_timestamp=_decision(3),
            )
        }
        monitor_tracking_obligations(
            repository,
            evidence_by_key=catch_up,
            evaluated_at=_decision(3),
            scan_run_id="scan-3",
            default_timeframe=TIMEFRAME,
        )
        caught = _progress(repository, entered.lifecycle_id)
    assert caught.tp1_at is not None
    assert caught.evaluation_cursor_open_at != once.evaluation_cursor_open_at


def test_out_of_order_invocation_does_not_regress_cursor(tmp_path: Path) -> None:
    db_path = tmp_path / "order.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository, lifecycle_id="life-order")
        later = {
            evidence_key(entered.symbol, TIMEFRAME): SymbolMonitoringEvidence(
                candles=tuple(_continuation_candles()),
                execution_timeframe=TIMEFRAME,
                decision_timestamp=_decision(2),
            )
        }
        monitor_tracking_obligations(
            repository,
            evidence_by_key=later,
            evaluated_at=_decision(2),
            scan_run_id="scan-later",
            default_timeframe=TIMEFRAME,
        )
        advanced = _progress(repository, entered.lifecycle_id).evaluation_cursor_close_at
        earlier = {
            evidence_key(entered.symbol, TIMEFRAME): SymbolMonitoringEvidence(
                candles=(_candle(0, high="107", low="103"),),
                execution_timeframe=TIMEFRAME,
                decision_timestamp=_decision(0),
            )
        }
        monitor_tracking_obligations(
            repository,
            evidence_by_key=earlier,
            evaluated_at=_decision(2),
            scan_run_id="scan-earlier",
            default_timeframe=TIMEFRAME,
        )
        progressed = _progress(repository, entered.lifecycle_id)
    assert progressed.evaluation_cursor_close_at == advanced
    assert "stale_execution_candle_history" in progressed.diagnostic


def test_continuity_gap_is_explicit_and_does_not_skip_ahead(tmp_path: Path) -> None:
    db_path = tmp_path / "hole.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository, lifecycle_id="life-hole")
        before = _progress(repository, entered.lifecycle_id).evaluation_cursor_close_at
        gapped = (
            _candle(0, high="107", low="103"),
            _candle(1, high="103", low="99"),
            _candle(3, high="99", low="91"),
        )
        evidence = {
            evidence_key(entered.symbol, TIMEFRAME): SymbolMonitoringEvidence(
                candles=gapped,
                execution_timeframe=TIMEFRAME,
                decision_timestamp=_decision(3),
            )
        }
        monitor_tracking_obligations(
            repository,
            evidence_by_key=evidence,
            evaluated_at=_decision(3),
            scan_run_id="scan-gap",
            default_timeframe=TIMEFRAME,
        )
        progressed = _progress(repository, entered.lifecycle_id)
    assert progressed.evaluation_cursor_close_at == before
    assert progressed.tp1_at is None
    assert progressed.integrity_status == INTEGRITY_UNVERIFIED
    assert "expected open" in progressed.diagnostic


def test_terminal_owner_does_not_reopen_on_later_discovery(tmp_path: Path) -> None:
    db_path = tmp_path / "terminal.db"
    stop_candles = [
        _candle(0, high="107", low="103"),
        _candle(1, high="103", low="99"),
        _candle(2, high="113", low="99"),
    ]
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository, lifecycle_id="life-stop")
        stopped = _evaluate(repository, entered, stop_candles)
        assert stopped.record.current_state == SetupLifecycleState.SL_HIT
        events = _event_count(repository, entered.lifecycle_id)
        assert select_tracking_obligations(repository) == ()
    _apply(db_path, _rejected_result(entered.symbol, tuple(stop_candles), mode="scalp", bias=None))
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        stored = repository.get_record_by_lifecycle_id(entered.lifecycle_id)
        assert stored is not None and stored.current_state == SetupLifecycleState.SL_HIT
        assert _event_count(repository, entered.lifecycle_id) == events
        assert _progress(repository, entered.lifecycle_id).terminal_outcome == SetupLifecycleState.SL_HIT.value


def test_entry_tp_and_stop_milestones_are_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "idem.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _latched(lifecycle_id="life-idem", state=SetupLifecycleState.CONFIRMED)
        repository.upsert_record(entered)
        entry_candles = [_candle(0, high="107", low="103"), _candle(1, high="103", low="99")]
        first = _evaluate(repository, entered, entry_candles)
        entry_events = _event_count(repository, entered.lifecycle_id)
        second = _evaluate(repository, first.record, entry_candles)
        assert first.progress is not None and second.progress is not None
        assert first.progress.entry_at == second.progress.entry_at
        assert _event_count(repository, entered.lifecycle_id) == entry_events
        tp_candles = entry_candles + [_candle(2, high="99", low="91")]
        tp_first = _evaluate(repository, second.record, tp_candles)
        tp_second = _evaluate(repository, tp_first.record, tp_candles)
        assert tp_first.progress is not None and tp_second.progress is not None
        assert tp_first.progress.tp1_at == tp_second.progress.tp1_at
        tp_events = _event_count(repository, entered.lifecycle_id)
        assert tp_events >= entry_events
        stop_candles = tp_candles + [_candle(3, high="113", low="99")]
        # TP1 is not terminal. A later stop still resolves the plan once.
        sl_first = _evaluate(repository, tp_second.record, stop_candles)
        sl_events = _event_count(repository, entered.lifecycle_id)
        sl_second = _evaluate(repository, sl_first.record, stop_candles)
        assert sl_first.record.current_state == SetupLifecycleState.SL_HIT
        assert sl_second.record.current_state == SetupLifecycleState.SL_HIT
        assert _event_count(repository, entered.lifecycle_id) == sl_events
        assert sl_first.progress is not None and sl_second.progress is not None
        assert sl_first.progress.stop_at == sl_second.progress.stop_at
        assert sl_first.progress.plan_version_id == entered.plan_version_id


def test_prelock_progress_binds_only_for_the_same_proven_economics(tmp_path: Path) -> None:
    db_path = tmp_path / "bind.db"
    unlocked = _record(lifecycle_id="life-bind", state=SetupLifecycleState.TRIGGERED, confirmed_at=None)
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(unlocked)
        _evaluate(repository, unlocked, [_candle(0, high="107", low="103")])
        raw = _progress(repository, unlocked.lifecycle_id)
        assert raw.plan_version_id is None
        marker = json.loads(raw.metadata_json)["plan_version_binding"]
        assert marker == PLAN_VERSION_BINDING_AWAITING
        locked = latch_economic_identities(
            unlocked.model_copy(update={"current_state": SetupLifecycleState.CONFIRMED, "confirmed_at": BASE.isoformat()}),
            instrument_venue="binance",
            plan_locked=True,
            previous=unlocked,
        )
        repository.upsert_record(locked)
        _evaluate(repository, locked, [_candle(0, high="107", low="103"), _candle(1, high="103", low="99")])
        bound = _progress(repository, unlocked.lifecycle_id)
    assert bound.plan_version_id == locked.plan_version_id
    assert bound.plan_version_id == proven_progress_plan_version_id(locked)

    foreign = _latched(lifecycle_id="life-foreign", symbol="ETHUSDT")
    decision = bind_progress_to_proven_plan_version(raw, foreign)
    assert decision.reason == "different_lifecycle"
    assert decision.bound is False
    assert decision.progress.plan_version_id is None

    changed = locked.model_copy(
        update={
            "entry_low": "200",
            "entry_high": "202",
            "stop_loss": "220",
            "tp1": "180",
            "tp2": "160",
            "tp3": "140",
            "invalidation_logic": "Closed structure above 220 invalidates the replacement plan.",
            "invalidation_reason": "Closed structure above 220 invalidates the replacement plan.",
        }
    )
    changed = latch_economic_identities(changed, instrument_venue="binance", plan_locked=True, previous=locked)
    mismatch = bind_progress_to_proven_plan_version(raw, changed)
    assert mismatch.bound is False
    assert mismatch.progress.plan_version_id is None

    wrong = bind_progress_to_proven_plan_version(
        raw.model_copy(update={"plan_version_id": "plan-version-wrong"}),
        locked,
    )
    assert wrong.reason == "wrong_plan_version"
    assert wrong.bound is False


def test_legacy_null_progress_cannot_be_guessed(tmp_path: Path) -> None:
    record = _latched(lifecycle_id="life-legacy")
    reconstructed = SetupLifecycleOutcomeProgress(
        lifecycle_id=record.lifecycle_id,
        plan_identity=canonical_plan_identity(record),
        symbol=record.symbol,
        mode=record.mode,
        direction=record.direction,
        execution_timeframe=TIMEFRAME,
        first_evaluated_at=_decision(0),
        last_evaluated_at=_decision(0),
        metadata_json=json.dumps({"source": "legacy-reconstruction"}),
    )
    with SQLiteSetupLifecycleRepository(tmp_path / "legacy.db") as repository:
        repository.upsert_record(record)
        repository.upsert_outcome_progress(reconstructed)
        resolved = resolve_persisted_plan_version(reconstructed, record, None)
        _evaluate(repository, record, [_candle(0, high="107", low="103"), _candle(1, high="103", low="99")])
        stored = _progress(repository, record.lifecycle_id)
    assert resolved.plan_version_id is None
    assert stored.plan_version_id is None
    assert stored.entry_at is not None


def test_wrong_plan_version_cannot_overwrite_a_bound_row(tmp_path: Path) -> None:
    db_path = tmp_path / "overwrite.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository, lifecycle_id="life-keep")
        progress = _progress(repository, entered.lifecycle_id)
        forged = progress.model_copy(update={"plan_version_id": "plan-version-other"})
        repository.upsert_outcome_progress(forged)
        stored = _progress(repository, entered.lifecycle_id)
    assert stored.plan_version_id == entered.plan_version_id


def test_public_confirmed_owner_keeps_receiving_closed_candle_evaluation(tmp_path: Path) -> None:
    db_path = tmp_path / "public.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        confirmed = _latched(lifecycle_id="life-eth", symbol="ETHUSDT", state=SetupLifecycleState.CONFIRMED)
        repository.upsert_record(confirmed)
        assert confirmed.plan_version_id is not None
        assert select_tracking_obligations(repository)[0].lifecycle_id == confirmed.lifecycle_id
    _apply(
        db_path,
        _rejected_result(
            "ETHUSDT",
            (_candle(0, high="107", low="103"), _candle(1, high="103", low="99")),
            mode="scalp",
            bias=None,
        ),
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        progressed = _progress(repository, confirmed.lifecycle_id)
        stored = repository.get_record_by_lifecycle_id(confirmed.lifecycle_id)
    assert stored is not None and stored.current_state == SetupLifecycleState.MANAGING
    assert progressed.entry_at is not None
    assert progressed.plan_version_id == confirmed.plan_version_id


def test_two_monitoring_passes_do_not_duplicate_economic_events(tmp_path: Path) -> None:
    db_path = tmp_path / "twice.db"
    evidence = {
        evidence_key("CAKEUSDT", TIMEFRAME): SymbolMonitoringEvidence(
            candles=tuple(_continuation_candles()),
            execution_timeframe=TIMEFRAME,
            decision_timestamp=_decision(2),
        )
    }
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository)
        monitor_tracking_obligations(
            repository,
            evidence_by_key=evidence,
            evaluated_at=_decision(2),
            scan_run_id="scan-a",
            default_timeframe=TIMEFRAME,
        )
        events = _event_count(repository, entered.lifecycle_id)
        tp1 = _progress(repository, entered.lifecycle_id).tp1_at
        monitor_tracking_obligations(
            repository,
            evidence_by_key=evidence,
            evaluated_at=_decision(2),
            scan_run_id="scan-b",
            default_timeframe=TIMEFRAME,
        )
        assert _event_count(repository, entered.lifecycle_id) == events
        assert _progress(repository, entered.lifecycle_id).tp1_at == tp1


def test_cursor_lag_diagnostic_does_not_change_eligibility(tmp_path: Path) -> None:
    db_path = tmp_path / "lag.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository, lifecycle_id="life-lag")
        before = _progress(repository, entered.lifecycle_id)
        before_state = repository.get_record_by_lifecycle_id(entered.lifecycle_id)
        lag = diagnose_tracking_cursor_lag(
            entered,
            last_processed_close_at=before.evaluation_cursor_close_at,
            latest_available_close_at=_decision(4),
        )
        after = _progress(repository, entered.lifecycle_id)
        after_state = repository.get_record_by_lifecycle_id(entered.lifecycle_id)
    assert lag.lag_seconds is not None and lag.lag_seconds > 0
    assert lag.plan_version_id == entered.plan_version_id
    assert lag.last_processed_close_at == before.evaluation_cursor_close_at
    assert after.evaluation_cursor_close_at == before.evaluation_cursor_close_at
    assert after_state == before_state
    missing = diagnose_tracking_cursor_lag(
        entered,
        last_processed_close_at=before.evaluation_cursor_close_at,
        latest_available_close_at=None,
        gap_reason=GAP_REQUIRED_CLOSED_CANDLE,
    )
    assert missing.lag_seconds is None
    assert missing.gap_reason == GAP_REQUIRED_CLOSED_CANDLE


def test_rejected_and_terminal_rows_are_not_tracking_obligations(tmp_path: Path) -> None:
    db_path = tmp_path / "filter.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        owned = _seed_entered(repository, lifecycle_id="life-owned")
        rejected = _record(
            lifecycle_id="life-rejected",
            symbol="HYPEUSDT",
            state=SetupLifecycleState.REJECTED,
            direction="N/A",
            plan_version_id=None,
        )
        repository.upsert_record(rejected)
        cooled = _latched(lifecycle_id="life-cool", symbol="LINKUSDT", state=SetupLifecycleState.COOLDOWN)
        repository.upsert_record(cooled)
        triggered = _record(
            lifecycle_id="life-triggered",
            symbol="ATOMUSDT",
            state=SetupLifecycleState.TRIGGERED,
            direction="long",
            confirmed_at=None,
        )
        repository.upsert_record(triggered)
        selected = {item.lifecycle_id for item in select_tracking_obligations(repository)}
    assert owned.lifecycle_id in selected
    assert "life-rejected" not in selected
    assert "life-cool" not in selected
    assert "life-triggered" not in selected


def test_empty_discovery_candles_still_fetch_for_the_owned_plan(tmp_path: Path) -> None:
    db_path = tmp_path / "empty-discovery.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository, lifecycle_id="life-empty", symbol="CAKEUSDT")
    fetched: list[str] = []

    async def fetch(symbol: str, timeframe: str, limit: int) -> tuple[dict[str, object], ...]:
        del timeframe, limit
        fetched.append(symbol)
        return tuple(_continuation_candles())

    import asyncio

    asyncio.run(
        monitor_obligations_with_market_data(
            db_path,
            evidence_by_key={
                evidence_key("CAKEUSDT", TIMEFRAME): SymbolMonitoringEvidence(
                    candles=(),
                    execution_timeframe=TIMEFRAME,
                    decision_timestamp=_decision(2),
                )
            },
            fetch_candles=fetch,
            execution_timeframe=TIMEFRAME,
            candle_limit=20,
            evaluated_at=_decision(2),
            scan_run_id="owner-monitor-empty",
        )
    )
    assert fetched == ["CAKEUSDT"]
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        assert _progress(repository, entered.lifecycle_id).tp1_at is not None


def test_strategy_gates_and_rejection_admission_stay_unchanged() -> None:
    assert MIN_PUBLIC_SETUP_QUALITY_SCORE == Decimal("88")
    assert MIN_PUBLIC_SIGNAL_GRADE == "A"
    assert DEFAULT_PUBLIC_RR_MIN == Decimal("3")
    assert classify_market_data_failure(TimeoutError("timed out")).startswith(GAP_EXCHANGE_MARKET_DATA)
    assert classify_market_data_failure(RuntimeError("Invalid symbol")) == GAP_MARKET_UNSUPPORTED


def test_locked_plan_monitoring_index_is_added_without_schema_bump(tmp_path: Path) -> None:
    db_path = tmp_path / "index.sqlite"
    connection = open_initialized_database(db_path)
    connection.execute("DROP INDEX IF EXISTS ix_lifecycle_records_epoch_locked_plan_state")
    connection.commit()
    connection.close()
    connection = open_initialized_database(db_path)
    try:
        row = connection.execute(
            """
            SELECT sql FROM sqlite_master
            WHERE type = 'index' AND name = 'ix_lifecycle_records_epoch_locked_plan_state'
            """
        ).fetchone()
        assert row is not None
        assert "plan_version_id IS NOT NULL" in str(row[0])
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 26
    finally:
        connection.close()


def test_missing_candle_evidence_without_fetch_stays_uncovered(tmp_path: Path) -> None:
    db_path = tmp_path / "uncovered.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        entered = _seed_entered(repository, lifecycle_id="life-away", symbol="SKYUSDT")
        before = _progress(repository, entered.lifecycle_id)
        result = monitor_tracking_obligations(
            repository,
            evidence_by_key={},
            evaluated_at=_decision(4),
            scan_run_id="scan-away",
            default_timeframe=TIMEFRAME,
            record_missing_evidence=False,
        )
        after = _progress(repository, entered.lifecycle_id)
    assert "SKYUSDT" in result.uncovered_symbols
    assert after.evaluation_cursor_close_at == before.evaluation_cursor_close_at
    assert after.diagnostic == before.diagnostic
