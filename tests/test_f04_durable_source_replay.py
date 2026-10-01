"""F04 durable source-replay foundation.

Capture stays off unless a test installs an explicit evidence path. These tests
use temporary databases only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import sqlite3
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.analytics.public_alert_funnel import DEFAULT_PUBLIC_RR_MIN
from app.analytics.public_signal_quality import (
    MIN_PUBLIC_SETUP_QUALITY_SCORE,
    MIN_PUBLIC_SIGNAL_GRADE,
)
from app.core.config import Settings
from app.data.candle_batch_evidence import (
    CACHE_KIND_EXPIRY_REFETCH,
    CACHE_KIND_HIT,
    CACHE_KIND_MISS,
    associate_execution_handoff,
    observe_adapter_normalized_batch,
    observe_cache_delivery,
    observe_closed_subset,
    observe_synthetic_2d_resample,
)
from app.lifecycle.economic_identity import latch_economic_identities
from app.lifecycle.models import (
    SetupLifecycleEvent,
    SetupLifecycleRecord,
    SetupLifecycleState,
    SetupTransitionReason,
)
from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.owner_monitoring import (
    SymbolMonitoringEvidence,
    evidence_key,
    monitor_obligations_with_market_data,
    monitor_tracking_obligations,
)
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import SetupLifecycleService
from app.lifecycle.state_machine import CONFIRMED_MIN_RR
from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerRunConfig,
    ScannerRunResult,
    ScannerSymbolResult,
)
from app.research.durable_source_replay.bounds import BoundExceeded, BoundLimits, DEFAULT_BOUNDS
from app.research.durable_source_replay.capture import (
    CaptureConfig,
    capture_counters,
    capture_enabled,
    capture_failures,
    invoke_closed_candle_outcomes,
    note_enclosing_transaction_opened,
    note_savepoint_opened,
    note_savepoint_released,
    note_savepoint_rolled_back,
    reset_capture_process_state,
    use_capture,
)
from app.research.durable_source_replay.cli import main
from app.research.durable_source_replay.codec import content_hash, decode_canonical, encode_canonical
from app.research.durable_source_replay.constants import (
    CALLER_LIFECYCLE_SERVICE,
    CALLER_OWNER_MONITORING,
    EVIDENCE_CORRUPT,
    EVIDENCE_INCOMPLETE,
    REPLAY_MATCH,
    TX_COMMIT_UNKNOWN,
    TX_ENCLOSING_COMMITTED,
    TX_ENCLOSING_ROLLED_BACK,
    TX_SAVEPOINT_ROLLED_BACK,
)
from app.research.durable_source_replay.paths import EvidencePathError, footprint_bytes, resolve_evidence_path
from app.research.durable_source_replay.replay import inspect_capture, replay_capture
from app.research.durable_source_replay.store import write_bundle
from app.research.queries import _origin_text
from app.storage.database import DEFAULT_DATABASE_PATH, SCHEMA_VERSION, open_initialized_database
from app.runtime_epoch.origin import register_operational_run
from tests.runtime_epoch_support import (
    SYNTHETIC_EPOCH_ID,
    SYNTHETIC_IDENTITY,
    ensure_synthetic_test_epoch,
    grant_synthetic_origin,
)

pytestmark = pytest.mark.no_auto_epoch

BASE = datetime(2026, 4, 1, tzinfo=UTC)
TIMEFRAME = "5m"
INVALIDATION = "Closed structure beyond the stored stop invalidates the plan."


@pytest.fixture(autouse=True)
def _reset_capture() -> None:
    reset_capture_process_state()
    yield
    reset_capture_process_state()


def _candle(index: int, *, high: str, low: str, open_price: str | None = None) -> dict[str, object]:
    opened = BASE + timedelta(minutes=5 * index)
    low_decimal = Decimal(low)
    return {
        "timestamp": int(opened.timestamp() * 1000),
        "open": Decimal(open_price) if open_price is not None else low_decimal,
        "high": Decimal(high),
        "low": low_decimal,
        "close": Decimal(high),
        "volume": Decimal("10.100"),
    }


def _decision(index: int) -> str:
    return (BASE + timedelta(minutes=5 * (index + 1))).isoformat()


def _record(**updates: object) -> SetupLifecycleRecord:
    values: dict[str, object] = {
        "lifecycle_id": "life-1",
        "symbol": "BTCUSDT",
        "mode": "swing",
        "direction": "long",
        "current_state": SetupLifecycleState.CONFIRMED,
        "first_seen_at": BASE.isoformat(),
        "last_seen_at": BASE.isoformat(),
        "last_transition_at": BASE.isoformat(),
        "confirmed_at": BASE.isoformat(),
        "invalidation_reason": INVALIDATION,
        "invalidation_logic": INVALIDATION,
        "setup_identity": "life-1-setup",
        "structural_anchor": "anchor-life-1",
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "90",
        "tp1": "110",
        "tp2": "120",
        "tp3": "130",
        "rr": "3.5",
    }
    values.update(updates)
    return SetupLifecycleRecord(**values)


def _latched(**updates: object) -> SetupLifecycleRecord:
    return latch_economic_identities(
        _record(**updates),
        instrument_venue="binance",
        plan_locked=True,
    )


def _operational(tmp_path: Path) -> Path:
    path = tmp_path / "operational.sqlite"
    connection = open_initialized_database(path)
    try:
        ensure_synthetic_test_epoch(connection)
        connection.commit()
    finally:
        connection.close()
    return path


def _own(path: Path, record: SetupLifecycleRecord) -> SetupLifecycleRecord:
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        decision = grant_synthetic_origin(
            repository.connection,
            symbol=record.symbol,
            run_id=f"run-{record.lifecycle_id}",
        )
        assert decision.granted and decision.origin_id
        owned = record.model_copy(
            update={
                "runtime_epoch_id": SYNTHETIC_EPOCH_ID,
                "creation_origin_id": decision.origin_id,
            }
        )
        repository.upsert_record(owned)
        repository.connection.commit()
    return owned


def _monitor(path: Path, record: SetupLifecycleRecord, candles: list[dict[str, object]], *, when: str) -> None:
    evidence = {
        evidence_key(record.symbol, TIMEFRAME): SymbolMonitoringEvidence(
            candles=tuple(candles),
            execution_timeframe=TIMEFRAME,
            decision_timestamp=when,
            lineage="supplied_without_delivery_envelope",
        )
    }
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        repository.connection.execute("BEGIN IMMEDIATE")
        note_enclosing_transaction_opened(repository.connection)
        monitor_tracking_obligations(
            repository,
            evidence_by_key=evidence,
            evaluated_at=when,
            scan_run_id=f"run-{record.lifecycle_id}",
            default_timeframe=TIMEFRAME,
        )


def _capture_ids(evidence: Path) -> list[str]:
    connection = sqlite3.connect(evidence)
    try:
        rows = connection.execute(
            "SELECT capture_id FROM captures ORDER BY recorded_at ASC, capture_id ASC"
        ).fetchall()
    finally:
        connection.close()
    return [row[0] for row in rows]


def _replay(evidence: Path, capture_id: str, scratch: Path) -> dict[str, object]:
    scratch.mkdir(parents=True, exist_ok=True)
    return replay_capture(evidence_path=evidence, capture_id=capture_id, scratch_dir=scratch)


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def test_codec_preserves_decimal_absence_and_timestamps() -> None:
    aware = datetime(2026, 4, 1, 0, 0, tzinfo=UTC)
    payload = {
        "present_null": None,
        "empty": "",
        "flag": True,
        "count": 1,
        "price": Decimal("100.100"),
        "other": Decimal("100.10"),
        "aware": aware,
        "naive": datetime(2026, 4, 1, 0, 0),
        "ordered": [Decimal("1.0"), Decimal("2.00")],
    }
    restored = decode_canonical(encode_canonical(payload))
    assert "missing" not in restored
    assert restored["present_null"] is None
    assert restored["empty"] == ""
    assert restored["flag"] is True
    assert restored["count"] == 1 and not isinstance(restored["count"], bool)
    assert restored["price"] == Decimal("100.100")
    assert restored["price"].as_tuple() == Decimal("100.100").as_tuple()
    assert restored["other"].as_tuple() != restored["price"].as_tuple()
    assert restored["aware"] == aware and restored["aware"].tzinfo is not None
    assert restored["naive"].tzinfo is None
    assert restored["ordered"] == [Decimal("1.0"), Decimal("2.00")]
    with pytest.raises(Exception):
        encode_canonical(Decimal("NaN"))


def test_capture_disabled_does_not_touch_evidence_or_change_schema(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    assert capture_enabled() is False
    _monitor(path, record, [_candle(0, high="101", low="99"), _candle(1, high="103", low="100")], when=_decision(1))
    assert not evidence.exists()
    assert capture_counters() == {}
    connection = open_initialized_database(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 26
    finally:
        connection.close()


def test_owner_and_service_calls_replay_after_restart(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    candles = [_candle(0, high="101", low="99"), _candle(1, high="103", low="100")]
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        _monitor(path, record, candles, when=_decision(1))
    ids = _capture_ids(evidence)
    assert len(ids) == 1
    before = _file_hash(evidence)
    report = _replay(evidence, ids[0], tmp_path / "scratch-owner")
    assert report["status"] == REPLAY_MATCH, report
    assert report["caller_path"] == CALLER_OWNER_MONITORING
    assert report["transaction_status"] == TX_ENCLOSING_COMMITTED
    assert report["claims"]["authenticated_venue_evidence"] is False
    assert report["claims"]["operational_persistence"] is True
    assert report["claims"]["local_delivery_provenance"] is False
    assert report["claims"]["possession_before_decision_cutoff"] is False
    assert _file_hash(evidence) == before
    connection = sqlite3.connect(evidence)
    try:
        row = connection.execute("SELECT capture_status, policy_supported FROM captures").fetchone()
        assert row == ("COMPLETE", 1)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        connection.close()
    assert open_initialized_database(path).execute("PRAGMA user_version").fetchone()[0] == 26


def test_same_plan_second_cursor_is_a_distinct_replay(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    first = [_candle(0, high="101", low="99"), _candle(1, high="103", low="100")]
    second = first + [_candle(2, high="112", low="104")]
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        _monitor(path, record, first, when=_decision(1))
        _monitor(path, record, second, when=_decision(2))
    ids = _capture_ids(evidence)
    assert len(ids) == 2 and ids[0] != ids[1]
    first_report = _replay(evidence, ids[0], tmp_path / "scratch-1")
    second_report = _replay(evidence, ids[1], tmp_path / "scratch-2")
    assert first_report["status"] == REPLAY_MATCH, first_report
    assert second_report["status"] == REPLAY_MATCH, second_report
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        progress = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)
    assert progress[0].entry_at is not None
    assert progress[0].tp1_at is not None


def test_outcome_cases_replay(tmp_path: Path) -> None:
    cases = {
        "entry": ([_candle(0, high="101", low="99"), _candle(1, high="103", low="100")], "entry"),
        "tp": (
            [
                _candle(0, high="101", low="99"),
                _candle(1, high="103", low="100"),
                _candle(2, high="112", low="104"),
            ],
            "tp",
        ),
        "sl": (
            [
                _candle(0, high="101", low="99"),
                _candle(1, high="103", low="100"),
                _candle(2, high="104", low="89"),
            ],
            "sl",
        ),
        "same_candle": (
            [
                _candle(0, high="101", low="99"),
                _candle(1, high="103", low="100"),
                _candle(2, high="131", low="89"),
            ],
            "sl",
        ),
        "gap": ([_candle(0, high="103", low="100"), _candle(2, high="112", low="104")], "integrity"),
        "future": ([_candle(5, high="112", low="104")], "future"),
        "empty_progress": ([_candle(0, high="101", low="99")], "noop"),
    }
    for name, (candles, kind) in cases.items():
        evidence = tmp_path / f"{name}.sqlite"
        path = _operational(tmp_path / name)
        (tmp_path / name).mkdir(exist_ok=True)
        record = _own(path, _latched(lifecycle_id=f"life-{name}", symbol="ETHUSDT"))
        when = _decision(0 if kind == "future" else max(0, len(candles) - 1))
        if kind == "future":
            when = BASE.isoformat()
        with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
            _monitor(path, record, candles, when=when)
        report = _replay(evidence, _capture_ids(evidence)[0], tmp_path / f"scratch-{name}")
        assert report["status"] == REPLAY_MATCH, (name, report)


def test_terminal_invalid_and_direct_noop_are_captured(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    terminal = _own(
        path,
        _latched(lifecycle_id="life-terminal", current_state=SetupLifecycleState.TP_HIT),
    )
    invalid = _own(
        path,
        _record(
            lifecycle_id="life-invalid",
            symbol="SOLUSDT",
            entry_low="N/A",
            current_state=SetupLifecycleState.CONFIRMED,
        ),
    )
    candles = [_candle(0, high="103", low="100")]
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            repository.connection.execute("BEGIN IMMEDIATE")
            note_enclosing_transaction_opened(repository.connection)
            for record in (terminal, invalid):
                invoke_closed_candle_outcomes(
                    evaluate_closed_candle_outcomes,
                    caller_path=CALLER_LIFECYCLE_SERVICE,
                    record=record,
                    execution_candles=candles,
                    execution_timeframe=TIMEFRAME,
                    decision_timestamp=_decision(0),
                    evaluated_at=_decision(0),
                    repository=repository,
                    scan_run_id=f"run-{record.lifecycle_id}",
                    evidence_lineage="lifecycle_service_symbol_result",
                )
    ids = _capture_ids(evidence)
    assert len(ids) == 2
    for index, capture_id in enumerate(ids):
        report = _replay(evidence, capture_id, tmp_path / f"scratch-direct-{index}")
        assert report["status"] == REPLAY_MATCH, report


def test_confirmation_event_fallback_and_historical_progress(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched(confirmed_at=None))
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        repository.insert_event(
            SetupLifecycleEvent(
                lifecycle_id=record.lifecycle_id,
                timestamp=BASE.isoformat(),
                symbol=record.symbol,
                from_state=SetupLifecycleState.TRIGGERED,
                to_state=SetupLifecycleState.CONFIRMED,
                reason=SetupTransitionReason.MULTI_SCAN_CONFIRMED,
                scan_run_id=f"run-{record.lifecycle_id}",
            )
        )
        from app.lifecycle.models import SetupLifecycleOutcomeProgress

        repository.upsert_outcome_progress(
            SetupLifecycleOutcomeProgress(
                lifecycle_id=record.lifecycle_id,
                plan_identity="historical-plan-not-compatible",
                symbol=record.symbol,
                mode=record.mode,
                direction=record.direction,
                execution_timeframe=TIMEFRAME,
                first_evaluated_at=BASE.isoformat(),
                last_evaluated_at=BASE.isoformat(),
                plan_version_id=record.plan_version_id,
            )
        )
        repository.connection.commit()
    candles = [_candle(0, high="101", low="99"), _candle(1, high="103", low="100")]
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        _monitor(path, record, candles, when=_decision(1))
    report = _replay(evidence, _capture_ids(evidence)[0], tmp_path / "scratch-history")
    assert report["status"] == REPLAY_MATCH, report


def test_identical_content_has_distinct_occurrences_and_shared_payloads(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        repository.connection.execute(
            "UPDATE setup_lifecycle_records SET runtime_epoch_id = ? WHERE lifecycle_id = ?",
            ("other-epoch", record.lifecycle_id),
        )
        repository.connection.commit()
    candles = [_candle(0, high="101", low="99")]
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        for _ in range(2):
            with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
                repository.connection.execute("BEGIN IMMEDIATE")
                note_enclosing_transaction_opened(repository.connection)
                invoke_closed_candle_outcomes(
                    evaluate_closed_candle_outcomes,
                    caller_path=CALLER_OWNER_MONITORING,
                    record=record,
                    execution_candles=candles,
                    execution_timeframe=TIMEFRAME,
                    decision_timestamp=_decision(0),
                    evaluated_at=_decision(0),
                    repository=repository,
                    scan_run_id=f"run-{record.lifecycle_id}",
                    evidence_lineage="supplied_without_delivery_envelope",
                )
    ids = _capture_ids(evidence)
    assert len(ids) == 2 and ids[0] != ids[1]
    connection = sqlite3.connect(evidence)
    try:
        payload_count = connection.execute("SELECT COUNT(*) FROM payloads").fetchone()[0]
        ref_count = connection.execute("SELECT COUNT(*) FROM capture_payloads").fetchone()[0]
    finally:
        connection.close()
    assert payload_count < ref_count
    for index, capture_id in enumerate(ids):
        report = _replay(evidence, capture_id, tmp_path / f"scratch-dup-{index}")
        assert report["status"] == REPLAY_MATCH, report
        assert report["claims"]["computation_replay"] is True


def test_lineage_attacks_do_not_become_valid_membership(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        repository.connection.execute(
            "UPDATE setup_lifecycle_records SET creation_origin_id = 'N/A' WHERE lifecycle_id = ?",
            (record.lifecycle_id,),
        )
        repository.connection.commit()
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        _monitor(path, record, [_candle(0, high="103", low="100")], when=_decision(0))
    report = _replay(evidence, _capture_ids(evidence)[0], tmp_path / "scratch-na")
    assert report["status"] == REPLAY_MATCH, report
    scratch = sqlite3.connect(tmp_path / "scratch-na" / "replay_operational.sqlite")
    try:
        granted = scratch.execute(
            "SELECT COUNT(*) FROM runtime_operational_origins WHERE status = 'granted'"
        ).fetchone()[0]
        stored_origin = scratch.execute(
            "SELECT creation_origin_id FROM setup_lifecycle_records"
        ).fetchone()[0]
    finally:
        scratch.close()
    assert stored_origin == "N/A"
    assert granted == 0
    assert _origin_text("N/A") == "N/A"
    assert _origin_text("  n/a ") == "n/a"
    assert _origin_text("   ") is None


def test_service_scanner_delivery_and_fetched_owner_evidence(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    register = open_initialized_database(path)
    try:
        ensure_synthetic_test_epoch(register)
        register_operational_run(register, run_id="scan-service", registered_at="2026-03-01T00:00:00+00:00")
        register.commit()
    finally:
        register.close()
    candles = (_candle(0, high="101", low="99"), _candle(1, high="103", low="100"))
    clock = datetime(2026, 4, 1, 0, 20, tzinfo=UTC)
    delivery = observe_adapter_normalized_batch(
        candles,
        requested_symbol="BTCUSDT",
        requested_interval=TIMEFRAME,
        requested_limit=2,
        effective_symbol="BTCUSDT",
        effective_interval=TIMEFRAME,
        effective_limit=2,
        endpoint_path="/fapi/v1/klines",
        adapter_class_label="test-adapter",
        capture_clock=lambda: clock,
        occurrence_factory=lambda: "occ-adapter",
        http_client_injected=False,
    )
    parent = observe_cache_delivery(
        candles,
        delivery_kind=CACHE_KIND_MISS,
        capture_clock=lambda: clock,
        occurrence_factory=lambda: "occ-cache-miss",
        cache_bookkeeping_created_at=None,
        cache_expires_at=None,
        cache_enabled=True,
        upstream=delivery,
        upstream_unavailable_reason=None,
        symbol="BTCUSDT",
        interval=TIMEFRAME,
        limit=2,
    )
    hit = observe_cache_delivery(
        candles,
        delivery_kind=CACHE_KIND_HIT,
        capture_clock=lambda: clock,
        occurrence_factory=lambda: "occ-cache-hit",
        cache_bookkeeping_created_at=1.0,
        cache_expires_at=2.0,
        cache_enabled=True,
        upstream=None,
        upstream_unavailable_reason="acquisition_unavailable_after_restart",
        symbol="BTCUSDT",
        interval=TIMEFRAME,
        limit=2,
    )
    selected = observe_closed_subset(
        hit,
        candles[:1],
        logical_cutoff=clock,
        capture_clock=lambda: clock,
        occurrence_factory=lambda: "occ-subset",
    )
    resampled = observe_synthetic_2d_resample(
        parent,
        candles[:1],
        target_interval="10m",
        logical_cutoff=clock,
        capture_clock=lambda: clock,
        occurrence_factory=lambda: "occ-2d",
    )
    symbol = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejected_strategy_modes=("swing",),
        strategy_diagnostics={
            "swing": {
                "mode": "swing",
                "bias": "long",
                "rr_to_tp2": Decimal("3.5"),
                "gates_passed": (),
                "gates_failed": ("rr_too_low",),
                "first_failed_gate": "rr_too_low",
            }
        },
        rejection_stage="rr_too_low",
        lifecycle_execution_candles=candles,
        lifecycle_execution_timeframe=TIMEFRAME,
        lifecycle_decision_timestamp=datetime(2026, 4, 1, 0, 15, tzinfo=UTC),
        lifecycle_execution_batch_delivery=resampled,
        evaluation_origin_kind="live_scan",
        evaluation_completed_at=clock,
    )
    config = ScannerRunConfig.model_validate(
        {
            "symbols": ["BTCUSDT"],
            "exchange": "binance",
            "account_equity": Decimal("10000"),
            "risk_per_trade_pct": Decimal("1"),
            "execution_timeframe": TIMEFRAME,
        }
    )
    result = ScannerRunResult(
        config=config,
        results=(symbol,),
        scanned_symbols=1,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        SetupLifecycleService(path, expected_identity=SYNTHETIC_IDENTITY).apply_to_run_result(
            result,
            scan_run_id="scan-service",
            now="2026-04-01T00:20:00+00:00",
        )
        owned = _own(path, _latched(lifecycle_id="life-fetch", symbol="ADAUSDT"))

        async def fetch(symbol_name: str, timeframe: str, limit: int) -> tuple[dict[str, object], ...]:
            assert symbol_name == "ADAUSDT"
            assert timeframe == TIMEFRAME
            assert limit == 2
            return tuple(candles)

        asyncio.run(
            monitor_obligations_with_market_data(
                path,
                evidence_by_key={},
                fetch_candles=fetch,
                execution_timeframe=TIMEFRAME,
                candle_limit=2,
                evaluated_at=_decision(1),
                scan_run_id=f"run-{owned.lifecycle_id}",
                expected_identity=SYNTHETIC_IDENTITY,
            )
        )
    connection = sqlite3.connect(evidence)
    try:
        rows = connection.execute(
            "SELECT caller_path, evidence_lineage FROM captures ORDER BY recorded_at ASC"
        ).fetchall()
    finally:
        connection.close()
    assert (CALLER_LIFECYCLE_SERVICE, "lifecycle_service_symbol_result") in rows
    assert (CALLER_OWNER_MONITORING, "fetched_without_delivery_envelope") in rows
    service_id = _capture_ids(evidence)[0]
    blob = _payload(evidence, service_id, "delivery")
    assert "occ-2d" in blob
    assert "occ-cache-miss" in blob
    assert "occ-adapter" in blob
    assert selected.occurrence_token == "occ-subset"
    report = _replay(evidence, service_id, tmp_path / "scratch-service")
    assert report["status"] == REPLAY_MATCH, report


def test_mutated_handoff_stays_a_separate_claim(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    candles = [_candle(0, high="101", low="99"), _candle(1, high="103", low="100")]
    delivery = observe_adapter_normalized_batch(
        candles,
        requested_symbol="BTCUSDT",
        requested_interval=TIMEFRAME,
        requested_limit=2,
        effective_symbol="BTCUSDT",
        effective_interval=TIMEFRAME,
        effective_limit=2,
        endpoint_path="/fapi/v1/klines",
        adapter_class_label="test-adapter",
        capture_clock=lambda: BASE,
        occurrence_factory=lambda: "occ-handoff",
        http_client_injected=False,
    )
    mutated = [_candle(0, high="101", low="99"), _candle(1, high="999", low="100")]
    from app.data.candle_batch_evidence import associate_execution_handoff

    handoff = associate_execution_handoff(
        delivery,
        execution_candles=mutated,
        execution_timeframe=TIMEFRAME,
        logical_cutoff=_decision(1),
    )
    assert handoff.disposition != "matched"
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            repository.connection.execute("BEGIN IMMEDIATE")
            note_enclosing_transaction_opened(repository.connection)
            invoke_closed_candle_outcomes(
                evaluate_closed_candle_outcomes,
                caller_path=CALLER_OWNER_MONITORING,
                record=record,
                execution_candles=mutated,
                execution_timeframe=TIMEFRAME,
                decision_timestamp=_decision(1),
                evaluated_at=_decision(1),
                repository=repository,
                scan_run_id=f"run-{record.lifecycle_id}",
                delivery=delivery,
                handoff=handoff,
                evidence_lineage="supplied_without_delivery_envelope",
            )
    text = _payload(evidence, _capture_ids(evidence)[0], "delivery")
    assert "mismatched" in text
    report = _replay(evidence, _capture_ids(evidence)[0], tmp_path / "scratch-handoff")
    assert report["status"] == REPLAY_MATCH, report


def test_corrupt_incomplete_and_unsupported_evidence_fail_closed(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        _monitor(path, record, [_candle(0, high="103", low="100"), _candle(1, high="104", low="101")], when=_decision(1))
    capture_id = _capture_ids(evidence)[0]
    _flip_payload(evidence)
    corrupt = _replay(evidence, capture_id, tmp_path / "scratch-corrupt")
    assert corrupt["status"] == "EVIDENCE_CORRUPT"
    assert corrupt["exit_code"] != 0
    _restore_from_copy(tmp_path, evidence)
    connection = sqlite3.connect(evidence)
    try:
        connection.execute("DELETE FROM payloads")
        connection.commit()
    finally:
        connection.close()
    missing = _replay(evidence, capture_id, tmp_path / "scratch-missing")
    assert missing["status"] == "EVIDENCE_CORRUPT"
    _restore_from_copy(tmp_path, evidence)
    connection = sqlite3.connect(evidence)
    try:
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
    finally:
        connection.close()
    unsupported = main(["inspect", "--evidence", str(evidence), "--capture-id", capture_id])
    assert unsupported != 0


def test_transaction_qualifications(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    candles = [_candle(0, high="103", low="100")]

    def boom(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise ValueError("injected_outcome_failure")

    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            repository.connection.execute("BEGIN IMMEDIATE")
            note_enclosing_transaction_opened(repository.connection)
            evidence_map = {
                evidence_key(record.symbol, TIMEFRAME): SymbolMonitoringEvidence(
                    candles=tuple(candles),
                    execution_timeframe=TIMEFRAME,
                    decision_timestamp=_decision(0),
                )
            }
            import app.lifecycle.owner_monitoring as owner_monitoring

            original = owner_monitoring.evaluate_closed_candle_outcomes
            owner_monitoring.evaluate_closed_candle_outcomes = boom
            try:
                monitor_tracking_obligations(
                    repository,
                    evidence_by_key=evidence_map,
                    evaluated_at=_decision(0),
                    scan_run_id=f"run-{record.lifecycle_id}",
                    default_timeframe=TIMEFRAME,
                )
            finally:
                owner_monitoring.evaluate_closed_candle_outcomes = original
    rolled = sqlite3.connect(evidence).execute(
        "SELECT capture_id, transaction_status FROM captures"
    ).fetchone()
    assert rolled[1] == TX_SAVEPOINT_ROLLED_BACK
    rolled_replay = _replay(evidence, rolled[0], tmp_path / "scratch-rolled")
    assert rolled_replay["status"] == "REPLAY_MISMATCH"
    assert rolled_replay["transaction_status"] == TX_SAVEPOINT_ROLLED_BACK

    evidence_unknown = tmp_path / "unknown.sqlite"
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence_unknown, operational_paths=(path,))):
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            invoke_closed_candle_outcomes(
                evaluate_closed_candle_outcomes,
                caller_path=CALLER_OWNER_MONITORING,
                record=record,
                execution_candles=candles,
                execution_timeframe=TIMEFRAME,
                decision_timestamp=_decision(0),
                evaluated_at=_decision(0),
                repository=repository,
                scan_run_id=f"run-{record.lifecycle_id}",
            )
    unknown = sqlite3.connect(evidence_unknown).execute(
        "SELECT capture_id, transaction_status FROM captures"
    ).fetchone()
    assert unknown[1] == TX_COMMIT_UNKNOWN
    unknown_replay = _replay(evidence_unknown, unknown[0], tmp_path / "scratch-unknown")
    assert unknown_replay["status"] == REPLAY_MATCH, unknown_replay
    assert unknown_replay["transaction_status"] == TX_COMMIT_UNKNOWN

    evidence_abort = tmp_path / "abort.sqlite"
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence_abort, operational_paths=(path,))):
        with pytest.raises(RuntimeError, match="abort_enclosing"):
            with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
                repository.connection.execute("BEGIN IMMEDIATE")
                note_enclosing_transaction_opened(repository.connection)
                invoke_closed_candle_outcomes(
                    evaluate_closed_candle_outcomes,
                    caller_path=CALLER_LIFECYCLE_SERVICE,
                    record=record,
                    execution_candles=candles,
                    execution_timeframe=TIMEFRAME,
                    decision_timestamp=_decision(0),
                    evaluated_at=_decision(0),
                    repository=repository,
                    scan_run_id=f"run-{record.lifecycle_id}",
                )
                raise RuntimeError("abort_enclosing")
    aborted = sqlite3.connect(evidence_abort).execute(
        "SELECT capture_id, transaction_status FROM captures"
    ).fetchone()
    assert aborted[1] == TX_ENCLOSING_ROLLED_BACK
    aborted_replay = _replay(evidence_abort, aborted[0], tmp_path / "scratch-abort")
    assert aborted_replay["status"] == REPLAY_MATCH, aborted_replay
    assert aborted_replay["transaction_status"] == TX_ENCLOSING_ROLLED_BACK
    assert aborted_replay["claims"]["computation_replay"] is True
    assert aborted_replay["claims"]["operational_persistence"] is False


def test_capture_failure_preserves_operational_result(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    candles = [_candle(0, high="101", low="99"), _candle(1, high="103", low="100")]

    def fail_encode(value: object) -> bytes:
        del value
        raise RuntimeError("sidecar_unavailable")

    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        import app.research.durable_source_replay.capture as capture_module

        original = capture_module.encode_canonical
        capture_module.encode_canonical = fail_encode
        try:
            _monitor(path, record, candles, when=_decision(1))
        finally:
            capture_module.encode_canonical = original
    assert not evidence.exists() or _capture_ids(evidence) == []
    assert capture_failures()
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        progress = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)
    assert progress and progress[0].entry_at is not None


def test_path_aliases_and_unrelated_databases_are_rejected(tmp_path: Path) -> None:
    operational = tmp_path / "operational.sqlite"
    operational.write_bytes(b"")
    with pytest.raises(EvidencePathError):
        resolve_evidence_path(operational, operational_paths=(operational,))
    with pytest.raises(EvidencePathError):
        resolve_evidence_path(tmp_path / "sub" / ".." / "operational.sqlite", operational_paths=(operational,))
    with pytest.raises(EvidencePathError):
        resolve_evidence_path(Path("scan_runs") / "main_live_runtime.sqlite")
    with pytest.raises(EvidencePathError):
        resolve_evidence_path(DEFAULT_DATABASE_PATH)
    wal = tmp_path / "operational.sqlite-wal"
    wal.write_bytes(b"x")
    with pytest.raises(EvidencePathError):
        resolve_evidence_path(wal)
    unrelated = tmp_path / "other.sqlite"
    connection = sqlite3.connect(unrelated)
    connection.execute("CREATE TABLE notes (id INTEGER)")
    connection.commit()
    connection.close()
    with pytest.raises(EvidencePathError, match="unrelated"):
        from app.research.durable_source_replay.store import write_bundle

        write_bundle(unrelated, captures=[])


def test_replay_does_not_use_network_or_strategy_replay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        _monitor(path, record, [_candle(0, high="103", low="100"), _candle(1, high="104", low="101")], when=_decision(1))
    capture_id = _capture_ids(evidence)[0]

    def blocked(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("network_used")

    monkeypatch.setattr(socket, "create_connection", blocked)
    source = Path("app/research/durable_source_replay/replay.py").read_text(encoding="utf-8")
    assert "strategy_replay" not in source
    assert "telegram" not in source.lower()
    report = _replay(evidence, capture_id, tmp_path / "scratch-offline")
    assert report["status"] == REPLAY_MATCH, report
    (tmp_path / "cli-scratch").mkdir()
    code = main(
        ["replay", "--evidence", str(evidence), "--capture-id", capture_id, "--scratch", str(tmp_path / "cli-scratch")]
    )
    assert code == 0


def test_bounds_and_parent_depth(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    candles = [_candle(index, high="103", low="100") for index in range(3)]
    limits = BoundLimits(max_candles=1, max_pending_captures=1, max_payload_bytes=1024, max_parent_depth=1)
    with use_capture(
        CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,)),
        bounds=limits,
    ):
        _monitor(path, record, candles, when=_decision(2))
    assert any(item["kind"] == "pre_capture_failed" for item in capture_failures())
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        stored = repository.get_record_by_lifecycle_id(record.lifecycle_id)
    assert stored is not None


def test_lock_wait_and_unsupported_policy_fail_visibly(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    candles = [_candle(0, high="103", low="100"), _candle(1, high="104", low="101")]
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        _monitor(path, record, candles, when=_decision(1))
    holder = sqlite3.connect(evidence)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with use_capture(
            CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,)),
            bounds=BoundLimits(max_lock_wait_ms=200),
        ):
            _monitor(path, record, candles + [_candle(2, high="112", low="104")], when=_decision(2))
    finally:
        holder.rollback()
        holder.close()
    assert any(item["kind"] == "flush_failed" for item in capture_failures())
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        progress = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)
    assert progress[0].tp1_at is not None

    policy_evidence = tmp_path / "policy.sqlite"
    with use_capture(CaptureConfig(enabled=True, evidence_path=policy_evidence, operational_paths=(path,))):
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            repository.connection.execute("BEGIN IMMEDIATE")
            note_enclosing_transaction_opened(repository.connection)
            invoke_closed_candle_outcomes(
                evaluate_closed_candle_outcomes,
                caller_path=CALLER_OWNER_MONITORING,
                record=record,
                execution_candles=candles,
                execution_timeframe="not-a-timeframe",
                decision_timestamp=_decision(1),
                evaluated_at=_decision(1),
                repository=repository,
                scan_run_id=f"run-{record.lifecycle_id}",
            )
    unsupported = _replay(policy_evidence, _capture_ids(policy_evidence)[0], tmp_path / "scratch-policy")
    assert unsupported["status"] == "UNSUPPORTED_IMPLEMENTATION_OR_POLICY"
    assert unsupported["exit_code"] != 0


def test_missing_evidence_is_not_an_invocation(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            repository.connection.execute("BEGIN IMMEDIATE")
            note_enclosing_transaction_opened(repository.connection)
            monitor_tracking_obligations(
                repository,
                evidence_by_key={},
                evaluated_at=_decision(0),
                scan_run_id=f"run-{record.lifecycle_id}",
                default_timeframe=TIMEFRAME,
                record_missing_evidence=True,
            )
    connection = sqlite3.connect(evidence)
    try:
        captures = connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
        kinds = {
            row[0]
            for row in connection.execute("SELECT kind FROM capture_diagnostics").fetchall()
        }
    finally:
        connection.close()
    assert captures == 0
    assert "missing_evidence" in kinds

    assert SCHEMA_VERSION == 26
    assert MIN_PUBLIC_SETUP_QUALITY_SCORE == Decimal("88")
    assert MIN_PUBLIC_SIGNAL_GRADE == "A"
    assert DEFAULT_PUBLIC_RR_MIN == Decimal("3")
    assert CONFIRMED_MIN_RR == Decimal("3")
    assert Settings.model_fields["order_execution_enabled"].default is False
    assert Settings.model_fields["local_manual_mode"].default is True
    assert Settings.model_fields["telegram_dry_run"].default is True
    assert Settings.model_fields["telegram_signals_enabled"].default is False


def test_synthetic_storage_measurements(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    batch = [_candle(index, high="103", low="100") for index in range(20)]
    tracemalloc.start()
    started = time.perf_counter()
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        for offset in range(4):
            _monitor(path, record, batch, when=_decision(19 + offset))
    elapsed = time.perf_counter() - started
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    connection = sqlite3.connect(evidence)
    try:
        captures = connection.execute("SELECT COUNT(*) FROM captures").fetchone()[0]
        payloads = connection.execute("SELECT COUNT(*) FROM payloads").fetchone()[0]
        payload_bytes = connection.execute("SELECT COALESCE(SUM(byte_length), 0) FROM payloads").fetchone()[0]
    finally:
        connection.close()
    wal = Path(str(evidence) + "-wal")
    measurement = {
        "captures": captures,
        "payload_rows": payloads,
        "payload_bytes": payload_bytes,
        "store_bytes": evidence.stat().st_size,
        "wal_bytes": wal.stat().st_size if wal.exists() else 0,
        "elapsed_seconds": round(elapsed, 4),
        "peak_traced_bytes": peak,
    }
    print("F04_MEASURE " + json.dumps(measurement, sort_keys=True))
    assert captures == 4
    assert payloads < captures * 6
    assert payload_bytes < 8 * 1024 * 1024
    assert measurement["wal_bytes"] < 512 * 1024 * 1024
    assert peak < 64 * 1024 * 1024


def test_cache_selection_and_both_2d_routes_are_stored(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    candles = (_candle(0, high="101", low="99"), _candle(1, high="103", low="100"))
    clock = datetime(2026, 4, 1, 0, 20, tzinfo=UTC)

    def adapter(token: str) -> object:
        return observe_adapter_normalized_batch(
            candles,
            requested_symbol="BTCUSDT",
            requested_interval="1d",
            requested_limit=2,
            effective_symbol="BTCUSDT",
            effective_interval="1d",
            effective_limit=2,
            endpoint_path="/fapi/v1/klines",
            adapter_class_label="test-adapter",
            capture_clock=lambda: clock,
            occurrence_factory=lambda: token,
            http_client_injected=False,
        )

    primary_source = observe_closed_subset(
        adapter("occ-primary-adapter"),
        candles,
        logical_cutoff=clock,
        capture_clock=lambda: clock,
        occurrence_factory=lambda: "occ-primary-closed-source",
    )
    primary_transform = observe_synthetic_2d_resample(
        primary_source,
        candles[:1],
        target_interval="2d",
        logical_cutoff=clock,
        capture_clock=lambda: clock,
        occurrence_factory=lambda: "occ-primary-2d",
    )
    primary = observe_closed_subset(
        primary_transform,
        candles[:1],
        logical_cutoff=clock,
        capture_clock=lambda: clock,
        occurrence_factory=lambda: "occ-primary-closed-2d",
    )
    reused = observe_cache_delivery(
        candles,
        delivery_kind=CACHE_KIND_HIT,
        capture_clock=lambda: clock,
        occurrence_factory=lambda: "occ-htf-hit",
        cache_bookkeeping_created_at=1.0,
        cache_expires_at=2.0,
        cache_enabled=True,
        upstream=None,
        upstream_unavailable_reason="acquisition_unavailable_after_restart",
        symbol="BTCUSDT",
        interval="1d",
        limit=2,
    )
    htf_parent = observe_closed_subset(
        reused,
        candles,
        logical_cutoff=clock,
        capture_clock=lambda: clock,
        occurrence_factory=lambda: "occ-htf-closed-1d",
    )
    htf_transform = observe_synthetic_2d_resample(
        htf_parent,
        candles[:1],
        target_interval="2d",
        logical_cutoff=clock,
        capture_clock=lambda: clock,
        occurrence_factory=lambda: "occ-htf-2d",
    )
    htf = observe_closed_subset(
        htf_transform,
        candles[:1],
        logical_cutoff=clock,
        capture_clock=lambda: clock,
        occurrence_factory=lambda: "occ-htf-closed-2d",
    )
    expiry = observe_cache_delivery(
        candles,
        delivery_kind=CACHE_KIND_EXPIRY_REFETCH,
        capture_clock=lambda: clock,
        occurrence_factory=lambda: "occ-expiry",
        cache_bookkeeping_created_at=1.0,
        cache_expires_at=2.0,
        cache_enabled=True,
        upstream=adapter("occ-expiry-adapter"),
        upstream_unavailable_reason=None,
        symbol="BTCUSDT",
        interval=TIMEFRAME,
        limit=2,
    )
    reversed_handoff = associate_execution_handoff(
        primary,
        execution_candles=tuple(reversed(candles)),
        execution_timeframe=TIMEFRAME,
        logical_cutoff=clock,
    )
    assert reversed_handoff.disposition != "matched"
    record = _own(path, _latched())
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            repository.connection.execute("BEGIN IMMEDIATE")
            note_enclosing_transaction_opened(repository.connection)
            for delivery, handoff in (
                (primary, None),
                (htf, None),
                (expiry, None),
                (primary, reversed_handoff),
            ):
                invoke_closed_candle_outcomes(
                    evaluate_closed_candle_outcomes,
                    caller_path=CALLER_LIFECYCLE_SERVICE,
                    record=record,
                    execution_candles=candles,
                    execution_timeframe=TIMEFRAME,
                    decision_timestamp=_decision(1),
                    evaluated_at=_decision(1),
                    repository=repository,
                    scan_run_id=f"run-{record.lifecycle_id}",
                    delivery=delivery,
                    handoff=handoff,
                    evidence_lineage="lifecycle_service_symbol_result",
                )
    blobs = [_payload(evidence, capture_id, "delivery") for capture_id in _capture_ids(evidence)]
    joined = "\n".join(blobs)
    for token in (
        "occ-primary-closed-2d",
        "occ-primary-2d",
        "occ-primary-adapter",
        "occ-htf-closed-2d",
        "occ-htf-2d",
        "occ-htf-hit",
        "acquisition_unavailable_after_restart",
        "occ-expiry",
        "expiry_refetch",
    ):
        assert token in joined
    assert any("mismatched" in blob for blob in blobs)
    shallow = tmp_path / "shallow.sqlite"
    with use_capture(
        CaptureConfig(enabled=True, evidence_path=shallow, operational_paths=(path,)),
        bounds=BoundLimits(max_parent_depth=0),
    ):
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            repository.connection.execute("BEGIN IMMEDIATE")
            note_enclosing_transaction_opened(repository.connection)
            invoke_closed_candle_outcomes(
                evaluate_closed_candle_outcomes,
                caller_path=CALLER_OWNER_MONITORING,
                record=record,
                execution_candles=candles,
                execution_timeframe=TIMEFRAME,
                decision_timestamp=_decision(1),
                evaluated_at=_decision(1),
                repository=repository,
                scan_run_id=f"run-{record.lifecycle_id}",
                delivery=primary,
                evidence_lineage="supplied_without_delivery_envelope",
            )
    incomplete = _replay(shallow, _capture_ids(shallow)[0], tmp_path / "scratch-shallow")
    assert incomplete["status"] == EVIDENCE_INCOMPLETE
    assert incomplete["claims"]["computation_replay"] is False
    assert incomplete["claims"]["local_delivery_provenance"] is False


def test_conflict_lineage_and_integrity_fail_closed(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    candles = [_candle(0, high="103", low="100")]
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        repository.connection.execute(
            """
            INSERT INTO runtime_operational_origins (
                origin_id, runtime_epoch_id, run_id, symbol, origin_kind, status, block_reason
            ) VALUES ('origin-denied', ?, 'conflict-run', ?, 'live_scan', 'denied', 'conflicting')
            """,
            (SYNTHETIC_EPOCH_ID, record.symbol),
        )
        repository.connection.execute(
            """
            UPDATE setup_lifecycle_records
            SET creation_origin_id = 'origin-denied', runtime_epoch_id = 'epoch-b'
            WHERE lifecycle_id = ?
            """,
            (record.lifecycle_id,),
        )
        repository.connection.commit()
    bare = _own(
        path,
        _record(
            lifecycle_id="life-bare",
            symbol="ETHUSDT",
            setup_identity="bare-setup",
            structural_anchor="anchor-life-bare",
        ),
    )
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        repository.connection.execute(
            "UPDATE setup_lifecycle_records SET plan_version_id = NULL WHERE lifecycle_id = ?",
            (bare.lifecycle_id,),
        )
        repository.connection.commit()
        conflict_record = repository.get_record_by_lifecycle_id(record.lifecycle_id)
        bare_record = repository.get_record_by_lifecycle_id(bare.lifecycle_id)
    assert conflict_record is not None and bare_record is not None
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            repository.connection.execute("BEGIN IMMEDIATE")
            note_enclosing_transaction_opened(repository.connection)
            for item in (conflict_record, bare_record):
                invoke_closed_candle_outcomes(
                    evaluate_closed_candle_outcomes,
                    caller_path=CALLER_OWNER_MONITORING,
                    record=item,
                    execution_candles=candles,
                    execution_timeframe=TIMEFRAME,
                    decision_timestamp=_decision(0),
                    evaluated_at=_decision(0),
                    repository=repository,
                    scan_run_id=f"run-{item.lifecycle_id}",
                )
    conflict_id = _capture_for_lifecycle(evidence, record.lifecycle_id)
    bare_id = _capture_for_lifecycle(evidence, bare.lifecycle_id)
    conflict = _replay(evidence, conflict_id, tmp_path / "scratch-conflict")
    assert conflict["status"] == REPLAY_MATCH, conflict
    assert conflict["claims"]["operational_persistence"] is True
    scratch = sqlite3.connect(tmp_path / "scratch-conflict" / "replay_operational.sqlite")
    try:
        stored = scratch.execute(
            """
            SELECT runtime_epoch_id, creation_origin_id
            FROM setup_lifecycle_records
            WHERE lifecycle_id = ?
            """,
            (record.lifecycle_id,),
        ).fetchone()
        denied = scratch.execute(
            "SELECT status FROM runtime_operational_origins WHERE origin_id = 'origin-denied'"
        ).fetchone()
        active = scratch.execute(
            "SELECT epoch_id FROM runtime_epoch_control WHERE control_key = 'active'"
        ).fetchone()
    finally:
        scratch.close()
    assert stored == ("epoch-b", "origin-denied")
    assert denied[0] == "denied"
    assert active[0] == SYNTHETIC_EPOCH_ID
    bare_report = _replay(evidence, bare_id, tmp_path / "scratch-bare")
    assert bare_report["status"] == REPLAY_MATCH, bare_report

    forged = tmp_path / "forged.sqlite"
    forged.write_bytes(evidence.read_bytes())
    connection = sqlite3.connect(forged)
    try:
        connection.execute("UPDATE captures SET implementation_fingerprint = 'sha256:forged'")
        connection.commit()
    finally:
        connection.close()
    fingerprint = _replay(forged, conflict_id, tmp_path / "scratch-forged")
    assert fingerprint["status"] == "UNSUPPORTED_IMPLEMENTATION_OR_POLICY"
    truncated = tmp_path / "truncated.sqlite"
    truncated.write_bytes(evidence.read_bytes()[:48])
    truncated_report = _replay(truncated, conflict_id, tmp_path / "scratch-truncated")
    assert truncated_report["status"] == EVIDENCE_CORRUPT
    assert truncated_report["exit_code"] != 0

    partial = tmp_path / "partial.sqlite"
    import app.research.durable_source_replay.store as store_module

    original = store_module._insert_capture

    def interrupted(connection: sqlite3.Connection, capture: object) -> None:
        del connection, capture
        raise RuntimeError("partial_sidecar")

    store_module._insert_capture = interrupted
    try:
        with use_capture(CaptureConfig(enabled=True, evidence_path=partial, operational_paths=(path,))):
            selected = _own(path, _latched(lifecycle_id="life-partial", symbol="SOLUSDT"))
            _monitor(path, selected, candles, when=_decision(0))
    finally:
        store_module._insert_capture = original
    assert any(item["kind"] == "flush_failed" for item in capture_failures())
    if partial.exists():
        probe = sqlite3.connect(partial)
        try:
            tables = {
                row[0]
                for row in probe.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'captures'"
                ).fetchall()
            }
            count = probe.execute("SELECT COUNT(*) FROM captures").fetchone()[0] if "captures" in tables else 0
        finally:
            probe.close()
        assert count == 0


def test_successful_evaluator_savepoint_rollback_is_not_persisted(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    candles = [_candle(0, high="101", low="99"), _candle(1, high="103", low="100")]
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            connection = repository.connection
            connection.execute("BEGIN IMMEDIATE")
            note_enclosing_transaction_opened(connection)
            connection.execute("SAVEPOINT lifecycle_symbol")
            note_savepoint_opened(connection, "lifecycle_symbol")
            invoke_closed_candle_outcomes(
                evaluate_closed_candle_outcomes,
                caller_path=CALLER_LIFECYCLE_SERVICE,
                record=record,
                execution_candles=candles,
                execution_timeframe=TIMEFRAME,
                decision_timestamp=_decision(1),
                evaluated_at=_decision(1),
                repository=repository,
                scan_run_id=f"run-{record.lifecycle_id}",
            )
            connection.execute("ROLLBACK TO lifecycle_symbol")
            note_savepoint_rolled_back(connection, "lifecycle_symbol")
            connection.execute("RELEASE lifecycle_symbol")
    report = _replay(evidence, _capture_ids(evidence)[0], tmp_path / "scratch-savepoint")
    assert report["status"] == REPLAY_MATCH, report
    assert report["transaction_status"] == TX_SAVEPOINT_ROLLED_BACK
    assert report["claims"]["computation_replay"] is True
    assert report["claims"]["operational_persistence"] is False
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        progress = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)
    assert not progress


def test_f04_r1_explicit_rollback_then_empty_commit_is_not_persistence(tmp_path: Path) -> None:
    import app.research.durable_source_replay.capture as capture_module

    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    flush_in_transaction: list[bool] = []
    original_writer = capture_module.write_bundle
    live_connection = {"value": None}

    def spy_writer(*args: object, **kwargs: object) -> None:
        connection = live_connection["value"]
        try:
            in_tx = bool(connection is not None and connection.in_transaction)
        except Exception:
            # Closed connections must not count as an open operational transaction.
            in_tx = False
        flush_in_transaction.append(in_tx)
        return original_writer(*args, **kwargs)

    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            connection = repository.connection
            live_connection["value"] = connection
            connection.execute("BEGIN IMMEDIATE")
            note_enclosing_transaction_opened(connection)
            invoke_closed_candle_outcomes(
                evaluate_closed_candle_outcomes,
                caller_path=CALLER_OWNER_MONITORING,
                record=record,
                execution_candles=[_candle(0, high="101", low="99"), _candle(1, high="103", low="100")],
                execution_timeframe=TIMEFRAME,
                decision_timestamp=_decision(1),
                evaluated_at=_decision(1),
                repository=repository,
                scan_run_id=f"run-{record.lifecycle_id}",
            )
            inside = connection.execute(
                "SELECT count(*) FROM setup_lifecycle_outcome_progress"
            ).fetchone()[0]
            assert inside == 1
            connection.rollback()
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        assert not repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)
    report = _replay(evidence, _capture_ids(evidence)[0], tmp_path / "scratch-r1")
    assert report["status"] == REPLAY_MATCH, report
    assert report["transaction_status"] == TX_ENCLOSING_ROLLED_BACK
    assert report["claims"]["operational_persistence"] is False

    evidence_exc = tmp_path / "exc.sqlite"
    path_exc = _operational(tmp_path / "exc-op")
    record_exc = _own(path_exc, _latched(lifecycle_id="life-exc", symbol="ETHUSDT"))
    capture_module.write_bundle = spy_writer
    try:
        with use_capture(CaptureConfig(enabled=True, evidence_path=evidence_exc, operational_paths=(path_exc,))):
            with pytest.raises(RuntimeError, match="abort"):
                with SQLiteSetupLifecycleRepository(path_exc, expected_identity=SYNTHETIC_IDENTITY) as repository:
                    live_connection["value"] = repository.connection
                    repository.connection.execute("BEGIN IMMEDIATE")
                    note_enclosing_transaction_opened(repository.connection)
                    invoke_closed_candle_outcomes(
                        evaluate_closed_candle_outcomes,
                        caller_path=CALLER_OWNER_MONITORING,
                        record=record_exc,
                        execution_candles=[_candle(0, high="101", low="99")],
                        execution_timeframe=TIMEFRAME,
                        decision_timestamp=_decision(0),
                        evaluated_at=_decision(0),
                        repository=repository,
                        scan_run_id=f"run-{record_exc.lifecycle_id}",
                    )
                    raise RuntimeError("abort")
    finally:
        capture_module.write_bundle = original_writer
    assert flush_in_transaction == [False]


def test_f04_r2_identity_init_failure_preserves_operational_evaluation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.research.durable_source_replay.capture as capture_module

    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    candles = [_candle(0, high="101", low="99"), _candle(1, high="103", low="100")]
    monkeypatch.setenv("SOURCE_REPLAY_CAPTURE_ENABLED", "true")
    monkeypatch.setenv("SOURCE_REPLAY_EVIDENCE_PATH", str(evidence))
    monkeypatch.setattr(
        capture_module,
        "implementation_fingerprint",
        lambda: (_ for _ in ()).throw(OSError("synthetic source identity read failure")),
    )
    reset_capture_process_state()
    _monitor(path, record, candles, when=_decision(1))
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        progress = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)
    assert progress and progress[0].entry_at is not None
    counters = capture_counters()
    assert counters.get("identity_init_failed") or counters.get("capture_failures_total")
    assert not evidence.exists() or _capture_ids(evidence) == []


def test_f04_r3_store_footprint_budget_rejects_first_write_and_diagnostics(tmp_path: Path) -> None:
    from app.research.durable_source_replay.paths import footprint_bytes
    from app.research.durable_source_replay.store import write_bundle

    evidence = tmp_path / "tiny.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    limits = BoundLimits(max_store_bytes=64 * 1024)
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,)), bounds=limits):
        _monitor(
            path,
            record,
            [_candle(i, high="103", low="100") for i in range(20)],
            when=_decision(19),
        )
    assert capture_failures()
    assert any("store" in item["reason"] or "BoundExceeded" in item["reason"] for item in capture_failures())
    assert not evidence.exists() or footprint_bytes(evidence) <= limits.max_store_bytes
    assert _capture_ids(evidence) == [] if evidence.exists() else True

    with pytest.raises(Exception) as excinfo:
        write_bundle(tmp_path / "too-small.sqlite", captures=[], limits=BoundLimits(max_store_bytes=1024))
    assert "store_budget_too_small" in str(excinfo.value)

    diag_evidence = tmp_path / "diag.sqlite"
    write_bundle(diag_evidence, captures=[], limits=BoundLimits(max_store_bytes=128 * 1024))
    path2 = _operational(tmp_path / "diag-op")
    record2 = _own(path2, _latched(lifecycle_id="life-diag", symbol="ETHUSDT"))
    reset_capture_process_state()
    with use_capture(
        CaptureConfig(enabled=True, evidence_path=diag_evidence, operational_paths=(path2,)),
        bounds=BoundLimits(max_store_bytes=128 * 1024),
    ):
        with SQLiteSetupLifecycleRepository(path2, expected_identity=SYNTHETIC_IDENTITY) as repository:
            for _ in range(300):
                from app.research.durable_source_replay.capture import note_non_invocation

                note_non_invocation(
                    kind="synthetic_gap",
                    detail="x" * 500,
                    lifecycle_id=record2.lifecycle_id,
                    connection=repository.connection,
                )
    assert capture_failures()
    assert footprint_bytes(diag_evidence) <= 128 * 1024


def test_f04_r4_conflicting_occurrence_metadata_fails_closed(tmp_path: Path) -> None:
    from app.research.durable_source_replay.replay import inspect_capture

    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        with pytest.raises(RuntimeError, match="rollback_after_evaluation"):
            with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
                repository.connection.execute("BEGIN IMMEDIATE")
                note_enclosing_transaction_opened(repository.connection)
                invoke_closed_candle_outcomes(
                    evaluate_closed_candle_outcomes,
                    caller_path=CALLER_OWNER_MONITORING,
                    record=record,
                    execution_candles=[_candle(0, high="101", low="99"), _candle(1, high="103", low="100")],
                    execution_timeframe=TIMEFRAME,
                    decision_timestamp=_decision(1),
                    evaluated_at=_decision(1),
                    repository=repository,
                    scan_run_id=f"run-{record.lifecycle_id}",
                )
                raise RuntimeError("rollback_after_evaluation")
    capture_id = _capture_ids(evidence)[0]
    before = _replay(evidence, capture_id, tmp_path / "scratch-before")
    assert before["status"] == REPLAY_MATCH
    assert before["transaction_status"] == TX_ENCLOSING_ROLLED_BACK
    assert before["claims"]["operational_persistence"] is False
    connection = sqlite3.connect(evidence)
    try:
        connection.execute(
            """
            UPDATE captures
            SET transaction_status = 'enclosing_committed',
                policy_id = 'conflicting-policy',
                reference_count = 99,
                store_schema_version = 99
            """
        )
        connection.commit()
    finally:
        connection.close()
    after = _replay(evidence, capture_id, tmp_path / "scratch-after")
    assert after["status"] in {"EVIDENCE_CORRUPT", "UNSUPPORTED_FORMAT"}
    assert after["claims"]["operational_persistence"] is False
    assert after["claims"]["computation_replay"] is False
    inspection = inspect_capture(evidence_path=evidence, capture_id=capture_id)
    assert inspection["status"] != "INSPECTION_OK"
    assert inspection["exit_code"] != 0


def test_f04_r5_padded_authority_lookups_replay(tmp_path: Path) -> None:
    for column in ("creation_origin_id", "origin_run_id"):
        root = tmp_path / column
        root.mkdir()
        evidence = root / "evidence.sqlite"
        path = _operational(root)
        record = _own(path, _latched(lifecycle_id=f"life-{column}", symbol="BNBUSDT" if column == "creation_origin_id" else "XRPUSDT"))
        with sqlite3.connect(path) as connection:
            if column == "creation_origin_id":
                padded = f"  {record.creation_origin_id}  "
                connection.execute(
                    "UPDATE setup_lifecycle_records SET creation_origin_id = ?",
                    (padded,),
                )
                connection.commit()
                record = record.model_copy(update={"creation_origin_id": padded})
            else:
                connection.execute(
                    "UPDATE runtime_operational_origins SET run_id = ?",
                    ("  run-" + record.lifecycle_id + "  ",),
                )
                connection.commit()
        with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
            with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
                repository.connection.execute("BEGIN IMMEDIATE")
                note_enclosing_transaction_opened(repository.connection)
                invoke_closed_candle_outcomes(
                    evaluate_closed_candle_outcomes,
                    caller_path=CALLER_OWNER_MONITORING,
                    record=record,
                    execution_candles=[_candle(0, high="101", low="99"), _candle(1, high="103", low="100")],
                    execution_timeframe=TIMEFRAME,
                    decision_timestamp=_decision(1),
                    evaluated_at=_decision(1),
                    repository=repository,
                    scan_run_id=None,
                )
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            progress = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)
        assert len(progress) == 1
        report = _replay(evidence, _capture_ids(evidence)[0], root / "scratch")
        assert report["status"] == REPLAY_MATCH, (column, report)


def test_f04_r6_implementation_fingerprint_covers_state_machine(tmp_path: Path) -> None:
    import shutil

    import app.research.durable_source_replay.identity as identity

    clone = tmp_path / "fingerprint_clone"
    for relative in identity.IMPLEMENTATION_FILES:
        target = clone / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(relative), target)
    assert "app/lifecycle/state_machine.py" in identity.IMPLEMENTATION_FILES
    assert "app/lifecycle/models.py" in identity.IMPLEMENTATION_FILES
    assert "app/core/trade_plan_integrity.py" in identity.IMPLEMENTATION_FILES
    assert "app/runtime_epoch/time_contract.py" in identity.IMPLEMENTATION_FILES
    original_root = identity.REPO_ROOT
    try:
        identity.REPO_ROOT = clone
        before = identity.implementation_fingerprint()
        target = clone / "app/lifecycle/state_machine.py"
        old = target.read_text(encoding="utf-8")
        new = old.replace(
            "return to_state in ALLOWED_TRANSITIONS.get(from_state, set())",
            "return False",
        )
        assert old != new
        target.write_text(new, encoding="utf-8")
        after = identity.implementation_fingerprint()
        assert before != after
    finally:
        identity.REPO_ROOT = original_root


def test_f04_r7_bounded_header_reads_and_failure_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch

    import app.research.durable_source_replay.capture as capture_module
    from app.research.durable_source_replay.paths import inspect_existing_store_file, read_sqlite_header

    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        _monitor(path, record, [_candle(0, high="103", low="100")], when=_decision(0))
    assert evidence.exists()
    header = read_sqlite_header(evidence)
    assert len(header) == 16
    reads: list[int] = []
    original_open = Path.open

    def tracking_open(self: Path, *args: object, **kwargs: object):
        handle = original_open(self, *args, **kwargs)
        if self.resolve() == evidence.resolve() and "b" in str(args[0] if args else kwargs.get("mode", "r")):
            original_read = handle.read

            def tracked_read(n: int = -1) -> bytes:
                data = original_read(n)
                reads.append(len(data))
                return data

            handle.read = tracked_read  # type: ignore[method-assign]
        return handle

    with patch.object(Path, "open", tracking_open):
        inspect_existing_store_file(evidence)
    assert reads
    assert max(reads) <= 16

    reset_capture_process_state()
    for _ in range(10_001):
        capture_module._remember_failure("synthetic_recurring_failure", ValueError("synthetic"))
    assert len(capture_failures()) <= BoundLimits().max_failure_details
    assert capture_counters().get("failure_details_truncated") == 1
    assert capture_counters().get("synthetic_recurring_failure") == 10_001


def _capture_for_lifecycle(evidence: Path, lifecycle_id: str) -> str:
    for capture_id in _capture_ids(evidence):
        decoded = decode_canonical(_payload(evidence, capture_id, "prestate").encode("utf-8"))
        if isinstance(decoded, dict) and decoded.get("lifecycle_id") == lifecycle_id:
            return capture_id
    raise AssertionError(f"missing capture for {lifecycle_id}")


def _payload(evidence: Path, capture_id: str, role: str) -> str:
    connection = sqlite3.connect(evidence)
    try:
        row = connection.execute(
            """
            SELECT payloads.canonical_bytes
            FROM capture_payloads
            JOIN payloads ON payloads.content_hash = capture_payloads.content_hash
            WHERE capture_payloads.capture_id = ? AND capture_payloads.role = ?
            """,
            (capture_id, role),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    return bytes(row[0]).decode("utf-8")


def _flip_payload(evidence: Path) -> None:
    snapshot = evidence.read_bytes()
    (evidence.parent / "evidence-backup.sqlite").write_bytes(snapshot)
    connection = sqlite3.connect(evidence)
    try:
        row = connection.execute("SELECT content_hash, canonical_bytes FROM payloads LIMIT 1").fetchone()
        blob = bytearray(row[1])
        blob[0] ^= 0xFF
        connection.execute(
            "UPDATE payloads SET canonical_bytes = ? WHERE content_hash = ?",
            (bytes(blob), row[0]),
        )
        connection.commit()
    finally:
        connection.close()


def _restore_from_copy(tmp_path: Path, evidence: Path) -> None:
    backup = evidence.parent / "evidence-backup.sqlite"
    evidence.write_bytes(backup.read_bytes())
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(evidence) + suffix)
        if sidecar.exists():
            sidecar.unlink()


def _progress_count(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        return int(
            connection.execute("SELECT count(*) FROM setup_lifecycle_outcome_progress").fetchone()[0]
        )


def test_f04_r1_released_descendant_outer_rollback_is_not_persistence(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            connection = repository.connection
            connection.execute("BEGIN IMMEDIATE")
            note_enclosing_transaction_opened(connection)
            for name in ("outer", "inner"):
                connection.execute(f"SAVEPOINT {name}")
                note_savepoint_opened(connection, name)
            invoke_closed_candle_outcomes(
                evaluate_closed_candle_outcomes,
                caller_path=CALLER_OWNER_MONITORING,
                record=record,
                execution_candles=[_candle(0, high="101", low="99"), _candle(1, high="103", low="100")],
                execution_timeframe=TIMEFRAME,
                decision_timestamp=_decision(1),
                evaluated_at=_decision(1),
                repository=repository,
                scan_run_id=f"run-{record.lifecycle_id}",
            )
            connection.execute("RELEASE inner")
            note_savepoint_released(connection, "inner")
            connection.execute("ROLLBACK TO outer")
            note_savepoint_rolled_back(connection, "outer")
            connection.execute("RELEASE outer")
            note_savepoint_released(connection, "outer")
    assert _progress_count(path) == 0
    report = _replay(evidence, _capture_ids(evidence)[0], tmp_path / "scratch-nested")
    assert report["status"] == REPLAY_MATCH, report
    assert report["transaction_status"] == TX_SAVEPOINT_ROLLED_BACK
    assert report["claims"]["computation_replay"] is True
    assert report["claims"]["operational_persistence"] is False


def test_f04_r1_sql_rollback_then_new_transaction_is_not_persistence(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            connection = repository.connection
            connection.execute("BEGIN IMMEDIATE")
            note_enclosing_transaction_opened(connection)
            invoke_closed_candle_outcomes(
                evaluate_closed_candle_outcomes,
                caller_path=CALLER_OWNER_MONITORING,
                record=record,
                execution_candles=[_candle(0, high="101", low="99"), _candle(1, high="103", low="100")],
                execution_timeframe=TIMEFRAME,
                decision_timestamp=_decision(1),
                evaluated_at=_decision(1),
                repository=repository,
                scan_run_id=f"run-{record.lifecycle_id}",
            )
            connection.execute("ROLLBACK")
            connection.execute("BEGIN IMMEDIATE")
            note_enclosing_transaction_opened(connection)
            connection.execute("UPDATE setup_lifecycle_records SET last_seen_at = last_seen_at")
    assert _progress_count(path) == 0
    report = _replay(evidence, _capture_ids(evidence)[0], tmp_path / "scratch-sql-rb")
    assert report["status"] == REPLAY_MATCH, report
    assert report["transaction_status"] in {TX_ENCLOSING_ROLLED_BACK, TX_COMMIT_UNKNOWN}
    assert report["claims"]["operational_persistence"] is False


def test_f04_r1_generation_preserves_earlier_commit_across_later_rollback(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    first = _own(path, _latched(lifecycle_id="life-gen-1", symbol="SOLUSDT"))
    second = _own(path, _latched(lifecycle_id="life-gen-2", symbol="ADAUSDT"))
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            connection = repository.connection
            connection.execute("BEGIN IMMEDIATE")
            note_enclosing_transaction_opened(connection)
            invoke_closed_candle_outcomes(
                evaluate_closed_candle_outcomes,
                caller_path=CALLER_OWNER_MONITORING,
                record=first,
                execution_candles=[_candle(0, high="101", low="99"), _candle(1, high="103", low="100")],
                execution_timeframe=TIMEFRAME,
                decision_timestamp=_decision(1),
                evaluated_at=_decision(1),
                repository=repository,
                scan_run_id=f"run-{first.lifecycle_id}",
            )
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            note_enclosing_transaction_opened(connection)
            invoke_closed_candle_outcomes(
                evaluate_closed_candle_outcomes,
                caller_path=CALLER_OWNER_MONITORING,
                record=second,
                execution_candles=[_candle(0, high="101", low="99"), _candle(1, high="103", low="100")],
                execution_timeframe=TIMEFRAME,
                decision_timestamp=_decision(1),
                evaluated_at=_decision(1),
                repository=repository,
                scan_run_id=f"run-{second.lifecycle_id}",
            )
            connection.execute("ROLLBACK")
    assert _progress_count(path) == 1
    captures = _capture_ids(evidence)
    assert len(captures) == 2
    reports = {
        capture_id: _replay(evidence, capture_id, tmp_path / f"scratch-{capture_id}")
        for capture_id in captures
    }
    statuses = {report["transaction_status"] for report in reports.values()}
    assert TX_ENCLOSING_COMMITTED in statuses
    assert TX_ENCLOSING_ROLLED_BACK in statuses or TX_COMMIT_UNKNOWN in statuses
    persisted = [report for report in reports.values() if report["claims"]["operational_persistence"]]
    discarded = [report for report in reports.values() if not report["claims"]["operational_persistence"]]
    assert len(persisted) == 1
    assert len(discarded) == 1
    assert all(report["status"] == REPLAY_MATCH for report in reports.values())


def test_f04_r3_pinned_reader_rejects_before_commit_and_preserves_prior(tmp_path: Path) -> None:
    path = tmp_path / "evidence.sqlite"
    write_bundle(path, captures=[])
    before = footprint_bytes(path)
    assert before > 0
    reader = sqlite3.connect(path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM evidence_meta").fetchall()
        for count in (10, 20):
            with pytest.raises(BoundExceeded) as excinfo:
                write_bundle(
                    path,
                    captures=[],
                    diagnostics=[
                        {
                            "diagnostic_id": f"diag-{count}-{index}",
                            "recorded_at": "2026-04-01T00:00:00+00:00",
                            "kind": "synthetic",
                            "lifecycle_id": None,
                            "detail": "x" * 500,
                            "transaction_status": "commit_unknown",
                        }
                        for index in range(count)
                    ],
                    limits=BoundLimits(max_store_bytes=96 * 1024),
                )
            assert excinfo.value.reason == "store_byte_limit"
            with sqlite3.connect(path) as probe:
                rows = probe.execute("SELECT count(*) FROM capture_diagnostics").fetchone()[0]
            assert rows == 0
            assert footprint_bytes(path) <= 96 * 1024
        # Boundary that previously fit still preserves an empty prior store.
        write_bundle(
            path,
            captures=[],
            diagnostics=[
                {
                    "diagnostic_id": f"diag-ok-{index}",
                    "recorded_at": "2026-04-01T00:00:00+00:00",
                    "kind": "synthetic",
                    "lifecycle_id": None,
                    "detail": "x" * 500,
                    "transaction_status": "commit_unknown",
                }
                for index in range(5)
            ],
            limits=BoundLimits(max_store_bytes=160 * 1024),
        )
        with sqlite3.connect(path) as probe:
            accepted = probe.execute("SELECT count(*) FROM capture_diagnostics").fetchone()[0]
        assert accepted == 5
        assert footprint_bytes(path) <= 160 * 1024
    finally:
        reader.rollback()
        reader.close()
    assert footprint_bytes(path) <= 160 * 1024


def test_f04_r4_savepoint_rolled_back_cannot_claim_enclosing_committed(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            connection = repository.connection
            connection.execute("BEGIN IMMEDIATE")
            note_enclosing_transaction_opened(connection)
            connection.execute("SAVEPOINT inner")
            note_savepoint_opened(connection, "inner")
            invoke_closed_candle_outcomes(
                evaluate_closed_candle_outcomes,
                caller_path=CALLER_OWNER_MONITORING,
                record=record,
                execution_candles=[_candle(0, high="101", low="99"), _candle(1, high="103", low="100")],
                execution_timeframe=TIMEFRAME,
                decision_timestamp=_decision(1),
                evaluated_at=_decision(1),
                repository=repository,
                scan_run_id=f"run-{record.lifecycle_id}",
            )
            connection.execute("ROLLBACK TO inner")
            note_savepoint_rolled_back(connection, "inner")
            connection.execute("RELEASE inner")
            note_savepoint_released(connection, "inner")
    assert _progress_count(path) == 0
    capture_id = _capture_ids(evidence)[0]
    before = _replay(evidence, capture_id, tmp_path / "scratch-before")
    assert before["status"] == REPLAY_MATCH
    assert before["transaction_status"] == TX_SAVEPOINT_ROLLED_BACK
    assert before["claims"]["operational_persistence"] is False
    with sqlite3.connect(evidence) as connection:
        connection.execute("UPDATE captures SET transaction_status = 'enclosing_committed'")
        connection.commit()
    after = _replay(evidence, capture_id, tmp_path / "scratch-after")
    assert after["status"] == EVIDENCE_CORRUPT
    assert after["reason"] == "savepoint_transaction_inconsistent"
    assert after["claims"]["operational_persistence"] is False
    assert after["claims"]["computation_replay"] is False
    inspection = inspect_capture(evidence_path=evidence, capture_id=capture_id)
    assert inspection["status"] != "INSPECTION_OK"
    assert inspection["exit_code"] != 0


def test_f04_r4_malformed_reference_count_is_structured_corrupt(tmp_path: Path) -> None:
    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        _monitor(path, record, [_candle(0, high="103", low="100")], when=_decision(0))
    capture_id = _capture_ids(evidence)[0]
    with sqlite3.connect(evidence) as connection:
        connection.execute("UPDATE captures SET reference_count = ?", ("malformed",))
        connection.commit()
    inspection = inspect_capture(evidence_path=evidence, capture_id=capture_id)
    assert "uncaught_exception" not in inspection
    assert inspection["status"] == EVIDENCE_CORRUPT
    assert inspection["reason"] == "malformed_reference_count"
    assert inspection["claims"]["operational_persistence"] is False
    assert inspection["claims"]["computation_replay"] is False
    report = _replay(evidence, capture_id, tmp_path / "scratch-malformed")
    assert report["status"] == EVIDENCE_CORRUPT
    assert report["claims"]["operational_persistence"] is False


def test_f04_r7_payload_and_reference_bounds_before_materialization(tmp_path: Path) -> None:
    from unittest.mock import patch

    import app.research.durable_source_replay.replay as replay_module

    evidence = tmp_path / "evidence.sqlite"
    path = _operational(tmp_path)
    record = _own(path, _latched())
    with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
        _monitor(
            path,
            record,
            [_candle(0, high="101", low="99"), _candle(1, high="103", low="100")],
            when=_decision(1),
        )
    capture_id = _capture_ids(evidence)[0]

    oversize = encode_canonical("x" * (DEFAULT_BOUNDS.max_decode_bytes + 19))
    digest = content_hash(oversize)
    with sqlite3.connect(evidence) as connection:
        connection.execute(
            "INSERT INTO payloads VALUES (?, ?, ?, ?)",
            (digest, "cci-durable-source-replay-codec-v1", len(oversize), oversize),
        )
        connection.execute(
            "UPDATE capture_payloads SET content_hash = ? WHERE role = 'call_inputs'",
            (digest,),
        )
        connection.commit()
    read_lengths: list[int] = []
    original_read = replay_module.read_payload

    def spy_read(*args: object, **kwargs: object):
        payload = original_read(*args, **kwargs)
        if payload:
            read_lengths.append(len(payload["bytes"]))
        return payload

    with patch.object(replay_module, "read_payload", spy_read):
        oversized = inspect_capture(evidence_path=evidence, capture_id=capture_id)
    assert oversized["status"] == EVIDENCE_CORRUPT
    assert oversized["reason"] in {"payload_byte_limit", "decode_byte_limit"}
    assert not any(length > DEFAULT_BOUNDS.max_decode_bytes for length in read_lengths)

    clean = tmp_path / "clean.sqlite"
    clean_path = _operational(tmp_path / "clean-op")
    clean_record = _own(clean_path, _latched(lifecycle_id="life-refs", symbol="DOTUSDT"))
    with use_capture(CaptureConfig(enabled=True, evidence_path=clean, operational_paths=(clean_path,))):
        _monitor(clean_path, clean_record, [_candle(0, high="103", low="100")], when=_decision(0))
    capture_id = _capture_ids(clean)[0]
    with sqlite3.connect(clean) as connection:
        refs = connection.execute(
            "SELECT role, ordinal, content_hash FROM capture_payloads ORDER BY role, ordinal"
        ).fetchall()
        digest = min(
            connection.execute("SELECT content_hash, byte_length FROM payloads").fetchall(),
            key=lambda row: row[1],
        )[0]
        for index in range(DEFAULT_BOUNDS.max_reference_count + 1 - len(refs)):
            connection.execute(
                "INSERT INTO capture_payloads VALUES (?, ?, ?, ?)",
                (capture_id, f"z_extra_{index:03}", digest, len(refs) + index),
            )
        connection.execute(
            "UPDATE captures SET reference_count = ?",
            (DEFAULT_BOUNDS.max_reference_count + 1,),
        )
        connection.commit()
    inspection = inspect_capture(evidence_path=clean, capture_id=capture_id)
    assert inspection["status"] == EVIDENCE_CORRUPT
    assert inspection["reason"] == "reference_count_limit"
    assert inspection["exit_code"] != 0
    assert inspection["claims"]["computation_replay"] is False
