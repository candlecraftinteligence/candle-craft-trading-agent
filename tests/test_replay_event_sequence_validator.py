from __future__ import annotations

import json
import socket

from app.backtesting.replay_event_sequence_validator import (
    REPLAY_EVENT_SEQUENCE_SCHEMA_VERSION,
    classify_sequence_event_type,
    normalize_sequence_status,
    replay_sequence_validation_result_to_dict,
    validate_replay_event_sequence,
    validate_replay_event_sequence_from_files,
)
from scripts import validate_replay_event_sequences as event_sequence_cli


def _event(status: str = "WATCH", **overrides) -> dict:
    data = {
        "setup_id": "setup-001",
        "symbol": "BTCUSDT",
        "strategy_mode": "swing",
        "direction": "long",
        "timestamp": "2026-05-31T00:00:00+00:00",
        "status": status,
    }
    data.update(overrides)
    return data


def _sequence(statuses: list[str], **overrides) -> list[dict]:
    events = []
    for index, status in enumerate(statuses):
        event = _event(
            status,
            timestamp=f"2026-05-31T00:{index:02d}:00+00:00",
            row_index=index,
            **overrides,
        )
        events.append(event)
    return events


def _issue_codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def _scanner_result(**overrides) -> dict:
    data = {
        "symbol": "BTCUSDT",
        "status": "scanned_no_setup",
        "status_history": ["scanned_no_setup"],
        "rejection_reason": "No deterministic setup.",
        "valid_strategy_modes": [],
        "rejected_strategy_modes": ["swing"],
        "strategy_diagnostics": {"swing": {"first_failed_gate": "N/A"}},
        "missing_data": [],
        "unverified_data": [],
    }
    data.update(overrides)
    return data


def _scanner_payload(result: dict | None = None) -> dict:
    return {
        "run_id": "run-001",
        "timestamp": "2026-05-31T00:00:00+00:00",
        "config": {
            "exchange": "binance",
            "market_type": "perpetual",
            "timeframe": "1h",
            "strategy_name": "liquidity_grab_pullback",
        },
        "results": [result or _scanner_result()],
    }


def test_status_normalization_common_variants() -> None:
    assert normalize_sequence_status("watching") == "WATCH"
    assert normalize_sequence_status("triggered") == "TRIGGERED"
    assert normalize_sequence_status("confirmed") == "CONFIRMED"
    assert normalize_sequence_status("tp1_hit") == "TP1_HIT"
    assert normalize_sequence_status("stopped") == "STOPPED"
    assert normalize_sequence_status("invalidated") == "INVALIDATED"
    assert normalize_sequence_status("cooldown") == "COOLDOWN"
    assert normalize_sequence_status("scanned_no_setup") == "SCANNED_NO_SETUP"


def test_event_type_classification_common_variants() -> None:
    assert classify_sequence_event_type(_event("trade_idea_created")) == "TRADE_IDEA_CREATED"
    assert classify_sequence_event_type(_event("alert_created")) == "ALERT_CREATED"
    assert classify_sequence_event_type(_event("journal_entry_created")) == "JOURNAL_ENTRY_CREATED"
    assert classify_sequence_event_type(_event("REJECTED")) == "NEGATIVE_EXAMPLE"


def test_minimal_event_validates_without_crash() -> None:
    result = validate_replay_event_sequence([_event()])

    assert result.schema_version == REPLAY_EVENT_SEQUENCE_SCHEMA_VERSION
    assert result.summary.total_events == 1
    assert result.summary.error_count == 0
    assert result.groups[0].sequence_ready is True


def test_no_setup_rejected_event_is_negative_example_and_not_error() -> None:
    result = validate_replay_event_sequence([_event("scanned_no_setup")])

    assert result.summary.negative_example_groups == 1
    assert result.summary.terminal_groups == 1
    assert result.summary.error_count == 0


def test_sane_watch_to_take_profit_sequence_has_no_warning() -> None:
    result = validate_replay_event_sequence(
        _sequence(["WATCH", "STALKING", "TRIGGERED", "CONFIRMED", "EXECUTING", "TP_HIT"])
    )

    assert result.summary.warning_count == 0
    assert result.groups[0].sequence_ready is True


