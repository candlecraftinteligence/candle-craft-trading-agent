from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.alerts.public_identity import canonical_public_event_key
from app.alerts.telegram_lifecycle import (
    SQLiteTelegramAlertAttemptRepository,
    TelegramAlertType,
    telegram_alert_decision_for_symbol,
)
from app.analytics.setup_quality import SetupQualityGrade
from app.data.dtos import NA
from app.lifecycle.economic_identity import mint_plan_version_id, mint_setup_id
from app.lifecycle.models import SetupLifecycleState, SetupTransitionReason
from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.state_machine import (
    PLAN_LOCK_STATES,
    LifecycleObservation,
    _confirmed_observation_ready,
    evaluate_lifecycle_transition,
)
from app.storage.database import SCHEMA_VERSION, open_initialized_database
from app.watch_mode import WatchActivation, current_result_is_valid_activation
from tests.test_activation_accounting_truth import _confirmed_observation as _p2a_observation
from tests.test_lifecycle_outcomes import _entry as _outcome_entry
from tests.test_lifecycle_outcomes import _evaluate as _evaluate_outcomes
from tests.test_lifecycle_outcomes import _record as _outcome_record
from tests.test_lifecycle_outcomes import _target as _outcome_target
from tests.test_storage_database import _scan_result
from tests.test_telegram_lifecycle_delivery_phase42 import (
    FakeSender,
    _run_result,
    _setup_quality_with_grade,
    _symbol,
    _trade_idea,
    run,
)
from tests.test_triggered_confirmed_telegram_delivery import _make_lifecycle_retry_due, _service


ANCHOR = "setup_generation_anchor|sweep-a"
VENUE = "binance"
LIFE = "life-p2b"
FORMER_OSCILLATION = (
    (SetupLifecycleState.CONFIRMED, SetupLifecycleState.ACTIONABLE_A_GRADE, "ACTIONABLE_A_GRADE"),
    (SetupLifecycleState.ACTIONABLE_A_GRADE, SetupLifecycleState.CONFIRMED, "PULLBACK_RR_VALID"),
)


def _ts(index: int, start: str = "2026-09-01T11:00:00+00:00") -> str:
    base = datetime.fromisoformat(start)
    return (base + timedelta(minutes=5 * index)).isoformat()


def _ready(**overrides: object) -> LifecycleObservation:
    data: dict[str, object] = {
        "symbol": "BTCUSDT",
        "mode": "swing",
        "direction": "long",
        "readiness_score": 85,
        "readiness_label": "VALID SETUP",
        "quality_score": 90,
        "quality_grade": "A",
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "95",
        "tp1": "110",
        "tp2": "117",
        "tp3": "124",
        "rr": "3.2",
        "failed_gate": NA,
        "invalidation_reason": "Invalid if price accepts below 95.",
        "sweep_detected": True,
        "structure_shift_detected": True,
        "pullback_valid": True,
        "rr_valid": True,
        "valid_trade_idea": True,
        "core_status": "idea_created",
        "setup_quality_state": "high_quality_trade",
        "technical_score": "70",
        "opportunity_score": "88",
        "min_technical_score": "50",
        "min_opportunity_score": "80",
        "actionable_a_grade_candidate": True,
        "actionability_state": "A_GRADE_ACTIONABLE",
        "entry_filled": False,
        "instrument_venue": VENUE,
        "structural_anchor": ANCHOR,
    }
    data.update(overrides)
    return LifecycleObservation(**data)


def _step(record, observation: LifecycleObservation, index: int, *, lifecycle_id: str = LIFE):
    return evaluate_lifecycle_transition(
        record,
        observation,
        lifecycle_id=lifecycle_id,
        now=_ts(index),
        scan_run_id=f"run-{index}",
    )


def _legit_confirmed(observation: LifecycleObservation | None = None, *, lifecycle_id: str = LIFE):
    obs = observation or _ready()
    first = _step(None, obs, 0, lifecycle_id=lifecycle_id)
    second = _step(first.record, obs, 1, lifecycle_id=lifecycle_id)
    return first, second


