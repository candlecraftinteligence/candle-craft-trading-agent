from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.core.local_runtime_checks import (
    check_directory_writable,
    check_docker_readiness,
    check_env_file_presence,
    check_generated_artifact_hygiene,
    check_masked_config_keys,
    check_postgresql_config,
    check_python_version,
    collect_local_diagnostics,
    diagnostics_to_dicts,
    find_generated_artifacts,
    has_hard_blockers,
    mask_database_url,
)


def test_python_version_check_requires_python_311() -> None:
    too_old = check_python_version((3, 10, 14))
    supported = check_python_version((3, 11, 0))

    assert too_old.status == "error"
    assert supported.status == "ok"


def test_database_url_masking_does_not_leak_password() -> None:
    masked = mask_database_url("postgresql+psycopg://candle:super-secret@localhost:5432/candle_craft")

    assert "super-secret" not in masked
    assert "***" in masked
    assert masked.startswith("postgresql+psycopg://")


def test_config_key_report_does_not_leak_raw_values() -> None:
    diagnostic = check_masked_config_keys(
        {
            "APP_NAME": "Candle Craft Trading Agent",
            "DATABASE_URL": "postgresql+psycopg://candle:secret@localhost:5432/candle_craft",
            "TELEGRAM_BOT_TOKEN": "telegram-secret-token",
            "TELEGRAM_CHAT_ID": "",
        }
    )
    payload = json.dumps(diagnostic.to_dict())

    assert diagnostic.status == "ok"
    assert "telegram-secret-token" not in payload
    assert "secret@localhost" not in payload
    assert diagnostic.details["keys"]["TELEGRAM_BOT_TOKEN"] == "set"
    assert diagnostic.details["keys"]["TELEGRAM_CHAT_ID"] == "empty"


def test_missing_env_is_warning_not_crash(tmp_path: Path) -> None:
    diagnostic = check_env_file_presence(tmp_path)

    assert diagnostic.status == "warning"
    assert diagnostic.details["present"] is False


def test_empty_env_values_are_reported_without_values() -> None:
    diagnostic = check_masked_config_keys({"DATABASE_URL": "", "TELEGRAM_BOT_TOKEN": ""})

    assert diagnostic.details["keys"]["DATABASE_URL"] == "empty"
    assert diagnostic.details["keys"]["TELEGRAM_BOT_TOKEN"] == "empty"


def test_postgresql_config_masks_database_url() -> None:
    diagnostic = check_postgresql_config(
        {"DATABASE_URL": "postgresql+psycopg://candle:db-secret@localhost:5432/candle_craft"}
    )
    payload = json.dumps(diagnostic.to_dict())

    assert diagnostic.status == "ok"
    assert "db-secret" not in payload
    assert "database_url" in diagnostic.details


def test_docker_permission_failure_becomes_warning() -> None:
    def fake_runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["docker", "version"],
            returncode=1,
            stdout="",
            stderr="permission denied while trying to connect to the Docker daemon socket",
        )

    diagnostic = check_docker_readiness(runner=fake_runner)

    assert diagnostic.status == "warning"
    assert "Docker is not ready" in diagnostic.message
    assert "permission denied" in diagnostic.message


def test_temp_writability_check_handles_success_and_failure(tmp_path: Path) -> None:
    success = check_directory_writable(tmp_path / "temp", name="temp", label="Temp directory")
    blocking_file = tmp_path / "not_a_directory"
    blocking_file.write_text("content", encoding="utf-8")
    failure = check_directory_writable(blocking_file, name="temp", label="Temp directory")

    assert success.status == "ok"
    assert failure.status == "error"
    assert failure.details["reason"] == "not_directory"


def test_generated_artifact_detection_finds_scan_json_files(tmp_path: Path) -> None:
    scan_runs = tmp_path / "scan_runs"
    scan_runs.mkdir()
    (scan_runs / "latest_scan.json").write_text("{}", encoding="utf-8")
    (scan_runs / "watch_state.json").write_text("{}", encoding="utf-8")
    (tmp_path / "scan_output.json").write_text("{}", encoding="utf-8")

    artifacts = find_generated_artifacts(tmp_path)
    diagnostic = check_generated_artifact_hygiene(tmp_path)

    assert "scan_runs/latest_scan.json" in artifacts
    assert "scan_runs/watch_state.json" in artifacts
    assert "scan_output.json" in artifacts
    assert diagnostic.status == "warning"
    assert "local ignored files" in diagnostic.message


def test_generated_artifact_warning_is_serializable_and_non_blocking(tmp_path: Path) -> None:
    scan_runs = tmp_path / "scan_runs"
    scan_runs.mkdir()
    (scan_runs / "latest_scan.json").write_text("{}", encoding="utf-8")

    diagnostic = check_generated_artifact_hygiene(tmp_path)
    payload = json.dumps(diagnostic.to_dict())

    assert diagnostic.status == "warning"
    assert has_hard_blockers([diagnostic]) is False
    assert "scan_runs/latest_scan.json" in payload


def test_json_diagnostics_are_serializable_and_secret_safe(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "DATABASE_URL=postgresql+psycopg://candle:local-secret@localhost:5432/candle_craft",
                "TELEGRAM_BOT_TOKEN=telegram-secret",
                "TELEGRAM_CHAT_ID=",
            ]
        ),
        encoding="utf-8",
    )

    def fake_runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=["docker", "version"], returncode=0, stdout="{}", stderr="")

    diagnostics = collect_local_diagnostics(tmp_path, env={}, docker_runner=fake_runner)
    payload = json.dumps(diagnostics_to_dicts(diagnostics))

    assert "local-secret" not in payload
    assert "telegram-secret" not in payload
    assert "postgresql+psycopg://" in payload


def test_local_diagnostics_make_no_exchange_or_network_calls(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(tuple(command))
        return subprocess.CompletedProcess(args=command, returncode=0, stdout="{}", stderr="")

    collect_local_diagnostics(tmp_path, env={}, docker_runner=fake_runner)

    assert calls == [("docker", "version", "--format", "{{json .}}")]
