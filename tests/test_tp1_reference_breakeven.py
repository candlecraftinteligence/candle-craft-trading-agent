from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.alerts.telegram_lifecycle import _canonical_public_outcome_sequence
from app.formatters.telegram_signal_formatter import TelegramAlertType, TelegramSignalMessage, format_telegram_signal_message
from app.lifecycle.models import SetupLifecycleState, SetupTransitionReason
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.research.evaluation_policy import build_runtime_evaluation_policy
from test_lifecycle_outcomes import _prime, _entry, _target, _candle, _evaluate, _decision, _stop


@pytest.mark.parametrize('direction', ['long', 'short'])
@pytest.mark.parametrize('targets', [1, 2])
def test_reference_stop_closes_before_original_stop_and_survives_restart(tmp_path: Path, direction: str, targets: int):
    path = tmp_path / 'be.sqlite'
    with SQLiteSetupLifecycleRepository(path) as repo:
        record, candles, _ = _prime(repo, direction=direction)
        original_stop = record.stop_loss
        candles.append(_entry(1, direction))
        result = _evaluate(repo, record, candles)
        for target in range(1, targets + 1):
            candles.append(_target(len(candles), direction, target))
            result = _evaluate(repo, result.record, candles)
        identity = result.progress.plan_identity
    # Cross 101 without reaching the original stop (90 long / 112 short).
    with SQLiteSetupLifecycleRepository(path) as repo:
        record = repo.get_record_by_lifecycle_id(record.lifecycle_id)
        candles.append(_candle(len(candles), high='105' if direction == 'long' else '101', low='101' if direction == 'long' else '97'))
        result = _evaluate(repo, record, candles)
        assert result.record.current_state == SetupLifecycleState.BREAKEVEN
        assert result.progress.terminal_outcome == 'BREAKEVEN'
        assert result.record.stop_loss == original_stop
        assert result.progress.plan_identity == identity
        assert result.progress.tp1_at is not None
        assert (result.progress.tp2_at is not None) == (targets == 2)
        repeated = _evaluate(repo, result.record, candles)
        assert repeated.progress.model_dump() == result.progress.model_dump()
        assert sum(e.reason == SetupTransitionReason.BREAKEVEN_STOP_HIT for e in repo.list_events(lifecycle_id=record.lifecycle_id)) == 1
        sequence = dict(_canonical_public_outcome_sequence(result.progress))
        assert TelegramAlertType.BREAKEVEN in sequence
        assert TelegramAlertType.SL_HIT not in sequence
        assert TelegramAlertType.TP1_HIT in sequence


@pytest.mark.parametrize('direction', ['long', 'short'])
def test_no_protection_before_tp1_and_same_candle_tp1_original_stop_is_loss(tmp_path, direction):
    with SQLiteSetupLifecycleRepository(tmp_path / 'ambiguous.sqlite') as repo:
        record, candles, _ = _prime(repo, direction=direction)
        candles.append(_entry(1, direction))
        result = _evaluate(repo, record, candles)
        candles.append(_candle(2, high='131', low='75'))
        result = _evaluate(repo, result.record, candles)
        assert result.progress.terminal_outcome == 'SL_HIT'
        assert result.progress.tp1_at is None


@pytest.mark.parametrize('direction', ['long', 'short'])
def test_active_breakeven_wins_over_later_target_on_ambiguous_candle(tmp_path, direction):
    with SQLiteSetupLifecycleRepository(tmp_path / 'be_ambiguous.sqlite') as repo:
        record, candles, _ = _prime(repo, direction=direction)
        candles.extend([_entry(1, direction), _target(2, direction, 1)])
        result = _evaluate(repo, record, candles)
        candles.append(_candle(3, high='140', low='70'))
        result = _evaluate(repo, result.record, candles)
        assert result.progress.terminal_outcome == 'BREAKEVEN'
        assert result.progress.tp2_at is None
        assert result.progress.tp3_at is None


def _legacy_progress(repo, progress):
    metadata = json.loads(progress.metadata_json)
    metadata.pop('stop_management')
    legacy = progress.model_copy(update={'metadata_json': json.dumps(metadata)})
    repo.upsert_outcome_progress(legacy)
    return legacy


