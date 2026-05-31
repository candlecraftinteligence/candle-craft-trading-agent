from __future__ import annotations

import csv
import json
import socket
from io import StringIO

from app.analytics.replay_dataset_export import (
    REPLAY_DATASET_SCHEMA_VERSION,
    export_replay_dataset_from_files,
    extract_replay_rows_from_artifact,
    rows_to_csv,
    rows_to_jsonl,
)
from scripts import export_replay_dataset


def _scanner_result(**overrides) -> dict:
    data = {
        "symbol": "BTCUSDT",
        "status": "idea_created",
        "status_history": ["idea_created"],
        "current_price": "100",
        "valid_strategy_modes": ["swing"],
        "rejected_strategy_modes": [],
        "strategy_diagnostics": {"swing": {"rr_to_tp2": "3.2", "first_failed_gate": "N/A"}},
        "missing_data": [],
        "unverified_data": [],
    }
    data.update(overrides)
    return data


def _scanner_payload(*, result: dict | None = None) -> dict:
    return {
        "run_id": "scan-001",
        "timestamp": "2026-05-31T00:00:00+00:00",
        "config": {
            "exchange": "binance",
            "market_type": "perpetual",
            "timeframe": "1h",
            "strategy_name": "liquidity_grab_pullback",
        },
        "results": [result or _scanner_result()],
    }


def test_minimal_valid_scanner_artifact_exports_one_replay_row() -> None:
    rows = extract_replay_rows_from_artifact(_scanner_payload())

    assert len(rows) == 1
    row = rows[0]
    assert row.schema_version == REPLAY_DATASET_SCHEMA_VERSION
    assert row.artifact_type == "scanner_run"
    assert row.row_type == "scanner_result"
    assert row.symbol == "BTCUSDT"
    assert row.status == "idea_created"
    assert row.replay_ready is True


def test_rejected_scanned_no_setup_exports_row_without_failure() -> None:
    rows = extract_replay_rows_from_artifact(
        _scanner_payload(
            result=_scanner_result(
                status="scanned_no_setup",
                status_history=["scanned_no_setup"],
                rejection_reason="No deterministic setup.",
                valid_strategy_modes=[],
                rejected_strategy_modes=["swing"],
            )
        )
    )

    assert len(rows) == 1
    assert rows[0].status == "scanned_no_setup"
    assert rows[0].rejection_reason == "No deterministic setup."
    assert rows[0].trade_idea_present is False
    assert rows[0].replay_ready is True


def test_missing_symbol_produces_replay_readiness_warning() -> None:
    result = _scanner_result()
    result.pop("symbol")

    row = extract_replay_rows_from_artifact(_scanner_payload(result=result))[0]

    assert row.symbol == "N/A"
    assert row.replay_ready is False
    assert "symbol missing." in row.replay_readiness_warnings


def test_missing_timestamp_produces_replay_readiness_warning() -> None:
    payload = _scanner_payload()
    payload.pop("timestamp")

    row = extract_replay_rows_from_artifact(payload)[0]

    assert row.scan_timestamp == "N/A"
    assert row.replay_ready is True
    assert "timestamp missing." in row.replay_readiness_warnings


def test_missing_values_become_na_not_invented_data() -> None:
    row = extract_replay_rows_from_artifact(_scanner_payload(result=_scanner_result(current_price=None)))[0]

    assert row.current_price == "N/A"
    assert row.entry_low == "N/A"
    assert row.entry_high == "N/A"
    assert row.stop == "N/A"
    assert row.tp1 == "N/A"


def test_status_history_is_serialized_deterministically() -> None:
    row = extract_replay_rows_from_artifact(
        _scanner_payload(result=_scanner_result(status_history=["watch", "triggered", "confirmed"]))
    )[0]

    payload = json.loads(rows_to_jsonl([row]).splitlines()[0])

    assert payload["status_history"] == ["watch", "triggered", "confirmed"]


