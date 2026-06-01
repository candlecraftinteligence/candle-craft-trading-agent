from __future__ import annotations

import json
import socket

from app.backtesting.outcome_lifecycle_integration import (
    OUTCOME_LIFECYCLE_INTEGRATION_SCHEMA_VERSION,
    audit_outcome_lifecycle_candidate,
    audit_outcome_lifecycle_candidates,
    build_outcome_event_payload_from_candidate,
    map_lifecycle_status_to_outcome_status,
    map_lifecycle_status_to_terminal_reason,
    outcome_lifecycle_result_to_dict,
)
from scripts import audit_outcome_lifecycle_integration as lifecycle_cli


def _row(**overrides) -> dict:
    data = {
        "source": "scan.json",
        "candidate_id": "candidate-001",
        "run_id": "run-001",
        "scan_id": "scan-001",
        "setup_id": "setup-001",
        "symbol": "BTCUSDT",
        "exchange": "binance",
        "market_type": "perpetual",
        "timeframe": "1h",
        "strategy_name": "liquidity_grab_pullback",
        "strategy_mode": "swing",
        "direction": "long",
        "status": "WATCH",
        "normalized_lifecycle_status": "WATCH",
    }
    data.update(overrides)
    return data


def _scanner_result(**overrides) -> dict:
    data = {
        "symbol": "BTCUSDT",
        "status": "WATCH",
        "status_history": ["WATCH"],
        "trade_idea": {
            "id": "idea-001",
            "direction": "long",
            "entry": "100",
            "stop_loss": {"price": "95"},
            "take_profits": [{"price": "110"}],
        },
        "valid_strategy_modes": ["swing"],
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


def _write_json(path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_lifecycle_status_mapping_table() -> None:
    expected = {
        "TP_HIT": ("TP_HIT", "take_profit"),
        "TP1_HIT": ("TP1_HIT", "take_profit"),
        "TP2_HIT": ("TP2_HIT", "take_profit"),
        "TP3_HIT": ("TP3_HIT", "take_profit"),
        "SL_HIT": ("SL_HIT", "stop_loss"),
        "STOPPED": ("SL_HIT", "stop_loss"),
        "INVALIDATED": ("INVALIDATED", "invalidation"),
        "CANCELLED": ("CANCELLED", "manual_close"),
        "EXPIRED": ("EXPIRED", "setup_expired"),
        "REJECTED": ("REJECTED", "rejected"),
        "scanned_no_setup": ("NO_SETUP", "no_setup"),
        "scan_error": ("SCAN_ERROR", "scan_error"),
        "WATCH": ("OPEN", "N/A"),
        "TRIGGERED": ("OPEN", "N/A"),
        "CONFIRMED": ("OPEN", "N/A"),
        "EXECUTING": ("OPEN", "N/A"),
        "unknown": ("UNKNOWN", "unknown"),
    }

    for status, pair in expected.items():
        assert map_lifecycle_status_to_outcome_status(status) == pair[0]
        assert map_lifecycle_status_to_terminal_reason(status) == pair[1]


def test_build_payload_preserves_safe_scalar_fields_only() -> None:
    payload = build_outcome_event_payload_from_candidate(
        _row(
            status="TP_HIT",
            outcome_timestamp="2026-05-31T04:00:00+00:00",
            exit_price="110",
            nested_source_blob={"do_not": "copy"},
        )
    )

    assert payload["schema_version"]
    assert payload["symbol"] == "BTCUSDT"
    assert payload["timeframe"] == "1h"
    assert payload["strategy_mode"] == "swing"
    assert payload["direction"] == "long"
    assert payload["outcome_status"] == "TP_HIT"
    assert payload["terminal_reason"] == "take_profit"
    assert "nested_source_blob" not in payload


def test_terminal_candidate_missing_timestamp_and_price_creates_blockers_and_warnings() -> None:
    candidate = audit_outcome_lifecycle_candidate(_row(status="TP_HIT", normalized_lifecycle_status="TP_HIT"))

    assert candidate.is_terminal_lifecycle is True
    assert candidate.is_outcome_event_eligible is True
    assert any("outcome_timestamp or closed_at" in blocker for blocker in candidate.blockers)
    assert any("exit_price or resolved_price" in blocker for blocker in candidate.blockers)
    assert any("outcome_timestamp or closed_at" in warning for warning in candidate.warnings)


def test_open_candidate_does_not_require_terminal_fields() -> None:
    candidate = audit_outcome_lifecycle_candidate(_row(status="WATCH", normalized_lifecycle_status="WATCH"))

    assert candidate.mapped_outcome_status == "OPEN"
    assert candidate.mapped_terminal_reason == "N/A"
    assert candidate.blockers == ()
    assert candidate.is_outcome_event_eligible is True


def test_no_setup_and_rejected_do_not_require_price_or_result_fields() -> None:
    result = audit_outcome_lifecycle_candidates(
        [
            _row(status="scanned_no_setup", normalized_lifecycle_status="scanned_no_setup"),
            _row(status="REJECTED", normalized_lifecycle_status="REJECTED", candidate_id="candidate-002"),
        ]
    )

    assert result.summary.negative_example_candidates == 2
    assert result.summary.eligible_candidates == 2
    assert result.summary.error_count == 0
    assert all(not candidate.blockers for candidate in result.candidates)


def test_eligibility_counts_aggregate_correctly() -> None:
    result = audit_outcome_lifecycle_candidates(
        [
            _row(candidate_id="open", status="WATCH"),
            _row(candidate_id="terminal", status="TP_HIT"),
            _row(candidate_id="missing-symbol", symbol="N/A", status="WATCH"),
            _row(candidate_id="unknown", status="parked", normalized_lifecycle_status="parked"),
        ]
    )

    assert result.summary.total_candidates == 4
    assert result.summary.eligible_candidates == 2
    assert result.summary.ineligible_candidates == 2
    assert result.summary.terminal_candidates == 1
    assert result.summary.open_candidates == 2
    assert result.summary.unknown_status_candidates == 1
    assert result.summary.mapped_outcome_status_counts["OPEN"] == 2


def test_result_dict_is_json_serializable() -> None:
    result = audit_outcome_lifecycle_candidates([_row()])
    payload = outcome_lifecycle_result_to_dict(result)

    assert json.loads(json.dumps(payload))["schema_version"] == OUTCOME_LIFECYCLE_INTEGRATION_SCHEMA_VERSION


def test_no_win_rate_pnl_profit_expectancy_or_edge_fields_exist_in_output() -> None:
    payload = outcome_lifecycle_result_to_dict(audit_outcome_lifecycle_candidates([_row()]))
    forbidden_key_fragments = ("win_rate", "pnl", "profit", "profitability", "expectancy", "edge")

    assert not _contains_forbidden_key(payload, forbidden_key_fragments)


def test_cli_json_emits_valid_json(tmp_path, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    _write_json(path, _scanner_payload())

    exit_code = lifecycle_cli.main(["--input", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema_version"] == OUTCOME_LIFECYCLE_INTEGRATION_SCHEMA_VERSION
    assert payload["summary"]["total_candidates"] == 1


def test_cli_default_does_not_append_outcome_events(tmp_path) -> None:
    path = tmp_path / "latest_scan.json"
    output = tmp_path / "outcome_events.jsonl"
    _write_json(path, _scanner_payload())

    exit_code = lifecycle_cli.main(["--input", str(path), "--events-output", str(output)])

    assert exit_code == 0
    assert not output.exists()


def test_cli_append_eligible_writes_jsonl_only_when_requested(tmp_path) -> None:
    path = tmp_path / "latest_scan.json"
    output = tmp_path / "outcome_events.jsonl"
    _write_json(path, _scanner_payload())

    exit_code = lifecycle_cli.main(["--input", str(path), "--append-eligible", "--events-output", str(output)])

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert payload["outcome_status"] == "OPEN"


def test_cli_append_eligible_refuses_blockers_by_default(tmp_path) -> None:
    path = tmp_path / "latest_scan.json"
    output = tmp_path / "outcome_events.jsonl"
    _write_json(path, _scanner_payload(_scanner_result(status="TP_HIT", status_history=["WATCH", "TP_HIT"])))

    exit_code = lifecycle_cli.main(["--input", str(path), "--append-eligible", "--events-output", str(output)])

    assert exit_code == 1
    assert not output.exists()


def test_cli_append_eligible_allow_blockers_appends_blocked_drafts(tmp_path) -> None:
    path = tmp_path / "latest_scan.json"
    output = tmp_path / "outcome_events.jsonl"
    _write_json(path, _scanner_payload(_scanner_result(status="TP_HIT", status_history=["WATCH", "TP_HIT"])))

    exit_code = lifecycle_cli.main(
        ["--input", str(path), "--append-eligible", "--allow-blockers", "--events-output", str(output)]
    )

    assert exit_code == 0
    payload = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert payload["outcome_status"] == "TP_HIT"
    assert payload["blockers"]


def test_cli_strict_returns_nonzero_when_warnings_exist(tmp_path) -> None:
    path = tmp_path / "latest_scan.json"
    payload = _scanner_payload()
    payload.pop("timestamp")
    _write_json(path, payload)

    exit_code = lifecycle_cli.main(["--input", str(path), "--strict"])

    assert exit_code == 1


def test_no_network_exchange_or_telegram_calls_are_made(monkeypatch) -> None:
    def fail_network(*args, **kwargs):
        raise AssertionError("outcome lifecycle integration must not call network, exchange, or Telegram transports")

    monkeypatch.setattr(socket, "create_connection", fail_network)

    result = audit_outcome_lifecycle_candidates([_row(status="WATCH")])

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
