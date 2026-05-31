from __future__ import annotations

import json
import socket

from app.backtesting.replay_outcome_readiness import (
    REPLAY_OUTCOME_READINESS_SCHEMA_VERSION,
    audit_replay_outcome_readiness,
    audit_replay_outcome_readiness_from_files,
    replay_outcome_readiness_result_to_dict,
    required_outcome_fields_for_candidate,
)
from scripts import audit_replay_outcome_readiness as readiness_cli


def _negative_row(**overrides) -> dict:
    data = {
        "source": "scan.json",
        "run_id": "run-001",
        "scan_timestamp": "2026-05-31T00:00:00+00:00",
        "symbol": "BTCUSDT",
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
        "timeframe": "1h",
        "status": "TRADE_IDEA_CREATED",
        "normalized_lifecycle_status": "CONFIRMED",
        "strategy_name": "liquidity_grab_pullback",
        "strategy_mode": "swing",
        "direction": "long",
        "entry": "100",
        "stop": "95",
        "tp1": "110",
    }
    data.update(overrides)
    return data


def _terminal_row(**overrides) -> dict:
    data = _trade_row(
        status="TP1_HIT",
        normalized_lifecycle_status="TP1_HIT",
        outcome_status="tp1_hit",
        terminal_timestamp="2026-05-31T04:00:00+00:00",
        exit_price="110",
        terminal_reason="tp1_hit",
        result_r="1.0",
    )
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


def test_no_setup_row_is_valid_negative_example_but_not_trade_outcome_ready() -> None:
    result = audit_replay_outcome_readiness([_negative_row()])
    candidate = result.candidates[0]

    assert candidate.negative_example_candidate is True
    assert candidate.negative_example_ready is True
    assert candidate.trade_like_candidate is False
    assert candidate.outcome_ready is False
    assert result.summary.negative_example_candidates == 1
    assert result.summary.error_count == 0


def test_trade_like_row_is_trade_contract_ready_but_not_terminal_outcome_ready() -> None:
    result = audit_replay_outcome_readiness([_trade_row()])
    candidate = result.candidates[0]

    assert candidate.trade_like_candidate is True
    assert candidate.trade_contract_ready is True
    assert candidate.terminal_candidate is False
    assert candidate.terminal_contract_ready is False
    assert candidate.outcome_ready is False
    assert result.summary.missing_terminal_field_count == 0


def test_terminal_row_without_result_r_creates_readiness_blocker_warning() -> None:
    result = audit_replay_outcome_readiness([_terminal_row(result_r="N/A")])
    candidate = result.candidates[0]

    assert candidate.terminal_candidate is True
    assert candidate.terminal_contract_ready is False
    assert candidate.outcome_ready is False
    assert "result_r" in candidate.missing_fields
    assert "result_r missing for terminal row." in candidate.outcome_readiness_blockers
    assert result.summary.warning_count >= 1


def test_terminal_row_with_required_outcome_fields_becomes_outcome_ready() -> None:
    result = audit_replay_outcome_readiness([_terminal_row()])
    candidate = result.candidates[0]

    assert candidate.identity_ready is True
    assert candidate.trade_contract_ready is True
    assert candidate.terminal_contract_ready is True
    assert candidate.outcome_ready is True
    assert result.summary.outcome_ready_candidates == 1
    assert result.summary.outcome_ready_rate == 1.0


def test_missing_symbol_creates_identity_blocker() -> None:
    result = audit_replay_outcome_readiness([_negative_row(symbol="N/A")])
    candidate = result.candidates[0]

    assert candidate.identity_ready is False
    assert "symbol" in candidate.missing_fields
    assert "symbol missing." in candidate.outcome_readiness_blockers
    assert result.summary.missing_identity_count == 1


def test_missing_timestamp_creates_readiness_warning() -> None:
    result = audit_replay_outcome_readiness([_trade_row(scan_timestamp="N/A")])
    candidate = result.candidates[0]

    assert any("timestamp missing" in warning for warning in candidate.outcome_readiness_warnings)
    assert not any("timestamp missing" in blocker for blocker in candidate.outcome_readiness_blockers)
    assert result.summary.missing_timestamp_count == 1


def test_field_missing_counts_aggregate_correctly() -> None:
    result = audit_replay_outcome_readiness(
        [
            _trade_row(direction="N/A"),
            _terminal_row(result_r="N/A"),
        ]
    )

    assert result.summary.field_missing_counts["direction"] == 1
    assert result.summary.field_missing_counts["result_r"] == 1
    assert result.summary.missing_trade_field_count == 1
    assert result.summary.missing_terminal_field_count == 1


def test_required_outcome_fields_for_candidate_reports_applicable_contract() -> None:
    requirements = required_outcome_fields_for_candidate(_terminal_row())
    field_keys = {requirement.field_key for requirement in requirements}

    assert "symbol" in field_keys
    assert "direction" in field_keys
    assert "result_r" in field_keys


def test_result_dict_is_json_serializable() -> None:
    payload = replay_outcome_readiness_result_to_dict(audit_replay_outcome_readiness([_terminal_row()]))

    encoded = json.dumps(payload, sort_keys=True)

    assert REPLAY_OUTCOME_READINESS_SCHEMA_VERSION in encoded


def test_no_win_rate_pnl_profit_expectancy_or_edge_fields_exist_in_output() -> None:
    payload = replay_outcome_readiness_result_to_dict(audit_replay_outcome_readiness([_trade_row(tp1="N/A")]))
    forbidden_key_fragments = ("win_rate", "pnl", "profit", "profitability", "expectancy", "edge")

    assert not _contains_forbidden_key(payload, forbidden_key_fragments)


def test_invalid_json_file_returns_error_without_crash(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    result = audit_replay_outcome_readiness_from_files([path])

    assert result.summary.error_count == 1
    assert any(issue.code == "export_error" for issue in result.issues)


def test_cli_json_emits_valid_json(tmp_path, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = readiness_cli.main(["--input", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema_version"] == REPLAY_OUTCOME_READINESS_SCHEMA_VERSION
    assert payload["summary"]["total_candidates"] == 1
    assert "candidates" not in payload


def test_cli_strict_returns_nonzero_when_warnings_exist(tmp_path) -> None:
    path = tmp_path / "latest_scan.json"
    payload = _scanner_payload()
    payload.pop("timestamp")
    path.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = readiness_cli.main(["--input", str(path), "--strict"])

    assert exit_code == 1


def test_cli_dry_run_with_output_does_not_create_file(tmp_path, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    output = tmp_path / "outcome_readiness.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = readiness_cli.main(["--input", str(path), "--output", str(output), "--dry-run"])

    assert exit_code == 0
    assert not output.exists()
    assert "Replay Outcome Field Readiness Contract" in capsys.readouterr().out


def test_no_network_exchange_or_telegram_calls_are_made(monkeypatch) -> None:
    def fail_network(*args, **kwargs):
        raise AssertionError("replay outcome readiness must not call network, exchange, or Telegram transports")

    monkeypatch.setattr(socket, "create_connection", fail_network)

    result = audit_replay_outcome_readiness([_terminal_row()])

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
