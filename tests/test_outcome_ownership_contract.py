from __future__ import annotations

import copy
import inspect
import json
import random
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.analytics.evidence_baseline_audit import build_evidence_baseline
from app.analytics.evidence_contract import UNAVAILABLE, evidence_contract_payload
from app.analytics.outcome_ownership import (
    OUTCOME_OWNERSHIP_VERSION,
    STATUS_AMBIGUOUS_CONTEXT,
    STATUS_CONFLICTING_ECONOMIC,
    STATUS_CONFLICTING_IDENTITY,
    STATUS_INCOMPLETE,
    STATUS_MISSING_LEGACY,
    dumps_outcome_ownership_report,
    project_outcome_ownership,
)
from app.data.dtos import NA
from app.lifecycle.economic_identity import (
    REASON_PLAN_VERSION_INVARIANT_VIOLATION,
    latch_economic_identities,
)
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState, SetupTransitionReason
from app.lifecycle.outcome_policy import canonical_plan_identity
from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.storage.database import SCHEMA_VERSION, open_initialized_database

BASE = datetime(2026, 1, 1, tzinfo=UTC)
TIMEFRAME = "5m"
INVALIDATION = "Closed structure beyond the stored stop invalidates the plan."
AUDITED_COMMIT = "da43358f552626c6ef48235a691fb3a53228c36b"
SYNTHETIC_REPORT = Path("docs/research/outcome_ownership_p3a_synthetic_report.json")


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


def _record(*, lifecycle_id: str = "life-1", **updates) -> SetupLifecycleRecord:
    values = {
        "lifecycle_id": lifecycle_id,
        "symbol": "BTCUSDT",
        "mode": "challenge",
        "direction": "long",
        "current_state": SetupLifecycleState.ACTIONABLE_A_GRADE,
        "first_seen_at": BASE.isoformat(),
        "last_seen_at": BASE.isoformat(),
        "last_transition_at": BASE.isoformat(),
        "confirmed_at": BASE.isoformat(),
        "invalidation_reason": INVALIDATION,
        "invalidation_logic": INVALIDATION,
        "setup_identity": f"{lifecycle_id}-setup",
        "structural_anchor": "sweep-a",
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "90",
        "tp1": "110",
        "tp2": "120",
        "tp3": "130",
    }
    values.update(updates)
    return SetupLifecycleRecord(**values)


def _latched(**updates) -> SetupLifecycleRecord:
    return latch_economic_identities(
        _record(**updates),
        instrument_venue="binance",
        plan_locked=True,
    )


def _evaluate(repository, record, candles, *, decision_index: int | None = None):
    index = len(candles) - 1 if decision_index is None else decision_index
    return evaluate_closed_candle_outcomes(
        record,
        execution_candles=candles,
        execution_timeframe=TIMEFRAME,
        decision_timestamp=_decision(index),
        evaluated_at=_decision(index),
        repository=repository,
        scan_run_id=f"scan-{index}",
    )


def _sql_rows(repository, table: str) -> list[dict[str, object]]:
    cursor = repository._connection.execute(f"SELECT * FROM {table}")
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _project(
    repository: SQLiteSetupLifecycleRepository,
    lifecycle_ids: Sequence[str],
    *,
    provenance: dict[str, object] | None = None,
):
    records = []
    progress = []
    events = []
    for lifecycle_id in lifecycle_ids:
        record = repository.get_record_by_lifecycle_id(lifecycle_id)
        if record is not None:
            records.append(record)
        progress.extend(repository.list_outcome_progress(lifecycle_id=lifecycle_id))
        events.extend(repository.list_events(lifecycle_id=lifecycle_id))
    analytics = list(repository.list_outcome_analytics(symbol="BTCUSDT"))
    return project_outcome_ownership(
        lifecycle_records=records,
        progress_rows=progress,
        event_rows=events,
        analytics_rows=analytics,
        provenance=provenance
        or {
            "synthetic": True,
            "label": "p3a-test",
            "audited_commit": AUDITED_COMMIT,
            "source_namespace": "synthetic-test",
            "coverage_complete": True,
        },
    )


