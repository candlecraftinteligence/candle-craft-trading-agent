from __future__ import annotations

import json
import socket

from app.analytics.replay_dataset_export import ReplayDatasetRow
from app.analytics.replay_dataset_quality import (
    analyze_replay_dataset_files,
    analyze_replay_rows,
    quality_result_to_dict,
)
from scripts import analyze_replay_dataset_quality


def _row(**overrides) -> ReplayDatasetRow:
    data = {
        "source": "scan.json",
        "artifact_type": "scanner_run",
        "row_type": "scanner_result",
        "row_id": "row-001",
        "run_id": "run-001",
        "scan_timestamp": "2026-05-31T00:00:00+00:00",
        "symbol": "BTCUSDT",
        "status": "CONFIRMED",
        "normalized_lifecycle_status": "CONFIRMED",
        "strategy_name": "liquidity_grab_pullback",
        "strategy_mode": "swing",
        "direction": "long",
        "replay_ready": True,
    }
    data.update(overrides)
    return ReplayDatasetRow(**data)


def _scanner_result(**overrides) -> dict:
    data = {
        "symbol": "BTCUSDT",
        "status": "idea_created",
        "status_history": ["idea_created"],
        "valid_strategy_modes": ["swing"],
        "rejected_strategy_modes": [],
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
        "config": {"exchange": "binance", "timeframe": "1h", "strategy_name": "liquidity_grab_pullback"},
        "results": [result or _scanner_result()],
    }


def _issue_codes(result) -> set[str]:
    return {issue.code for issue in result.issues}


def _field(result, field_name: str):
    return next(quality for quality in result.summary.field_quality if quality.field_name == field_name)


def test_empty_rows_produce_warning_and_quality_score_zero() -> None:
    result = analyze_replay_rows([])

    assert result.error_count == 0
    assert result.warning_count == 1
    assert result.summary.total_rows == 0
    assert result.summary.quality_score == 0
    assert "no_rows" in _issue_codes(result)


def test_valid_minimal_rows_produce_metrics_with_no_errors() -> None:
    result = analyze_replay_rows([_row()])

    assert result.error_count == 0
    assert result.warning_count == 0
    assert result.summary.total_rows == 1
    assert result.summary.symbols == ("BTCUSDT",)
    assert result.summary.status_counts == {"CONFIRMED": 1}
    assert result.summary.quality_score > 0


def test_replay_ready_rate_is_computed_correctly() -> None:
    result = analyze_replay_rows([_row(row_id="row-001"), _row(row_id="row-002", replay_ready=False)])

    assert result.summary.replay_ready_rows == 1
    assert result.summary.replay_not_ready_rows == 1
    assert result.summary.replay_ready_rate == 0.5


def test_no_setup_rows_are_counted_but_not_treated_as_failure() -> None:
    result = analyze_replay_rows(
        [
            _row(
                status="scanned_no_setup",
                normalized_lifecycle_status="scanned_no_setup",
                rejection_reason="No deterministic setup.",
            )
        ]
    )

    assert result.error_count == 0
    assert result.summary.no_setup_rows == 1
    assert "terminal_rows_without_result_or_outcome" not in _issue_codes(result)


def test_trade_idea_alert_and_journal_presence_counts_are_computed() -> None:
    result = analyze_replay_rows(
        [
            _row(
                trade_idea_present=True,
                alert_present=True,
                journal_entry_present=True,
                trade_idea_id="idea-001",
                alert_id="alert-001",
                journal_entry_id="journal-001",
            )
        ]
    )

    assert result.summary.trade_idea_rows == 1
    assert result.summary.alert_rows == 1
    assert result.summary.journal_entry_rows == 1


def test_field_completeness_counts_na_as_not_complete() -> None:
    result = analyze_replay_rows(
        [
            {"row_id": "row-001", "run_id": "run-001", "symbol": "BTCUSDT", "status": "CONFIRMED", "replay_ready": True},
            {"row_id": "row-002", "run_id": "run-001", "symbol": "N/A", "status": "CONFIRMED", "replay_ready": False},
        ]
    )

    symbol_quality = _field(result, "symbol")

    assert symbol_quality.present_count == 2
    assert symbol_quality.missing_count == 1
    assert symbol_quality.na_count == 1
    assert symbol_quality.completeness_rate == 0.5
    assert symbol_quality.missing_row_indices == (1,)