def _symbol_from_transition(result, *, signal_id: str = LIFE):
    previous = result.from_state
    symbol = _symbol(
        result.to_state,
        previous=previous,
        transitioned=result.transitioned,
        signal_id=signal_id,
        setup_quality=_setup_quality_with_grade(SetupQualityGrade.A, quality_score=90),
        trade_idea=_trade_idea(opportunity_grade="A", opportunity_score=Decimal("88")),
    )
    transitions = (result,) if result.transitioned else ()
    return symbol.model_copy(
        update={
            "lifecycle_state": result.record,
            "lifecycle_transition": result,
            "lifecycle_transitions": transitions,
        }
    )


def test_schema_version_unchanged_for_p2b() -> None:
    assert SCHEMA_VERSION == 25
    assert SetupLifecycleState.CONFIRMED in PLAN_LOCK_STATES
    assert SetupLifecycleState.ACTIONABLE_A_GRADE in PLAN_LOCK_STATES
    assert SetupLifecycleState.TRIGGERED not in PLAN_LOCK_STATES


def test_legitimate_confirmation_then_a_grade_does_not_oscillate() -> None:
    obs = _ready()
    assert _confirmed_observation_ready(obs) is True
    init, confirmed = _legit_confirmed(obs)
    assert init.to_state == SetupLifecycleState.TRIGGERED
    assert init.record.confirmation_count == 1
    assert init.record.confirmed_at is None
    assert confirmed.transitioned is True
    assert confirmed.to_state == SetupLifecycleState.CONFIRMED
    assert confirmed.reason == SetupTransitionReason.MULTI_SCAN_CONFIRMED
    assert confirmed.record.confirmation_count == 2
    confirmed_at = confirmed.record.confirmed_at
    assert confirmed_at == _ts(1)

    stay = _step(confirmed.record, obs, 2)
    again = _step(stay.record, obs, 3)
    later_candle = _step(again.record, obs, 4)
    for result, expected_from in (
        (stay, SetupLifecycleState.CONFIRMED),
        (again, SetupLifecycleState.CONFIRMED),
        (later_candle, SetupLifecycleState.CONFIRMED),
    ):
        assert result.transitioned is False
        assert result.from_state == expected_from
        assert result.to_state == SetupLifecycleState.CONFIRMED
        assert result.reason == SetupTransitionReason.NO_CHANGE
        assert result.event is None
        assert result.record.current_state == SetupLifecycleState.CONFIRMED
        assert result.record.confirmation_count == 2
        assert result.record.confirmed_at == confirmed_at
        assert result.record.actionability_state == "A_GRADE_ACTIONABLE"
        assert result.record.last_transition_at == confirmed.record.last_transition_at
        assert (result.from_state, result.to_state, result.reason.name) not in {
            (src, dst, reason) for src, dst, reason in FORMER_OSCILLATION
        }


def test_preconfirmation_actionable_still_requires_existing_confirmation_evidence() -> None:
    weak = _ready(
        valid_trade_idea=False,
        rr="2.6",
        core_status="near_miss",
        setup_quality_state="watchlist_near_miss",
        quality_grade="A",
    )
    assert _confirmed_observation_ready(weak) is False
    first = _step(None, weak, 0, lifecycle_id="life-pre")
    assert first.to_state == SetupLifecycleState.ACTIONABLE_A_GRADE
    second = _step(first.record, weak, 1, lifecycle_id="life-pre")
    assert second.to_state == SetupLifecycleState.ACTIONABLE_A_GRADE
    assert second.record.confirmed_at is None
    ready = _ready()
    too_early = _ready(
        valid_trade_idea=False,
        quality_grade="B",
        quality_score=70,
        rr="2.1",
        actionable_a_grade_candidate=False,
    )
    blocked = _step(second.record, too_early, 2, lifecycle_id="life-pre")
    assert blocked.to_state != SetupLifecycleState.CONFIRMED
    promoted = _step(second.record, ready, 3, lifecycle_id="life-pre")
    assert promoted.to_state == SetupLifecycleState.CONFIRMED
    assert promoted.record.confirmation_count >= promoted.record.required_confirmation_cycles