def _interpretable(payload: dict[str, object]) -> list[dict[str, object]]:
    return [item for item in payload["plan_interpretations"] if item.get("interpretable")]


def test_repeated_scans_one_plan_interpretation_retains_events(tmp_path: Path) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "repeat.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        candles = [_candle(0, high="99", low="95")]
        first = _evaluate(repository, record, candles)
        candles.append(_candle(1, high="103", low="99"))
        entered = _evaluate(repository, first.record, candles)
        candles.append(_candle(2, high="111", low="103"))
        tp1 = _evaluate(repository, entered.record, candles)
        candles.append(_candle(3, high="99", low="95"))
        _evaluate(repository, tp1.record, candles)

        before = _sql_rows(repository, "setup_lifecycle_outcome_progress")
        payload = _project(repository, [record.lifecycle_id])
        after = _sql_rows(repository, "setup_lifecycle_outcome_progress")
        assert before == after
        assert dumps_outcome_ownership_report(payload) == dumps_outcome_ownership_report(
            _project(repository, [record.lifecycle_id])
        )
        assert len(repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)) == 1
        interpretations = _interpretable(payload)
        assert len(interpretations) == 1
        assert interpretations[0]["plan_version_id"] == record.plan_version_id
        assert interpretations[0]["milestones"]["entry_at"]
        assert interpretations[0]["milestones"]["tp1_at"]
        assert interpretations[0]["not_a_unique_trade"] is True
        assert payload["source_evidence"]["lifecycle_events"]["retained_count"] >= 2
        assert payload["p2a_event_units"]["entry_activated_event_records"]["value"] == 1
        assert payload["p2a_event_units"]["entry_fill_simulated_event_records"]["value"] >= 1
        assert payload["canonical_outcome_per_plan_version"]["established"] is False


def test_duplicate_and_contradictory_source_records_do_not_inflate_or_hide() -> None:
    record = _latched()
    identity = canonical_plan_identity(record)
    progress = {
        "lifecycle_id": record.lifecycle_id,
        "plan_identity": identity,
        "symbol": "BTCUSDT",
        "mode": "challenge",
        "direction": "long",
        "execution_timeframe": "5m",
        "tracking_start_at": BASE.isoformat(),
        "entry_at": _decision(1),
        "terminal_outcome": NA,
        "first_evaluated_at": _decision(0),
        "last_evaluated_at": _decision(1),
    }
    payload = project_outcome_ownership(
        lifecycle_records=[record, record],
        progress_rows=[progress, copy.deepcopy(progress)],
        provenance={"synthetic": True, "source_namespace": "dup", "coverage_complete": True},
    )
    assert payload["source_evidence"]["outcome_progress"]["retained_count"] == 1
    assert payload["counts"]["interpretable_plan_outcomes"]["value"] == 1

    conflict = copy.deepcopy(progress)
    conflict["terminal_outcome"] = SetupLifecycleState.SL_HIT.value
    conflict["stop_at"] = _decision(2)
    conflicting = project_outcome_ownership(
        lifecycle_records=[record],
        progress_rows=[progress, conflict],
        provenance={"synthetic": True, "source_namespace": "conflict", "coverage_complete": True},
    )
    assert conflicting["source_evidence"]["outcome_progress"]["retained_count"] == 2
    assert any(item["integrity_status"] == STATUS_CONFLICTING_ECONOMIC for item in conflicting["evaluations"])
    assert _interpretable(conflicting) == []


def test_changed_tp_ladder_keeps_p1_distinction_and_flags_claimed_id_conflict() -> None:
    first = _latched(lifecycle_id="life-tp-a")
    second = _latched(lifecycle_id="life-tp-b", tp3="140")
    assert first.plan_version_id != second.plan_version_id
    payload = project_outcome_ownership(
        lifecycle_records=[first, second],
        provenance={"synthetic": True, "source_namespace": "ladders", "coverage_complete": True},
    )
    assert payload["plan_identity_inventory"]["count"] == 2

    adversarial = second.model_dump(mode="json")
    adversarial["plan_version_id"] = first.plan_version_id
    flagged = project_outcome_ownership(
        lifecycle_records=[adversarial],
        provenance={"synthetic": True, "source_namespace": "claimed-id", "coverage_complete": True},
    )
    assert flagged["evaluations"][0]["integrity_status"] == STATUS_CONFLICTING_IDENTITY