def test_existing_active_adopts_once_without_new_candle_then_stops_at_reference(tmp_path):
    with SQLiteSetupLifecycleRepository(tmp_path / 'adopt.sqlite') as repo:
        record, candles, _ = _prime(repo)
        candles.extend([_entry(1, 'long'), _target(2, 'long', 1)])
        reached = _evaluate(repo, record, candles)
        legacy = _legacy_progress(repo, reached.progress)
        adopted = _evaluate(repo, reached.record, candles)
        binding = json.loads(adopted.progress.metadata_json)['stop_management']
        assert binding['prior_policy'] == 'fixed_original_stop'
        assert binding['effective_from_candle_open_at'] == _decision(2)
        assert adopted.progress.evaluation_cursor_open_at == legacy.evaluation_cursor_open_at
        candles.append(_candle(3, high='105', low='100'))
        result = _evaluate(repo, adopted.record, candles)
        assert result.progress.terminal_outcome == 'BREAKEVEN'
        assert json.loads(result.progress.metadata_json)['stop_management'] == binding


def test_adoption_does_not_reclassify_pending_candles_before_cutoff(tmp_path):
    with SQLiteSetupLifecycleRepository(tmp_path / 'pending.sqlite') as repo:
        record, candles, _ = _prime(repo)
        candles.extend([_entry(1, 'long'), _target(2, 'long', 1)])
        reached = _evaluate(repo, record, candles)
        _legacy_progress(repo, reached.progress)
        candles.append(_stop(3, 'long'))
        result = _evaluate(repo, reached.record, candles)
        assert result.progress.terminal_outcome == 'SL_HIT'
        assert result.progress.tp1_at == reached.progress.tp1_at
        frozen = result.progress
        assert _evaluate(repo, result.record, candles).progress.model_dump() == frozen.model_dump()


def test_unknown_policy_fails_closed_without_cursor_advance(tmp_path):
    with SQLiteSetupLifecycleRepository(tmp_path / 'bad.sqlite') as repo:
        record, candles, progress = _prime(repo)
        metadata = json.loads(progress.metadata_json)
        metadata['stop_management']['policy'] = 'future-version'
        repo.upsert_outcome_progress(progress.model_copy(update={'metadata_json': json.dumps(metadata)}))
        candles.append(_entry(1, 'long'))
        result = _evaluate(repo, record, candles)
        assert result.progress.evaluation_cursor_open_at == progress.evaluation_cursor_open_at
        assert result.progress.integrity_status == 'Failed'
        assert result.progress.entry_at is None


def test_completed_historical_sl_with_tp1_is_not_rewritten(tmp_path):
    with SQLiteSetupLifecycleRepository(tmp_path / 'closed_history.sqlite') as repo:
        record, candles, _ = _prime(repo)
        candles.extend([_entry(1, 'long'), _target(2, 'long', 1)])
        reached = _evaluate(repo, record, candles)
        legacy = _legacy_progress(repo, reached.progress).model_copy(update={
            'terminal_outcome': 'SL_HIT', 'stop_at': _decision(3), 'outcome_at': _decision(3),
        })
        closed = reached.record.model_copy(update={'current_state': SetupLifecycleState.SL_HIT})
        repo.upsert_record(closed)
        repo.upsert_outcome_progress(legacy)
        legacy = repo.get_outcome_progress(lifecycle_id=closed.lifecycle_id, plan_identity=legacy.plan_identity)
        assert legacy is not None
        candles.append(_stop(3, 'long'))
        before_events = repo.list_events(lifecycle_id=record.lifecycle_id)
        result = _evaluate(repo, closed, candles)
        assert result.progress.model_dump() == legacy.model_dump()
        assert 'stop_management' not in json.loads(result.progress.metadata_json)
        assert repo.list_events(lifecycle_id=record.lifecycle_id) == before_events


@pytest.mark.parametrize('direction', ['long', 'short'])
def test_first_tp1_candle_does_not_invent_intrabar_breakeven_sequence(tmp_path, direction):
    with SQLiteSetupLifecycleRepository(tmp_path / 'same_bar.sqlite') as repo:
        record, candles, _ = _prime(repo, direction=direction)
        candles.append(_entry(1, direction))
        entered = _evaluate(repo, record, candles)
        candles.append(_candle(2, high='111' if direction == 'long' else '102', low='100' if direction == 'long' else '91'))
        result = _evaluate(repo, entered.record, candles)
        assert result.progress.tp1_at == _decision(2)
        assert result.progress.stop_at is None
        assert result.progress.terminal_outcome == 'N/A'
        candles.append(_candle(3, high='105', low='99'))
        assert _evaluate(repo, result.record, candles).progress.terminal_outcome == 'BREAKEVEN'