def test_trade_idea_maps_selected_fields_without_nested_blob_copy() -> None:
    row = extract_replay_rows_from_artifact(
        _scanner_payload(
            result=_scanner_result(
                trade_idea={
                    "trade_idea_id": "idea-001",
                    "direction": "long",
                    "entry_zone": {"low": "99", "high": "101", "price": "100"},
                    "stop_loss": {"price": "95"},
                    "invalidation": "Invalid below 95.",
                    "take_profits": [{"price": "110"}, {"price": "120"}, {"price": "130"}],
                    "best_rr": "3.5",
                    "confidence_score": "88",
                    "grade": "A",
                    "nested_blob": {"raw": "do-not-copy"},
                },
            )
        )
    )[0]
    jsonl = rows_to_jsonl([row])

    assert row.trade_idea_present is True
    assert row.trade_idea_id == "idea-001"
    assert row.entry_low == "99"
    assert row.entry_high == "101"
    assert row.stop == "95"
    assert row.tp2 == "120"
    assert row.grade == "A"
    assert "do-not-copy" not in jsonl


def test_secret_like_source_keys_do_not_appear_in_jsonl_or_csv_values() -> None:
    row = extract_replay_rows_from_artifact(
        _scanner_payload(
            result=_scanner_result(
                api_key="SECRET-KEY-SHOULD-NOT-LEAK",
                trade_idea={"api_secret": "SECRET-TRADE-IDEA", "invalidation": "Invalid below 95."},
            )
        )
    )[0]

    output = rows_to_jsonl([row]) + rows_to_csv([row])

    assert "SECRET-KEY-SHOULD-NOT-LEAK" not in output
    assert "SECRET-TRADE-IDEA" not in output


def test_rows_to_jsonl_produces_valid_json_lines_with_schema_version() -> None:
    row = extract_replay_rows_from_artifact(_scanner_payload())[0]

    payload = json.loads(rows_to_jsonl([row]))

    assert payload["schema_version"] == REPLAY_DATASET_SCHEMA_VERSION
    assert payload["source"] == "in_memory"


def test_rows_to_csv_produces_stable_header_and_rows() -> None:
    row = extract_replay_rows_from_artifact(_scanner_payload())[0]
    output = rows_to_csv([row])
    parsed = list(csv.DictReader(StringIO(output)))

    header = output.splitlines()[0]
    assert header.startswith("schema_version,source,artifact_type,row_type,row_id,run_id")
    assert parsed[0]["schema_version"] == REPLAY_DATASET_SCHEMA_VERSION
    assert parsed[0]["status_history"] == '["idea_created"]'


def test_invalid_json_file_produces_export_error_without_crash(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    result = export_replay_dataset_from_files([path])

    assert result.summary.error_count == 1
    assert result.summary.artifact_counts == {"invalid_json": 1}
    assert "invalid_json" in result.errors[0]


def test_unknown_json_produces_zero_rows_with_warning(tmp_path) -> None:
    path = tmp_path / "unknown.json"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    result = export_replay_dataset_from_files([path])

    assert result.rows == ()
    assert result.summary.warning_count == 1
    assert "unknown_json" in result.warnings[0]


def test_cli_dry_run_exits_successfully_on_valid_local_artifact(tmp_path, capsys) -> None:
    path = tmp_path / "scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = export_replay_dataset.main(["--input", str(path), "--dry-run"])

    assert exit_code == 0
    assert "Replay Dataset Export" in capsys.readouterr().out


def test_cli_json_summary_emits_valid_json(tmp_path, capsys) -> None:
    path = tmp_path / "scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = export_replay_dataset.main(["--input", str(path), "--json-summary"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["summary"]["row_count"] == 1
    assert payload["output"]["dry_run"] is True


def test_cli_output_writes_file_in_tmp_path(tmp_path) -> None:
    path = tmp_path / "scan.json"
    output = tmp_path / "dataset.csv"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = export_replay_dataset.main(["--input", str(path), "--output", str(output), "--format", "csv"])

    assert exit_code == 0
    assert output.exists()
    assert output.read_text(encoding="utf-8").startswith("schema_version,source")


def test_strict_mode_returns_nonzero_when_warnings_exist(tmp_path) -> None:
    path = tmp_path / "scan.json"
    result = _scanner_result()
    result.pop("symbol")
    path.write_text(json.dumps(_scanner_payload(result=result)), encoding="utf-8")

    exit_code = export_replay_dataset.main(["--input", str(path), "--strict", "--dry-run"])

    assert exit_code == 1


def test_no_network_exchange_or_telegram_calls_are_made(monkeypatch) -> None:
    def fail_network(*args, **kwargs):
        raise AssertionError("replay export must not call network, exchange, or Telegram transports")

    monkeypatch.setattr(socket, "create_connection", fail_network)

    rows = extract_replay_rows_from_artifact(_scanner_payload())

    assert len(rows) == 1