def test_one_plan_across_generations_inventory_once_outcomes_not_collapsed(
    tmp_path: Path,
) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "gens.db") as repository:
        first = _latched(lifecycle_id="life-gen-1")
        second = _latched(lifecycle_id="life-gen-2")
        assert first.plan_version_id == second.plan_version_id
        repository.upsert_record(first)
        _evaluate(repository, first, [_candle(0, high="99", low="95")])
        repository.supersede_record(first.lifecycle_id)
        repository.upsert_record(second)
        _evaluate(repository, second, [_candle(0, high="99", low="95")])

        payload = _project(repository, ["life-gen-1", "life-gen-2"])
        assert payload["plan_identity_inventory"]["count"] == 1
        assert payload["plan_identity_inventory"]["values"][0]["lifecycle_ids"] == [
            "life-gen-1",
            "life-gen-2",
        ]
        assert _interpretable(payload) == []
        unresolved = [item for item in payload["plan_interpretations"] if not item["interpretable"]]
        assert unresolved
        assert unresolved[0]["integrity_status"] == STATUS_AMBIGUOUS_CONTEXT
        progress_keys = {
            (item["lifecycle_id"], item["plan_identity"])
            for item in payload["evaluations"]
            if item["plan_identity"]
        }
        assert len(progress_keys) == 2


def test_replay_and_live_namespaces_do_not_merge() -> None:
    record = _latched()
    identity = canonical_plan_identity(record)
    live = {
        "lifecycle_id": record.lifecycle_id,
        "plan_identity": identity,
        "symbol": "BTCUSDT",
        "tracking_start_at": BASE.isoformat(),
        "entry_at": _decision(1),
        "tp1_at": _decision(2),
        "terminal_outcome": NA,
        "execution_timeframe": "5m",
        "source_namespace": "live-monitoring",
        "first_evaluated_at": _decision(0),
        "last_evaluated_at": _decision(2),
    }
    replay = copy.deepcopy(live)
    replay["source_namespace"] = "replay-run-a"
    replay["tp3_at"] = _decision(4)
    replay["terminal_outcome"] = SetupLifecycleState.TP_HIT.value
    payload = project_outcome_ownership(
        lifecycle_records=[
            {**record.model_dump(mode="json"), "source_namespace": "live-monitoring"},
            {**record.model_dump(mode="json"), "source_namespace": "replay-run-a"},
        ],
        progress_rows=[live, replay],
        provenance={"synthetic": True, "coverage_complete": True},
    )
    interpretations = _interpretable(payload)
    assert len(interpretations) == 2
    statuses = {item["economic_status"] for item in interpretations}
    assert "open_after_entry" in statuses
    assert "tp3_terminal" in statuses
    missing_anchor = copy.deepcopy(live)
    missing_anchor.pop("tracking_start_at")
    missing_anchor["source_namespace"] = "replay-run-b"
    anchored = project_outcome_ownership(
        lifecycle_records=[{**record.model_dump(mode="json"), "source_namespace": "replay-run-b"}],
        progress_rows=[missing_anchor],
        provenance={"synthetic": True, "source_namespace": "unused", "coverage_complete": True},
    )
    assert _interpretable(anchored) == []
    assert anchored["plan_interpretations"][0]["integrity_status"] == STATUS_AMBIGUOUS_CONTEXT


def test_helper_does_not_repair_timestamps_or_evaluate_candles(tmp_path: Path) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "entry.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
        _evaluate(repository, record, candles)
        progress = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)[0]
        stripped = progress.model_dump(mode="json")
        stripped["entry_at"] = None
        payload = project_outcome_ownership(
            lifecycle_records=[repository.get_record_by_lifecycle_id(record.lifecycle_id)],
            progress_rows=[stripped],
            provenance={"synthetic": True, "source_namespace": "stripped", "coverage_complete": True},
        )
        interpretation = _interpretable(payload)[0]
        assert interpretation["milestones"]["entry_at"] is None
        source = Path("app/analytics/outcome_ownership.py").read_text(encoding="utf-8")
        assert "evaluate_closed_candle_outcomes" not in source
        assert "closed_candles_as_of" not in source