@pytest.mark.parametrize('kind', [TelegramAlertType.TP1_HIT, TelegramAlertType.TP2_HIT, TelegramAlertType.BREAKEVEN])
def test_public_updates_use_midpoint_reference_and_no_original_sl_loss(kind):
    message = TelegramSignalMessage(symbol='BTCUSDT', entry_low='100', entry_high='102', stop_loss='90', tp1='110', tp2='120', tp3='130')
    text = format_telegram_signal_message(kind, message)
    assert '101' in text
    assert 'entry-zone midpoint' in text
    assert 'not your actual fill' in text
    if kind == TelegramAlertType.TP1_HIT:
        assert 'Move SL to breakeven.' in text
    elif kind == TelegramAlertType.BREAKEVEN:
        assert 'BREAKEVEN' in text
        assert 'Result: SL' not in text
        assert 'SL 90' not in text


def test_policy_explicitly_versions_reference_breakeven():
    manifest = build_runtime_evaluation_policy(execution_timeframe='5m').to_canonical_dict()
    assert manifest['rule_vocabulary_version'] == 'cci-runtime-closed-candle-rules-v2'
    assert manifest['rules']['stop_management']['terminal_label'] == 'BREAKEVEN'


@pytest.mark.parametrize('targets', [1, 2])
def test_breakeven_public_delivery_is_persisted_once_and_closes_tracking(tmp_path, targets):
    from app.alerts.telegram_lifecycle import SQLiteTelegramAlertAttemptRepository
    from app.telegram_admin.active_watchlists import _canonical_outcome_alert_types, load_active_public_signals
    from test_public_tp_milestone_delivery import _send_confirmed_root
    from test_triggered_confirmed_telegram_delivery import FakeSender, _service, _empty_run_result, run

    path = tmp_path / 'public_be.sqlite'
    sender = FakeSender()
    symbol = _send_confirmed_root(path, sender)
    record, progress = _public_breakeven_progress(path, symbol, targets)
    with SQLiteSetupLifecycleRepository(path) as repo:
        repo.upsert_record(record)
        repo.upsert_outcome_progress(progress)
    first = run(_service(path, sender).deliver_for_run(_empty_run_result()))
    repeated = run(_service(path, sender).deliver_for_run(_empty_run_result()))
    assert first.sent == 2  # highest reached TP, then distinct reference-stop exit
    assert repeated.sent == 0
    with SQLiteTelegramAlertAttemptRepository(path) as repo:
        sent = [item for item in repo.list_attempts() if item.telegram_status == 'sent']
        be = [item for item in sent if item.alert_type == 'BREAKEVEN']
        assert len(be) == 1
        assert all(item.alert_type != 'SL_HIT' for item in sent)
        assert repo.has_sent_terminal_outcome(signal_id=be[0].signal_id)
    types = _canonical_outcome_alert_types(progress.model_dump())
    assert 'BREAKEVEN' in types
    assert 'SL_HIT' not in types
    dashboard = load_active_public_signals(project_root=tmp_path, database_path=path, limit=10)
    assert dashboard.source_available
    assert dashboard.total == 0


def test_public_breakeven_without_policy_evidence_is_blocked(tmp_path):
    from test_public_tp_milestone_delivery import _send_confirmed_root, _persist_progress
    from test_triggered_confirmed_telegram_delivery import FakeSender, _service, _empty_run_result, run

    path = tmp_path / 'unverified_be.sqlite'
    sender = FakeSender()
    symbol = _send_confirmed_root(path, sender)
    record, progress = _persist_progress(path, symbol, tp_count=1, stop=True)
    with SQLiteSetupLifecycleRepository(path) as repo:
        repo.upsert_record(record.model_copy(update={'current_state': SetupLifecycleState.BREAKEVEN}))
        repo.upsert_outcome_progress(progress.model_copy(update={'terminal_outcome': 'BREAKEVEN'}))
    assert run(_service(path, sender).deliver_for_run(_empty_run_result())).sent == 0


