from __future__ import annotations

import json
import socket

from app.analytics.lifecycle_replay_audit import (
    audit_lifecycle_artifact,
    audit_lifecycle_file,
    normalize_lifecycle_status,
)
from scripts import audit_lifecycle_replay


def _record(**overrides) -> dict:
    data = {
        "run_id": "run-001",
        "setup_id": "setup-001",
        "symbol": "BTCUSDT",
        "timestamp": "2026-05-31T00:00:00+00:00",
        "status": "CONFIRMED",
        "status_history": ["WATCH", "STALKING", "TRIGGERED", "CONFIRMED"],
        "mode": "swing",
        "direction": "long",
        "entry": "100",
        "invalidation": "Invalid below 95.",
        "tp1": "110",
    }
    data.update(overrides)
    return data


def _issue_codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_status_normalization_common_variants() -> None:
    assert normalize_lifecycle_status("watching") == "WATCH"
    assert normalize_lifecycle_status("triggered") == "TRIGGERED"
    assert normalize_lifecycle_status("confirmed") == "CONFIRMED"
    assert normalize_lifecycle_status("tp1_hit") == "TP1_HIT"
    assert normalize_lifecycle_status("stopped") == "STOPPED"
    assert normalize_lifecycle_status("invalidated") == "INVALIDATED"
    assert normalize_lifecycle_status("cooldown") == "COOLDOWN"
    assert normalize_lifecycle_status("scanned_no_setup") == "scanned_no_setup"


def test_valid_watch_to_confirmed_sequence_has_no_transition_warning() -> None:
    result = audit_lifecycle_artifact([_record()])

    assert result.error_count == 0
    assert "suspicious_transition" not in _issue_codes(result)
    assert "valid_transition_sequence" in _issue_codes(result)


def test_watch_to_take_profit_without_trigger_or_confirm_warns() -> None:
    result = audit_lifecycle_artifact(
        [
            _record(
                status="TP_HIT",
                status_history=["WATCH", "TP_HIT"],
                outcome="tp_hit",
                result_r="2.0",
            )
        ]
    )

    assert result.error_count == 0
    assert {"suspicious_transition", "terminal_without_trigger"} <= _issue_codes(result)


def test_status_history_not_list_is_error() -> None:
    result = audit_lifecycle_artifact([_record(status_history="WATCH")])

    assert result.error_count == 1
    assert "status_history_not_list" in _issue_codes(result)


def test_missing_symbol_warns() -> None:
    record = _record()
    record.pop("symbol")

    result = audit_lifecycle_artifact([record])

    assert result.error_count == 0
    assert "missing_symbol" in _issue_codes(result)


def test_missing_stable_identifier_warns() -> None:
    record = _record()
    record.pop("run_id")
    record.pop("setup_id")

    result = audit_lifecycle_artifact([record])

    assert result.error_count == 0
    assert "missing_stable_identifier" in _issue_codes(result)


def test_missing_timestamp_warns() -> None:
    record = _record()
    record.pop("timestamp")

    result = audit_lifecycle_artifact([record])

    assert result.error_count == 0
    assert "missing_timestamp" in _issue_codes(result)


def test_terminal_status_without_result_or_outcome_warns() -> None:
    result = audit_lifecycle_artifact(
        [
            _record(
                status="SL_HIT",
                status_history=["WATCH", "STALKING", "TRIGGERED", "CONFIRMED", "EXECUTING", "SL_HIT"],
            )
        ]
    )

    assert result.error_count == 0
    assert "missing_terminal_outcome" in _issue_codes(result)


def test_rejected_no_setup_does_not_fail_merely_for_rejection() -> None:
    result = audit_lifecycle_artifact(
        [
            _record(
                status="rejected",
                status_history=["rejected"],
                rejection_reason="No deterministic setup.",
                direction=None,
                mode=None,
                entry=None,
                invalidation=None,
                tp1=None,
            )
        ]
    )

    assert result.error_count == 0
    assert "suspicious_transition" not in _issue_codes(result)
    assert "missing_terminal_outcome" not in _issue_codes(result)


def test_unknown_status_produces_warning() -> None:
    result = audit_lifecycle_artifact([_record(status="parked", status_history=["parked"])])

    assert result.error_count == 0
    assert "unknown_status" in _issue_codes(result)


def test_audit_lifecycle_file_handles_invalid_json_as_error(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    result = audit_lifecycle_file(path)

    assert result.error_count == 1
    assert "invalid_json" in _issue_codes(result)


def test_cli_json_output_is_valid_json(tmp_path, capsys) -> None:
    path = tmp_path / "lifecycle.json"
    path.write_text(json.dumps([_record()]), encoding="utf-8")

    exit_code = audit_lifecycle_replay.main(["--json", str(path)])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["summary"]["error_count"] == 0
    assert payload["results"][0]["record_count"] == 1


def test_strict_mode_returns_nonzero_when_warnings_exist(tmp_path, capsys) -> None:
    path = tmp_path / "lifecycle.json"
    record = _record()
    record.pop("symbol")
    path.write_text(json.dumps([record]), encoding="utf-8")

    exit_code = audit_lifecycle_replay.main(["--strict", str(path)])

    assert exit_code == 1
    assert "missing_symbol" in capsys.readouterr().out


def test_audit_does_not_require_network_exchange_or_telegram_calls(monkeypatch) -> None:
    def fail_call(*args, **kwargs):
        raise AssertionError("audit must not call network, exchange, or Telegram transports")

    monkeypatch.setattr(socket, "create_connection", fail_call)

    result = audit_lifecycle_artifact([_record()])

    assert result.error_count == 0