def test_tp1_then_sl_preserves_milestone_and_does_not_promote_generic_tp(tmp_path: Path) -> None:
    record = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "sl.db") as repository:
        repository.upsert_record(record)
        candles = [
            _candle(0, high="99", low="95"),
            _candle(1, high="103", low="99"),
            _candle(2, high="111", low="103"),
            _candle(3, high="105", low="89"),
        ]
        result = _evaluate(repository, record, candles)
        payload = _project(repository, [record.lifecycle_id])
        interpretation = _interpretable(payload)[0]
        assert interpretation["milestones"]["tp1_at"] == result.progress.tp1_at
        assert interpretation["milestones"]["stop_at"] == result.progress.stop_at
        assert interpretation["economic_status"] == "stop_after_entry"
        assert interpretation["progress_terminal_outcome"] == SetupLifecycleState.SL_HIT.value
        assert payload["unavailable_metrics"]["win_rate"]["value"] is None
        assert payload["unavailable_metrics"]["realized_pnl"]["status"] == UNAVAILABLE

    generic = {
        "lifecycle_id": record.lifecycle_id,
        "plan_identity": canonical_plan_identity(record),
        "symbol": "BTCUSDT",
        "tracking_start_at": BASE.isoformat(),
        "entry_at": _decision(1),
        "terminal_outcome": SetupLifecycleState.TP_HIT.value,
        "execution_timeframe": "5m",
        "first_evaluated_at": _decision(0),
        "last_evaluated_at": _decision(2),
    }
    promoted = project_outcome_ownership(
        lifecycle_records=[record],
        progress_rows=[generic],
        provenance={"synthetic": True, "source_namespace": "generic-tp", "coverage_complete": True},
    )
    interpretation = _interpretable(promoted)[0]
    assert interpretation["generic_tp_hit_not_promoted"] is True
    assert interpretation["economic_status"] == "generic_tp_hit_not_promoted"
    assert interpretation["milestones"]["tp3_at"] is None


def test_pre_entry_invalidation_and_expiry_are_not_stops(tmp_path: Path) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "invalid.db") as repository:
        invalidated = _latched(current_state=SetupLifecycleState.INVALIDATED)
        repository.upsert_record(invalidated)
        _evaluate(repository, invalidated, [_candle(0, high="99", low="95")])
        invalid_payload = _project(repository, [invalidated.lifecycle_id])
        assert _interpretable(invalid_payload)[0]["economic_status"] == "invalidation_before_entry"

    with SQLiteSetupLifecycleRepository(tmp_path / "expired.db") as repository:
        expired = _latched(lifecycle_id="life-exp", current_state=SetupLifecycleState.EXPIRED)
        repository.upsert_record(expired)
        _evaluate(repository, expired, [_candle(0, high="99", low="95")])
        expired_payload = _project(repository, [expired.lifecycle_id])
        assert _interpretable(expired_payload)[0]["economic_status"] == "expiry_before_entry"

    open_record = _latched(lifecycle_id="life-open")
    uncertain = project_outcome_ownership(
        lifecycle_records=[open_record],
        progress_rows=[
            {
                "lifecycle_id": "life-open",
                "plan_identity": canonical_plan_identity(open_record),
                "symbol": "BTCUSDT",
                "tracking_start_at": BASE.isoformat(),
                "execution_timeframe": "5m",
                "terminal_outcome": NA,
                "first_evaluated_at": _decision(0),
                "last_evaluated_at": _decision(0),
            }
        ],
        provenance={"synthetic": True, "source_namespace": "uncertain", "coverage_complete": False},
    )
    interpretation = _interpretable(uncertain)[0]
    assert interpretation["economic_status"] == "open_or_unresolved_without_entry"
    assert interpretation["milestones"]["entry_at"] is None
    assert uncertain["provenance"]["coverage_complete"] is False


