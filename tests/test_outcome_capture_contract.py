from __future__ import annotations

import json
import socket

from app.backtesting.outcome_capture_contract import (
    OUTCOME_CAPTURE_SCHEMA_VERSION,
    build_outcome_capture_record,
    default_outcome_field_specs,
    outcome_capture_result_to_dict,
    outcome_record_to_dict,
    validate_outcome_capture_record,
    validate_outcome_capture_records,
)
from scripts import validate_outcome_capture_contract as outcome_cli


def _negative_row(**overrides) -> dict:
    data = {
        "source": "scan.json",
        "run_id": "run-001",
        "scan_timestamp": "2026-05-31T00:00:00+00:00",
        "symbol": "BTCUSDT",
        "exchange": "binance",
        "market_type": "perpetual",
        "timeframe": "1h",
        "status": "scanned_no_setup",
        "normalized_lifecycle_status": "scanned_no_setup",
        "rejection_reason": "No deterministic setup.",
    }
    data.update(overrides)
    return data


def _trade_row(**overrides) -> dict:
    data = {
        "source": "scan.json",
        "run_id": "run-001",
        "setup_id": "setup-001",
        "scan_timestamp": "2026-05-31T00:00:00+00:00",
        "symbol": "BTCUSDT",
        "exchange": "binance",
        "market_type": "perpetual",
        "timeframe": "1h",
        "status": "TRADE_IDEA_CREATED",
        "normalized_lifecycle_status": "CONFIRMED",
        "strategy_name": "liquidity_grab_pullback",
        "strategy_mode": "swing",
        "direction": "long",
        "entry": "100",
        "stop": "95",
        "tp1": "110",
        "best_rr": "2.1",
        "rr_to_tp2": "1.8",
    }
    data.update(overrides)
    return data


def _terminal_overrides(**overrides) -> dict:
    data = {
        "outcome_status": "TP_HIT",
        "terminal_reason": "take_profit",
        "outcome_timestamp": "2026-05-31T04:00:00+00:00",
        "exit_price": "110",
    }
    data.update(overrides)
    return data


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


