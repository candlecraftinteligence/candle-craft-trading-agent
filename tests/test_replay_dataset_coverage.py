from __future__ import annotations

import json
import socket

from app.analytics.replay_dataset_coverage import (
    analyze_replay_dataset_coverage,
    analyze_replay_export_coverage_from_files,
    classify_lifecycle_bucket,
    classify_setup_research_bucket,
    coverage_result_to_dict,
)
from app.analytics.replay_dataset_export import ReplayDatasetRow
from scripts import analyze_replay_dataset_coverage as coverage_cli


def _row(**overrides) -> ReplayDatasetRow:
    data = {
        "source": "scan.json",
        "artifact_type": "scanner_run",
        "row_type": "scanner_result",
        "row_id": "row-001",
        "run_id": "run-001",
        "scan_timestamp": "2026-05-31T00:00:00+00:00",
        "symbol": "BTCUSDT",
        "exchange": "binance",
        "market_type": "perpetual",
        "timeframe": "1h",
        "strategy_name": "liquidity_grab_pullback",
        "strategy_mode": "swing",
        "direction": "long",
        "status": "CONFIRMED",
        "normalized_lifecycle_status": "CONFIRMED",
        "replay_ready": True,
    }
    data.update(overrides)
    return ReplayDatasetRow(**data)


def _terminal_trade_row(**overrides) -> ReplayDatasetRow:
    data = {
        "status": "TP_HIT",
        "normalized_lifecycle_status": "TP_HIT",
        "trade_idea_present": True,
        "trade_idea_id": "idea-001",
        "result_r": "1.5",
        "outcome_status": "tp_hit",
    }
    data.update(overrides)
    return _row(**data)


