from __future__ import annotations

import json
import socket

from app.backtesting.outcome_event_capture import (
    OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION,
    append_outcome_event,
    build_outcome_event_record,
    outcome_event_record_to_dict,
    outcome_event_summary_to_dict,
    read_outcome_events,
    summarize_outcome_events,
    validate_outcome_event_record,
)
from scripts import capture_outcome_event as capture_cli
from scripts import summarize_outcome_events as summarize_cli


def _payload(**overrides) -> dict:
    data = {
        "source": "manual_fixture",
        "candidate_id": "candidate-001",
        "symbol": "BTCUSDT",
        "exchange": "binance",
        "market_type": "perpetual",
        "timeframe": "1h",
        "strategy_name": "liquidity_grab_pullback",
        "strategy_mode": "swing",
        "direction": "long",
        "setup_id": "setup-001",
        "trade_idea_id": "idea-001",
        "alert_id": "alert-001",
        "run_id": "run-001",
        "scan_id": "scan-001",
    }
    data.update(overrides)
    return data


def _terminal_payload(**overrides) -> dict:
    data = _payload(
        outcome_status="TP_HIT",
        terminal_reason="take_profit",
        outcome_timestamp="2026-05-31T04:00:00+00:00",
        exit_price="110",
    )
    data.update(overrides)
    return data


