"""RUNTIME_CHECKPOINT_READINESS: bounded preflight collector and evidence assessment.

Synthetic fixtures only. No live Runtime database, Telegram, exchange, or order traffic.

Evidence labels below are test documentation, not application enums.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

import app.storage.database as database_module
import app.storage.runtime_checkpoint_readiness as readiness
from app.storage.database import (
    SCHEMA_VERSION,
    connect_database,
    identify_schema_version,
    open_initialized_database,
    open_read_only_database,
)
from app.storage.maintenance import inspect_database, sha256_file
from app.storage.scan_payloads import SUPPORTED_FORMATS
from scripts import runtime_checkpoint_readiness as readiness_cli

ANCHOR = "2bac6b4eda271bc69f2c34f42d6b7592e37fa8cc"
FAKE_DEPLOYED = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
FAKE_TARGET = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
AS_OF = "2026-10-05T08:00:00Z"
BASELINE_START = "2026-10-03T08:00:00Z"
BASELINE_END = "2026-10-05T06:00:00Z"
RESTORE_OBSERVED = "2026-10-04T12:00:00Z"
REHEARSAL_OBSERVED = "2026-10-04T18:00:00Z"
SOURCE_ID = "synthetic-source:schema-fixture.sqlite"
SNAPSHOT_ID = "synthetic-snapshot:rehearsal-pre-schema-26"
RESTORE_PATH = "synthetic-restore:/isolated/candidate.sqlite"


def _state(path: Path) -> tuple[int, int, str, bool, bool]:
    stat = path.stat()
    return (
        stat.st_size,
        stat.st_mtime_ns,
        sha256_file(path),
        Path(f"{path}-wal").exists(),
        Path(f"{path}-shm").exists(),
    )


def _v25(tmp_path: Path, name: str = "ready.sqlite") -> Path:
    path = tmp_path / name
    with open_initialized_database(path) as connection:
        connection.execute(
            """
            INSERT INTO scan_runs (
                run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
                strategy, timeframes_json, market_regime, runtime_stats_json,
                command_preset, command_used, total_valid_setups, near_misses,
                rejected, data_issues, data_issues_json, raw_payload_json
            ) VALUES (
                'run-1', '2026-09-11T12:00:00Z', 'binance', 'test', 1, '["BTCUSDT"]',
                'test', '{}', 'unknown', '{}', 'N/A', 'N/A', 0, 0, 0, 0, '[]', '{}'
            )
            """
        )
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.commit()
    with sqlite3.connect(path) as connection:
        mode = str(connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]).lower()
        if mode != "delete":
            raise AssertionError(f"fixture journal_mode={mode!r}")
        connection.commit()
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)
    return path


def _legacy_v24(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE scan_runs (
                run_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                raw_payload_json TEXT NOT NULL
            );
            CREATE INDEX ix_scan_runs_timestamp ON scan_runs(timestamp);
            INSERT INTO scan_runs(run_id, timestamp, raw_payload_json)
            VALUES ('legacy', '2026-09-01T00:00:00Z', '{"results":[]}');
            PRAGMA user_version = 24;
            """
        )


def _attach_sql_guard(connection: sqlite3.Connection) -> None:
    connection.set_trace_callback(readiness.assert_sql_is_read_only)


GIB = 1024**3


def _gib(n: int) -> int:
    return n * GIB


_BASELINE_SPAN_SECONDS = 165600
_BASELINE_EARLIEST_BYTES = 8 * GIB
_BASELINE_DELTA_BYTES = 165600
_BASELINE_HORIZON_SECONDS = 10 * 24 * 60 * 60
_BASELINE_PROJECTED_BYTES = 864000
_BASELINE_DERIVED_BYTES = 864000


def _growth_sample(observed_at: str, allocated_bytes: int, *, quality: str = "representative") -> dict[str, Any]:
    return {
        "observed_at_utc": observed_at,
        "combined_allocated_bytes": allocated_bytes,
        "comparable": True,
        "quality": quality,
    }


def _growth_derivation(**overrides: Any) -> dict[str, Any]:
    derivation = {
        "evidence_class": "declared_assertion",
        "unit": "bytes",
        "observation_count": 2,
        "earliest_allocated_bytes": _BASELINE_EARLIEST_BYTES,
        "latest_allocated_bytes": _BASELINE_EARLIEST_BYTES + _BASELINE_DELTA_BYTES,
        "observed_delta_bytes": _BASELINE_DELTA_BYTES,
        "observation_seconds": _BASELINE_SPAN_SECONDS,
        "horizon_seconds": _BASELINE_HORIZON_SECONDS,
        "projected_bytes": _BASELINE_PROJECTED_BYTES,
        "allowance_bytes": 0,
        "derived_budget_bytes": _BASELINE_DERIVED_BYTES,
    }
    derivation.update(overrides)
    return derivation


def _workload_baseline() -> dict[str, Any]:
    return {
        "status": "representative",
        "workload_description": "ordinary scanner cadence on a synthetic packet",
        "cadence_description": "configured scan interval; the historical five-minute value is not assumed",
        "observation_started_utc": BASELINE_START,
        "observation_ended_utc": BASELINE_END,
        "planning_horizon_days": 10,
        "comparable": True,
        "source_identity": SOURCE_ID,
        "samples": [
            _growth_sample(BASELINE_START, _BASELINE_EARLIEST_BYTES),
            _growth_sample(BASELINE_END, _BASELINE_EARLIEST_BYTES + _BASELINE_DELTA_BYTES),
        ],
        "growth_derivation": _growth_derivation(),
    }


def _label_only_baseline() -> dict[str, Any]:
    """The rejected published shape: descriptions and a horizon, no quantitative trace."""

    return {
        "status": "representative",
        "workload_description": "ordinary scanner cadence on a synthetic packet",
        "cadence_description": "configured scan interval; the historical five-minute value is not assumed",
        "observation_started_utc": BASELINE_START,
        "observation_ended_utc": BASELINE_END,
        "planning_horizon_days": 10,
        "comparable": True,
        "source_identity": SOURCE_ID,
    }


