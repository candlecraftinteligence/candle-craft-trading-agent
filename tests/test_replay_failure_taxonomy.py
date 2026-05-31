from __future__ import annotations

import json
import socket

from app.analytics.replay_dataset_export import ReplayDatasetRow
from app.analytics.replay_failure_taxonomy import (
    analyze_replay_failure_taxonomy,
    analyze_replay_failure_taxonomy_from_files,
    classify_failure_family,
    extract_failure_reasons,
    failure_taxonomy_result_to_dict,
)
from scripts import analyze_replay_failure_taxonomy as taxonomy_cli


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
        "config": {"exchange": "binance", "timeframe": "1h", "strategy_name": "liquidity_grab_pullback"},
        "results": [result or _scanner_result()],
    }


def _warning_codes(result) -> set[str]:
    return {message.split(":", 1)[0] for message in result.warnings}


def _dimension(result, name: str):
    return next(dimension for dimension in result.dimensions if dimension.dimension_name == name)


def test_empty_rows_produce_warning_and_no_crash() -> None:
    result = analyze_replay_failure_taxonomy([])

    assert result.error_count == 0
    assert result.warning_count == 1
    assert result.summary.total_rows == 0
    assert "no_rows" in _warning_codes(result)


def test_no_failure_rows_produce_warning_and_no_errors() -> None:
    result = analyze_replay_failure_taxonomy([_row(status="CONFIRMED", normalized_lifecycle_status="CONFIRMED")])

    assert result.error_count == 0
    assert result.summary.failure_rows == 0
    assert "no_failure_rows" in _warning_codes(result)


def test_first_failed_gate_maps_to_failure_family() -> None:
    row = _row(status="REJECTED", normalized_lifecycle_status="REJECTED", first_failed_gate="BOS_STRUCTURE_SHIFT_MISSING")

    result = analyze_replay_failure_taxonomy([row])

    assert classify_failure_family(row) == "missing_structure_shift"
    assert result.summary.top_failure_family == "missing_structure_shift"


def test_rejection_reason_maps_to_failure_family() -> None:
    row = _row(status="REJECTED", normalized_lifecycle_status="REJECTED", rejection_reason="RR minimum not met.")

    result = analyze_replay_failure_taxonomy([row])

    assert classify_failure_family(row) == "rr_failure"
    assert result.summary.top_failure_family == "rr_failure"


def test_rejection_reasons_list_is_flattened_into_reasons() -> None:
    row = _row(
        status="REJECTED",
        normalized_lifecycle_status="REJECTED",
        rejection_reasons=("Funding conflict", "OI crowding"),
    )

    reasons = extract_failure_reasons(row)
    result = analyze_replay_failure_taxonomy([row])
    counts = {bucket.key: bucket.count for bucket in _dimension(result, "failure_reason").buckets}

    assert "Funding conflict" in reasons
    assert "OI crowding" in reasons
    assert counts["Funding conflict"] == 1
    assert counts["OI crowding"] == 1


def test_scanned_no_setup_row_is_included_and_not_system_error() -> None:
    result = analyze_replay_failure_taxonomy(
        [_row(status="scanned_no_setup", normalized_lifecycle_status="scanned_no_setup")]
    )

    assert result.summary.failure_rows == 1
    assert result.summary.no_setup_failure_rows == 1
    assert result.summary.scan_error_rows == 0
    assert result.summary.top_failure_family == "no_setup"


def test_scan_error_row_maps_to_scan_error_family() -> None:
    result = analyze_replay_failure_taxonomy([_row(status="scan_error", normalized_lifecycle_status="scan_error")])

    assert result.summary.scan_error_rows == 1
    assert result.summary.top_failure_family == "scan_error"


def test_invalidated_stopped_and_sl_rows_map_to_expected_families() -> None:
    result = analyze_replay_failure_taxonomy(
        [
            _row(row_id="row-001", status="INVALIDATED", normalized_lifecycle_status="INVALIDATED"),
            _row(row_id="row-002", status="STOPPED", normalized_lifecycle_status="STOPPED"),
            _row(row_id="row-003", status="SL_HIT", normalized_lifecycle_status="SL_HIT"),
        ]
    )
    families = {bucket.key: bucket.count for bucket in _dimension(result, "failure_family").buckets}

    assert families["invalidated"] == 1
    assert families["stopped_or_sl"] == 2
    assert result.summary.invalidated_rows == 1
    assert result.summary.stopped_or_sl_rows == 2


def test_trade_idea_row_without_failure_evidence_is_excluded() -> None:
    result = analyze_replay_failure_taxonomy(
        [
            _row(
                status="trade_idea_created",
                normalized_lifecycle_status="trade_idea_created",
                trade_idea_present=True,
                trade_idea_id="idea-001",
            )
        ]
    )

    assert result.summary.total_rows == 1
    assert result.summary.failure_rows == 0