def test_loss_of_confirmation_readiness_can_still_select_actionable() -> None:
    _, confirmed = _legit_confirmed()
    unready = _ready(rr="2.6")
    assert _confirmed_observation_ready(unready) is False
    demote = _step(confirmed.record, unready, 2)
    assert demote.transitioned is True
    assert demote.to_state == SetupLifecycleState.ACTIONABLE_A_GRADE
    assert demote.reason == SetupTransitionReason.ACTIONABLE_A_GRADE
    assert demote.record.actionability_state == "A_GRADE_ACTIONABLE"
    assert demote.record.confirmed_at == confirmed.record.confirmed_at


def test_genuine_exits_win_while_actionability_is_true() -> None:
    _, confirmed = _legit_confirmed()
    invalidated = _step(confirmed.record, _ready(invalidated=True), 2)
    assert invalidated.to_state == SetupLifecycleState.INVALIDATED
    assert invalidated.reason == SetupTransitionReason.SETUP_INVALIDATED
    expired = _step(confirmed.record, _ready(expired=True), 2)
    assert expired.to_state == SetupLifecycleState.EXPIRED
    assert expired.reason == SetupTransitionReason.SETUP_EXPIRED
    filled = _step(confirmed.record, _ready(entry_filled=True), 2)
    assert filled.to_state == SetupLifecycleState.EXECUTING
    cooldown = _step(invalidated.record, _ready(), 3)
    assert cooldown.to_state == SetupLifecycleState.COOLDOWN


def test_executing_to_managing_still_requires_fill() -> None:
    _, confirmed = _legit_confirmed()
    executing = _step(confirmed.record, _ready(entry_filled=True), 2)
    assert executing.to_state == SetupLifecycleState.EXECUTING
    still_executing = _step(executing.record, _ready(entry_filled=False), 3)
    assert still_executing.to_state == SetupLifecycleState.EXECUTING
    managing = _step(executing.record, _ready(entry_filled=True), 3)
    assert managing.to_state == SetupLifecycleState.MANAGING
    assert managing.reason == SetupTransitionReason.ENTRY_FILL_SIMULATED


def test_decay_does_not_skip_invalidation_or_expire_confirmed(tmp_path: Path) -> None:
    _, confirmed = _legit_confirmed()
    stayed = confirmed.record
    for index in range(2, 6):
        stayed = _step(stayed, _ready(), index).record
        assert stayed.current_state == SetupLifecycleState.CONFIRMED
    assert stayed.decay_count >= 1
    invalidated = _step(stayed, _ready(invalidated=True), 6)
    assert invalidated.to_state == SetupLifecycleState.INVALIDATED


def test_confirmation_count_and_confirmed_at_are_unchanged_by_removed_reentry() -> None:
    _, confirmed = _legit_confirmed(_p2a_observation(actionability_state="A_GRADE_ACTIONABLE"))
    first_at = confirmed.record.confirmed_at
    stay = _step(confirmed.record, _p2a_observation(actionability_state="A_GRADE_ACTIONABLE"), 2)
    assert stay.record.confirmation_count == 2
    assert stay.record.required_confirmation_cycles == 2
    assert stay.record.confirmed_at == first_at
    assert stay.event is None


