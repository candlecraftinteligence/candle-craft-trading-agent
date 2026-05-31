from __future__ import annotations

import json
import socket

from app.analytics.replay_dataset_export import ReplayDatasetRow
from app.analytics.replay_research_report import (
    REPLAY_RESEARCH_REPORT_SCHEMA_VERSION,
    build_replay_research_report_from_artifacts,
    build_replay_research_report_from_rows,
    default_replay_research_artifact_paths,
    format_replay_research_report_markdown,
    replay_research_report_to_dict,
)
from scripts import generate_replay_research_report


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


def _priority_text(result) -> str:
    return "\n".join(
        f"{priority.priority} {priority.evidence} {priority.suggested_next_phase} {priority.safety_note}"
        for priority in result.priorities
    )


def test_default_artifact_path_resolver_returns_expected_candidates_without_existing_files(tmp_path) -> None:
    paths = default_replay_research_artifact_paths(tmp_path)

    assert paths == [
        tmp_path / "scan_output.json",
        tmp_path / "scan_runs" / "latest_scan.json",
        tmp_path / "scan_runs" / "watch_state.json",
        tmp_path / "scan_runs" / "performance_memory.json",
    ]


def test_build_report_from_minimal_rows_produces_summary_with_no_errors() -> None:
    result = build_replay_research_report_from_rows([_row()])

    assert result.summary.schema_version == REPLAY_RESEARCH_REPORT_SCHEMA_VERSION
    assert result.summary.total_rows == 1
    assert result.summary.replay_ready_rows == 1
    assert result.summary.error_count == 0


def test_no_setup_dominant_rows_create_no_setup_research_priority() -> None:
    result = build_replay_research_report_from_rows(
        [
            _row(row_id="row-001", status="scanned_no_setup", normalized_lifecycle_status="scanned_no_setup"),
            _row(row_id="row-002", status="scanned_no_setup", normalized_lifecycle_status="scanned_no_setup"),
            _row(row_id="row-003", status="CONFIRMED", normalized_lifecycle_status="CONFIRMED"),
        ]
    )

    assert "no-setup concentration" in _priority_text(result)


def test_replay_readiness_gap_dominant_rows_create_readiness_priority() -> None:
    result = build_replay_research_report_from_rows(
        [
            _row(
                row_id="row-001",
                status="WATCH",
                normalized_lifecycle_status="WATCH",
                replay_ready=False,
                replay_readiness_warnings=("timestamp missing.",),
            ),
            _row(
                row_id="row-002",
                status="WATCH",
                normalized_lifecycle_status="WATCH",
                replay_ready=False,
                replay_readiness_warnings=("symbol missing.",),
            ),
        ]
    )

    assert "Improve replay identifiers/timestamps/outcome fields" in _priority_text(result)


def test_pullback_failure_rows_create_pullback_review_priority_without_gate_weakening_recommendation() -> None:
    result = build_replay_research_report_from_rows(
        [
            _row(
                status="REJECTED",
                normalized_lifecycle_status="REJECTED",
                first_failed_gate="PULLBACK_OB_FVG_ACCEPTANCE_FAILED",
            )
        ]
    )
    text = _priority_text(result).lower()

    assert "pullback failure" in text
    assert "weaken" not in text
    assert "live trading" not in text


def test_sparse_symbol_coverage_produces_data_collection_priority() -> None:
    result = build_replay_research_report_from_rows(
        [
            _row(row_id="row-001", symbol="BTCUSDT"),
            _row(row_id="row-002", symbol="ETHUSDT"),
            _row(row_id="row-003", symbol="SOLUSDT"),
            _row(row_id="row-004", symbol="XRPUSDT"),
        ]
    )

    assert "longer-duration data" in _priority_text(result)


def test_missing_terminal_outcomes_and_result_r_produces_outcome_enrichment_priority() -> None:
    result = build_replay_research_report_from_rows([_row()])

    assert "Outcome enrichment needed before expectancy analysis." in _priority_text(result)


def test_markdown_output_contains_required_sections() -> None:
    markdown = format_replay_research_report_markdown(build_replay_research_report_from_rows([_row()]))

    for section in (
        "Candle Craft Replay Research Report",
        "Executive Summary",
        "Artifact Inputs",
        "Dataset Quality",
        "Dataset Coverage",
        "Failure Taxonomy",
        "Replay Readiness Gaps",
        "Research Priorities",
        "Safety Notes",
    ):
        assert section in markdown


def test_markdown_output_does_not_include_secret_like_values_from_input_rows() -> None:
    result = build_replay_research_report_from_rows(
        [
            _row(
                status="REJECTED",
                normalized_lifecycle_status="REJECTED",
                rejection_reason="SECRET-TOKEN-SHOULD-NOT-LEAK",
            )
        ]
    )
    markdown = format_replay_research_report_markdown(result)

    assert "SECRET-TOKEN-SHOULD-NOT-LEAK" not in markdown
    assert "[REDACTED]" in markdown


def test_json_report_is_serializable() -> None:
    payload = replay_research_report_to_dict(build_replay_research_report_from_rows([_row()]))

    encoded = json.dumps(payload, sort_keys=True)

    assert REPLAY_RESEARCH_REPORT_SCHEMA_VERSION in encoded


def test_cli_json_emits_valid_json(tmp_path, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = generate_replay_research_report.main(["--input", str(path), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["schema_version"] == REPLAY_RESEARCH_REPORT_SCHEMA_VERSION
    assert payload["summary"]["total_rows"] == 1


def test_cli_markdown_emits_markdown_text(tmp_path, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = generate_replay_research_report.main(["--input", str(path), "--markdown"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert output.startswith("# Candle Craft Replay Research Report")


def test_cli_dry_run_with_output_does_not_create_file(tmp_path, capsys) -> None:
    path = tmp_path / "latest_scan.json"
    output = tmp_path / "report.md"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = generate_replay_research_report.main(["--input", str(path), "--output", str(output), "--dry-run"])

    assert exit_code == 0
    assert not output.exists()
    assert "Candle Craft Replay Research Report" in capsys.readouterr().out


def test_cli_output_writes_report_only_in_tmp_path(tmp_path) -> None:
    path = tmp_path / "latest_scan.json"
    output = tmp_path / "report.json"
    path.write_text(json.dumps(_scanner_payload()), encoding="utf-8")

    exit_code = generate_replay_research_report.main(["--input", str(path), "--json", "--output", str(output)])

    assert exit_code == 0
    assert output.exists()
    assert json.loads(output.read_text(encoding="utf-8"))["summary"]["total_rows"] == 1


def test_cli_strict_returns_nonzero_when_warnings_exist(tmp_path) -> None:
    path = tmp_path / "latest_scan.json"
    result = _scanner_result()
    result.pop("symbol")
    path.write_text(json.dumps(_scanner_payload(result)), encoding="utf-8")

    exit_code = generate_replay_research_report.main(["--input", str(path), "--strict"])

    assert exit_code == 1


def test_invalid_or_unreadable_input_produces_error(tmp_path) -> None:
    path = tmp_path / "missing.json"

    result = build_replay_research_report_from_artifacts([path])

    assert result.summary.error_count > 0
    assert any("unreadable_json" in error for error in result.errors)


def test_no_network_exchange_or_telegram_calls_are_made(monkeypatch) -> None:
    def fail_network(*args, **kwargs):
        raise AssertionError("research report must not call network, exchange, or Telegram transports")

    monkeypatch.setattr(socket, "create_connection", fail_network)

    result = build_replay_research_report_from_rows([_row()])

    assert result.summary.error_count == 0