def _scanner_payload(result: dict | None = None, **overrides) -> dict:
    data = {
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
    data.update(overrides)
    return data


def test_default_field_specs_include_required_identity_setup_and_outcome_fields() -> None:
    names = {spec.name for spec in default_outcome_field_specs()}

    assert {"schema_version", "candidate_id", "symbol", "timeframe"} <= names
    assert {"entry", "entry_low", "entry_high", "stop", "invalidation", "tp1", "tp2", "tp3"} <= names
    assert {"outcome_status", "terminal_reason", "exit_price", "resolved_price", "result_r"} <= names
    assert {"capture_status", "capture_source", "warnings", "blockers"} <= names


def test_build_record_from_no_setup_candidate_creates_negative_example_record() -> None:
    record = build_outcome_capture_record(_negative_row())
    result = validate_outcome_capture_record(record)

    assert record.outcome_status == "NO_SETUP"
    assert record.capture_status == "rejected_negative_example"
    assert record.result_r == "N/A"
    assert "No deterministic setup." in record.capture_notes
    assert result.summary.negative_example_records == 1
    assert result.summary.blocker_count == 0


def test_build_record_from_trade_like_candidate_creates_incomplete_record_without_inferred_result_r() -> None:
    record = build_outcome_capture_record(_trade_row())

    assert record.outcome_status == "N/A"
    assert record.capture_status == "incomplete"
    assert record.result_r == "N/A"
    assert record.exit_price == "N/A"


def test_explicit_terminal_tp_hit_override_validates_required_terminal_fields() -> None:
    record = build_outcome_capture_record(_trade_row(), overrides=_terminal_overrides())
    result = validate_outcome_capture_record(record)

    assert record.outcome_status == "TP_HIT"
    assert record.capture_status == "captured"
    assert result.summary.valid_records == 1
    assert result.summary.blocker_count == 0
    assert result.summary.records_missing_result_r == 1


def test_terminal_status_missing_timestamp_creates_blocker_and_warning() -> None:
    record = build_outcome_capture_record(
        _trade_row(),
        overrides=_terminal_overrides(outcome_timestamp="N/A"),
    )

    assert "terminal_timestamp" in record.missing_required_fields
    assert any("outcome_timestamp or closed_at" in blocker for blocker in record.blockers)
    assert any("outcome_timestamp or closed_at" in warning for warning in record.warnings)


def test_terminal_status_missing_terminal_reason_creates_blocker_and_warning() -> None:
    record = build_outcome_capture_record(
        _trade_row(),
        overrides=_terminal_overrides(terminal_reason="N/A"),
    )

    assert "terminal_reason" in record.missing_required_fields
    assert any("terminal_reason" in blocker for blocker in record.blockers)
    assert any("terminal_reason" in warning for warning in record.warnings)


def test_invalid_outcome_status_creates_blocker_and_warning() -> None:
    record = build_outcome_capture_record(_trade_row(), overrides={"outcome_status": "BROKEN"})

    assert record.capture_status == "invalid"
    assert any("outcome_status BROKEN is not allowed" in blocker for blocker in record.blockers)
    assert any("outcome_status BROKEN is not allowed" in warning for warning in record.warnings)


def test_invalid_terminal_reason_creates_blocker_and_warning() -> None:
    record = build_outcome_capture_record(
        _trade_row(),
        overrides=_terminal_overrides(terminal_reason="mystery"),
    )

    assert record.capture_status == "invalid"
    assert any("terminal_reason mystery is not allowed" in blocker for blocker in record.blockers)
    assert any("terminal_reason mystery is not allowed" in warning for warning in record.warnings)


def test_open_status_does_not_require_terminal_fields() -> None:
    record = build_outcome_capture_record(_trade_row(), overrides={"outcome_status": "OPEN"})
    result = validate_outcome_capture_record(record)

    assert record.outcome_status == "OPEN"
    assert "terminal_timestamp" not in record.missing_required_fields
    assert result.summary.blocker_count == 0
    assert result.summary.open_records == 1


def test_no_setup_and_rejected_records_do_not_require_exit_price_or_result_r() -> None:
    result = validate_outcome_capture_records(
        [
            build_outcome_capture_record(_negative_row()),
            build_outcome_capture_record(_negative_row(status="rejected", normalized_lifecycle_status="rejected")),
        ]
    )

    assert result.summary.negative_example_records == 2
    assert result.summary.records_missing_exit_price == 0
    assert result.summary.blocker_count == 0


def test_result_r_is_preserved_only_when_explicitly_supplied() -> None:
    draft_record = build_outcome_capture_record(_trade_row())
    explicit_record = build_outcome_capture_record(_trade_row(result_r="1.25"), overrides={"outcome_status": "OPEN"})

    assert draft_record.result_r == "N/A"
    assert explicit_record.result_r == "1.25"


def test_result_r_is_never_computed_from_entry_stop_and_exit_price() -> None:
    record = build_outcome_capture_record(
        _trade_row(entry="100", stop="95"),
        overrides=_terminal_overrides(exit_price="115"),
    )

    assert record.result_r == "N/A"


def test_validation_summary_aggregates_status_reason_and_missing_fields() -> None:
    result = validate_outcome_capture_records(
        [
            build_outcome_capture_record(_trade_row()),
            build_outcome_capture_record(_trade_row(), overrides=_terminal_overrides(outcome_timestamp="N/A")),
            build_outcome_capture_record(_negative_row()),
        ]
    )

    assert result.summary.total_records == 3
    assert result.summary.outcome_status_counts["N/A"] == 1
    assert result.summary.outcome_status_counts["TP_HIT"] == 1
    assert result.summary.outcome_status_counts["NO_SETUP"] == 1
    assert result.summary.terminal_reason_counts["take_profit"] == 1
    assert result.summary.field_missing_counts["result_r"] == 3
    assert result.summary.records_missing_terminal_timestamp == 1


def test_output_dict_is_json_serializable() -> None:
    result_payload = outcome_capture_result_to_dict(
        validate_outcome_capture_records([build_outcome_capture_record(_trade_row())])
    )
    record_payload = outcome_record_to_dict(build_outcome_capture_record(_negative_row()))

    assert json.loads(json.dumps(result_payload))["schema_version"] == OUTCOME_CAPTURE_SCHEMA_VERSION
    assert json.loads(json.dumps(record_payload))["schema_version"] == OUTCOME_CAPTURE_SCHEMA_VERSION


def test_no_win_rate_pnl_profit_expectancy_or_edge_fields_exist_in_output() -> None:
    payload = outcome_capture_result_to_dict(
        validate_outcome_capture_records([build_outcome_capture_record(_trade_row())])
    )
    forbidden_key_fragments = ("win_rate", "pnl", "profit", "profitability", "expectancy", "edge")

    assert not _contains_forbidden_key(payload, forbidden_key_fragments)


def test_cli_json_emits_valid_json(tmp_path, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = outcome_cli.main(["--input", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema_version"] == OUTCOME_CAPTURE_SCHEMA_VERSION
    assert payload["summary"]["total_records"] == 1
    assert "records" not in payload


def test_cli_strict_returns_nonzero_when_warnings_exist(tmp_path) -> None:
    path = tmp_path / "latest_scan.json"
    payload = _scanner_payload()
    payload.pop("timestamp")
    path.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = outcome_cli.main(["--input", str(path), "--strict"])

    assert exit_code == 1


def test_cli_dry_run_with_output_does_not_create_file(tmp_path, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    output = tmp_path / "latest_outcome_capture_contract.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = outcome_cli.main(["--input", str(path), "--output", str(output), "--dry-run"])

    assert exit_code == 0
    assert not output.exists()
    assert "Outcome Capture Contract Validation" in capsys.readouterr().out


def test_no_network_exchange_or_telegram_calls_are_made(monkeypatch) -> None:
    def fail_network(*args, **kwargs):
        raise AssertionError("outcome capture contract must not call network, exchange, or Telegram transports")

    monkeypatch.setattr(socket, "create_connection", fail_network)

    result = validate_outcome_capture_records([build_outcome_capture_record(_trade_row(), overrides=_terminal_overrides())])

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
