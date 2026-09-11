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


def _capacity_ok() -> dict[str, int | bool]:
    total = 100 * 1024**3
    return {
        "volume_total_bytes": total,
        "volume_free_bytes": 80 * 1024**3,
        "new_backup_bytes": 20 * 1024**3,
        "concurrent_restore_or_candidate_bytes": 20 * 1024**3,
        "migration_temp_bytes": 5 * 1024**3,
        "additional_peak_WAL_and_log_bytes": 2 * 1024**3,
        "growth_budget_bytes": 8 * 1024**3,
        "operating_reserve_bytes": max(10 * 1024**3, total // 10),
        "backup_shares_db_volume": True,
        "restore_shares_db_volume": True,
    }


def _complete_packet(collector: dict[str, Any] | None = None) -> dict[str, Any]:
    known = [str(item["id"]) for item in readiness.CONSUMER_INVENTORY]
    return {
        "collector_report": collector or {},
        "operator": {
            "deployed_application_sha": FAKE_DEPLOYED,
            "target_sha": FAKE_TARGET,
            "accounted_consumers": known,
            "unknown_external_readers_resolved": True,
            "restore_evidence": {
                "restore_tested": True,
                "integrity_ok": True,
                "restore_path_distinct_from_source": True,
                "snapshot_identity": "abc123",
            },
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
    assert sqlite_section["schema_version"] == SCHEMA_VERSION == 25
    assert sqlite_section["connection"]["sqlite_uri_mode"] == "ro"
    assert sqlite_section["connection"]["query_only_readback"] == 1
    assert sqlite_section["connection"]["immutable_requested"] is False
    assert sqlite_section["connection"]["live_mutable_source"] is True
    assert sqlite_section["recent_runs"]["status"] == "not_requested"
    assert "scan_runs" in sqlite_section["tables"]
    assert "raw_payload_format" in sqlite_section["columns"]["scan_runs"]
    assert "ix_scan_runs_timestamp" in sqlite_section["indexes"]["scan_runs"]
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
    assert report["sqlite"]["schema_version"] == 25
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


def test_complete_packet_is_reviewable_but_not_authorized(tmp_path: Path) -> None:
    collector = readiness.collect_runtime_checkpoint_preflight(_v25(tmp_path))
    assessment = readiness.assess_runtime_checkpoint_evidence(_complete_packet(collector))
    assert assessment["overall_disposition"] == "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    assert assessment["go_for_runtime_deployment"] is False
    assert assessment["operational_status"] == "RUNTIME_CHECKPOINT_NOT_YET_AUTHORIZED"
    assert assessment["release"]["functional_anchor_sha"] == ANCHOR


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


def test_schema_version_and_decoder_inventory_remain_v25() -> None:
    assert SCHEMA_VERSION == 25
    assert set(readiness.DECODER_COMPATIBILITY_INVENTORY["supported_formats"]) == set(SUPPORTED_FORMATS)
    assert readiness.FUNCTIONAL_ANCHOR_SHA == ANCHOR
    wolf = next(item for item in readiness.CONSUMER_INVENTORY if item["id"] == "wolf_briefing")
    assert "load_logical_scan_payload" in str(wolf["payload"])
    inspect_item = next(item for item in readiness.CONSUMER_INVENTORY if item["id"] == "sqlite_maintenance_inspect")
    assert "assume_immutable_when_sidecars_absent=True" in str(inspect_item["payload"])


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
    assert version == 25


def test_no_cli_default_database_path() -> None:
    parser_source = inspect.getsource(readiness_cli._parse_args)
    assert "DEFAULT_DATABASE_PATH" not in parser_source
    assert "main_live_runtime" not in parser_source
    with pytest.raises(SystemExit):
        readiness_cli._parse_args(["collect"])


def test_collector_module_hash_is_of_this_file() -> None:
    identity = readiness.collector_identity()
    expected = hashlib.sha256(Path(readiness.__file__).read_bytes()).hexdigest()
    assert identity["module_sha256"] == expected
    assert identity["process"]["sqlite_linked_version"]
    assert "not the deployed application identity" in identity["process"]["note"]