def test_watch_to_take_profit_without_trigger_warns() -> None:
    result = validate_replay_event_sequence(_sequence(["WATCH", "TP_HIT"]))

    assert result.summary.error_count == 0
    assert "watch_to_tp_without_active_context" in _issue_codes(result)
    assert result.groups[0].sequence_ready is False


def test_rejected_to_executing_warns() -> None:
    result = validate_replay_event_sequence(_sequence(["REJECTED", "EXECUTING"]))

    assert result.summary.error_count == 0
    assert "rejected_to_executing" in _issue_codes(result)


def test_invalidated_to_executing_warns() -> None:
    result = validate_replay_event_sequence(_sequence(["INVALIDATED", "EXECUTING"]))

    assert result.summary.error_count == 0
    assert "invalidated_to_executing" in _issue_codes(result)


def test_alert_created_without_trade_idea_context_warns() -> None:
    result = validate_replay_event_sequence([_event("ALERT_CREATED", alert_id="alert-001")])

    assert result.summary.error_count == 0
    assert "alert_without_trade_idea_context" in _issue_codes(result)


def test_journal_entry_without_trade_or_alert_context_warns() -> None:
    result = validate_replay_event_sequence([_event("JOURNAL_ENTRY_CREATED", journal_entry_id="journal-001")])

    assert result.summary.error_count == 0
    assert "journal_without_trade_context" in _issue_codes(result)


def test_terminal_event_followed_by_executing_warns() -> None:
    result = validate_replay_event_sequence(_sequence(["TP_HIT", "EXECUTING"]))

    assert result.summary.error_count == 0
    assert "terminal_followed_by_executing" in _issue_codes(result)


def test_timestamp_decrease_warns() -> None:
    result = validate_replay_event_sequence(
        [
            _event("WATCH", timestamp="2026-05-31T00:02:00+00:00"),
            _event("STALKING", timestamp="2026-05-31T00:01:00+00:00"),
        ]
    )

    assert result.groups[0].ordering_method == "input_order_timestamp_decrease"
    assert result.summary.timestamp_order_issue_count == 1
    assert "timestamp_decrease" in _issue_codes(result)


def test_duplicate_event_identity_warns() -> None:
    result = validate_replay_event_sequence(
        [
            _event("WATCH", event_id="event-001"),
            _event("STALKING", event_id="event-001", timestamp="2026-05-31T00:01:00+00:00"),
        ]
    )

    assert result.summary.duplicate_event_count == 1
    assert "duplicate_event_identity" in _issue_codes(result)


def test_missing_timestamp_group_uses_input_order_fallback_and_warning() -> None:
    result = validate_replay_event_sequence([_event("WATCH", timestamp="N/A")])

    assert result.groups[0].ordering_method == "input_order_missing_timestamps"
    assert result.summary.groups_missing_timestamps == 1
    assert "missing_timestamps" in _issue_codes(result)


def test_missing_symbol_creates_warning() -> None:
    result = validate_replay_event_sequence([_event("WATCH", symbol="N/A")])

    assert result.summary.error_count == 0
    assert "missing_symbol" in _issue_codes(result)
    assert result.groups[0].sequence_ready is False


def test_unknown_status_creates_warning() -> None:
    result = validate_replay_event_sequence([_event("parked")])

    assert result.summary.error_count == 0
    assert "unknown_status" in _issue_codes(result)
    assert "unknown_event_type" in _issue_codes(result)


def test_grouping_uses_stable_identities_and_fallbacks_deterministically() -> None:
    result = validate_replay_event_sequence(
        [
            _event("WATCH", setup_id="setup-001"),
            _event("WATCH", setup_id="N/A", trade_idea_id="idea-001"),
            _event("WATCH", setup_id="N/A", trade_idea_id="N/A", alert_id="alert-001"),
            _event("WATCH", setup_id="N/A", run_id="run-001"),
            _event("WATCH", setup_id="N/A", run_id="N/A", scan_id="scan-001"),
            _event("WATCH", setup_id="N/A", run_id="N/A", scan_id="N/A", candidate_id="candidate-001"),
            _event(
                "WATCH",
                setup_id="N/A",
                run_id="N/A",
                scan_id="N/A",
                candidate_id="N/A",
                row_index=9,
            ),
        ]
    )
    keys = [group.group_key for group in result.groups]

    assert keys == [
        "setup_id:setup-001",
        "trade_idea_id:idea-001",
        "alert_id:alert-001",
        "run_symbol_mode:run-001|BTCUSDT|swing",
        "scan_symbol_mode:scan-001|BTCUSDT|swing",
        "candidate_id:candidate-001",
        "symbol_row:BTCUSDT|9",
    ]