def test_readiness_warning_counts_aggregate_correctly() -> None:
    result = analyze_replay_rows(
        [
            _row(row_id="row-001", replay_readiness_warnings=("symbol missing.", "timestamp missing.")),
            _row(row_id="row-002", replay_readiness_warnings=("symbol missing.",)),
        ]
    )

    assert result.summary.readiness_warning_counts == {"symbol missing.": 2, "timestamp missing.": 1}


def test_duplicate_row_identities_produce_warning() -> None:
    result = analyze_replay_rows([_row(row_id="duplicate"), _row(row_id="duplicate")])

    assert result.summary.duplicate_row_identity_count == 1
    assert result.summary.duplicate_row_identity_examples
    assert "duplicate_row_identities" in _issue_codes(result)


def test_terminal_rows_without_result_or_outcome_produce_warning() -> None:
    result = analyze_replay_rows([_row(status="SL_HIT", normalized_lifecycle_status="SL_HIT")])

    assert result.summary.terminal_outcome_rows == 1
    assert result.summary.terminal_rows_without_result_or_outcome == 1
    assert "terminal_rows_without_result_or_outcome" in _issue_codes(result)


def test_missing_symbol_timestamp_and_identifier_produce_warnings() -> None:
    result = analyze_replay_rows(
        [
            {
                "source": "scan.json",
                "artifact_type": "scanner_run",
                "row_type": "scanner_result",
                "row_id": "row-001",
                "symbol": "N/A",
                "scan_timestamp": "N/A",
                "status": "CONFIRMED",
                "normalized_lifecycle_status": "CONFIRMED",
                "replay_ready": False,
            }
        ]
    )

    assert {"high_symbol_missingness", "high_timestamp_missingness", "high_identifier_missingness"} <= _issue_codes(result)


def test_quality_result_to_dict_is_json_serializable() -> None:
    payload = quality_result_to_dict(analyze_replay_rows([_row()]))

    encoded = json.dumps(payload, sort_keys=True)

    assert "replay_dataset_quality_v1" in encoded


def test_cli_default_human_mode_exits_zero_with_valid_artifacts(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")
    monkeypatch.setattr(analyze_replay_dataset_quality, "default_artifact_paths", lambda: (path,))

    exit_code = analyze_replay_dataset_quality.main([])

    assert exit_code == 0
    assert "Replay Dataset Quality Metrics" in capsys.readouterr().out


def test_cli_json_emits_valid_json(tmp_path, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = analyze_replay_dataset_quality.main(["--input", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema_version"] == "replay_dataset_quality_v1"
    assert payload["summary"]["total_rows"] == 1


def test_cli_strict_returns_nonzero_when_warnings_exist(tmp_path) -> None:
    path = tmp_path / "latest_scan.json"
    result = _scanner_result()
    result.pop("symbol")
    path.write_text(json.dumps(_scanner_payload(result)), encoding="utf-8")

    exit_code = analyze_replay_dataset_quality.main(["--input", str(path), "--strict"])

    assert exit_code == 1


def test_invalid_jsonl_input_returns_error(tmp_path) -> None:
    path = tmp_path / "dataset.jsonl"
    path.write_text('{"row_id": "ok"}\n{not json\n', encoding="utf-8")

    result = analyze_replay_dataset_files([path])

    assert result.error_count == 1
    assert "invalid_jsonl" in _issue_codes(result)


def test_csv_input_returns_clear_warning_without_crashing(tmp_path) -> None:
    path = tmp_path / "dataset.csv"
    path.write_text("row_id,symbol\nrow-001,BTCUSDT\n", encoding="utf-8")

    result = analyze_replay_dataset_files([path])

    assert result.error_count == 0
    assert "unsupported_csv_input" in _issue_codes(result)


def test_no_network_exchange_or_telegram_calls_are_made(monkeypatch) -> None:
    def fail_network(*args, **kwargs):
        raise AssertionError("quality metrics must not call network, exchange, or Telegram transports")

    monkeypatch.setattr(socket, "create_connection", fail_network)

    result = analyze_replay_rows([_row()])

    assert result.error_count == 0
