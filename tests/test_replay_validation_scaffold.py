from __future__ import annotations

import json
import socket

from app.analytics.replay_dataset_export import ReplayDatasetRow
from app.backtesting.replay_validation_scaffold import (
    REPLAY_VALIDATION_SCHEMA_VERSION,
    build_replay_validation_candidates,
    build_replay_validation_plan,
    build_replay_validation_plan_from_files,
    build_replay_validation_timeline,
    replay_validation_result_to_dict,
)
from scripts import build_replay_validation_plan as replay_validation_cli


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
        "status": "scanned_no_setup",
        "normalized_lifecycle_status": "scanned_no_setup",
        "replay_ready": True,
    }
    data.update(overrides)
    return ReplayDatasetRow(**data)


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


def test_minimal_row_creates_candidate() -> None:
    candidates = build_replay_validation_candidates([_row()])

    assert len(candidates) == 1
    assert candidates[0].schema_version == REPLAY_VALIDATION_SCHEMA_VERSION
    assert candidates[0].symbol == "BTCUSDT"
    assert candidates[0].validation_ready is True


def test_no_setup_rejected_row_creates_negative_example_candidate_without_error() -> None:
    result = build_replay_validation_plan(
        [
            _row(
                status="REJECTED",
                normalized_lifecycle_status="REJECTED",
                rejection_reason="No deterministic setup.",
            )
        ]
    )

    assert result.total_candidates == 1
    assert result.negative_example_candidates == 1
    assert result.error_count == 0


def test_missing_symbol_creates_candidate_with_blocker() -> None:
    candidate = build_replay_validation_candidates([_row(symbol="N/A")])[0]

    assert candidate.symbol == "N/A"
    assert candidate.validation_ready is False
    assert "symbol missing." in candidate.validation_blockers


def test_trade_like_row_without_stop_or_invalidation_gets_blocker() -> None:
    candidate = build_replay_validation_candidates(
        [
            _row(
                status="idea_created",
                normalized_lifecycle_status="CONFIRMED",
                trade_idea_id="idea-001",
                direction="long",
                entry="100",
            )
        ]
    )[0]

    assert candidate.validation_ready is False
    assert "stop or invalidation missing for trade-like row." in candidate.validation_blockers


def test_terminal_row_without_result_or_outcome_gets_blocker() -> None:
    candidate = build_replay_validation_candidates(
        [_row(status="TP1_HIT", normalized_lifecycle_status="TP1_HIT", direction="long", entry="100", stop="95")]
    )[0]

    assert candidate.validation_ready is False
    assert "outcome_status or result_r missing for terminal outcome row." in candidate.validation_blockers


def test_deterministic_candidate_id_is_stable() -> None:
    first = build_replay_validation_candidates([_row()])[0]
    second = build_replay_validation_candidates([_row()])[0]

    assert first.candidate_id == second.candidate_id


def test_timeline_orders_by_timestamp_when_timestamps_are_present() -> None:
    candidates = build_replay_validation_candidates(
        [
            _row(row_id="row-002", symbol="ETHUSDT", scan_timestamp="2026-05-31T02:00:00+00:00"),
            _row(row_id="row-001", symbol="BTCUSDT", scan_timestamp="2026-05-31T01:00:00+00:00"),
        ]
    )

    timeline = build_replay_validation_timeline(candidates)

    assert timeline.ordering_method == "timestamp_then_candidate_id"
    assert [event.symbol for event in timeline.ordered_events] == ["BTCUSDT", "ETHUSDT"]


def test_timeline_preserves_input_order_when_timestamps_are_missing() -> None:
    candidates = build_replay_validation_candidates(
        [
            _row(row_id="row-001", symbol="BTCUSDT", scan_timestamp="N/A"),
            _row(row_id="row-002", symbol="ETHUSDT", scan_timestamp="2026-05-31T01:00:00+00:00"),
        ]
    )

    timeline = build_replay_validation_timeline(candidates)

    assert timeline.ordering_method == "input_order_missing_timestamps"
    assert [event.symbol for event in timeline.ordered_events] == ["BTCUSDT", "ETHUSDT"]


def test_validation_ready_rate_is_computed_correctly() -> None:
    result = build_replay_validation_plan([_row(row_id="row-001"), _row(row_id="row-002", symbol="N/A")])

    assert result.total_candidates == 2
    assert result.validation_ready_candidates == 1
    assert result.validation_ready_rate == 0.5


def test_blocker_counts_aggregate_correctly() -> None:
    result = build_replay_validation_plan([_row(row_id="row-001", symbol="N/A"), _row(row_id="row-002", symbol="N/A")])

    assert result.blocker_counts["symbol missing."] == 2


def test_replay_validation_result_to_dict_is_json_serializable() -> None:
    payload = replay_validation_result_to_dict(build_replay_validation_plan([_row()]))

    encoded = json.dumps(payload, sort_keys=True)

    assert REPLAY_VALIDATION_SCHEMA_VERSION in encoded


def test_build_replay_validation_plan_from_files_handles_invalid_json_without_crash(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    result = build_replay_validation_plan_from_files([path])

    assert result.error_count == 1
    assert any(issue.code == "export_error" for issue in result.issues)


def test_cli_default_human_mode_exits_zero_with_valid_artifact(tmp_path, capsys, monkeypatch) -> None:
    path = tmp_path / "latest_scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")
    monkeypatch.setattr(replay_validation_cli, "default_artifact_paths", lambda: (path,))

    exit_code = replay_validation_cli.main([])

    assert exit_code == 0
    assert "Historical Replay Validation Plan" in capsys.readouterr().out


def test_cli_json_emits_valid_json(tmp_path, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = replay_validation_cli.main(["--input", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema_version"] == REPLAY_VALIDATION_SCHEMA_VERSION
    assert payload["total_candidates"] == 1


def test_cli_dry_run_with_output_does_not_create_file(tmp_path, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    output = tmp_path / "validation_plan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = replay_validation_cli.main(["--input", str(path), "--output", str(output), "--dry-run"])

    assert exit_code == 0
    assert not output.exists()
    assert "Historical Replay Validation Plan" in capsys.readouterr().out


def test_cli_output_writes_only_requested_tmp_path(tmp_path) -> None:
    path = tmp_path / "latest_scan.json"
    output = tmp_path / "validation_plan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = replay_validation_cli.main(["--input", str(path), "--json", "--output", str(output)])

    assert exit_code == 0
    assert output.exists()
    assert json.loads(output.read_text(encoding="utf-8"))["total_candidates"] == 1


def test_cli_strict_returns_nonzero_when_warnings_exist(tmp_path) -> None:
    path = tmp_path / "latest_scan.json"
    scanner_result = _scanner_result()
    scanner_result.pop("symbol")
    path.write_text(json.dumps(_scanner_payload(scanner_result)), encoding="utf-8")

    exit_code = replay_validation_cli.main(["--input", str(path), "--strict"])

    assert exit_code == 1


def test_no_network_exchange_or_telegram_calls_are_made(monkeypatch) -> None:
    def fail_network(*args, **kwargs):
        raise AssertionError("replay validation scaffold must not call network, exchange, or Telegram transports")

    monkeypatch.setattr(socket, "create_connection", fail_network)

    result = build_replay_validation_plan([_row()])

    assert result.error_count == 0


def test_no_win_rate_profitability_or_expectancy_fields_are_present() -> None:
    payload = replay_validation_result_to_dict(build_replay_validation_plan([_row()]))
    forbidden_key_fragments = ("win_rate", "profit", "profitability", "expectancy", "edge")

    assert not _contains_forbidden_key(payload, forbidden_key_fragments)


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