def test_plan_ids_and_economics_stay_locked_through_repaired_sequence_and_restart(tmp_path: Path) -> None:
    obs = _ready()
    db_path = tmp_path / "p1.sqlite"
    expected_setup = mint_setup_id(
        instrument_venue=VENUE,
        symbol="BTCUSDT",
        direction="long",
        mode="swing",
        structural_anchor=ANCHOR,
    ).identity
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        init, confirmed = _legit_confirmed(obs)
        repository.upsert_record(init.record)
        repository.insert_event(init.event)
        repository.upsert_record(confirmed.record)
        repository.insert_event(confirmed.event)
        stay = _step(confirmed.record, obs, 2)
        repository.upsert_record(stay.record)
        assert stay.event is None
        assert stay.record.setup_id == expected_setup == confirmed.record.setup_id
        assert stay.record.plan_version_id == confirmed.record.plan_version_id
        assert stay.record.entry_low == "100"
        assert stay.record.stop_loss == "95"
        assert stay.record.tp1 == "110"
        changed = _step(
            stay.record,
            _ready(entry_low="101", entry_high="103", stop_loss="94", tp1="111"),
            3,
        )
        assert changed.record.entry_low == "100"
        assert changed.record.stop_loss == "95"
        assert changed.record.tp1 == "110"
        assert changed.record.setup_id == expected_setup
        assert changed.record.plan_version_id == stay.record.plan_version_id
        expected_plan = mint_plan_version_id(
            setup_id=expected_setup,
            entry_low="100",
            entry_high="102",
            stop_loss="95",
            tp1="110",
            tp2="117",
            tp3="124",
            invalidation="Invalid if price accepts below 95.",
        ).identity
        assert changed.record.plan_version_id == expected_plan
        reloaded = repository.get_record(symbol="BTCUSDT", mode="swing", direction="long")
        restarted = _step(reloaded, obs, 4)
        assert restarted.record.setup_id == expected_setup
        assert restarted.record.plan_version_id == expected_plan
        assert restarted.record.entry_low == "100"


