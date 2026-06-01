from __future__ import annotations

import json

import pytest

from scripts import audit_scan_row_visibility


def _write_scan(path, *, run_id: str) -> None:
    path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "results": [
                    {
                        "symbol": "BTCUSDT",
                        "display_status": "near_miss",
                        "failed_stage": "target_integrity",
                        "lifecycle_current_state": "N/A",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def _write_manifest(
    project_root,
    *,
    run_id: str = "run-latest",
    latest_scan_path: str | None = "scan_runs/latest_scan.json",
) -> None:
    manifest = project_root / "scan_runs" / "scan_run_manifest.jsonl"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    row = {"run_id": run_id}
    if latest_scan_path is not None:
        row["latest_scan_path"] = latest_scan_path
    manifest.write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_no_path_uses_latest_manifest_latest_scan_path(tmp_path, monkeypatch, capsys) -> None:
    scan_path = tmp_path / "scan_runs" / "latest_scan.json"
    scan_path.parent.mkdir(parents=True, exist_ok=True)
    _write_scan(scan_path, run_id="run-latest")
    _write_manifest(tmp_path, run_id="run-latest", latest_scan_path="scan_runs/latest_scan.json")
    monkeypatch.setattr(audit_scan_row_visibility, "PROJECT_ROOT", tmp_path)

    exit_code = audit_scan_row_visibility.main(["--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["manifest_run_id"] == "run-latest"
    assert payload["audited_path"] == str(scan_path)
    assert payload["audited_file_run_id"] == "run-latest"
    assert payload["run_ids_match"] is True
    assert payload["warning"] == "N/A"


def test_explicit_stale_path_prints_non_fatal_warning(tmp_path, monkeypatch, capsys) -> None:
    stale_path = tmp_path / "stale.json"
    _write_scan(stale_path, run_id="run-stale")
    _write_manifest(tmp_path, run_id="run-latest", latest_scan_path="scan_runs/latest_scan.json")
    monkeypatch.setattr(audit_scan_row_visibility, "PROJECT_ROOT", tmp_path)

    exit_code = audit_scan_row_visibility.main([str(stale_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "WARNING: Audited file run_id does not match latest manifest run_id; this file may be stale." in output
    assert "manifest_run_id: run-latest" in output
    assert "audited_file_run_id: run-stale" in output
    assert "run_ids_match: False" in output


def test_matching_explicit_path_does_not_print_stale_warning(tmp_path, monkeypatch, capsys) -> None:
    scan_path = tmp_path / "scan.json"
    _write_scan(scan_path, run_id="run-latest")
    _write_manifest(tmp_path, run_id="run-latest", latest_scan_path="scan_runs/latest_scan.json")
    monkeypatch.setattr(audit_scan_row_visibility, "PROJECT_ROOT", tmp_path)

    exit_code = audit_scan_row_visibility.main([str(scan_path)])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "WARNING:" not in output
    assert "run_ids_match: True" in output


def test_missing_manifest_has_clear_diagnostic(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(audit_scan_row_visibility, "PROJECT_ROOT", tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        audit_scan_row_visibility.main([])

    assert "no manifest rows were found" in str(exc_info.value)
    assert "scan_run_manifest.jsonl" in str(exc_info.value)


def test_manifest_without_latest_scan_path_has_clear_diagnostic(tmp_path, monkeypatch) -> None:
    _write_manifest(tmp_path, run_id="run-latest", latest_scan_path=None)
    monkeypatch.setattr(audit_scan_row_visibility, "PROJECT_ROOT", tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        audit_scan_row_visibility.main([])

    assert "Latest manifest row has no latest_scan_path" in str(exc_info.value)
    assert "--save-run scan_runs/latest_scan.json" in str(exc_info.value)