def test_cooldown_does_not_erase_economic_terminal_and_latest_row_is_not_authority(
    tmp_path: Path,
) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "cool.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        candles = [
            _candle(0, high="99", low="95"),
            _candle(1, high="103", low="99"),
            _candle(2, high="111", low="103"),
            _candle(3, high="105", low="89"),
        ]
        result = _evaluate(repository, record, candles)
        cooled = result.record.model_copy(update={"current_state": SetupLifecycleState.COOLDOWN})
        repository.upsert_record(cooled)
        payload = _project(repository, [record.lifecycle_id])
        interpretation = _interpretable(payload)[0]
        assert interpretation["economic_status"] == "stop_after_entry"
        assert interpretation["successor_note"]

    record = _latched(lifecycle_id="life-conflict-term")
    identity = canonical_plan_identity(record)
    first = {
        "id": 1,
        "lifecycle_id": record.lifecycle_id,
        "plan_identity": identity,
        "symbol": "BTCUSDT",
        "tracking_start_at": BASE.isoformat(),
        "entry_at": _decision(1),
        "stop_at": _decision(3),
        "terminal_outcome": SetupLifecycleState.SL_HIT.value,
        "execution_timeframe": "5m",
        "first_evaluated_at": _decision(0),
        "last_evaluated_at": _decision(3),
        "updated_at": "2026-01-01 00:00:00",
    }
    later_tp = {
        **first,
        "id": 2,
        "stop_at": None,
        "tp3_at": _decision(4),
        "terminal_outcome": SetupLifecycleState.TP_HIT.value,
        "last_evaluated_at": _decision(4),
        "updated_at": "2026-01-02 00:00:00",
    }
    conflict = project_outcome_ownership(
        lifecycle_records=[record],
        progress_rows=[later_tp, first],
        provenance={"synthetic": True, "source_namespace": "latest-not-authority", "coverage_complete": True},
    )
    assert any(item["integrity_status"] == STATUS_CONFLICTING_ECONOMIC for item in conflict["evaluations"])
    assert _interpretable(conflict) == []


def test_input_shuffle_preserves_result_and_lifecycle_only_arrival_cannot_overwrite() -> None:
    record = _latched()
    identity = canonical_plan_identity(record)
    progress = {
        "lifecycle_id": record.lifecycle_id,
        "plan_identity": identity,
        "symbol": "BTCUSDT",
        "tracking_start_at": BASE.isoformat(),
        "entry_at": _decision(1),
        "stop_at": _decision(3),
        "terminal_outcome": SetupLifecycleState.SL_HIT.value,
        "execution_timeframe": "5m",
        "first_evaluated_at": _decision(0),
        "last_evaluated_at": _decision(3),
    }
    events = [
        {
            "event_id": 2,
            "lifecycle_id": record.lifecycle_id,
            "reason": SetupTransitionReason.STOP_LOSS_HIT.value,
            "timestamp": _decision(3),
            "to_state": SetupLifecycleState.SL_HIT.value,
            "notes": json.dumps({"plan_identity": identity}),
        },
        {
            "event_id": 1,
            "lifecycle_id": record.lifecycle_id,
            "reason": SetupTransitionReason.ENTRY_ACTIVATED.value,
            "timestamp": _decision(1),
            "to_state": SetupLifecycleState.MANAGING.value,
            "notes": json.dumps({"plan_identity": identity}),
        },
    ]
    cooldown_record = record.model_copy(update={"current_state": SetupLifecycleState.COOLDOWN})
    first = project_outcome_ownership(
        lifecycle_records=[cooldown_record],
        progress_rows=[progress],
        event_rows=events,
        provenance={"synthetic": True, "source_namespace": "shuffle", "coverage_complete": True},
    )
    shuffled_events = list(events)
    random.Random(7).shuffle(shuffled_events)
    second = project_outcome_ownership(
        lifecycle_records=[cooldown_record],
        progress_rows=[progress],
        event_rows=shuffled_events,
        provenance={"synthetic": True, "source_namespace": "shuffle", "coverage_complete": True},
    )
    assert dumps_outcome_ownership_report(first) == dumps_outcome_ownership_report(second)
    assert _interpretable(first)[0]["economic_status"] == "stop_after_entry"