def test_summary_counts_ready_and_not_ready_groups() -> None:
    result = validate_replay_event_sequence([_event("WATCH", setup_id="setup-001"), _event("WATCH", setup_id="setup-002", symbol="N/A")])

    assert result.summary.total_groups == 2
    assert result.summary.sequence_ready_groups == 1
    assert result.summary.sequence_not_ready_groups == 1


def test_result_dict_is_json_serializable() -> None:
    payload = replay_sequence_validation_result_to_dict(validate_replay_event_sequence([_event()]))

    encoded = json.dumps(payload, sort_keys=True)

    assert REPLAY_EVENT_SEQUENCE_SCHEMA_VERSION in encoded


def test_result_dict_does_not_contain_forbidden_performance_fields() -> None:
    payload = replay_sequence_validation_result_to_dict(validate_replay_event_sequence([_event()]))
    forbidden_key_fragments = ("win_rate", "pnl", "profit", "expectancy", "edge", "average_r", "profitability_score")

    assert not _contains_forbidden_key(payload, forbidden_key_fragments)


def test_validate_from_files_handles_invalid_json_without_crash(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    result = validate_replay_event_sequence_from_files([path])

    assert result.summary.error_count == 1
    assert "export_error" in _issue_codes(result)


def test_cli_default_human_mode_exits_zero_with_valid_local_artifact(tmp_path, capsys, monkeypatch) -> None:
    path = tmp_path / "latest_scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")
    monkeypatch.setattr(event_sequence_cli, "default_artifact_paths", lambda: (path,))

    exit_code = event_sequence_cli.main([])

    assert exit_code == 0
    assert "Historical Replay Event Sequence Validation" in capsys.readouterr().out


def test_cli_json_emits_valid_json(tmp_path, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = event_sequence_cli.main(["--input", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema_version"] == REPLAY_EVENT_SEQUENCE_SCHEMA_VERSION
    assert payload["summary"]["total_groups"] == 1


def test_cli_dry_run_with_output_does_not_create_file(tmp_path, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    output = tmp_path / "event_sequence_validation.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = event_sequence_cli.main(["--input", str(path), "--output", str(output), "--dry-run"])

    assert exit_code == 0
    assert not output.exists()
    assert "Historical Replay Event Sequence Validation" in capsys.readouterr().out


def test_cli_output_writes_only_requested_tmp_path(tmp_path) -> None:
    path = tmp_path / "latest_scan.json"
    output = tmp_path / "event_sequence_validation.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = event_sequence_cli.main(["--input", str(path), "--json", "--output", str(output)])

    assert exit_code == 0
    assert output.exists()
    assert json.loads(output.read_text(encoding="utf-8"))["summary"]["total_events"] == 1


def test_cli_strict_returns_nonzero_when_warnings_exist(tmp_path) -> None:
    path = tmp_path / "latest_scan.json"
    scanner_result = _scanner_result()
    scanner_result.pop("symbol")
    path.write_text(json.dumps(_scanner_payload(scanner_result)), encoding="utf-8")

    exit_code = event_sequence_cli.main(["--input", str(path), "--strict"])

    assert exit_code == 1


def test_no_network_exchange_or_telegram_calls_are_made(monkeypatch) -> None:
    def fail_network(*args, **kwargs):
        raise AssertionError("event sequence validation must not call network, exchange, or Telegram transports")

    monkeypatch.setattr(socket, "create_connection", fail_network)

    result = validate_replay_event_sequence([_event()])

    assert result.summary.error_count == 0


def _contains_forbidden_key(value, forbidden_key_fragments: tuple[str, ...]) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if any(fragment in str(key).lower() for fragment in forbidden_key_fragments):
                return True
            if _contains_forbidden_key(item, forbidden_key_fragments):
                return True
    elif isinstance(value, list):
        return any(_contains_forbidden_key(item, forbidden_key_fragments) for item in value)
    return False