@pytest.mark.parametrize('transport_state', ['RETRYABLE', 'UNCERTAIN'])
def test_breakeven_transport_recovery_preserves_delivery_state(tmp_path, transport_state):
    from app.alerts.telegram_lifecycle import SQLiteTelegramAlertAttemptRepository
    from test_public_tp_milestone_delivery import _send_confirmed_root
    from test_triggered_confirmed_telegram_delivery import (
        TransportStateSequenceSender, _make_lifecycle_retry_due, _service, _empty_run_result, run,
    )

    path = tmp_path / 'be_transport.sqlite'
    sender = TransportStateSequenceSender('SENT', 'SENT', transport_state, 'SENT')
    symbol = _send_confirmed_root(path, sender)
    record, progress = _public_breakeven_progress(path, symbol, 1)
    with SQLiteSetupLifecycleRepository(path) as repo:
        repo.upsert_record(record)
        repo.upsert_outcome_progress(progress)
    first = run(_service(path, sender).deliver_for_run(_empty_run_result()))
    assert first.sent == 1  # TP1 was delivered; BE transport did not confirm success.
    if transport_state == 'RETRYABLE':
        _make_lifecycle_retry_due(path)
    retry = run(_service(path, sender).deliver_for_run(_empty_run_result()))
    assert retry.sent == (1 if transport_state == 'RETRYABLE' else 0)
    assert run(_service(path, sender).deliver_for_run(_empty_run_result())).sent == 0
    with SQLiteTelegramAlertAttemptRepository(path) as repo:
        attempts = [item for item in repo.list_attempts() if item.alert_type == 'BREAKEVEN']
        assert len(attempts) == 1
        assert attempts[0].delivery_state == ('SENT' if transport_state == 'RETRYABLE' else 'UNCERTAIN')


def _public_breakeven_progress(path, symbol, targets):
    from app.lifecycle.breakeven import bind_stop_policy
    from app.lifecycle.outcome_policy import stored_plan_geometry
    from test_public_tp_milestone_delivery import _persist_progress

    record, entered = _persist_progress(path, symbol, tp_count=0)
    # The shared delivery fixture defaults to a later cursor; represent the
    # actual entry-time snapshot before establishing this policy's boundary.
    entered = entered.model_copy(update={
        'evaluation_cursor_open_at': json.loads(entered.metadata_json)['causal_candle_open'],
        'evaluation_cursor_close_at': entered.entry_at,
        'last_evaluated_at': entered.entry_at,
    })
    adopted = bind_stop_policy(entered, stored_plan_geometry(record),
                               decision_timestamp=entered.entry_at, evaluated_at=entered.entry_at)
    with SQLiteSetupLifecycleRepository(path) as repo:
        repo.upsert_outcome_progress(adopted)
    record, progress = _persist_progress(path, symbol, tp_count=targets, stop=True)
    metadata = json.loads(progress.metadata_json)
    metadata['stop_management'] = json.loads(adopted.metadata_json)['stop_management']
    return (
        record.model_copy(update={'current_state': SetupLifecycleState.BREAKEVEN}),
        progress.model_copy(update={'terminal_outcome': 'BREAKEVEN', 'metadata_json': json.dumps(metadata)}),
    )


@pytest.mark.no_auto_epoch
def test_owner_monitoring_breakeven_replays_offline_and_is_not_a_research_loss(tmp_path):
    from app.research.durable_source_replay.capture import CaptureConfig, use_capture, reset_capture_process_state
    from app.research.durable_source_replay.constants import REPLAY_MATCH
    from app.research.queries import build_research_report
    from tests.runtime_epoch_support import SYNTHETIC_IDENTITY
    from test_f04_durable_source_replay import (
        _operational, _own, _latched, _monitor, _capture_ids, _replay,
        _candle as capture_candle, _decision as capture_decision,
    )

    reset_capture_process_state()
    try:
        path = _operational(tmp_path)
        record = _own(path, _latched())
        evidence = tmp_path / 'evidence.sqlite'
        candles = [capture_candle(0, high='103', low='99'),
                   capture_candle(1, high='111', low='103'),
                   capture_candle(2, high='105', low='101')]
        # Adopt at initial tracking, before the later target and reference stop.
        _monitor(path, record, candles[:1], when=capture_decision(0))
        with use_capture(CaptureConfig(enabled=True, evidence_path=evidence, operational_paths=(path,))):
            _monitor(path, record, candles, when=capture_decision(2))
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repo:
            stored = repo.get_record_by_lifecycle_id(record.lifecycle_id)
            assert stored.current_state == SetupLifecycleState.BREAKEVEN
            assert stored.plan_version_id == record.plan_version_id
        ids = _capture_ids(evidence)
        assert len(ids) == 1
        report = _replay(evidence, ids[0], tmp_path / 'offline')
        assert report['status'] == REPLAY_MATCH, report
        assert report['claims']['expectancy'] is False
        research = build_research_report(path, query='lifecycle_conversion')
        counts = research['conversion_counts']
        assert counts['EXECUTING_to_BREAKEVEN']['to_count'] == 1
        assert counts['EXECUTING_to_SL_HIT']['to_count'] == 0
    finally:
        reset_capture_process_state()
