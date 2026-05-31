from __future__ import annotations

import json
import socket

from app.analytics.scan_persistence_audit import (
    audit_scan_persistence_artifact,
    audit_scan_persistence_file,
)
from scripts import audit_scan_persistence


def _scanner_payload(*, results: list[dict] | None = None) -> dict:
    return {
        "run_id": "scan-001",
        "timestamp": "2026-05-31T00:00:00+00:00",
        "config": {"exchange": "binance", "symbols": [{"symbol": "BTCUSDT"}]},
        "results": results
        if results is not None
        else [
            {
                "symbol": "BTCUSDT",
                "status": "idea_created",
                "status_history": ["idea_created"],
                "current_price": "100",
                "trade_idea": {"invalidation": "Invalid below 95.", "risk_warning": "Risk capital only."},
                "valid_strategy_modes": ["swing"],
                "rejected_strategy_modes": [],
                "strategy_diagnostics": {"swing": {"is_valid": True}},
                "missing_data": [],
                "unverified_data": [],
            }
        ],
    }


def _issue_codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def test_valid_scanner_run_with_config_results_has_no_errors() -> None:
    result = audit_scan_persistence_artifact(_scanner_payload())

    assert result.artifact_type == "scanner_run"
    assert result.is_valid is True
    assert result.error_count == 0
    assert result.result_count == 1
    assert result.symbol_count == 1
    assert result.status_counts == {"idea_created": 1}


def test_scanner_result_missing_symbol_produces_warning() -> None:
    payload = _scanner_payload(
        results=[
            {
                "status": "scanned_no_setup",
                "status_history": ["scanned_no_setup"],
                "rejection_reason": "No valid setup.",
            }
        ]
    )

    result = audit_scan_persistence_artifact(payload)

    assert result.error_count == 0
    assert "missing_symbol" in _issue_codes(result)


def test_scanner_alert_without_trade_idea_produces_warning() -> None:
    payload = _scanner_payload(
        results=[
            {
                "symbol": "BTCUSDT",
                "status": "alert_dry_run_created",
                "status_history": ["idea_created", "alert_dry_run_created"],
                "alert_result": {"status": "dry_run"},
            }
        ]
    )

    result = audit_scan_persistence_artifact(payload)

    assert result.error_count == 0
    assert "alert_without_trade_idea" in _issue_codes(result)


def test_scanned_no_setup_without_trade_idea_is_allowed() -> None:
    payload = _scanner_payload(
        results=[
            {
                "symbol": "BTCUSDT",
                "status": "scanned_no_setup",
                "status_history": ["scanned_no_setup"],
                "rejection_reason": "No valid setup.",
            }
        ]
    )

    result = audit_scan_persistence_artifact(payload)

    assert result.error_count == 0
    assert "trade_idea_on_no_setup" not in _issue_codes(result)
    assert "alert_without_trade_idea" not in _issue_codes(result)
    assert "journal_without_trade_idea" not in _issue_codes(result)


def test_missing_and_unverified_data_wrong_type_produces_warning() -> None:
    payload = _scanner_payload(
        results=[
            {
                "symbol": "BTCUSDT",
                "status": "scanned_no_setup",
                "status_history": ["scanned_no_setup"],
                "rejection_reason": "No valid setup.",
                "missing_data": "candles: N/A",
                "unverified_data": {"funding": "Unverified"},
            }
        ]
    )

    result = audit_scan_persistence_artifact(payload)

    assert result.error_count == 0
    assert "sequence_field_not_list" in _issue_codes(result)


def test_invalid_json_file_produces_error_and_cli_exit_code(tmp_path, capsys) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    result = audit_scan_persistence_file(path)
    exit_code = audit_scan_persistence.main([str(path)])

    assert result.artifact_type == "invalid_json"
    assert result.error_count == 1
    assert exit_code == 1
    assert "invalid_json" in capsys.readouterr().out


def test_watch_state_artifact_classification_works() -> None:
    payload = {
        "updated_at": "2026-05-31T00:00:00+00:00",
        "symbols": {
            "BTCUSDT": {
                "last_status": "near_miss",
                "readiness_label": "WATCH",
                "last_seen_at": "2026-05-31T00:00:00+00:00",
            }
        },
    }

    result = audit_scan_persistence_artifact(payload)

    assert result.artifact_type == "watch_state"
    assert result.error_count == 0
    assert result.symbol_count == 1
    assert result.status_counts == {"near_miss": 1}


def test_performance_memory_artifact_classification_works() -> None:
    payload = {
        "version": 1,
        "setup_stats": {
            "signature": {
                "total_occurrences": 1,
                "filled_occurrences": 1,
                "average_r": "1",
                "r_multiples": ["1"],
            }
        },
        "symbol_stats": {
            "BTCUSDT": {
                "symbol": "BTCUSDT",
                "total_occurrences": 1,
                "filled_occurrences": 1,
                "r_multiples": ["1"],
            }
        },
    }

    result = audit_scan_persistence_artifact(payload)

    assert result.artifact_type == "performance_memory"
    assert result.error_count == 0
    assert result.result_count == 1
    assert result.symbol_count == 1


def test_lifecycle_replay_readiness_warnings_are_produced() -> None:
    payload = {
        "config": {"exchange": "binance"},
        "results": [
            {
                "symbol": "BTCUSDT",
                "status": "scanned_no_setup",
                "status_history": ["scanned_no_setup"],
                "rejection_reason": "No valid setup.",
            }
        ],
    }

    result = audit_scan_persistence_artifact(payload)

    assert result.error_count == 0
    assert {"missing_run_id", "missing_run_timestamp"} <= _issue_codes(result)


def test_json_output_is_serializable(tmp_path, capsys) -> None:
    path = tmp_path / "scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = audit_scan_persistence.main(["--json", str(path)])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["summary"]["error_count"] == 0
    assert payload["results"][0]["artifact_type"] == "scanner_run"


def test_audit_does_not_require_network_calls(monkeypatch) -> None:
    def fail_network(*args, **kwargs):
        raise AssertionError("network should not be used")

    monkeypatch.setattr(socket, "create_connection", fail_network)

    result = audit_scan_persistence_artifact(_scanner_payload())

    assert result.error_count == 0