def _write_json(path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_build_event_with_minimal_payload_fills_na_fields() -> None:
    record = build_outcome_event_record({"symbol": "btcusdt"})

    assert record.schema_version == OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION
    assert record.event_id.startswith("oe-")
    assert record.source == "manual"
    assert record.capture_source == "manual"
    assert record.captured_at == "N/A"
    assert record.symbol == "BTCUSDT"
    assert record.outcome_status == "N/A"
    assert record.tags == ()
    assert record.time_to_outcome_minutes == 0


def test_event_id_is_deterministic_for_same_payload() -> None:
    first = build_outcome_event_record(_payload(symbol="ETHUSDT"))
    second = build_outcome_event_record(_payload(symbol="ETHUSDT"))

    assert first.event_id == second.event_id


def test_valid_terminal_tp_hit_event_requires_timestamp_reason_and_exit() -> None:
    record = build_outcome_event_record(_terminal_payload())
    issues = validate_outcome_event_record(record)

    assert record.outcome_status == "TP_HIT"
    assert not [issue for issue in issues if issue.severity == "blocker"]


def test_terminal_event_missing_timestamp_creates_blocker() -> None:
    record = build_outcome_event_record(_terminal_payload(outcome_timestamp="N/A", closed_at="N/A"))

    assert any("outcome_timestamp or closed_at" in blocker for blocker in record.blockers)
    assert any(issue.code == "terminal_timestamp_missing" for issue in validate_outcome_event_record(record))


def test_invalid_outcome_status_creates_blocker() -> None:
    record = build_outcome_event_record(_payload(outcome_status="BROKEN"))

    assert any("outcome_status BROKEN is not allowed" in blocker for blocker in record.blockers)


def test_invalid_terminal_reason_creates_blocker() -> None:
    record = build_outcome_event_record(_terminal_payload(terminal_reason="mystery"))

    assert any("terminal_reason mystery is not allowed" in blocker for blocker in record.blockers)


def test_open_event_does_not_require_terminal_fields() -> None:
    record = build_outcome_event_record(_payload(outcome_status="OPEN"))
    summary = summarize_outcome_events([record])

    assert record.outcome_status == "OPEN"
    assert record.blockers == ()
    assert summary.open_events == 1


def test_no_setup_and_rejected_do_not_require_exit_price_or_result_r() -> None:
    summary = summarize_outcome_events(
        [
            build_outcome_event_record(_payload(outcome_status="NO_SETUP")),
            build_outcome_event_record(_payload(outcome_status="REJECTED")),
        ]
    )

    assert summary.negative_example_events == 2
    assert summary.blocker_count == 0


def test_result_r_numeric_like_accepted_only_if_explicitly_supplied() -> None:
    missing = build_outcome_event_record(_payload(outcome_status="OPEN"))
    numeric = build_outcome_event_record(_payload(outcome_status="OPEN", result_r="1.25"))
    invalid = build_outcome_event_record(_payload(outcome_status="OPEN", result_r="not-a-number"))

    assert missing.result_r == "N/A"
    assert numeric.result_r == "1.25"
    assert numeric.blockers == ()
    assert any("result_r must be numeric-like" in blocker for blocker in invalid.blockers)


def test_result_r_is_never_computed() -> None:
    record = build_outcome_event_record(_terminal_payload(entry="100", stop="95", exit_price="115"))

    assert record.result_r == "N/A"


def test_append_outcome_event_appends_one_json_line_and_does_not_overwrite_existing_lines(tmp_path) -> None:
    path = tmp_path / "outcome_events.jsonl"
    first = build_outcome_event_record(_payload(outcome_status="OPEN", symbol="BTCUSDT"))
    second = build_outcome_event_record(_payload(outcome_status="NO_SETUP", symbol="ETHUSDT"))

    append_outcome_event(path, first)
    append_outcome_event(path, second)
    lines = path.read_text(encoding="utf-8").splitlines()

    assert len(lines) == 2
    assert json.loads(lines[0])["event_id"] == first.event_id
    assert json.loads(lines[1])["event_id"] == second.event_id


def test_read_outcome_events_reads_valid_lines(tmp_path) -> None:
    path = tmp_path / "outcome_events.jsonl"
    record = build_outcome_event_record(_payload(outcome_status="OPEN"))
    append_outcome_event(path, record)

    result = read_outcome_events(path)

    assert result.summary.total_events == 1
    assert result.records[0].event_id == record.event_id


def test_read_outcome_events_reports_invalid_json_line_and_continues(tmp_path) -> None:
    path = tmp_path / "outcome_events.jsonl"
    valid = build_outcome_event_record(_payload(outcome_status="OPEN"))
    path.write_text(json.dumps(outcome_event_record_to_dict(valid)) + "\n{broken\n", encoding="utf-8")

    result = read_outcome_events(path)

    assert result.summary.total_events == 1
    assert result.summary.error_count == 1
    assert any(issue.code == "invalid_jsonl" for issue in result.issues)


def test_summarize_outcome_events_aggregates_status_reason_symbol_and_strategy_counts() -> None:
    summary = summarize_outcome_events(
        [
            build_outcome_event_record(_terminal_payload(symbol="BTCUSDT")),
            build_outcome_event_record(_payload(outcome_status="OPEN", symbol="ETHUSDT")),
            build_outcome_event_record(_payload(outcome_status="NO_SETUP", symbol="BTCUSDT", strategy_mode="scalp")),
        ]
    )

    assert summary.total_events == 3
    assert summary.terminal_events == 1
    assert summary.open_events == 1
    assert summary.negative_example_events == 1
    assert summary.outcome_status_counts["TP_HIT"] == 1
    assert summary.terminal_reason_counts["take_profit"] == 1
    assert summary.symbol_count == 2
    assert summary.strategy_mode_counts["swing"] == 2


def test_output_dicts_are_json_serializable() -> None:
    record = build_outcome_event_record(_payload(outcome_status="OPEN"))
    summary = summarize_outcome_events([record])

    assert json.loads(json.dumps(outcome_event_record_to_dict(record)))["schema_version"] == OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION
    assert json.loads(json.dumps(outcome_event_summary_to_dict(summary)))["total_events"] == 1


def test_no_win_rate_pnl_profit_expectancy_or_edge_fields_exist_in_output() -> None:
    payload = {
        "record": outcome_event_record_to_dict(build_outcome_event_record(_payload(outcome_status="OPEN"))),
        "summary": outcome_event_summary_to_dict(summarize_outcome_events([build_outcome_event_record(_payload())])),
    }
    forbidden_key_fragments = ("win_rate", "pnl", "profit", "profitability", "expectancy", "edge")

    assert not _contains_forbidden_key(payload, forbidden_key_fragments)


def test_capture_cli_dry_run_does_not_create_output_file(tmp_path, capsys) -> None:
    input_path = tmp_path / "event.json"
    output_path = tmp_path / "outcome_events.jsonl"
    _write_json(input_path, _payload(outcome_status="OPEN"))

    exit_code = capture_cli.main(["--input-json", str(input_path), "--output", str(output_path), "--dry-run"])

    assert exit_code == 0
    assert not output_path.exists()
    assert "Outcome Event Capture" in capsys.readouterr().out


def test_capture_cli_writes_jsonl_only_in_tmp_path(tmp_path) -> None:
    input_path = tmp_path / "event.json"
    output_path = tmp_path / "outcome_events.jsonl"
    _write_json(input_path, _payload(outcome_status="OPEN"))

    exit_code = capture_cli.main(["--input-json", str(input_path), "--output", str(output_path)])

    assert exit_code == 0
    assert output_path.exists()
    assert len(output_path.read_text(encoding="utf-8").splitlines()) == 1


def test_capture_cli_refuses_blockers_by_default(tmp_path) -> None:
    input_path = tmp_path / "event.json"
    output_path = tmp_path / "outcome_events.jsonl"
    _write_json(input_path, _terminal_payload(outcome_timestamp="N/A", closed_at="N/A"))

    exit_code = capture_cli.main(["--input-json", str(input_path), "--output", str(output_path)])

    assert exit_code == 1
    assert not output_path.exists()


def test_capture_cli_allow_blockers_appends_with_blockers(tmp_path) -> None:
    input_path = tmp_path / "event.json"
    output_path = tmp_path / "outcome_events.jsonl"
    _write_json(input_path, _terminal_payload(outcome_timestamp="N/A", closed_at="N/A"))

    exit_code = capture_cli.main(
        ["--input-json", str(input_path), "--output", str(output_path), "--allow-blockers"]
    )

    assert exit_code == 0
    payload = json.loads(output_path.read_text(encoding="utf-8").splitlines()[0])
    assert payload["blockers"]


def test_summarize_cli_missing_file_exits_0_with_warning(tmp_path, capsys) -> None:
    missing_path = tmp_path / "missing.jsonl"

    exit_code = summarize_cli.main(["--input", str(missing_path)])

    assert exit_code == 0
    assert "file_missing" in capsys.readouterr().out


def test_summarize_cli_json_emits_valid_json(tmp_path, capsys) -> None:
    path = tmp_path / "outcome_events.jsonl"
    append_outcome_event(path, build_outcome_event_record(_payload(outcome_status="OPEN")))

    exit_code = summarize_cli.main(["--input", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema_version"] == OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION
    assert payload["summary"]["total_events"] == 1


def test_no_network_exchange_or_telegram_calls_are_made(monkeypatch, tmp_path) -> None:
    def fail_network(*args, **kwargs):
        raise AssertionError("outcome event capture must not call network, exchange, or Telegram transports")

    monkeypatch.setattr(socket, "create_connection", fail_network)

    path = tmp_path / "outcome_events.jsonl"
    result = append_outcome_event(path, build_outcome_event_record(_terminal_payload()))

    assert result.appended is True


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