def _scanner_result(**overrides) -> dict:
    data = {
        "symbol": "BTCUSDT",
        "status": "TP_HIT",
        "status_history": ["WATCH", "TRIGGERED", "CONFIRMED", "TP_HIT"],
        "trade_idea": {"trade_idea_id": "idea-001", "direction": "long", "invalidation": "Invalid below 95."},
        "replay_result": {"result_r": "1.5", "outcome_status": "tp_hit"},
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


def _gap_codes(result) -> set[str]:
    return {gap.code for gap in result.gaps}


def _dimension(result, name: str):
    return next(dimension for dimension in result.dimensions if dimension.dimension_name == name)


def test_empty_rows_produce_warning_and_no_crash() -> None:
    result = analyze_replay_dataset_coverage([])

    assert result.error_count == 0
    assert result.warning_count > 0
    assert result.summary.total_rows == 0
    assert "no_rows" in _gap_codes(result)


def test_valid_minimal_rows_produce_dimensions_and_no_errors() -> None:
    result = analyze_replay_dataset_coverage([_terminal_trade_row()])

    assert result.error_count == 0
    assert result.summary.total_rows == 1
    assert _dimension(result, "symbol").buckets[0].key == "BTCUSDT"
    assert _dimension(result, "lifecycle_bucket").buckets[0].key == "terminal_tp"


def test_lifecycle_bucket_classification_for_known_statuses() -> None:
    assert classify_lifecycle_bucket("WATCH") == "watch"
    assert classify_lifecycle_bucket("TRIGGERED") == "triggered"
    assert classify_lifecycle_bucket("CONFIRMED") == "confirmed"
    assert classify_lifecycle_bucket("TP_HIT") == "terminal_tp"
    assert classify_lifecycle_bucket("SL_HIT") == "terminal_sl"
    assert classify_lifecycle_bucket("INVALIDATED") == "invalidated"
    assert classify_lifecycle_bucket("COOLDOWN") == "cooldown"
    assert classify_lifecycle_bucket("scanned_no_setup") == "no_setup"
    assert classify_lifecycle_bucket("scan_error") == "scan_error"
    assert classify_lifecycle_bucket("parked") == "unknown"


def test_rejected_no_setup_rows_are_counted_but_not_treated_as_failure() -> None:
    result = analyze_replay_dataset_coverage(
        [
            _row(row_id="row-001", status="REJECTED", normalized_lifecycle_status="REJECTED", rejection_reason="Weak setup."),
            _row(row_id="row-002", status="scanned_no_setup", normalized_lifecycle_status="scanned_no_setup"),
        ]
    )

    assert result.error_count == 0
    assert result.summary.rejected_rows == 1
    assert result.summary.no_setup_rows == 1


def test_setup_research_bucket_classification() -> None:
    assert classify_setup_research_bucket({"trade_idea_present": True}) == "trade_idea"
    assert classify_setup_research_bucket({"alert_present": True}) == "alerted"
    assert classify_setup_research_bucket({"journal_entry_present": True}) == "journaled"
    assert classify_setup_research_bucket({"status": "scanned_no_setup"}) == "no_setup"
    assert classify_setup_research_bucket({"status": "REJECTED"}) == "rejected"
    assert classify_setup_research_bucket({"first_failed_gate": "RR_TOO_LOW"}) == "gate_failed"
    assert classify_setup_research_bucket({"status": "near_miss"}) == "near_miss"
    assert classify_setup_research_bucket({"status": "parked"}) == "unknown"


def test_top_buckets_are_sorted_by_count_desc_then_key_asc() -> None:
    result = analyze_replay_dataset_coverage(
        [
            _terminal_trade_row(row_id="row-001", status="B", normalized_lifecycle_status="B"),
            _terminal_trade_row(row_id="row-002", status="A", normalized_lifecycle_status="A"),
            _terminal_trade_row(row_id="row-003", status="B", normalized_lifecycle_status="B"),
            _terminal_trade_row(row_id="row-004", status="A", normalized_lifecycle_status="A"),
            _terminal_trade_row(row_id="row-005", status="C", normalized_lifecycle_status="C"),
        ],
        top_n=3,
    )

    assert [bucket.key for bucket in _dimension(result, "status").top_buckets] == ["A", "B", "C"]


def test_sparse_buckets_are_detected_using_min_bucket_count() -> None:
    result = analyze_replay_dataset_coverage(
        [
            _terminal_trade_row(row_id="row-001", symbol="BTCUSDT"),
            _terminal_trade_row(row_id="row-002", symbol="BTCUSDT"),
            _terminal_trade_row(row_id="row-003", symbol="ETHUSDT"),
            _terminal_trade_row(row_id="row-004", symbol="XRPUSDT"),
        ],
        min_bucket_count=2,
    )

    assert [bucket.key for bucket in _dimension(result, "symbol").sparse_buckets] == ["ETHUSDT", "XRPUSDT"]
    assert result.summary.sparse_symbol_count == 2


def test_replay_ready_rate_is_computed_correctly() -> None:
    result = analyze_replay_dataset_coverage(
        [
            _terminal_trade_row(row_id="row-001", replay_ready=True),
            _terminal_trade_row(row_id="row-002", replay_ready=False),
        ]
    )

    assert result.summary.replay_ready_rows == 1
    assert result.summary.replay_ready_rate == 0.5


def test_unknown_lifecycle_bucket_warning_triggers_above_threshold() -> None:
    result = analyze_replay_dataset_coverage(
        [
            _terminal_trade_row(row_id="row-001", status="parked", normalized_lifecycle_status="parked"),
            _terminal_trade_row(row_id="row-002", status="CONFIRMED", normalized_lifecycle_status="CONFIRMED"),
        ]
    )

    assert "high_unknown_lifecycle_bucket_rate" in _gap_codes(result)


def test_first_failed_gate_counts_aggregate_correctly() -> None:
    result = analyze_replay_dataset_coverage(
        [
            _row(row_id="row-001", first_failed_gate="RR_TOO_LOW", status="REJECTED", normalized_lifecycle_status="REJECTED"),
            _row(row_id="row-002", first_failed_gate="RR_TOO_LOW", status="REJECTED", normalized_lifecycle_status="REJECTED"),
            _row(row_id="row-003", first_failed_gate="NO_CLEAR_TARGET", status="REJECTED", normalized_lifecycle_status="REJECTED"),
        ]
    )

    counts = {bucket.key: bucket.count for bucket in _dimension(result, "first_failed_gate").buckets}

    assert counts == {"RR_TOO_LOW": 2, "NO_CLEAR_TARGET": 1}
    assert result.summary.rows_with_first_failed_gate == 3


def test_coverage_result_to_dict_is_json_serializable() -> None:
    payload = coverage_result_to_dict(analyze_replay_dataset_coverage([_terminal_trade_row()]))

    encoded = json.dumps(payload, sort_keys=True)

    assert "replay_dataset_coverage_v1" in encoded


def test_cli_default_human_mode_exits_zero_with_valid_artifacts(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")
    monkeypatch.setattr(coverage_cli, "default_artifact_paths", lambda: (path,))

    exit_code = coverage_cli.main([])

    assert exit_code == 0
    assert "Replay Dataset Coverage Report" in capsys.readouterr().out


def test_cli_json_emits_valid_json(tmp_path, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = coverage_cli.main(["--input", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema_version"] == "replay_dataset_coverage_v1"
    assert payload["summary"]["total_rows"] == 1


def test_cli_strict_returns_nonzero_when_warnings_exist(tmp_path) -> None:
    path = tmp_path / "latest_scan.json"
    result = _scanner_result()
    result.pop("symbol")
    path.write_text(json.dumps(_scanner_payload(result)), encoding="utf-8")

    exit_code = coverage_cli.main(["--input", str(path), "--strict"])

    assert exit_code == 1


def test_invalid_jsonl_input_returns_error(tmp_path) -> None:
    path = tmp_path / "dataset.jsonl"
    path.write_text('{"row_id": "ok"}\n{not json\n', encoding="utf-8")

    result = analyze_replay_export_coverage_from_files([path])

    assert result.error_count == 1
    assert "invalid_jsonl" in _gap_codes(result)


def test_csv_input_returns_clear_warning_without_crashing(tmp_path) -> None:
    path = tmp_path / "dataset.csv"
    path.write_text("row_id,symbol\nrow-001,BTCUSDT\n", encoding="utf-8")

    result = analyze_replay_export_coverage_from_files([path])

    assert result.error_count == 0
    assert "unsupported_csv_input" in _gap_codes(result)


def test_no_network_exchange_or_telegram_calls_are_made(monkeypatch) -> None:
    def fail_network(*args, **kwargs):
        raise AssertionError("coverage reporting must not call network, exchange, or Telegram transports")

    monkeypatch.setattr(socket, "create_connection", fail_network)

    result = analyze_replay_dataset_coverage([_terminal_trade_row()])

    assert result.error_count == 0