def test_replay_readiness_warnings_only_row_maps_to_gap_family() -> None:
    result = analyze_replay_failure_taxonomy(
        [_row(row_id="row-001", replay_readiness_warnings=("symbol missing.",), status="WATCH", normalized_lifecycle_status="WATCH")]
    )

    assert result.summary.failure_rows == 1
    assert result.summary.top_failure_family == "replay_readiness_gap"
    assert result.summary.rows_with_replay_readiness_warnings == 1


def test_unknown_failure_mapping_warns_when_high_unknown_rate() -> None:
    result = analyze_replay_failure_taxonomy(
        [
            _row(row_id="row-001", status="setup_failed", normalized_lifecycle_status="setup_failed"),
            _row(row_id="row-002", status="setup_failed", normalized_lifecycle_status="setup_failed"),
        ]
    )

    assert result.summary.unknown_failure_rows == 2
    assert "high_unknown_failure_rate" in _warning_codes(result)


def test_pattern_detection_groups_failure_family_and_strategy_mode() -> None:
    result = analyze_replay_failure_taxonomy(
        [
            _row(row_id="row-001", strategy_mode="swing", first_failed_gate="RR_TOO_LOW"),
            _row(row_id="row-002", strategy_mode="swing", first_failed_gate="RR_TOO_LOW"),
            _row(row_id="row-003", strategy_mode="scalp", first_failed_gate="TRUST_METER_LOW"),
        ]
    )

    patterns = [pattern for pattern in result.patterns if pattern.pattern_name == "failure_family+strategy_mode"]

    assert patterns[0].key == "failure_family=rr_failure|strategy_mode=swing"
    assert patterns[0].count == 2


def test_top_buckets_and_patterns_sort_by_count_desc_then_key_asc() -> None:
    result = analyze_replay_failure_taxonomy(
        [
            _row(row_id="row-001", strategy_mode="b", first_failed_gate="TRUST_METER_LOW"),
            _row(row_id="row-002", strategy_mode="a", first_failed_gate="RR_TOO_LOW"),
            _row(row_id="row-003", strategy_mode="b", first_failed_gate="RR_TOO_LOW"),
            _row(row_id="row-004", strategy_mode="a", first_failed_gate="TRUST_METER_LOW"),
        ]
    )

    family_keys = [bucket.key for bucket in _dimension(result, "failure_family").top_buckets[:2]]
    pattern_keys = [
        pattern.key
        for pattern in result.patterns
        if pattern.pattern_name == "failure_family+strategy_mode"
    ]

    assert family_keys == ["rr_failure", "trust_meter_failure"]
    assert pattern_keys[:2] == [
        "failure_family=rr_failure|strategy_mode=a",
        "failure_family=rr_failure|strategy_mode=b",
    ]


def test_failure_taxonomy_result_to_dict_is_json_serializable() -> None:
    payload = failure_taxonomy_result_to_dict(analyze_replay_failure_taxonomy([_row(first_failed_gate="RR_TOO_LOW")]))

    encoded = json.dumps(payload, sort_keys=True)

    assert "replay_failure_taxonomy_v1" in encoded


def test_cli_default_human_mode_exits_zero_with_valid_artifacts(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")
    monkeypatch.setattr(taxonomy_cli, "default_artifact_paths", lambda: (path,))

    exit_code = taxonomy_cli.main([])

    assert exit_code == 0
    assert "Replay Failure Taxonomy Report" in capsys.readouterr().out


def test_cli_json_emits_valid_json(tmp_path, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = taxonomy_cli.main(["--input", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema_version"] == "replay_failure_taxonomy_v1"
    assert payload["summary"]["total_rows"] == 1


def test_cli_strict_returns_nonzero_when_warnings_exist(tmp_path) -> None:
    path = tmp_path / "latest_scan.json"
    result = _scanner_result()
    result.pop("symbol")
    path.write_text(json.dumps(_scanner_payload(result)), encoding="utf-8")

    exit_code = taxonomy_cli.main(["--input", str(path), "--strict"])

    assert exit_code == 1


def test_invalid_jsonl_input_returns_error(tmp_path) -> None:
    path = tmp_path / "dataset.jsonl"
    path.write_text('{"row_id": "ok"}\n{not json\n', encoding="utf-8")

    result = analyze_replay_failure_taxonomy_from_files([path])

    assert result.error_count == 1
    assert any("invalid_jsonl" in error for error in result.errors)


def test_no_network_exchange_or_telegram_calls_are_made(monkeypatch) -> None:
    def fail_network(*args, **kwargs):
        raise AssertionError("failure taxonomy must not call network, exchange, or Telegram transports")

    monkeypatch.setattr(socket, "create_connection", fail_network)

    result = analyze_replay_failure_taxonomy([_row(first_failed_gate="RR_TOO_LOW")])

    assert result.error_count == 0