def test_p2a_units_are_not_fill_occurrences() -> None:
    record = _latched()
    events = [
        {
            "event_id": index,
            "lifecycle_id": record.lifecycle_id,
            "reason": reason,
            "timestamp": _decision(index),
            "to_state": SetupLifecycleState.CONFIRMED.value,
        }
        for index, reason in enumerate(
            (
                SetupTransitionReason.ENTRY_ZONE_TOUCHED.value,
                SetupTransitionReason.ENTRY_ACTIVATED.value,
                SetupTransitionReason.ENTRY_FILL_SIMULATED.value,
                "WATCH_ALERT_ACTIVATION",
                "PUBLIC_DELIVERY",
                "SETUP_TRIGGER",
            ),
            start=1,
        )
    ]
    payload = project_outcome_ownership(
        lifecycle_records=[record],
        event_rows=events,
        provenance={"synthetic": True, "source_namespace": "p2a", "coverage_complete": True},
    )
    units = payload["p2a_event_units"]
    assert units["entry_zone_touched_event_records"]["value"] == 1
    assert units["entry_activated_event_records"]["value"] == 1
    assert units["entry_fill_simulated_event_records"]["value"] == 1
    assert units["zone_touch_creates_fill"] is False
    assert units["public_delivery_creates_outcome"] is False
    assert units["generic_entry_activation_creates_unique_trade"] is False
    assert payload["unavailable_metrics"]["fill_occurrence_count"]["status"] == UNAVAILABLE
    assert payload["unavailable_metrics"]["unique_trade_count"]["value"] is None


def test_missing_plan_id_and_unbound_progress_are_unavailable() -> None:
    legacy = _record()
    assert legacy.plan_version_id is None
    payload = project_outcome_ownership(
        lifecycle_records=[legacy],
        progress_rows=[
            {
                "lifecycle_id": legacy.lifecycle_id,
                "plan_identity": canonical_plan_identity(legacy),
                "symbol": "BTCUSDT",
                "tracking_start_at": BASE.isoformat(),
                "execution_timeframe": "5m",
                "terminal_outcome": NA,
                "first_evaluated_at": _decision(0),
                "last_evaluated_at": _decision(0),
            }
        ],
        provenance={"synthetic": True, "source_namespace": "legacy", "coverage_complete": True},
    )
    assert payload["evaluations"][0]["integrity_status"] == STATUS_MISSING_LEGACY
    assert payload["plan_identity_inventory"]["count"] == 0

    current = _latched()
    unbound = {
        "lifecycle_id": current.lifecycle_id,
        "plan_identity": "plan-not-this-geometry",
        "symbol": "BTCUSDT",
        "tracking_start_at": BASE.isoformat(),
        "execution_timeframe": "5m",
        "terminal_outcome": NA,
        "first_evaluated_at": _decision(0),
        "last_evaluated_at": _decision(0),
    }
    lent = project_outcome_ownership(
        lifecycle_records=[current],
        progress_rows=[unbound],
        provenance={"synthetic": True, "source_namespace": "lend", "coverage_complete": True},
    )
    assert lent["evaluations"][0]["integrity_status"] == STATUS_INCOMPLETE
    assert lent["evaluations"][0]["attribution"]["plan_version_id"] is None
    assert _interpretable(lent) == []


def test_geometry_change_after_latch_is_identity_conflict_not_new_owner(tmp_path: Path) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "geom.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
        entered = _evaluate(repository, record, candles)
        replacement = entered.record.model_copy(
            update={
                "current_state": SetupLifecycleState.ACTIONABLE_A_GRADE,
                "entry_low": "200",
                "entry_high": "202",
                "stop_loss": "190",
                "tp1": "210",
                "tp2": "220",
                "tp3": "230",
                "invalidation_logic": "Closed structure below 190 invalidates the replacement plan.",
                "invalidation_reason": "Closed structure below 190 invalidates the replacement plan.",
            }
        )
        replacement = latch_economic_identities(
            replacement,
            instrument_venue="binance",
            plan_locked=True,
            previous=entered.record,
        )
        assert replacement.plan_version_id == record.plan_version_id
        assert replacement.economic_identity_reason
        assert REASON_PLAN_VERSION_INVARIANT_VIOLATION in replacement.economic_identity_reason
        repository.upsert_record(replacement)
        _evaluate(repository, replacement, [_candle(0, high="199", low="195")])
        payload = _project(repository, [record.lifecycle_id])
        assert len(repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)) == 2
        assert _interpretable(payload) == []
        assert any(item["integrity_status"] == STATUS_CONFLICTING_IDENTITY for item in payload["evaluations"])