def test_legacy_null_p1_records_are_readable_without_backfill(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.sqlite"
    _, confirmed = _legit_confirmed()
    legacy = confirmed.record.model_copy(
        update={"setup_id": None, "plan_version_id": None, "economic_identity_reason": None}
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(legacy)
        loaded = repository.get_record_by_lifecycle_id(LIFE)
        assert loaded.setup_id is None
        assert loaded.plan_version_id is None
        next_obs = _step(loaded, _ready(), 2)
        assert next_obs.record.current_state == SetupLifecycleState.CONFIRMED
        actionable_seed = legacy.model_copy(
            update={
                "current_state": SetupLifecycleState.ACTIONABLE_A_GRADE,
                "lifecycle_id": "life-act",
                "symbol": "ETHUSDT",
            }
        )
        repository.upsert_record(actionable_seed)
        loaded_actionable = repository.get_record_by_lifecycle_id("life-act")
        assert loaded_actionable.current_state == SetupLifecycleState.ACTIONABLE_A_GRADE
        assert loaded_actionable.setup_id is None


def test_closed_candle_fill_and_tp_from_confirmed_remain_compatible(tmp_path: Path) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        record = _outcome_record(state=SetupLifecycleState.CONFIRMED, mode="swing")
        repository.upsert_record(record)
        candles = [_outcome_entry(0, "long")]
        filled = _evaluate_outcomes(repository, record, candles)
        assert filled.record.current_state == SetupLifecycleState.MANAGING
        assert any(
            event.reason == SetupTransitionReason.ENTRY_ACTIVATED
            for event in repository.list_events(lifecycle_id=record.lifecycle_id)
        )
        candles.append(_outcome_target(1, "long", 1))
        tp = _evaluate_outcomes(repository, filled.record, candles)
        assert tp.progress is not None
        assert tp.progress.tp1_at is not None


def test_watch_activation_accounting_is_not_implied_by_lifecycle_stay() -> None:
    result = _scan_result()
    assert current_result_is_valid_activation(result.results[0]) is False
    assert WatchActivation(
        symbol="BTCUSDT",
        mode="swing",
        message="alert",
        delivery_status="dry_run",
        delivery_detail="Dry run.",
    ).symbol == "BTCUSDT"


def test_fresh_database_stays_on_current_schema(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite"
    with open_initialized_database(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_public_first_confirmation_is_preserved_and_reentry_is_not_required(tmp_path: Path) -> None:
    db_path = tmp_path / "public.sqlite"
    sender = FakeSender()
    service = _service(db_path, sender)
    init, confirmed = _legit_confirmed()
    first_confirmed = _symbol_from_transition(confirmed)
    first = run(service.deliver_for_run(_run_result(first_confirmed), scan_run_id="confirm-1"))
    assert first.sent == 1
    assert any("SIGNAL CONFIRMED" in message for message in sender.messages)
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts()
        confirmed_attempts = [item for item in attempts if item.alert_type == TelegramAlertType.SIGNAL_CONFIRMED.value]
        assert len(confirmed_attempts) == 1
        event_key = confirmed_attempts[0].public_watchlist_event_key
        message_hash = confirmed_attempts[0].message_hash
        assert event_key == canonical_public_event_key(
            confirmed_attempts[0].public_watchlist_plan_id,
            TelegramAlertType.SIGNAL_CONFIRMED.value,
        )

    stay = _step(confirmed.record, _ready(), 2)
    stay_symbol = _symbol_from_transition(stay)
    stay_decision = telegram_alert_decision_for_symbol(stay_symbol)
    assert stay_decision.eligible is False
    assert stay_decision.reason == "unchanged_lifecycle_state"
    repeated = run(service.deliver_for_run(_run_result(stay_symbol), scan_run_id="confirm-2"))
    assert repeated.sent == 0
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        confirmed_attempts = [
            item
            for item in repository.list_attempts()
            if item.alert_type == TelegramAlertType.SIGNAL_CONFIRMED.value
        ]
        assert len(confirmed_attempts) == 1
        assert confirmed_attempts[0].public_watchlist_event_key == event_key
        assert confirmed_attempts[0].message_hash == message_hash

    unready = _step(stay.record, _ready(rr="2.6"), 3)
    unready_symbol = _symbol_from_transition(unready)
    unready_decision = telegram_alert_decision_for_symbol(unready_symbol)
    assert unready_decision.eligible is False
    assert unready.to_state == SetupLifecycleState.ACTIONABLE_A_GRADE


def test_pending_confirmation_recovers_from_outbox_without_reentry(tmp_path: Path) -> None:
    db_path = tmp_path / "retry.sqlite"
    sender = FakeSender("failed")
    service = _service(db_path, sender)
    _, confirmed = _legit_confirmed()
    symbol = _symbol_from_transition(confirmed)
    first = run(service.deliver_for_run(_run_result(symbol), scan_run_id="initial"))
    assert first.sent == 0
    stay = _step(confirmed.record, _ready(), 2)
    stay_symbol = _symbol_from_transition(stay)
    stay_decision = telegram_alert_decision_for_symbol(stay_symbol)
    assert stay_decision.reason == "unchanged_lifecycle_state"
    _make_lifecycle_retry_due(db_path)
    sender.status = "sent"
    recovered = run(service.deliver_for_run(_run_result(stay_symbol), scan_run_id="recovery"))
    assert recovered.sent == 1
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        events = repository._connection.execute(
            "SELECT event_type, delivery_state FROM public_alert_events"
        ).fetchall()
        confirmed_sent = [
            item
            for item in repository.list_attempts()
            if item.alert_type == TelegramAlertType.SIGNAL_CONFIRMED.value
        ]
    assert any(row[0].lower() == "signal_confirmed" for row in events)
    assert len(confirmed_sent) == 1


def test_seeded_historically_oscillated_record_with_sent_confirmation_stays_readable(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "historical.sqlite"
    _, confirmed = _legit_confirmed()
    with SQLiteSetupLifecycleRepository(db_path) as lifecycle:
        lifecycle.upsert_record(confirmed.record)
        if confirmed.event is not None:
            lifecycle.insert_event(confirmed.event)
    sender = FakeSender()
    service = _service(db_path, sender)
    sent = run(service.deliver_for_run(_run_result(_symbol_from_transition(confirmed)), scan_run_id="hist-sent"))
    assert sent.sent == 1
    loaded_stay = _step(confirmed.record, _ready(), 2)
    assert loaded_stay.to_state == SetupLifecycleState.CONFIRMED
    repeated = run(service.deliver_for_run(_run_result(_symbol_from_transition(loaded_stay)), scan_run_id="hist-repeat"))
    assert repeated.sent == 0
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        confirmed_attempts = [
            item
            for item in repository.list_attempts()
            if item.alert_type == TelegramAlertType.SIGNAL_CONFIRMED.value
        ]
        assert len(confirmed_attempts) == 1