def _capacity_ok() -> dict[str, Any]:
    total = _gib(100)
    return {
        "volume_total_bytes": total,
        "volume_free_bytes": _gib(80),
        "new_backup_bytes": _gib(20),
        "concurrent_restore_or_candidate_bytes": _gib(20),
        "migration_temp_bytes": _gib(5),
        "additional_peak_WAL_and_log_bytes": _gib(2),
        "growth_budget_bytes": _gib(8),
        "operating_reserve_bytes": max(_gib(10), total // 10),
        "backup_shares_db_volume": True,
        "restore_shares_db_volume": True,
        "existing_occupancy_already_in_free_space": True,
        "freelist_not_subtracted": True,
        "unperformed_cleanup_not_subtracted": True,
        "compression_not_assumed": True,
        "workload_baseline": _workload_baseline(),
    }


def _absent_plan_state_index() -> dict[str, Any]:
    return {
        "status": "absent",
        "name": readiness.PLAN_STATE_INDEX_NAME,
        "table": None,
        "columns": [],
        "unique": None,
        "partial": None,
        "partial_predicate": None,
    }


def _matching_plan_state_index() -> dict[str, Any]:
    return {
        "name": readiness.PLAN_STATE_INDEX_NAME,
        "table": readiness.PLAN_STATE_INDEX_TABLE,
        "columns": list(readiness.PLAN_STATE_INDEX_COLUMNS),
        "unique": False,
        "partial": True,
        "partial_predicate": "plan_version_id IS NOT NULL",
    }


def _measured_collector(schema: int = 25) -> dict[str, Any]:
    return {
        "sqlite": {
            "status": "measured",
            "schema_version": schema,
            "indexes": {},
            "plan_state_index": _absent_plan_state_index(),
        },
        "filesystem": {"status": "measured"},
    }


def _volume_floor(total: int) -> int:
    return max(_gib(10), int(total) // 10)


def _db_volume_only_required(capacity: dict[str, Any], *, applied_reserve: int | None = None) -> int:
    reserve = capacity["operating_reserve_bytes"] if applied_reserve is None else applied_reserve
    required = (
        int(capacity["migration_temp_bytes"])
        + int(capacity["additional_peak_WAL_and_log_bytes"])
        + int(capacity["growth_budget_bytes"])
        + int(reserve)
    )
    if capacity["backup_shares_db_volume"]:
        required += int(capacity["new_backup_bytes"])
    if capacity["restore_shares_db_volume"]:
        required += int(capacity["concurrent_restore_or_candidate_bytes"])
    return required


def _external_required(allocation: int, total: int, *, declared: int | None = None, stronger: int | None = None) -> int:
    applied = _volume_floor(total)
    if declared is not None:
        applied = max(applied, declared)
    if stronger is not None:
        applied = max(applied, stronger)
    return int(allocation) + applied


def _same_external_capacity(*, free_bytes: int, total_bytes: int | None = None) -> dict[str, Any]:
    capacity = _capacity_ok()
    total = _gib(100) if total_bytes is None else total_bytes
    capacity["backup_shares_db_volume"] = False
    capacity["restore_shares_db_volume"] = False
    capacity["backup_restore_share_volume"] = True
    capacity["backup_volume_label"] = "ext-shared"
    capacity["restore_volume_label"] = "ext-shared"
    capacity["backup_volume_total_bytes"] = total
    capacity["backup_volume_free_bytes"] = free_bytes
    capacity["restore_volume_total_bytes"] = total
    capacity["restore_volume_free_bytes"] = free_bytes
    return capacity


def _distinct_external_capacity(*, backup_free: int, restore_free: int, total_bytes: int | None = None) -> dict[str, Any]:
    capacity = _capacity_ok()
    total = _gib(100) if total_bytes is None else total_bytes
    capacity["backup_shares_db_volume"] = False
    capacity["restore_shares_db_volume"] = False
    capacity["backup_restore_share_volume"] = False
    capacity["backup_volume_label"] = "ext-backup"
    capacity["restore_volume_label"] = "ext-restore"
    capacity["backup_volume_total_bytes"] = total
    capacity["backup_volume_free_bytes"] = backup_free
    capacity["restore_volume_total_bytes"] = total
    capacity["restore_volume_free_bytes"] = restore_free
    return capacity


def _target_rehearsal(source_schema: int) -> dict[str, Any]:
    return {
        "evidence_class": "declared_assertion",
        "source_identity": SOURCE_ID,
        "source_schema_version": source_schema,
        "isolated_copy": True,
        "restored_copy_path": RESTORE_PATH,
        "snapshot_identity": SNAPSHOT_ID,
        "observed_at_utc": REHEARSAL_OBSERVED,
        "migrated_schema_version": SCHEMA_VERSION,
        "plan_state_index": _matching_plan_state_index(),
        "epoch_lineage_tables": {
            name: list(columns) for name, columns in readiness.EPOCH_LINEAGE_COLUMNS.items()
        },
        "preservation": {key: "preserved" for key in readiness.PRESERVATION_KEYS},
        "migration_duration_seconds": 90,
        "restore_duration_seconds": 120,
        "peak_allocation_bytes": _gib(4),
        "planned_downtime_budget_seconds": 3600,
        "recovery_budget_seconds": 7200,
        "final_cutover_reverification": "required_before_consumers",
    }


def _complete_packet(collector: dict[str, Any] | None = None) -> dict[str, Any]:
    known = [str(item["id"]) for item in readiness.CONSUMER_INVENTORY]
    report = collector or {}
    sqlite_section = report.get("sqlite") if isinstance(report.get("sqlite"), dict) else {}
    source_schema = sqlite_section.get("schema_version")
    if type(source_schema) is not int:
        source_schema = 25
    return {
        "contract_version": readiness.PACKET_CONTRACT_VERSION,
        "collector_report": report,
        "operator": {
            "evidence_origin": "synthetic",
            "evidence_as_of_utc": AS_OF,
            "deployed_application_sha": FAKE_DEPLOYED,
            "target_sha": FAKE_TARGET,
            "release_evidence": {
                "historical_functional_anchor_sha": ANCHOR,
                "target_sha": FAKE_TARGET,
                "deployed_application_sha": FAKE_DEPLOYED,
                "review_evidence_class": "declared_assertion",
                "ci_evidence_class": "declared_assertion",
            },
            "accounted_consumers": known,
            "unknown_external_readers_resolved": True,
            "source_replay_capture": {
                "capture_switch": "SOURCE_REPLAY_CAPTURE_ENABLED",
                "enabled": False,
                "evidence_class": "declared_assertion",
            },
            "restore_evidence": {
                "restore_tested": True,
                "integrity_ok": True,
                "integrity_evidence_class": "declared_assertion",
                "restore_path_distinct_from_source": True,
                "source_identity": SOURCE_ID,
                "snapshot_identity": SNAPSHOT_ID,
                "restored_copy_path": RESTORE_PATH,
                "observed_at_utc": RESTORE_OBSERVED,
            },
            "target_rehearsal": _target_rehearsal(source_schema),
            "capacity": _capacity_ok(),
            "go_for_runtime_deployment": True,
            "telegram_bot_token": "should-not-leak",
        },
    }


def test_missing_source_does_not_create_database_or_output(tmp_path: Path) -> None:
    source = tmp_path / "missing.sqlite"
    output = tmp_path / "out.json"
    with pytest.raises(readiness.ReadinessError, match="does not exist"):
        readiness.collect_runtime_checkpoint_preflight(source)
    assert not source.exists()
    assert not Path(f"{source}-wal").exists()
    assert not Path(f"{source}-shm").exists()
    missing_parent = tmp_path / "no-dir" / "out.json"
    with pytest.raises(readiness.ReadinessError, match="parent directory"):
        readiness.write_report_exclusive({"ok": True}, missing_parent)
    assert not output.exists()
    assert not missing_parent.exists()


def test_cli_missing_source_creates_nothing(tmp_path: Path) -> None:
    source = tmp_path / "nope.sqlite"
    output = tmp_path / "report.json"
    code = readiness_cli.main(
        ["collect", "--source-path", str(source), "--output-path", str(output)]
    )
    assert code == 2
    assert not source.exists()
    assert not output.exists()


def test_non_database_file_keeps_filesystem_and_marks_sqlite_unavailable(tmp_path: Path) -> None:
    source = tmp_path / "notes.txt"
    source.write_text("not sqlite", encoding="utf-8")
    before = source.read_bytes()
    report = readiness.collect_runtime_checkpoint_preflight(source)
    assert report["filesystem"]["status"] == "measured"
    assert report["filesystem"]["main"]["size_bytes"] == len(before)
    assert report["sqlite"]["status"] == readiness.UNAVAILABLE
    assert source.read_bytes() == before
    assert report["observed_deployed_application"]["status"] == readiness.UNAVAILABLE
    assert report["go_for_runtime_deployment"] is False


def test_filesystem_only_does_not_open_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _v25(tmp_path)

    def must_not_open(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("filesystem-only must not open SQLite")

    monkeypatch.setattr(readiness, "open_read_only_database", must_not_open)
    report = readiness.collect_runtime_checkpoint_preflight(source, filesystem_only=True)
    assert report["sqlite"] == {"status": "not_requested", "reason": "filesystem_only"}
    assert report["filesystem"]["main"]["exists"] is True
    assert report["filesystem"]["volume"]["status"] == "measured"


def test_initialized_v25_metadata_does_not_count_or_migrate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _v25(tmp_path)
    before = _state(path)
    real_open = readiness.open_read_only_database
    traced: list[str] = []

    def guarded_open(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_open(*args, **kwargs)
        traced.append("opened")
        _attach_sql_guard(connection)
        return connection

    monkeypatch.setattr(readiness, "open_read_only_database", guarded_open)
    report = readiness.collect_runtime_checkpoint_preflight(path)
    sqlite_section = report["sqlite"]
    assert sqlite_section["status"] == "measured"
    assert sqlite_section["schema_version"] == SCHEMA_VERSION == 26
    assert sqlite_section["connection"]["sqlite_uri_mode"] == "ro"
    assert sqlite_section["connection"]["query_only_readback"] == 1
    assert sqlite_section["connection"]["immutable_requested"] is False
    assert sqlite_section["connection"]["live_mutable_source"] is True
    assert sqlite_section["recent_runs"]["status"] == "not_requested"
    assert "scan_runs" in sqlite_section["tables"]
    assert "raw_payload_format" in sqlite_section["columns"]["scan_runs"]
    assert "ix_scan_runs_timestamp" in sqlite_section["indexes"]["scan_runs"]
    assert sqlite_section["plan_state_index"]["status"] == "matched"
    assert sqlite_section["plan_state_index"]["columns"] == list(readiness.PLAN_STATE_INDEX_COLUMNS)
    assert sqlite_section["plan_state_index"]["partial_predicate"] == readiness.PLAN_STATE_INDEX_PREDICATE
    assert sqlite_section["epoch_lineage"]["tables"]["runtime_epochs"]["matches_expected"] is True
    assert sqlite_section["epoch_lineage"]["tables"]["runtime_operational_origins"]["matches_expected"] is True
    assert report["release_contract"]["packet_contract_version"] == readiness.PACKET_CONTRACT_VERSION
    assert report["collector_identity"]["tool_version"] == readiness.TOOL_VERSION
    assert report["observed_deployed_application"]["reason"] == "not_inferred_from_collector_checkout"
    collector_sha = report["collector_identity"]["git"].get("commit_sha")
    if isinstance(collector_sha, str):
        assert report["observed_deployed_application"].get("commit_sha") is None
    assert _state(path) == before
    assert traced == ["opened"]


def test_collect_never_requests_immutable_when_sidecars_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "no-sidecar.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t(x INTEGER)")
        connection.execute("PRAGMA user_version = 1")
    uris: list[str] = []
    real_connect = database_module.sqlite3.connect

    def spy(database: object, *args: object, **kwargs: object) -> sqlite3.Connection:
        uris.append(str(database))
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(database_module.sqlite3, "connect", spy)
    report = readiness.collect_runtime_checkpoint_preflight(path)
    assert report["sqlite"]["status"] == "measured"
    assert any("mode=ro" in uri for uri in uris)
    assert all("immutable=1" not in uri for uri in uris)
    assert report["sqlite"]["connection"]["immutable_requested"] is False


def test_wal_header_without_sidecars_does_not_create_wal_or_shm(tmp_path: Path) -> None:
    path = tmp_path / "closed-wal.sqlite"
    with open_initialized_database(path) as connection:
        connection.commit()
    Path(f"{path}-wal").unlink(missing_ok=True)
    Path(f"{path}-shm").unlink(missing_ok=True)
    before = _state(path)
    assert before[3:] == (False, False)
    report = readiness.collect_runtime_checkpoint_preflight(path)
    assert report["sqlite"]["status"] == readiness.UNAVAILABLE
    assert report["sqlite"]["reason"] == "wal_header_without_sidecars"
    assert report["filesystem"]["status"] == "measured"
    assert _state(path)[3:] == (False, False)


def test_wal_without_shm_keeps_filesystem_and_does_not_create_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "wal-only.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE t(x INTEGER)")
        connection.execute("PRAGMA user_version = 1")
    wal = Path(f"{path}-wal")
    wal.write_bytes(b"fixture-wal")
    shm = Path(f"{path}-shm")
    assert not shm.exists()
    before = path.read_bytes()
    report = readiness.collect_runtime_checkpoint_preflight(path)
    assert report["filesystem"]["wal"]["exists"] is True
    assert report["filesystem"]["shm"]["exists"] is False
    assert report["sqlite"]["status"] == readiness.UNAVAILABLE
    assert report["sqlite"]["reason"] == "wal_without_shm"
    assert path.read_bytes() == before
    assert wal.exists()
    assert not shm.exists()


def test_live_wal_with_sidecars_is_opened_without_immutable(tmp_path: Path) -> None:
    path = tmp_path / "live-wal.sqlite"
    with open_initialized_database(path) as writer:
        writer.execute(
            """
            INSERT INTO scan_runs (
                run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
                strategy, timeframes_json, market_regime, runtime_stats_json,
                command_preset, command_used, total_valid_setups, near_misses,
                rejected, data_issues, data_issues_json, raw_payload_json
            ) VALUES (
                'live', '2026-09-11T12:00:00Z', 'binance', 'test', 1, '["BTCUSDT"]',
                'test', '{}', 'unknown', '{}', 'N/A', 'N/A', 0, 0, 0, 0, '[]', '{}'
            )
            """
        )
        writer.commit()
        assert Path(f"{path}-wal").exists() or Path(f"{path}-shm").exists()
        report = readiness.collect_runtime_checkpoint_preflight(path, include_recent_runs=True)
    assert report["sqlite"]["status"] == "measured"
    assert report["sqlite"]["connection"]["immutable_requested"] is False
    assert report["sqlite"]["connection"]["live_mutable_source"] is True
    assert report["sqlite"]["schema_version"] == 26
    assert report["sqlite"]["recent_runs"]["status"] == "measured"


def test_older_and_newer_schemas_are_not_initialized_or_migrated(tmp_path: Path) -> None:
    older = tmp_path / "v24.sqlite"
    _legacy_v24(older)
    older_before = _state(older)
    older_report = readiness.collect_runtime_checkpoint_preflight(older, include_recent_runs=True)
    assert older_report["sqlite"]["schema_version"] == 24
    assert older_report["sqlite"]["schema_status"] == "older-supported-for-read-only-inspection"
    assert "raw_payload_format" not in set(older_report["sqlite"]["columns"]["scan_runs"])
    sample = older_report["sqlite"]["recent_runs"]["rows"][0]["raw_payload_format"]
    assert sample["status"] == readiness.UNAVAILABLE
    assert sample["reason"] == "column_absent"
    with sqlite3.connect(older) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(scan_runs)")}
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert "raw_payload_format" not in columns
    assert version == 24
    assert _state(older) == older_before

    newer = tmp_path / "v26.sqlite"
    with sqlite3.connect(newer) as connection:
        connection.execute("CREATE TABLE future(value TEXT)")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    newer_before = _state(newer)
    newer_report = readiness.collect_runtime_checkpoint_preflight(newer)
    assert newer_report["sqlite"]["schema_version"] == SCHEMA_VERSION + 1
    assert newer_report["sqlite"]["schema_status"] == "unsupported-newer"
    with sqlite3.connect(newer) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert names == {"future"}
    assert _state(newer) == newer_before


def test_recent_runs_require_proven_index(tmp_path: Path) -> None:
    indexed = _v25(tmp_path, "indexed.sqlite")
    report = readiness.collect_runtime_checkpoint_preflight(indexed, include_recent_runs=True)
    recent = report["sqlite"]["recent_runs"]
    assert recent["status"] == "measured"
    assert recent["row_count"] == 1
    assert "ix_scan_runs_timestamp" in recent["query_plan"]
    assert recent["rows"][0]["run_id"] == "run-1"

    unindexed = tmp_path / "no-index.sqlite"
    with sqlite3.connect(unindexed) as connection:
        connection.execute(
            "CREATE TABLE scan_runs (run_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO scan_runs VALUES ('x', '2026-09-01T00:00:00Z')")
        connection.execute("PRAGMA user_version = 25")
    blocked = readiness.collect_runtime_checkpoint_preflight(unindexed, include_recent_runs=True)
    assert blocked["sqlite"]["recent_runs"]["status"] == readiness.UNAVAILABLE
    assert blocked["sqlite"]["recent_runs"]["reason"] == "timestamp_index_absent"


def test_query_deadline_unavailable_and_connection_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _v25(tmp_path, "timeout.sqlite")
    before = _state(path)
    real_open = readiness.open_read_only_database

    def exploding_open(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_open(*args, **kwargs)
        original = connection.execute

        def execute(sql: str, *rest: object, **extra: object) -> sqlite3.Cursor:
            if sql.startswith("PRAGMA page_count"):
                raise sqlite3.OperationalError("interrupted")
            return original(sql, *rest, **extra)

        connection.execute = execute  # type: ignore[method-assign]
        return connection

    monkeypatch.setattr(readiness, "open_read_only_database", exploding_open)
    report = readiness.collect_runtime_checkpoint_preflight(path)
    assert report["sqlite"]["status"] == readiness.UNAVAILABLE
    assert report["sqlite"]["reason"] == "query_deadline_exceeded"
    assert report["filesystem"]["status"] == "measured"
    assert _state(path)[3:] == before[3:]
    with connect_database(path) as writer:
        writer.execute(
            """
            INSERT INTO scan_runs (
                run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
                strategy, timeframes_json, market_regime, runtime_stats_json,
                command_preset, command_used, total_valid_setups, near_misses,
                rejected, data_issues, data_issues_json, raw_payload_json
            ) VALUES (
                'after-timeout', '2026-09-11T14:00:00Z', 'binance', 'test', 1, '["BTCUSDT"]',
                'test', '{}', 'unknown', '{}', 'N/A', 'N/A', 0, 0, 0, 0, '[]', '{}'
            )
            """
        )
        writer.commit()
        count = writer.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
    assert count == 2


def test_output_is_exclusive_and_redacts_secrets(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    payload = {
        "ok": True,
        "telegram_bot_token": "secret-token",
        "nested": {"chat_id": 1, "safe": "keep"},
        "command_line": "python --token x",
    }
    written = readiness.write_report_exclusive(payload, output)
    loaded = json.loads(written.read_text(encoding="utf-8"))
    assert loaded["ok"] is True
    assert loaded["nested"]["safe"] == "keep"
    assert "telegram_bot_token" not in loaded
    assert "command_line" not in loaded
    assert "chat_id" not in loaded["nested"]
    with pytest.raises(readiness.ReadinessError, match="overwrite"):
        readiness.write_report_exclusive(payload, output)
    assert json.loads(output.read_text(encoding="utf-8")) == loaded


def test_cli_collect_and_assess_round_trip(tmp_path: Path) -> None:
    source = _v25(tmp_path)
    collect_out = tmp_path / "collect.json"
    assert (
        readiness_cli.main(
            ["collect", "--source-path", str(source), "--output-path", str(collect_out)]
        )
        == 0
    )
    collector = json.loads(collect_out.read_text(encoding="utf-8"))
    evidence = tmp_path / "evidence.json"
    packet = _complete_packet(collector)
    evidence.write_text(json.dumps(packet), encoding="utf-8")
    assess_out = tmp_path / "assess.json"
    code = readiness_cli.main(
        ["assess", "--evidence-path", str(evidence), "--output-path", str(assess_out)]
    )
    assessment = json.loads(assess_out.read_text(encoding="utf-8"))
    assert assessment["go_for_runtime_deployment"] is False
    assert assessment["operational_status"] == readiness.OPERATIONAL_STATUS
    assert "telegram_bot_token" not in json.dumps(assessment)
    assert assessment["overall_disposition"] == "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    assert code == 0


def test_assessment_adversarial_inputs_never_fabricate_go() -> None:
    empty = readiness.assess_runtime_checkpoint_evidence({})
    assert empty["go_for_runtime_deployment"] is False
    assert empty["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "operator.deployed_application_sha" in empty["missing_prerequisites"]

    wrong_sha = _complete_packet()
    wrong_sha["operator"]["deployed_application_sha"] = "not-a-sha"
    wrong = readiness.assess_runtime_checkpoint_evidence(wrong_sha)
    assert wrong["overall_disposition"] == "INCOMPLETE_PREREQUISITES"

    mismatched = _complete_packet(
        {"sqlite": {"status": "measured", "schema_version": SCHEMA_VERSION + 3}, "filesystem": {"status": "measured"}}
    )
    adverse_schema = readiness.assess_runtime_checkpoint_evidence(mismatched)
    assert adverse_schema["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert any("newer than supported" in item for item in adverse_schema["adverse_results"])

    missing_consumer = _complete_packet(
        {"sqlite": {"status": "measured", "schema_version": 25}, "filesystem": {"status": "measured"}}
    )
    missing_consumer["operator"]["accounted_consumers"] = ["scanner_watch_store"]
    incomplete_consumers = readiness.assess_runtime_checkpoint_evidence(missing_consumer)
    assert incomplete_consumers["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert any(item.startswith("unresolved_consumers:") for item in incomplete_consumers["missing_prerequisites"])

    zero_growth = _complete_packet(
        {"sqlite": {"status": "measured", "schema_version": 25}, "filesystem": {"status": "measured"}}
    )
    zero_growth["operator"]["capacity"]["growth_budget_bytes"] = 0
    assert readiness.assess_runtime_checkpoint_evidence(zero_growth)["overall_disposition"] == "INCOMPLETE_PREREQUISITES"

    negative = _complete_packet(
        {"sqlite": {"status": "measured", "schema_version": 25}, "filesystem": {"status": "measured"}}
    )
    negative["operator"]["capacity"]["growth_budget_bytes"] = -1
    assert readiness.assess_runtime_checkpoint_evidence(negative)["overall_disposition"] == "INCOMPLETE_PREREQUISITES"

    unknown_growth = _complete_packet(
        {"sqlite": {"status": "measured", "schema_version": 25}, "filesystem": {"status": "measured"}}
    )
    unknown_growth["operator"]["capacity"].pop("growth_budget_bytes")
    assert (
        readiness.assess_runtime_checkpoint_evidence(unknown_growth)["overall_disposition"]
        == "INCOMPLETE_PREREQUISITES"
    )

    shared = _complete_packet(
        {"sqlite": {"status": "measured", "schema_version": 25}, "filesystem": {"status": "measured"}}
    )
    shared["operator"]["capacity"]["volume_free_bytes"] = 1
    adverse_cap = readiness.assess_runtime_checkpoint_evidence(shared)
    assert adverse_cap["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "insufficient_free_capacity" in adverse_cap["adverse_results"]
    assert adverse_cap["capacity"]["shared_volume_accounting"] is True

    split = _complete_packet(
        {"sqlite": {"status": "measured", "schema_version": 25}, "filesystem": {"status": "measured"}}
    )
    split["operator"]["capacity"]["backup_shares_db_volume"] = False
    split["operator"]["capacity"]["restore_shares_db_volume"] = False
    split_result = readiness.assess_runtime_checkpoint_evidence(split)
    assert split_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"

    no_restore = _complete_packet(
        {"sqlite": {"status": "measured", "schema_version": 25}, "filesystem": {"status": "measured"}}
    )
    no_restore["operator"]["restore_evidence"] = {}
    assert readiness.assess_runtime_checkpoint_evidence(no_restore)["overall_disposition"] == "INCOMPLETE_PREREQUISITES"

    failed_restore = _complete_packet(
        {"sqlite": {"status": "measured", "schema_version": 25}, "filesystem": {"status": "measured"}}
    )
    failed_restore["operator"]["restore_evidence"]["integrity_ok"] = False
    failed = readiness.assess_runtime_checkpoint_evidence(failed_restore)
    assert failed["overall_disposition"] == "ADVERSE_MEASURED_RESULT"


def test_separate_backup_volume_insufficient_is_adverse() -> None:
    capacity = _capacity_ok()
    capacity["backup_shares_db_volume"] = False
    capacity["restore_shares_db_volume"] = True
    capacity["backup_volume_total_bytes"] = _gib(100)
    capacity["backup_volume_free_bytes"] = _gib(1)
    result = readiness.assess_capacity_budget(capacity)
    assert result["status"] == "insufficient"
    assert result["sufficient_on_packet"] is False
    assert result["volumes"]["db"]["sufficient"] is True
    assert result["volumes"]["backup"]["sufficient"] is False
    assert result["volumes"]["backup"]["required_bytes"] == _external_required(
        capacity["new_backup_bytes"], capacity["backup_volume_total_bytes"]
    )
    assert "new_backup_bytes" not in result["volumes"]["db"]["allocations"]

    packet = _complete_packet(_measured_collector())
    packet["operator"]["capacity"] = capacity
    assessment = readiness.assess_runtime_checkpoint_evidence(packet)
    assert assessment["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert assessment["go_for_runtime_deployment"] is False
    assert assessment["overall_disposition"] != "PACKET_REVIEWABLE_NOT_AUTHORIZED"


def test_separate_restore_volume_insufficient_is_adverse() -> None:
    capacity = _capacity_ok()
    capacity["backup_shares_db_volume"] = True
    capacity["restore_shares_db_volume"] = False
    capacity["restore_volume_total_bytes"] = _gib(100)
    capacity["restore_volume_free_bytes"] = _gib(1)
    result = readiness.assess_capacity_budget(capacity)
    assert result["status"] == "insufficient"
    assert result["volumes"]["db"]["sufficient"] is True
    assert result["volumes"]["restore"]["sufficient"] is False
    assert result["volumes"]["restore"]["required_bytes"] == _external_required(
        capacity["concurrent_restore_or_candidate_bytes"], capacity["restore_volume_total_bytes"]
    )
    assert "concurrent_restore_or_candidate_bytes" not in result["volumes"]["db"]["allocations"]

    packet = _complete_packet(_measured_collector())
    packet["operator"]["capacity"] = capacity
    assessment = readiness.assess_runtime_checkpoint_evidence(packet)
    assert assessment["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert assessment["go_for_runtime_deployment"] is False


def test_separate_backup_and_restore_volumes_sufficient_are_not_double_charged() -> None:
    capacity = _distinct_external_capacity(backup_free=_gib(40), restore_free=_gib(40))
    result = readiness.assess_capacity_budget(capacity)
    assert result["status"] == "measured"
    assert result["sufficient_on_packet"] is True
    db = result["volumes"]["db"]
    backup = result["volumes"]["ext-backup"]
    restore = result["volumes"]["ext-restore"]
    assert db["required_bytes"] == _db_volume_only_required(capacity)
    assert db["required_bytes"] == _gib(5) + _gib(2) + _gib(8) + _gib(10)
    assert "new_backup_bytes" not in db["allocations"]
    assert "concurrent_restore_or_candidate_bytes" not in db["allocations"]
    assert backup["required_bytes"] == _external_required(_gib(20), _gib(100))
    assert restore["required_bytes"] == _external_required(_gib(20), _gib(100))
    assert "concurrent_restore_or_candidate_bytes" not in backup["allocations"]
    assert "new_backup_bytes" not in restore["allocations"]
    packet = _complete_packet(_measured_collector())
    packet["operator"]["capacity"] = capacity
    assessment = readiness.assess_runtime_checkpoint_evidence(packet)
    assert assessment["overall_disposition"] == "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    assert assessment["go_for_runtime_deployment"] is False


def test_mixed_topology_charges_only_the_correct_volume() -> None:
    backup_on_db = _capacity_ok()
    backup_on_db["backup_shares_db_volume"] = True
    backup_on_db["restore_shares_db_volume"] = False
    backup_on_db["restore_volume_total_bytes"] = _gib(100)
    backup_on_db["restore_volume_free_bytes"] = _gib(40)
    mixed_backup = readiness.assess_capacity_budget(backup_on_db)
    db = mixed_backup["volumes"]["db"]
    restore = mixed_backup["volumes"]["restore"]
    assert "new_backup_bytes" in db["allocations"]
    assert "concurrent_restore_or_candidate_bytes" not in db["allocations"]
    assert db["required_bytes"] == _db_volume_only_required(backup_on_db)
    assert restore["required_bytes"] == _external_required(
        backup_on_db["concurrent_restore_or_candidate_bytes"], backup_on_db["restore_volume_total_bytes"]
    )
    assert restore["applied_reserve_bytes"] == _volume_floor(backup_on_db["restore_volume_total_bytes"])
    assert "backup" not in mixed_backup["volumes"]
    assert mixed_backup["status"] == "measured"

    restore_on_db = _capacity_ok()
    restore_on_db["backup_shares_db_volume"] = False
    restore_on_db["restore_shares_db_volume"] = True
    restore_on_db["backup_volume_total_bytes"] = _gib(100)
    restore_on_db["backup_volume_free_bytes"] = _gib(40)
    mixed_restore = readiness.assess_capacity_budget(restore_on_db)
    db = mixed_restore["volumes"]["db"]
    backup = mixed_restore["volumes"]["backup"]
    assert "concurrent_restore_or_candidate_bytes" in db["allocations"]
    assert "new_backup_bytes" not in db["allocations"]
    assert db["required_bytes"] == _db_volume_only_required(restore_on_db)
    assert backup["required_bytes"] == _external_required(
        restore_on_db["new_backup_bytes"], restore_on_db["backup_volume_total_bytes"]
    )
    assert backup["applied_reserve_bytes"] == _volume_floor(restore_on_db["backup_volume_total_bytes"])
    assert "restore" not in mixed_restore["volumes"]
    assert mixed_restore["status"] == "measured"


def test_operating_reserve_below_floor_never_sufficient_even_with_stronger_flag() -> None:
    capacity = _capacity_ok()
    floor = max(_gib(10), int(capacity["volume_total_bytes"]) // 10)
    capacity["operating_reserve_bytes"] = floor - 1
    capacity["stronger_reserve_requirement_recorded"] = True
    result = readiness.assess_capacity_budget(capacity)
    assert result["status"] != "measured"
    assert result.get("sufficient_on_packet") is not True
    assert result["status"] in {"incomplete", "insufficient"}
    packet = _complete_packet(_measured_collector())
    packet["operator"]["capacity"] = capacity
    assessment = readiness.assess_runtime_checkpoint_evidence(packet)
    assert assessment["overall_disposition"] in {"INCOMPLETE_PREREQUISITES", "ADVERSE_MEASURED_RESULT"}
    assert assessment["overall_disposition"] != "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    assert assessment["go_for_runtime_deployment"] is False
    assert assessment["capacity"].get("sufficient_on_packet") is not True


def test_stronger_reserve_above_floor_is_applied() -> None:
    capacity = _capacity_ok()
    floor = max(_gib(10), int(capacity["volume_total_bytes"]) // 10)
    stronger = floor + _gib(15)
    capacity["stronger_reserve_requirement_bytes"] = stronger
    capacity["volume_free_bytes"] = _db_volume_only_required(capacity, applied_reserve=stronger) + _gib(1)
    result = readiness.assess_capacity_budget(capacity)
    assert result["status"] == "measured"
    assert result["volumes"]["db"]["applied_reserve_bytes"] == stronger
    assert result["volumes"]["db"]["required_bytes"] == _db_volume_only_required(capacity, applied_reserve=stronger)
    assert result["volumes"]["db"]["planning_reserve_floor_bytes"] == floor
    too_small = dict(capacity)
    too_small["volume_free_bytes"] = _db_volume_only_required(capacity, applied_reserve=floor) + _gib(1)
    blocked = readiness.assess_capacity_budget(too_small)
    assert blocked["status"] == "insufficient"
    assert blocked["volumes"]["db"]["applied_reserve_bytes"] == stronger


def test_shared_volume_sufficient_and_insufficient_behavior_is_preserved() -> None:
    ok = readiness.assess_capacity_budget(_capacity_ok())
    assert ok["status"] == "measured"
    assert ok["shared_volume_accounting"] is True
    assert ok["sufficient_on_packet"] is True
    assert ok["volumes"]["db"]["required_bytes"] == _db_volume_only_required(_capacity_ok())
    assert set(ok["volumes"]["db"]["roles"]) == {"db", "backup", "restore"}

    short = _capacity_ok()
    short["volume_free_bytes"] = 1
    adverse = readiness.assess_capacity_budget(short)
    assert adverse["status"] == "insufficient"
    assert adverse["shared_volume_accounting"] is True
    packet = _complete_packet(_measured_collector())
    packet["operator"]["capacity"] = short
    assessment = readiness.assess_runtime_checkpoint_evidence(packet)
    assert assessment["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "insufficient_free_capacity" in assessment["adverse_results"]


def test_same_external_volume_does_not_reuse_free_space_twice() -> None:
    capacity = _same_external_capacity(free_bytes=_gib(30))
    result = readiness.assess_capacity_budget(capacity)
    assert result["status"] == "insufficient"
    assert result["sufficient_on_packet"] is False
    shared = result["volumes"]["ext-shared"]
    assert set(shared["roles"]) == {"backup", "restore"}
    assert shared["required_bytes"] == _external_required(_gib(20) + _gib(20), _gib(100))
    assert shared["free_bytes"] == _gib(30)
    assert shared["required_bytes"] > shared["free_bytes"]
    packet = _complete_packet(_measured_collector())
    packet["operator"]["capacity"] = capacity
    assessment = readiness.assess_runtime_checkpoint_evidence(packet)
    assert assessment["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert assessment["overall_disposition"] != "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    assert assessment["go_for_runtime_deployment"] is False


def test_same_external_volume_sufficient_when_combined_plus_reserve_fits() -> None:
    capacity = _same_external_capacity(free_bytes=_gib(51))
    result = readiness.assess_capacity_budget(capacity)
    assert result["status"] == "measured"
    shared = result["volumes"]["ext-shared"]
    assert shared["required_bytes"] == _gib(20) + _gib(20) + _volume_floor(_gib(100))
    assert shared["applied_reserve_bytes"] == _volume_floor(_gib(100))
    assert result["volumes"]["db"]["required_bytes"] == _db_volume_only_required(capacity)
    packet = _complete_packet(_measured_collector())
    packet["operator"]["capacity"] = capacity
    assessment = readiness.assess_runtime_checkpoint_evidence(packet)
    assert assessment["overall_disposition"] == "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    assert assessment["go_for_runtime_deployment"] is False


def test_distinct_external_volumes_do_not_charge_each_other() -> None:
    capacity = _distinct_external_capacity(backup_free=_gib(31), restore_free=_gib(31))
    result = readiness.assess_capacity_budget(capacity)
    assert result["status"] == "measured"
    backup = result["volumes"]["ext-backup"]
    restore = result["volumes"]["ext-restore"]
    assert backup["required_bytes"] == _external_required(_gib(20), _gib(100))
    assert restore["required_bytes"] == _external_required(_gib(20), _gib(100))
    assert "concurrent_restore_or_candidate_bytes" not in backup["allocations"]
    assert "new_backup_bytes" not in restore["allocations"]
    assert backup["roles"] == ["backup"]
    assert restore["roles"] == ["restore"]


def test_external_volume_matching_allocation_without_reserve_is_not_sufficient() -> None:
    capacity = _capacity_ok()
    capacity["backup_shares_db_volume"] = False
    capacity["restore_shares_db_volume"] = True
    capacity["backup_volume_total_bytes"] = _gib(100)
    capacity["backup_volume_free_bytes"] = capacity["new_backup_bytes"]
    result = readiness.assess_capacity_budget(capacity)
    assert result["status"] == "insufficient"
    backup = result["volumes"]["backup"]
    assert backup["free_bytes"] == capacity["new_backup_bytes"]
    assert backup["required_bytes"] == _external_required(capacity["new_backup_bytes"], _gib(100))
    assert backup["applied_reserve_bytes"] == _volume_floor(_gib(100))
    assert backup["required_bytes"] > backup["free_bytes"]
    packet = _complete_packet(_measured_collector())
    packet["operator"]["capacity"] = capacity
    assessment = readiness.assess_runtime_checkpoint_evidence(packet)
    assert assessment["overall_disposition"] == "ADVERSE_MEASURED_RESULT"


def test_missing_topology_is_incomplete_and_does_not_default_to_shared_db() -> None:
    capacity = _capacity_ok()
    del capacity["backup_shares_db_volume"]
    del capacity["restore_shares_db_volume"]
    result = readiness.assess_capacity_budget(capacity)
    assert result["status"] == "incomplete"
    assert result["sufficient_on_packet"] is False
    assert "capacity.backup_shares_db_volume" in result["missing"]
    assert "capacity.restore_shares_db_volume" in result["missing"]
    packet = _complete_packet(_measured_collector())
    packet["operator"]["capacity"] = capacity
    assessment = readiness.assess_runtime_checkpoint_evidence(packet)
    assert assessment["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert assessment["overall_disposition"] != "PACKET_REVIEWABLE_NOT_AUTHORIZED"

    both_off = _capacity_ok()
    both_off["backup_shares_db_volume"] = False
    both_off["restore_shares_db_volume"] = False
    both_off["backup_volume_total_bytes"] = _gib(100)
    both_off["backup_volume_free_bytes"] = _gib(80)
    both_off["restore_volume_total_bytes"] = _gib(100)
    both_off["restore_volume_free_bytes"] = _gib(80)
    missing_share = readiness.assess_capacity_budget(both_off)
    assert missing_share["status"] == "incomplete"
    assert "capacity.backup_restore_share_volume" in missing_share["missing"]


def test_contradictory_topology_is_incomplete_never_sufficient() -> None:
    shares_db_and_distinct = _capacity_ok()
    shares_db_and_distinct["db_volume_label"] = "vol-db"
    shares_db_and_distinct["backup_volume_label"] = "vol-external"
    labeled = readiness.assess_capacity_budget(shares_db_and_distinct)
    assert labeled["status"] == "incomplete"
    assert labeled["sufficient_on_packet"] is not True
    assert any("backup_volume_label" in item for item in labeled["missing"])

    shares_and_fields = _capacity_ok()
    shares_and_fields["backup_volume_total_bytes"] = _gib(100)
    shares_and_fields["backup_volume_free_bytes"] = _gib(40)
    fields = readiness.assess_capacity_budget(shares_and_fields)
    assert fields["status"] == "incomplete"
    assert any("backup_volume_total_bytes" in item for item in fields["missing"])

    mixed_and_shared = _capacity_ok()
    mixed_and_shared["backup_shares_db_volume"] = True
    mixed_and_shared["restore_shares_db_volume"] = False
    mixed_and_shared["backup_restore_share_volume"] = True
    mixed_and_shared["restore_volume_total_bytes"] = _gib(100)
    mixed_and_shared["restore_volume_free_bytes"] = _gib(80)
    mixed = readiness.assess_capacity_budget(mixed_and_shared)
    assert mixed["status"] == "incomplete"
    assert any("backup_restore_share_volume=true" in item for item in mixed["missing"])

    same_label_but_distinct = _distinct_external_capacity(backup_free=_gib(80), restore_free=_gib(80))
    same_label_but_distinct["backup_volume_label"] = "same"
    same_label_but_distinct["restore_volume_label"] = "same"
    labels = readiness.assess_capacity_budget(same_label_but_distinct)
    assert labels["status"] == "incomplete"
    assert labels["sufficient_on_packet"] is not True

    packet = _complete_packet(_measured_collector())
    packet["operator"]["capacity"] = shares_db_and_distinct
    assessment = readiness.assess_runtime_checkpoint_evidence(packet)
    assert assessment["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert assessment["go_for_runtime_deployment"] is False


def test_windows_local_device_does_not_treat_unknown_as_local() -> None:
    assert readiness.classify_windows_local_device("DRIVE_FIXED") is True
    assert readiness.classify_windows_local_device("DRIVE_REMOVABLE") is True
    assert readiness.classify_windows_local_device("DRIVE_RAMDISK") is True
    assert readiness.classify_windows_local_device("DRIVE_REMOTE") is False
    unknown = readiness.classify_windows_local_device("DRIVE_UNKNOWN")
    assert unknown == {"status": readiness.UNAVAILABLE, "reason": "drive_type_DRIVE_UNKNOWN"}
    missing = readiness.classify_windows_local_device("DRIVE_NO_ROOT_DIR")
    assert missing == {"status": readiness.UNAVAILABLE, "reason": "drive_type_DRIVE_NO_ROOT_DIR"}


def test_complete_packet_is_reviewable_but_not_authorized(tmp_path: Path) -> None:
    collector = readiness.collect_runtime_checkpoint_preflight(_v25(tmp_path))
    assessment = readiness.assess_runtime_checkpoint_evidence(_complete_packet(collector))
    assert assessment["overall_disposition"] == "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    assert assessment["go_for_runtime_deployment"] is False
    assert assessment["operational_status"] == "RUNTIME_CHECKPOINT_NOT_YET_AUTHORIZED"
    assert assessment["release"]["functional_anchor_sha"] == ANCHOR
    assert assessment["release"]["tool_attested"] is False
    assert collector["sqlite"]["plan_state_index"]["status"] == "matched"
    assert collector["sqlite"]["plan_state_index"]["name"] == readiness.PLAN_STATE_INDEX_NAME
    assert collector["sqlite"]["plan_state_index"]["partial"] is True
    assert collector["sqlite"]["plan_state_index"]["unique"] is False
    assert assessment["source_compatibility"]["plan_state_index"] == "matched"
    assert assessment["target_rehearsal"]["plan_state_index"] == "matched"
    assert assessment["growth_trace"]["derived_budget_bytes"] == _BASELINE_DERIVED_BYTES
    assert assessment["growth_trace"]["tool_attested"] is False


def test_module_does_not_import_settings_telegram_or_writable_constructors() -> None:
    source = Path(readiness.__file__).read_text(encoding="utf-8")
    assert "app.core.config" not in source
    assert "from app.alerts" not in source
    assert "open_initialized_database" not in source
    assert "connect_database" not in source
    assert "inspect_database(" not in source
    assert "from app.storage.maintenance import" not in source
    assert "assume_immutable_when_sidecars_absent=False" in source


def test_inspect_database_still_uses_historic_immutable_default() -> None:
    source = inspect.getsource(inspect_database)
    assert "open_read_only_database(path, require_supported_schema=False)" in source
    assert "assume_immutable_when_sidecars_absent=False" not in source


def test_schema_version_and_decoder_inventory_remain_current() -> None:
    assert SCHEMA_VERSION == 26
    assert readiness.TOOL_VERSION == "cci-runtime-checkpoint-readiness-v3"
    assert readiness.PACKET_CONTRACT_VERSION == "f06-runtime-release-readiness-v2"
    assert set(readiness.DECODER_COMPATIBILITY_INVENTORY["supported_formats"]) == set(SUPPORTED_FORMATS)
    assert readiness.FUNCTIONAL_ANCHOR_SHA == ANCHOR
    assert readiness.PLAN_STATE_INDEX_COLUMNS == (
        "runtime_epoch_id",
        "current_state",
        "lifecycle_id",
    )
    assert readiness.PLAN_STATE_INDEX_PREDICATE == "plan_version_id is not null"
    wolf = next(item for item in readiness.CONSUMER_INVENTORY if item["id"] == "wolf_briefing")
    assert "load_logical_scan_payload" in str(wolf["payload"])
    inspect_item = next(item for item in readiness.CONSUMER_INVENTORY if item["id"] == "sqlite_maintenance_inspect")
    assert "assume_immutable_when_sidecars_absent=True" in str(inspect_item["payload"])
    replay = next(item for item in readiness.CONSUMER_INVENTORY if item["id"] == "durable_source_replay")
    assert "SOURCE_REPLAY_CAPTURE_ENABLED" in str(replay["startup"])
    source = Path(readiness.__file__).read_text(encoding="utf-8")
    assert "migrate_existing_database" not in source
    assert "CREATE INDEX" not in source


def test_concurrent_writer_changes_are_not_diagnostic_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _v25(tmp_path, "live-writer.sqlite")
    real_open = readiness.open_read_only_database

    def guarded_open(*args: object, **kwargs: object) -> sqlite3.Connection:
        connection = real_open(*args, **kwargs)
        _attach_sql_guard(connection)
        return connection

    monkeypatch.setattr(readiness, "open_read_only_database", guarded_open)
    with connect_database(path) as writer:
        writer.execute(
            """
            INSERT INTO scan_runs (
                run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
                strategy, timeframes_json, market_regime, runtime_stats_json,
                command_preset, command_used, total_valid_setups, near_misses,
                rejected, data_issues, data_issues_json, raw_payload_json
            ) VALUES (
                'run-writer', '2026-09-11T13:00:00Z', 'binance', 'test', 1, '["ETHUSDT"]',
                'test', '{}', 'unknown', '{}', 'N/A', 'N/A', 0, 0, 0, 0, '[]', '{}'
            )
            """
        )
        writer.commit()
        report = readiness.collect_runtime_checkpoint_preflight(path, include_recent_runs=True)
    assert report["sqlite"]["status"] == "measured"
    with open_read_only_database(path) as reader:
        count = reader.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
        version = identify_schema_version(reader)
    assert count == 2
    assert version == SCHEMA_VERSION


def test_no_cli_default_database_path() -> None:
    parser_source = inspect.getsource(readiness_cli._parse_args)
    assert "DEFAULT_DATABASE_PATH" not in parser_source
    assert "main_live_runtime" not in parser_source
    with pytest.raises(SystemExit):
        readiness_cli._parse_args(["collect"])


def test_reproduced_schema26_packet_is_no_longer_reviewable() -> None:
    legacy = {
        "collector_report": {
            "sqlite": {"status": "measured", "schema_version": 26, "indexes": {}},
            "filesystem": {"status": "measured"},
        },
        "operator": {
            "deployed_application_sha": FAKE_DEPLOYED,
            "target_sha": FAKE_TARGET,
            "accounted_consumers": [str(item["id"]) for item in readiness.CONSUMER_INVENTORY],
            "unknown_external_readers_resolved": True,
            "restore_evidence": {
                "restore_tested": True,
                "restore_path_distinct_from_source": True,
                "snapshot_identity": "filename-only.sqlite",
            },
            "capacity": {
                "volume_total_bytes": _gib(100),
                "volume_free_bytes": _gib(80),
                "new_backup_bytes": _gib(20),
                "concurrent_restore_or_candidate_bytes": _gib(20),
                "migration_temp_bytes": _gib(5),
                "additional_peak_WAL_and_log_bytes": _gib(2),
                "growth_budget_bytes": _gib(8),
                "operating_reserve_bytes": _gib(10),
                "backup_shares_db_volume": True,
                "restore_shares_db_volume": True,
            },
            "go_for_runtime_deployment": True,
        },
    }
    assessment = readiness.assess_runtime_checkpoint_evidence(legacy)
    assert assessment["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert assessment["go_for_runtime_deployment"] is False
    missing = assessment["missing_prerequisites"]
    assert "packet.contract_version" in missing
    assert "restore_evidence.integrity_ok" in missing
    assert "capacity.workload_baseline" in missing
    assert "operator.target_rehearsal" in missing
    assert assessment["source_compatibility"]["plan_state_index"] == "absent"
    assert assessment["evidence_origin"] == readiness.UNAVAILABLE


def test_missing_integrity_is_incomplete_and_failed_integrity_is_adverse() -> None:
    missing_integrity = _complete_packet(_measured_collector())
    missing_integrity["operator"]["restore_evidence"].pop("integrity_ok")
    omitted = readiness.assess_runtime_checkpoint_evidence(missing_integrity)
    assert omitted["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "restore_evidence.integrity_ok" in omitted["missing_prerequisites"]
    assert omitted["go_for_runtime_deployment"] is False

    unknown = _complete_packet(_measured_collector())
    unknown["operator"]["restore_evidence"]["integrity_ok"] = "unknown"
    unknown_result = readiness.assess_runtime_checkpoint_evidence(unknown)
    assert unknown_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "restore_evidence.integrity_ok" in unknown_result["missing_prerequisites"]

    filename_only = _complete_packet(_measured_collector())
    filename_only["operator"]["restore_evidence"]["integrity_evidence_class"] = "filename"
    filename_result = readiness.assess_runtime_checkpoint_evidence(filename_only)
    assert filename_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert filename_result["restore"]["tool_attested"] is False


def test_plan_state_index_name_is_not_compatibility() -> None:
    named_only = _complete_packet(_measured_collector(SCHEMA_VERSION))
    named_only["collector_report"]["sqlite"]["indexes"] = {
        "setup_lifecycle_records": [readiness.PLAN_STATE_INDEX_NAME]
    }
    named_only["collector_report"]["sqlite"].pop("plan_state_index")
    unproven = readiness.assess_runtime_checkpoint_evidence(named_only)
    assert unproven["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "source_plan_state_index_definition_unproven" in unproven["missing_prerequisites"]

    wrong = _complete_packet(_measured_collector(SCHEMA_VERSION))
    wrong["collector_report"]["sqlite"]["plan_state_index"] = {
        "status": "definition_mismatch",
        "name": readiness.PLAN_STATE_INDEX_NAME,
        "table": "setup_lifecycle_records",
        "columns": ["lifecycle_id"],
        "unique": False,
        "partial": False,
        "partial_predicate": None,
    }
    mismatched = readiness.assess_runtime_checkpoint_evidence(wrong)
    assert mismatched["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "source_plan_state_index_definition_mismatch" in mismatched["adverse_results"]
    assert mismatched["go_for_runtime_deployment"] is False

    wrong_target = _complete_packet(_measured_collector(25))
    wrong_target["operator"]["target_rehearsal"]["plan_state_index"] = {
        "table": "setup_lifecycle_records",
        "columns": ["current_state", "lifecycle_id"],
        "unique": False,
        "partial_predicate": "plan_version_id IS NOT NULL",
    }
    target = readiness.assess_runtime_checkpoint_evidence(wrong_target)
    assert target["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "target_rehearsal.plan_state_index_definition_mismatch" in target["adverse_results"]


def test_schema26_without_index_needs_verified_target_rehearsal() -> None:
    unverified = _complete_packet(_measured_collector(SCHEMA_VERSION))
    unverified["operator"]["target_rehearsal"].pop("migrated_schema_version")
    blocked = readiness.assess_runtime_checkpoint_evidence(unverified)
    assert blocked["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "target_rehearsal.migrated_schema_version" in blocked["missing_prerequisites"]

    verified = _complete_packet(_measured_collector(SCHEMA_VERSION))
    ready = readiness.assess_runtime_checkpoint_evidence(verified)
    assert ready["overall_disposition"] == "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    assert ready["go_for_runtime_deployment"] is False
    assert ready["source_compatibility"]["finding"] == "source_plan_state_index_absent"
    assert ready["target_rehearsal"]["plan_state_index"] == "matched"
    assert ready["target_rehearsal"]["final_cutover_reverification"] == "required_before_consumers"
    assert ready["evidence_origin"] == "synthetic"
    assert any("not Runtime measurements" in note for note in ready["notes"])


def test_older_source_rehearsal_must_reach_schema_26() -> None:
    older = _complete_packet(_measured_collector(24))
    accepted = readiness.assess_runtime_checkpoint_evidence(older)
    assert accepted["overall_disposition"] == "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    assert accepted["schema"]["observed"] == 24
    assert accepted["schema"]["target"] == 26
    assert accepted["go_for_runtime_deployment"] is False

    unverified = _complete_packet(_measured_collector(24))
    unverified["operator"]["target_rehearsal"]["migrated_schema_version"] = 25
    blocked = readiness.assess_runtime_checkpoint_evidence(unverified)
    assert blocked["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "target_rehearsal.migrated_schema_version" in blocked["missing_prerequisites"]


def test_growth_baseline_must_be_representative_and_long_enough() -> None:
    invented = _complete_packet(_measured_collector())
    invented["operator"]["capacity"].pop("workload_baseline")
    missing = readiness.assess_runtime_checkpoint_evidence(invented)
    assert missing["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "capacity.workload_baseline" in missing["missing_prerequisites"]
    assert any("not measured capacity" in note for note in missing["notes"])

    short = _complete_packet(_measured_collector())
    short["operator"]["capacity"]["workload_baseline"]["observation_ended_utc"] = "2026-10-03T09:00:00Z"
    short_result = readiness.assess_runtime_checkpoint_evidence(short)
    assert short_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "capacity.workload_baseline.observation_duration_below_24h" in short_result["missing_prerequisites"]

    idle = _complete_packet(_measured_collector())
    idle["operator"]["capacity"]["workload_baseline"]["status"] = "idle"
    idle["operator"]["capacity"]["growth_budget_bytes"] = 0
    idle_result = readiness.assess_runtime_checkpoint_evidence(idle)
    assert idle_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert any("idle" in item for item in idle_result["missing_prerequisites"])

    incomparable = _complete_packet(_measured_collector())
    incomparable["operator"]["capacity"]["workload_baseline"]["comparable"] = False
    incomparable["operator"]["capacity"]["workload_baseline"]["samples"] = [
        {"observed_at_utc": BASELINE_END, "quality": "incomparable"}
    ]
    incomparable_result = readiness.assess_runtime_checkpoint_evidence(incomparable)
    assert incomparable_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "capacity.workload_baseline.comparable" in incomparable_result["missing_prerequisites"]

    horizon = _complete_packet(_measured_collector())
    horizon["operator"]["capacity"]["workload_baseline"]["planning_horizon_days"] = 9
    horizon_result = readiness.assess_runtime_checkpoint_evidence(horizon)
    assert horizon_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "capacity.planning_horizon_days" in horizon_result["missing_prerequisites"]


def test_capture_enabled_is_outside_contract_and_unknown_is_not_disabled() -> None:
    enabled = _complete_packet(_measured_collector())
    enabled["operator"]["source_replay_capture"]["enabled"] = True
    enabled_result = readiness.assess_runtime_checkpoint_evidence(enabled)
    assert enabled_result["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "source_replay_capture_enabled_outside_initial_cutover_contract" in enabled_result["adverse_results"]
    assert enabled_result["go_for_runtime_deployment"] is False

    unknown = _complete_packet(_measured_collector())
    unknown["operator"]["source_replay_capture"]["enabled"] = "unknown"
    unknown_result = readiness.assess_runtime_checkpoint_evidence(unknown)
    assert unknown_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "source_replay_capture.enabled_unknown_is_not_disabled" in unknown_result["missing_prerequisites"]

    wrong_switch = _complete_packet(_measured_collector())
    wrong_switch["operator"]["source_replay_capture"]["capture_switch"] = "REPLAY_ENABLED"
    wrong_switch_result = readiness.assess_runtime_checkpoint_evidence(wrong_switch)
    assert wrong_switch_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "source_replay_capture.capture_switch" in wrong_switch_result["missing_prerequisites"]


def test_evidence_identity_distinguishes_fresh_stale_and_contradictory() -> None:
    fresh = readiness.assess_runtime_checkpoint_evidence(_complete_packet(_measured_collector()))
    assert fresh["overall_disposition"] == "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    assert fresh["evidence_identity"]["as_of_utc"] == AS_OF
    assert fresh["release"]["tool_attested"] is False
    assert fresh["release"]["functional_anchor_sha"] == ANCHOR

    stale = _complete_packet(_measured_collector())
    stale["operator"]["restore_evidence"]["observed_at_utc"] = "2026-09-01T00:00:00Z"
    stale_result = readiness.assess_runtime_checkpoint_evidence(stale)
    assert stale_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "stale_evidence:restore_evidence.observed_at_utc" in stale_result["missing_prerequisites"]

    future = _complete_packet(_measured_collector())
    future["operator"]["target_rehearsal"]["observed_at_utc"] = "2026-10-05T09:00:00Z"
    future_result = readiness.assess_runtime_checkpoint_evidence(future)
    assert future_result["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert any(item.startswith("contradictory_evidence_timestamp:") for item in future_result["adverse_results"])

    contradictory = _complete_packet(_measured_collector())
    contradictory["operator"]["target_rehearsal"]["source_identity"] = "other-source"
    contradictory["operator"]["target_rehearsal"]["source_schema_version"] = 24
    contradictory_result = readiness.assess_runtime_checkpoint_evidence(contradictory)
    assert contradictory_result["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "contradictory_source_identity" in contradictory_result["adverse_results"]
    assert "contradictory_source_schema_version" in contradictory_result["adverse_results"]

    rewritten_anchor = _complete_packet(_measured_collector())
    rewritten_anchor["operator"]["release_evidence"]["historical_functional_anchor_sha"] = FAKE_TARGET
    rewritten = readiness.assess_runtime_checkpoint_evidence(rewritten_anchor)
    assert rewritten["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "functional_anchor_rewritten" in rewritten["adverse_results"]
    assert rewritten["release"]["functional_anchor_sha"] == ANCHOR


def test_capacity_accounting_rejects_cleanup_and_compression_shortcuts() -> None:
    cleanup = _complete_packet(_measured_collector())
    cleanup["operator"]["capacity"]["reclaimed_cleanup_bytes"] = _gib(30)
    cleanup_result = readiness.assess_runtime_checkpoint_evidence(cleanup)
    assert cleanup_result["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "unperformed_cleanup_subtracted" in cleanup_result["adverse_results"]

    compressed = _complete_packet(_measured_collector())
    compressed["operator"]["capacity"]["compression_ratio"] = 0.5
    compressed_result = readiness.assess_runtime_checkpoint_evidence(compressed)
    assert compressed_result["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "compression_assumed" in compressed_result["adverse_results"]

    freelist = _complete_packet(_measured_collector())
    freelist["operator"]["capacity"]["freelist_not_subtracted"] = False
    freelist_result = readiness.assess_runtime_checkpoint_evidence(freelist)
    assert freelist_result["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "freelist_subtracted_from_capacity" in freelist_result["adverse_results"]


def test_reported_deployment_does_not_complete_missing_evidence() -> None:
    packet = _complete_packet(_measured_collector())
    packet["operator"]["deployment_reported_successful"] = True
    packet["operator"]["restore_evidence"].pop("integrity_ok")
    assessment = readiness.assess_runtime_checkpoint_evidence(packet)
    assert assessment["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "restore_evidence.integrity_ok" in assessment["missing_prerequisites"]
    assert any("reported successful deployment" in note for note in assessment["notes"])
    assert assessment["go_for_runtime_deployment"] is False


def test_collect_does_not_install_or_repair_plan_state_index(tmp_path: Path) -> None:
    missing = _v25(tmp_path, "missing-index.sqlite")
    with sqlite3.connect(missing) as connection:
        connection.execute(f"DROP INDEX IF EXISTS {readiness.PLAN_STATE_INDEX_NAME}")
        connection.commit()
    before = _state(missing)
    report = readiness.collect_runtime_checkpoint_preflight(missing)
    assert report["sqlite"]["plan_state_index"]["status"] == "absent"
    assert report["go_for_runtime_deployment"] is False
    with sqlite3.connect(missing) as connection:
        row = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?",
            (readiness.PLAN_STATE_INDEX_NAME,),
        ).fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert row is None
    assert version == SCHEMA_VERSION == 26
    assert _state(missing) == before

    wrong = _v25(tmp_path, "wrong-index.sqlite")
    with sqlite3.connect(wrong) as connection:
        connection.execute(f"DROP INDEX IF EXISTS {readiness.PLAN_STATE_INDEX_NAME}")
        connection.execute(
            f"""
            CREATE INDEX {readiness.PLAN_STATE_INDEX_NAME}
                ON setup_lifecycle_records(lifecycle_id)
            """
        )
        connection.commit()
    wrong_before = _state(wrong)
    wrong_report = readiness.collect_runtime_checkpoint_preflight(wrong)
    assert wrong_report["sqlite"]["plan_state_index"]["status"] == "definition_mismatch"
    assert wrong_report["sqlite"]["plan_state_index"]["columns"] == ["lifecycle_id"]
    assert _state(wrong) == wrong_before
    packet = _complete_packet(wrong_report)
    assessment = readiness.assess_runtime_checkpoint_evidence(packet)
    assert assessment["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert assessment["evidence_origin"] == "synthetic"
    assert assessment["go_for_runtime_deployment"] is False


def test_plan_state_index_identity_and_partial_flag_fail_closed() -> None:
    reviewable = readiness.assess_runtime_checkpoint_evidence(_complete_packet(_measured_collector(SCHEMA_VERSION)))
    assert reviewable["overall_disposition"] == "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    assert reviewable["source_compatibility"]["finding"] == "source_plan_state_index_absent"

    def target_case(mutation: str) -> dict[str, Any]:
        packet = _complete_packet(_measured_collector(SCHEMA_VERSION))
        index = packet["operator"]["target_rehearsal"]["plan_state_index"]
        if mutation == "wrong_name":
            index["name"] = "ix_unrelated_but_same_columns"
        elif mutation == "missing_name":
            index.pop("name")
        elif mutation == "partial_false":
            index["partial"] = False
        elif mutation == "partial_missing":
            index.pop("partial")
        elif mutation == "wrong_table":
            index["table"] = "scan_runs"
        elif mutation == "wrong_order":
            index["columns"] = ["current_state", "runtime_epoch_id", "lifecycle_id"]
        elif mutation == "wrong_predicate":
            index["partial_predicate"] = "1 = 1"
        elif mutation == "unique_true":
            index["unique"] = True
        return readiness.assess_runtime_checkpoint_evidence(packet)

    for mutation in ("wrong_name", "partial_false", "wrong_table", "wrong_order", "wrong_predicate", "unique_true"):
        adverse = target_case(mutation)
        assert adverse["overall_disposition"] == "ADVERSE_MEASURED_RESULT", mutation
        assert "target_rehearsal.plan_state_index_definition_mismatch" in adverse["adverse_results"]
        assert adverse["go_for_runtime_deployment"] is False

    for mutation in ("missing_name", "partial_missing"):
        incomplete = target_case(mutation)
        assert incomplete["overall_disposition"] == "INCOMPLETE_PREREQUISITES", mutation
        assert "target_rehearsal.plan_state_index_definition_mismatch" not in incomplete["adverse_results"]
        assert incomplete["go_for_runtime_deployment"] is False

    missing_name = target_case("missing_name")
    assert "target_rehearsal.plan_state_index.name" in missing_name["missing_prerequisites"]
    partial_missing = target_case("partial_missing")
    assert "target_rehearsal.plan_state_index.partial" in partial_missing["missing_prerequisites"]

    source_wrong = _complete_packet(_measured_collector(SCHEMA_VERSION))
    source_wrong["collector_report"]["sqlite"]["plan_state_index"] = _matching_plan_state_index()
    source_wrong["collector_report"]["sqlite"]["plan_state_index"]["name"] = "ix_other"
    source_result = readiness.assess_runtime_checkpoint_evidence(source_wrong)
    assert source_result["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "source_plan_state_index_definition_mismatch" in source_result["adverse_results"]

    source_partial = _complete_packet(_measured_collector(SCHEMA_VERSION))
    source_partial["collector_report"]["sqlite"]["plan_state_index"] = _matching_plan_state_index()
    source_partial["collector_report"]["sqlite"]["plan_state_index"].pop("partial")
    source_partial_result = readiness.assess_runtime_checkpoint_evidence(source_partial)
    assert source_partial_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "source_plan_state_index.partial" in source_partial_result["missing_prerequisites"]
    assert "source_plan_state_index_definition_incomplete" in source_partial_result["missing_prerequisites"]


def test_growth_budget_requires_a_quantitative_trace() -> None:
    positive = readiness.assess_runtime_checkpoint_evidence(_complete_packet(_measured_collector(SCHEMA_VERSION)))
    assert positive["overall_disposition"] == "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    assert positive["growth_trace"]["tool_attested"] is False
    assert positive["growth_trace"]["growth_evidence_class"] == "declared_assertion"
    assert readiness.derived_growth_budget_bytes(
        observed_delta_bytes=_BASELINE_DELTA_BYTES,
        observation_seconds=_BASELINE_SPAN_SECONDS,
        horizon_seconds=_BASELINE_HORIZON_SECONDS,
        allowance_bytes=0,
    ) == _BASELINE_DERIVED_BYTES
    assert (
        readiness.derived_growth_budget_bytes(
            observed_delta_bytes=1,
            observation_seconds=86400,
            horizon_seconds=864000,
            allowance_bytes=0,
        )
        == 10
    )
    assert (
        readiness.derived_growth_budget_bytes(
            observed_delta_bytes=0,
            observation_seconds=86400,
            horizon_seconds=864000,
        )
        is None
    )

    old_shape = _complete_packet(_measured_collector(SCHEMA_VERSION))
    old_shape["operator"]["capacity"]["workload_baseline"] = _label_only_baseline()
    old_shape["operator"]["capacity"]["growth_budget_bytes"] = 1
    old_shape["operator"]["capacity"]["workload_baseline"]["planning_horizon_days"] = 365
    unlabeled = readiness.assess_runtime_checkpoint_evidence(old_shape)
    assert unlabeled["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "capacity.workload_baseline.samples" in unlabeled["missing_prerequisites"]
    assert "capacity.growth_derivation" in unlabeled["missing_prerequisites"]
    assert unlabeled["go_for_runtime_deployment"] is False

    labels = _complete_packet(_measured_collector(SCHEMA_VERSION))
    labels["operator"]["capacity"]["workload_baseline"]["samples"] = [
        {"quality": "representative", "comparable": True},
        {"quality": "representative", "comparable": True},
    ]
    labels_result = readiness.assess_runtime_checkpoint_evidence(labels)
    assert labels_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "capacity.workload_baseline.samples[0].combined_allocated_bytes" in labels_result["missing_prerequisites"]
    assert "contradictory_growth_derivation" not in labels_result["adverse_results"]

    missing_derivation = _complete_packet(_measured_collector(SCHEMA_VERSION))
    missing_derivation["operator"]["capacity"]["workload_baseline"].pop("growth_derivation")
    missing_derivation_result = readiness.assess_runtime_checkpoint_evidence(missing_derivation)
    assert missing_derivation_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "capacity.growth_derivation" in missing_derivation_result["missing_prerequisites"]

    referenced = _complete_packet(_measured_collector(SCHEMA_VERSION))
    referenced["operator"]["capacity"]["workload_baseline"]["growth_derivation"] = _growth_derivation(
        evidence_class="referenced_result",
        evidence_reference="synthetic-evidence:/growth-summary.json",
    )
    referenced_result = readiness.assess_runtime_checkpoint_evidence(referenced)
    assert referenced_result["overall_disposition"] == "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    assert referenced_result["growth_trace"]["growth_evidence_class"] == "referenced_result"
    assert referenced_result["growth_trace"]["tool_attested"] is False

    bare_reference = _complete_packet(_measured_collector(SCHEMA_VERSION))
    bare_reference["operator"]["capacity"]["workload_baseline"]["growth_derivation"] = {
        "evidence_class": "referenced_result",
        "evidence_reference": "synthetic-evidence:/growth-summary.json",
        "unit": "bytes",
    }
    bare_result = readiness.assess_runtime_checkpoint_evidence(bare_reference)
    assert bare_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "capacity.growth_derivation.observed_delta_bytes" in bare_result["missing_prerequisites"]

    below = _complete_packet(_measured_collector(SCHEMA_VERSION))
    below["operator"]["capacity"]["growth_budget_bytes"] = _BASELINE_DERIVED_BYTES - 1
    below_result = readiness.assess_runtime_checkpoint_evidence(below)
    assert below_result["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "growth_budget_below_derived_requirement" in below_result["adverse_results"]
    assert below_result["go_for_runtime_deployment"] is False

    equal_budget = _complete_packet(_measured_collector(SCHEMA_VERSION))
    equal_budget["operator"]["capacity"]["growth_budget_bytes"] = _BASELINE_DERIVED_BYTES
    assert (
        readiness.assess_runtime_checkpoint_evidence(equal_budget)["overall_disposition"]
        == "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    )

    contradictory = _complete_packet(_measured_collector(SCHEMA_VERSION))
    contradictory["operator"]["capacity"]["workload_baseline"]["growth_derivation"]["projected_bytes"] = 1
    contradictory_result = readiness.assess_runtime_checkpoint_evidence(contradictory)
    assert contradictory_result["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "contradictory_growth_derivation" in contradictory_result["adverse_results"]

    year = _complete_packet(_measured_collector(SCHEMA_VERSION))
    year["operator"]["capacity"]["growth_budget_bytes"] = 1
    year["operator"]["capacity"]["workload_baseline"]["planning_horizon_days"] = 365
    year_result = readiness.assess_runtime_checkpoint_evidence(year)
    assert year_result["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "contradictory_growth_derivation" in year_result["adverse_results"]
    assert "growth_budget_below_derived_requirement" in year_result["adverse_results"]

    one_byte = _complete_packet(_measured_collector(SCHEMA_VERSION))
    baseline = one_byte["operator"]["capacity"]["workload_baseline"]
    baseline["observation_started_utc"] = "2026-10-04T06:00:00Z"
    baseline["observation_ended_utc"] = "2026-10-05T06:00:00Z"
    baseline["observation_duration_seconds"] = 86400
    baseline["samples"] = [
        _growth_sample("2026-10-04T06:00:00Z", 1000),
        _growth_sample("2026-10-05T06:00:00Z", 1001),
    ]
    baseline["growth_derivation"] = _growth_derivation(
        earliest_allocated_bytes=1000,
        latest_allocated_bytes=1001,
        observed_delta_bytes=1,
        observation_seconds=86400,
        projected_bytes=10,
        derived_budget_bytes=10,
    )
    one_byte["operator"]["capacity"]["growth_budget_bytes"] = 10
    one_byte_result = readiness.assess_runtime_checkpoint_evidence(one_byte)
    assert one_byte_result["overall_disposition"] == "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    one_byte["operator"]["capacity"]["growth_budget_bytes"] = 9
    one_byte_short = readiness.assess_runtime_checkpoint_evidence(one_byte)
    assert one_byte_short["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "growth_budget_below_derived_requirement" in one_byte_short["adverse_results"]

    shrinking = _complete_packet(_measured_collector(SCHEMA_VERSION))
    shrinking["operator"]["capacity"]["workload_baseline"]["samples"] = [
        _growth_sample(BASELINE_START, _BASELINE_EARLIEST_BYTES + 10),
        _growth_sample(BASELINE_END, _BASELINE_EARLIEST_BYTES),
    ]
    shrinking_result = readiness.assess_runtime_checkpoint_evidence(shrinking)
    assert shrinking_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "capacity.workload_baseline.shrinking_observation" in shrinking_result["missing_prerequisites"]
    assert "contradictory_growth_derivation" not in shrinking_result["adverse_results"]

    flat = _complete_packet(_measured_collector(SCHEMA_VERSION))
    flat["operator"]["capacity"]["workload_baseline"]["samples"] = [
        _growth_sample(BASELINE_START, _BASELINE_EARLIEST_BYTES),
        _growth_sample(BASELINE_END, _BASELINE_EARLIEST_BYTES),
    ]
    flat_result = readiness.assess_runtime_checkpoint_evidence(flat)
    assert flat_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "capacity.workload_baseline.flat_observation" in flat_result["missing_prerequisites"]

    bad_timestamp = _complete_packet(_measured_collector(SCHEMA_VERSION))
    bad_timestamp["operator"]["capacity"]["workload_baseline"]["samples"][1]["observed_at_utc"] = "not-a-timestamp"
    bad_time_result = readiness.assess_runtime_checkpoint_evidence(bad_timestamp)
    assert bad_time_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "capacity.workload_baseline.samples[1].observed_at_utc" in bad_time_result["missing_prerequisites"]

    bad_number = _complete_packet(_measured_collector(SCHEMA_VERSION))
    bad_number["operator"]["capacity"]["workload_baseline"]["samples"][0]["combined_allocated_bytes"] = 1.5
    bad_number["operator"]["capacity"]["workload_baseline"]["growth_derivation"]["horizon_seconds"] = "864000"
    bad_number_result = readiness.assess_runtime_checkpoint_evidence(bad_number)
    assert bad_number_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "capacity.workload_baseline.samples[0].combined_allocated_bytes" in bad_number_result["missing_prerequisites"]
    assert "capacity.growth_derivation.horizon_seconds" in bad_number_result["missing_prerequisites"]
    assert "contradictory_growth_derivation" not in bad_number_result["adverse_results"]

    stale = _complete_packet(_measured_collector(SCHEMA_VERSION))
    stale_baseline = stale["operator"]["capacity"]["workload_baseline"]
    stale_baseline["observation_started_utc"] = "2026-09-18T00:00:00Z"
    stale_baseline["observation_ended_utc"] = "2026-09-20T00:00:00Z"
    stale_baseline["observation_duration_seconds"] = 172800
    stale_baseline["samples"] = [
        _growth_sample("2026-09-18T00:00:00Z", 1000),
        _growth_sample("2026-09-20T00:00:00Z", 1000 + 172800),
    ]
    stale_baseline["growth_derivation"] = _growth_derivation(
        earliest_allocated_bytes=1000,
        latest_allocated_bytes=1000 + 172800,
        observed_delta_bytes=172800,
        observation_seconds=172800,
    )
    stale_result = readiness.assess_runtime_checkpoint_evidence(stale)
    assert stale_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "stale_evidence:capacity.workload_baseline.observation_ended_utc" in stale_result["missing_prerequisites"]


def test_malformed_nested_evidence_stays_structured() -> None:
    table = next(iter(readiness.EPOCH_LINEAGE_COLUMNS))

    origin = _complete_packet(_measured_collector(SCHEMA_VERSION))
    origin["operator"]["evidence_origin"] = []
    origin_result = readiness.assess_runtime_checkpoint_evidence(origin)
    assert origin_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "operator.evidence_origin" in origin_result["missing_prerequisites"]
    assert origin_result["evidence_origin"] == readiness.UNAVAILABLE
    assert origin_result["go_for_runtime_deployment"] is False
    assert "should-not-leak" not in json.dumps(origin_result)

    source_columns = _complete_packet(_measured_collector(SCHEMA_VERSION))
    tables = {
        name: {"present": True, "columns": list(columns)}
        for name, columns in readiness.EPOCH_LINEAGE_COLUMNS.items()
    }
    tables[table]["columns"] = 1
    source_columns["collector_report"]["sqlite"]["epoch_lineage"] = {"tables": tables}
    source_result = readiness.assess_runtime_checkpoint_evidence(source_columns)
    assert source_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert f"source_epoch_lineage.{table}.columns" in source_result["missing_prerequisites"]

    wrong_columns = _complete_packet(_measured_collector(SCHEMA_VERSION))
    wrong_tables = {
        name: {"present": True, "columns": list(columns)}
        for name, columns in readiness.EPOCH_LINEAGE_COLUMNS.items()
    }
    wrong_tables[table]["columns"] = ["not_a_lineage_column"]
    wrong_columns["collector_report"]["sqlite"]["epoch_lineage"] = {"tables": wrong_tables}
    wrong_source = readiness.assess_runtime_checkpoint_evidence(wrong_columns)
    assert wrong_source["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert f"source_epoch_lineage_definition_mismatch:{table}" in wrong_source["adverse_results"]

    both = _complete_packet(_measured_collector(SCHEMA_VERSION))
    both["operator"]["evidence_origin"] = []
    both["operator"]["source_replay_capture"]["enabled"] = True
    both_result = readiness.assess_runtime_checkpoint_evidence(both)
    assert both_result["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "operator.evidence_origin" in both_result["missing_prerequisites"]
    assert "source_replay_capture_enabled_outside_initial_cutover_contract" in both_result["adverse_results"]

    target_columns = _complete_packet(_measured_collector(SCHEMA_VERSION))
    target_columns["operator"]["target_rehearsal"]["epoch_lineage_tables"][table] = 1
    target_result = readiness.assess_runtime_checkpoint_evidence(target_columns)
    assert target_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert f"target_rehearsal.epoch_lineage_tables.{table}" in target_result["missing_prerequisites"]

    preservation = _complete_packet(_measured_collector(SCHEMA_VERSION))
    key = readiness.PRESERVATION_KEYS[0]
    preservation["operator"]["target_rehearsal"]["preservation"][key] = {}
    preservation_result = readiness.assess_runtime_checkpoint_evidence(preservation)
    assert preservation_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert f"target_rehearsal.preservation:{key}" in preservation_result["missing_prerequisites"]

    quality = _complete_packet(_measured_collector(SCHEMA_VERSION))
    quality["operator"]["capacity"]["workload_baseline"]["samples"] = [
        {
            "observed_at_utc": BASELINE_START,
            "combined_allocated_bytes": _BASELINE_EARLIEST_BYTES,
            "comparable": True,
            "quality": [],
        },
        _growth_sample(BASELINE_END, _BASELINE_EARLIEST_BYTES + _BASELINE_DELTA_BYTES),
    ]
    quality_result = readiness.assess_runtime_checkpoint_evidence(quality)
    assert quality_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "capacity.workload_baseline.samples[0].quality" in quality_result["missing_prerequisites"]
    assert quality_result["go_for_runtime_deployment"] is False


def test_cli_malformed_origin_writes_incomplete_assessment(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    packet = _complete_packet(_measured_collector(SCHEMA_VERSION))
    packet["operator"]["evidence_origin"] = []
    evidence = tmp_path / "malformed.json"
    output = tmp_path / "assessment.json"
    evidence.write_text(json.dumps(packet), encoding="utf-8")
    code = readiness_cli.main(["assess", "--evidence-path", str(evidence), "--output-path", str(output)])
    captured = capsys.readouterr()
    assert code == 0
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err
    written = output.read_text(encoding="utf-8")
    assessment = json.loads(written)
    assert assessment["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "operator.evidence_origin" in assessment["missing_prerequisites"]
    assert assessment["go_for_runtime_deployment"] is False
    assert "should-not-leak" not in written
    assert "telegram_bot_token" not in written
    again = readiness_cli.main(["assess", "--evidence-path", str(evidence), "--output-path", str(output)])
    again_captured = capsys.readouterr()
    assert again == 2
    assert "Traceback" not in again_captured.err
    assert "should-not-leak" not in output.read_text(encoding="utf-8")


def test_malformed_source_index_is_not_absence() -> None:
    control = readiness.assess_runtime_checkpoint_evidence(_complete_packet(_measured_collector(SCHEMA_VERSION)))
    assert control["overall_disposition"] == "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    assert control["source_compatibility"]["finding"] == "source_plan_state_index_absent"
    assert control["go_for_runtime_deployment"] is False

    for supplied in ([], 1, "absent"):
        packet = _complete_packet(_measured_collector(SCHEMA_VERSION))
        packet["collector_report"]["sqlite"]["plan_state_index"] = supplied
        result = readiness.assess_runtime_checkpoint_evidence(packet)
        assert result["overall_disposition"] == "INCOMPLETE_PREREQUISITES", supplied
        assert "source_plan_state_index.supplied_type" in result["missing_prerequisites"]
        assert result["source_compatibility"]["finding"] is None
        assert result["source_compatibility"]["plan_state_index"] == "malformed"
        assert result["go_for_runtime_deployment"] is False
        assert "should-not-leak" not in json.dumps(result)

    columns = _complete_packet(_measured_collector(SCHEMA_VERSION))
    columns["collector_report"]["sqlite"]["plan_state_index"]["columns"] = 1
    columns_result = readiness.assess_runtime_checkpoint_evidence(columns)
    assert columns_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "source_plan_state_index.columns" in columns_result["missing_prerequisites"]
    assert "source_plan_state_index_definition_incomplete" in columns_result["missing_prerequisites"]
    assert columns_result["source_compatibility"]["finding"] is None

    partial = _complete_packet(_measured_collector(SCHEMA_VERSION))
    partial["collector_report"]["sqlite"]["plan_state_index"]["partial"] = "false"
    partial_result = readiness.assess_runtime_checkpoint_evidence(partial)
    assert partial_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert "source_plan_state_index.partial" in partial_result["missing_prerequisites"]
    assert partial_result["go_for_runtime_deployment"] is False

    adverse = _complete_packet(_measured_collector(SCHEMA_VERSION))
    adverse["collector_report"]["sqlite"]["plan_state_index"] = []
    adverse["operator"]["source_replay_capture"]["enabled"] = True
    adverse_result = readiness.assess_runtime_checkpoint_evidence(adverse)
    assert adverse_result["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "source_plan_state_index.supplied_type" in adverse_result["missing_prerequisites"]
    assert "source_replay_capture_enabled_outside_initial_cutover_contract" in adverse_result["adverse_results"]


def test_source_epoch_presence_requires_a_boolean() -> None:
    table = next(iter(readiness.EPOCH_LINEAGE_COLUMNS))
    other = next(name for name in readiness.EPOCH_LINEAGE_COLUMNS if name != table)

    def lineage(present: object, *, other_columns: list[str] | None = None) -> dict[str, Any]:
        tables = {
            name: {"present": True, "columns": list(columns)}
            for name, columns in readiness.EPOCH_LINEAGE_COLUMNS.items()
        }
        tables[table]["present"] = present
        if other_columns is not None:
            tables[other]["columns"] = other_columns
        return {"tables": tables}

    absent = _complete_packet(_measured_collector(SCHEMA_VERSION))
    absent["collector_report"]["sqlite"]["epoch_lineage"] = {
        "tables": {name: {"present": False, "columns": []} for name in readiness.EPOCH_LINEAGE_COLUMNS}
    }
    absent_result = readiness.assess_runtime_checkpoint_evidence(absent)
    assert absent_result["overall_disposition"] == "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    assert absent_result["source_compatibility"]["epoch_lineage"] == "absent_table"
    assert absent_result["source_compatibility"]["finding"] == "source_plan_state_index_absent"
    assert absent_result["go_for_runtime_deployment"] is False

    for marker in ([], {}, 1, "false", None):
        packet = _complete_packet(_measured_collector(SCHEMA_VERSION))
        packet["collector_report"]["sqlite"]["epoch_lineage"] = lineage(marker)
        result = readiness.assess_runtime_checkpoint_evidence(packet)
        assert result["overall_disposition"] == "INCOMPLETE_PREREQUISITES", marker
        assert f"source_epoch_lineage.{table}.present" in result["missing_prerequisites"]
        assert result["source_compatibility"]["epoch_lineage"] == "malformed"
        assert result["go_for_runtime_deployment"] is False
        assert "should-not-leak" not in json.dumps(result)

    omitted = _complete_packet(_measured_collector(SCHEMA_VERSION))
    omitted["collector_report"]["sqlite"]["epoch_lineage"] = lineage(True)
    del omitted["collector_report"]["sqlite"]["epoch_lineage"]["tables"][table]["present"]
    omitted_result = readiness.assess_runtime_checkpoint_evidence(omitted)
    assert omitted_result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert f"source_epoch_lineage.{table}.present" in omitted_result["missing_prerequisites"]

    both = _complete_packet(_measured_collector(SCHEMA_VERSION))
    both["collector_report"]["sqlite"]["epoch_lineage"] = lineage("false", other_columns=["not_a_lineage_column"])
    both_result = readiness.assess_runtime_checkpoint_evidence(both)
    assert both_result["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert f"source_epoch_lineage.{table}.present" in both_result["missing_prerequisites"]
    assert f"source_epoch_lineage_definition_mismatch:{other}" in both_result["adverse_results"]
    assert both_result["go_for_runtime_deployment"] is False


def test_cli_malformed_source_presence_writes_incomplete_assessment(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    table = next(iter(readiness.EPOCH_LINEAGE_COLUMNS))
    packet = _complete_packet(_measured_collector(SCHEMA_VERSION))
    packet["collector_report"]["sqlite"]["epoch_lineage"] = {
        "tables": {
            name: {"present": True, "columns": list(columns)}
            for name, columns in readiness.EPOCH_LINEAGE_COLUMNS.items()
        }
    }
    packet["collector_report"]["sqlite"]["epoch_lineage"]["tables"][table]["present"] = "false"
    evidence = tmp_path / "source_presence_packet.json"
    output = tmp_path / "source_presence_assessment.json"
    evidence.write_text(json.dumps(packet), encoding="utf-8")
    code = readiness_cli.main(["assess", "--evidence-path", str(evidence), "--output-path", str(output)])
    captured = capsys.readouterr()
    assert code == 0
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err
    written = output.read_text(encoding="utf-8")
    assessment = json.loads(written)
    assert assessment["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert f"source_epoch_lineage.{table}.present" in assessment["missing_prerequisites"]
    assert assessment["go_for_runtime_deployment"] is False
    assert "should-not-leak" not in written
    assert "telegram_bot_token" not in written
    again = readiness_cli.main(["assess", "--evidence-path", str(evidence), "--output-path", str(output)])
    again_captured = capsys.readouterr()
    assert again == 2
    assert "Traceback" not in again_captured.err
    assert "should-not-leak" not in output.read_text(encoding="utf-8")


def test_preservation_failure_and_downtime_overrun_are_adverse() -> None:
    failed = _complete_packet(_measured_collector())
    failed["operator"]["target_rehearsal"]["preservation"]["public_outbox_sent_uncertain"] = "failed"
    failed_result = readiness.assess_runtime_checkpoint_evidence(failed)
    assert failed_result["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert any("preservation_failed:public_outbox_sent_uncertain" in item for item in failed_result["adverse_results"])

    overrun = _complete_packet(_measured_collector())
    overrun["operator"]["target_rehearsal"]["planned_downtime_budget_seconds"] = 30
    overrun_result = readiness.assess_runtime_checkpoint_evidence(overrun)
    assert overrun_result["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "rehearsal_exceeds_downtime_budget" in overrun_result["adverse_results"]


def test_runbook_targets_schema_26_without_authorizing_deployment() -> None:
    text = Path("docs/operations/runtime_checkpoint_readiness.md").read_text(encoding="utf-8")
    assert "Expected application schema: **v25**" not in text
    assert "rehearsal-pre-v25" not in text
    assert "schema 26" in text
    assert "SOURCE_REPLAY_CAPTURE_ENABLED" in text
    assert readiness.PLAN_STATE_INDEX_NAME in text
    assert "plan_version_id IS NOT NULL" in text
    assert ANCHOR in text
    assert "immutable=1" in text
    assert "GO_FOR_RUNTIME_DEPLOYMENT" in text
    assert "changing WAL" in text
    assert "older outbox" in text


def test_collector_module_hash_is_of_this_file() -> None:
    identity = readiness.collector_identity()
    expected = hashlib.sha256(Path(readiness.__file__).read_bytes()).hexdigest()
    assert identity["module_sha256"] == expected
    assert identity["process"]["sqlite_linked_version"]
    assert "not the deployed application identity" in identity["process"]["note"]