def test_counts_expose_population_and_empty_input_does_not_fabricate_trades() -> None:
    empty = project_outcome_ownership(provenance={"synthetic": True, "coverage_complete": False})
    assert empty["counts"]["raw_source_rows"]["outcome_progress"] == 0
    assert empty["counts"]["interpretable_plan_outcomes"]["value"] == 0
    assert empty["unavailable_metrics"]["unique_trade_count"]["value"] is None
    assert empty["unavailable_metrics"]["unique_trade_count"]["status"] == UNAVAILABLE
    assert "not a losing" in empty["provenance"]["coverage_note"]
    assert empty["integrity"]["all_supplied_evaluations_accounted"] is True

    record = _latched()
    identity = canonical_plan_identity(record)
    payload = project_outcome_ownership(
        lifecycle_records=[record],
        progress_rows=[
            {
                "lifecycle_id": record.lifecycle_id,
                "plan_identity": identity,
                "symbol": "BTCUSDT",
                "tracking_start_at": BASE.isoformat(),
                "entry_at": _decision(1),
                "tp1_at": _decision(2),
                "tp2_at": _decision(3),
                "tp3_at": _decision(4),
                "terminal_outcome": SetupLifecycleState.TP_HIT.value,
                "execution_timeframe": "5m",
                "first_evaluated_at": _decision(0),
                "last_evaluated_at": _decision(4),
            }
        ],
        provenance={"synthetic": True, "source_namespace": "overlap", "coverage_complete": True},
    )
    overlapping = payload["counts"]["overlapping_milestones"]
    assert overlapping["overlapping"] is True
    assert overlapping["tp1"] == overlapping["tp2"] == overlapping["tp3"] == 1
    assert overlapping["entry_activation"] == 1
    assert "not disjoint" in overlapping["note"].lower()


def test_helper_leaves_inputs_and_sqlite_rows_unchanged(tmp_path: Path) -> None:
    original_record = _latched()
    record_copy = original_record.model_dump(mode="json")
    progress = {
        "lifecycle_id": original_record.lifecycle_id,
        "plan_identity": canonical_plan_identity(original_record),
        "symbol": "BTCUSDT",
        "tracking_start_at": BASE.isoformat(),
        "execution_timeframe": "5m",
        "terminal_outcome": NA,
        "first_evaluated_at": _decision(0),
        "last_evaluated_at": _decision(0),
    }
    progress_copy = copy.deepcopy(progress)
    project_outcome_ownership(
        lifecycle_records=[original_record],
        progress_rows=[progress],
        provenance={"synthetic": True, "source_namespace": "immut", "coverage_complete": True},
    )
    assert original_record.model_dump(mode="json") == record_copy
    assert progress == progress_copy

    db_path = tmp_path / "frozen.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        record = _latched()
        repository.upsert_record(record)
        _evaluate(repository, record, [_candle(0, high="99", low="95")])
        before_rows = {
            table: _sql_rows(repository, table)
            for table in (
                "setup_lifecycle_records",
                "setup_lifecycle_outcome_progress",
                "setup_lifecycle_events",
                "setup_outcome_analytics",
            )
        }
        schema_before = repository._connection.execute("PRAGMA user_version").fetchone()[0]
        _project(repository, [record.lifecycle_id])
        after_rows = {table: _sql_rows(repository, table) for table in before_rows}
        schema_after = repository._connection.execute("PRAGMA user_version").fetchone()[0]
    assert before_rows == after_rows
    assert schema_before == schema_after == SCHEMA_VERSION == 21
    source = Path("app/analytics/outcome_ownership.py").read_text(encoding="utf-8")
    assert "sqlite3" not in source
    assert "open_initialized_database" not in source
    assert "get_settings" not in source


def test_helper_is_not_wired_into_runtime_or_research_consumers() -> None:
    from app.alerts import telegram_lifecycle
    from app.analytics import evidence_baseline_audit, performance_memory, symbol_health
    from app.lifecycle import outcomes, service, state_machine
    from app.research import queries

    banned = "outcome_ownership"
    for module in (
        state_machine,
        service,
        outcomes,
        telegram_lifecycle,
        symbol_health,
        performance_memory,
        queries,
        evidence_baseline_audit,
    ):
        assert banned not in inspect.getsource(module)
    assert SCHEMA_VERSION == 21
    assert evidence_contract_payload()["outcome_ownership"]["feeds_operational_decisions"] is False


def test_fresh_schema_remains_v21_and_progress_has_no_plan_version_column(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite"
    with open_initialized_database(path) as connection:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        progress_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(setup_lifecycle_outcome_progress)")
        }
        record_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(setup_lifecycle_records)")
        }
    assert user_version == 21 == SCHEMA_VERSION
    assert "plan_identity" in progress_columns
    assert "plan_version_id" not in progress_columns
    assert "plan_version_id" in record_columns


def test_synthetic_report_artifact_is_labeled_and_matches_helper(tmp_path: Path) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "report.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        candles = [
            _candle(0, high="99", low="95"),
            _candle(1, high="103", low="99"),
            _candle(2, high="111", low="103"),
            _candle(3, high="105", low="89"),
        ]
        _evaluate(repository, record, candles)
        payload = _project(
            repository,
            [record.lifecycle_id],
            provenance={
                "synthetic": True,
                "label": "p3a-synthetic-tp1-then-sl",
                "audited_commit": AUDITED_COMMIT,
                "source_namespace": "synthetic-report",
                "coverage_complete": True,
                "evaluation_context": "closed-candle producer fixture; not a live audit",
            },
        )
    artifact = json.loads(SYNTHETIC_REPORT.read_text(encoding="utf-8"))
    assert artifact["provenance"]["synthetic"] is True
    assert artifact["provenance"]["not_live_audit"] is True
    assert artifact["ownership_version"] == OUTCOME_OWNERSHIP_VERSION
    assert artifact["canonical_outcome_per_plan_version"]["established"] is False
    assert artifact["counts"]["interpretable_plan_outcomes"]["value"] == 1
    assert artifact["plan_interpretations"][0]["economic_status"] == "stop_after_entry"
    assert payload["plan_interpretations"][0]["economic_status"] == "stop_after_entry"
    assert payload["counts"]["overlapping_milestones"]["tp1"] == 1


def test_evidence_baseline_audit_still_does_not_project_ownership(tmp_path: Path) -> None:
    path = tmp_path / "audit.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE scan_runs (
                run_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                is_watch_iteration INTEGER NOT NULL DEFAULT 0,
                valid_activations INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE setup_lifecycle_events (
                event_id INTEGER PRIMARY KEY,
                lifecycle_id TEXT,
                timestamp TEXT NOT NULL,
                reason TEXT NOT NULL,
                scan_run_id TEXT,
                from_state TEXT,
                to_state TEXT
            );
            CREATE TABLE setup_lifecycle_records (
                lifecycle_id TEXT PRIMARY KEY,
                current_state TEXT,
                setup_id TEXT,
                plan_version_id TEXT,
                last_seen_at TEXT
            );
            CREATE TABLE setup_lifecycle_outcome_progress (
                id INTEGER PRIMARY KEY,
                lifecycle_id TEXT,
                plan_identity TEXT
            );
            CREATE TABLE setup_outcome_analytics (
                id INTEGER PRIMARY KEY,
                lifecycle_id TEXT,
                final_outcome TEXT
            );
            INSERT INTO scan_runs(run_id, timestamp, is_watch_iteration, valid_activations)
            VALUES ('s1', '2026-09-01T12:00:00+00:00', 1, 0);
            """
        )
        connection.commit()
    baseline = build_evidence_baseline(path, start="2026-09-01T00:00:00Z", cutoff="2026-09-03T00:00:00Z")
    assert "outcome_ownership" not in baseline
    assert "activation_accounting" in baseline
