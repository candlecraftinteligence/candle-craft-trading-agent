from __future__ import annotations

import json
import multiprocessing
import shutil
import sqlite3
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.alerts.telegram_lifecycle import SQLiteTelegramAlertAttemptRepository
import app.storage.database as database_module
import app.storage.maintenance as maintenance_module
from app.storage.database import (
    SCHEMA_VERSION,
    WRITABLE_BUSY_TIMEOUT_MS,
    WRITABLE_WAL_AUTOCHECKPOINT_PAGES,
    DatabaseMissingError,
    StorageError,
    UnsupportedSchemaVersionError,
    connect_database,
    identify_schema_version,
    open_initialized_database,
    open_read_only_database,
)
from app.storage.maintenance import (
    CORE_TABLES,
    MaintenanceError,
    WarningThresholds,
    check_database,
    checkpoint_database,
    create_verified_backup,
    inspect_database,
    plan_backup,
    sha256_file,
    verify_backup,
)
from app.watch_supervisor import WatchFailureDisposition, classify_watch_exception
from app.storage.repositories import list_scan_history
from scripts import sqlite_maintenance


def _database(tmp_path: Path, name: str = "runtime.sqlite") -> Path:
    path = tmp_path / name
    with open_initialized_database(path):
        pass
    return path


def _open_initialized_database_in_process(
    path_text: str,
    start_at: float,
) -> tuple[str, int]:
    while time.time() < start_at:
        time.sleep(0.005)
    with open_initialized_database(Path(path_text)) as connection:
        return (
            str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
            int(connection.execute("PRAGMA user_version").fetchone()[0]),
        )


def _insert_scan_run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    timestamp: str = "2026-07-18T10:00:00+00:00",
) -> None:
    connection.execute(
        """
        INSERT INTO scan_runs (
            run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
            strategy, timeframes_json, market_regime, runtime_stats_json,
            command_preset, command_used, total_valid_setups, near_misses,
            rejected, data_issues, data_issues_json, raw_payload_json
        ) VALUES (?, ?, 'test', 'test', 1, '["BTCUSDT"]', 'test', '{}',
                  'TEST', '{}', 'test', 'test', 0, 0, 1, 0, '[]', '{}')
        """,
        (run_id, timestamp),
    )


def _insert_lifecycle(connection: sqlite3.Connection, lifecycle_id: str) -> None:
    connection.execute(
        """
        INSERT INTO setup_lifecycle_records (
            lifecycle_id, symbol, mode, direction, current_state,
            first_seen_at, last_seen_at, last_transition_at
        ) VALUES (?, ?, 'swing', 'long', 'WATCHLISTED',
                  '2026-07-18T10:00:00Z', '2026-07-18T10:00:00Z', '2026-07-18T10:00:00Z')
        """,
        (lifecycle_id, lifecycle_id.upper()),
    )


def _insert_outbox_attempt(connection: sqlite3.Connection, signal_id: str) -> None:
    connection.execute(
        """
        INSERT INTO telegram_alert_attempts (
            signal_id, symbol, direction, new_state, alert_type,
            lifecycle_state, telegram_status, message_hash
        ) VALUES (?, 'BTCUSDT', 'long', 'WATCHLISTED', 'WATCHLIST',
                  'WATCHLISTED', 'pending', 'hash')
        """,
        (signal_id,),
    )


def _state(path: Path) -> tuple[int, int, str, bool, bool]:
    stat = path.stat()
    return (
        stat.st_size,
        stat.st_mtime_ns,
        sha256_file(path),
        Path(f"{path}-wal").exists(),
        Path(f"{path}-shm").exists(),
    )

class _ReadOnlySafetyCursor:
    def __init__(self, row: object) -> None:
        self._row = row

    def fetchone(self) -> object:
        return self._row


class _ReadOnlySafetyConnection:
    def __init__(self, query_only_row: object) -> None:
        self.query_only_row = query_only_row
        self.statements: list[str] = []
        self.closed = False

    def execute(self, statement: str) -> _ReadOnlySafetyCursor:
        self.statements.append(statement)
        if statement == "PRAGMA query_only":
            return _ReadOnlySafetyCursor(self.query_only_row)
        if statement == "PRAGMA user_version":
            return _ReadOnlySafetyCursor((0,))
        return _ReadOnlySafetyCursor(None)

    def close(self) -> None:
        self.closed = True


def _backup(
    source: Path,
    archive: Path,
    *,
    now: datetime | None = None,
    unique_suffix: str | None = None,
) -> dict[str, object]:
    return create_verified_backup(
        source,
        archive,
        allow_unsafe_temp=True,
        now=now,
        unique_suffix=unique_suffix,
    )


def test_writable_connection_profile_is_explicit_and_verified(tmp_path: Path) -> None:
    path = tmp_path / "profile.sqlite"
    connection = open_initialized_database(path)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == WRITABLE_BUSY_TIMEOUT_MS
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert (
            connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
            == WRITABLE_WAL_AUTOCHECKPOINT_PAGES
        )
    finally:
        connection.close()


@pytest.mark.parametrize("preinitialize", [False, True], ids=["fresh", "already-wal"])
def test_concurrent_thread_database_open_is_wal_safe(
    tmp_path: Path,
    preinitialize: bool,
) -> None:
    path = tmp_path / f"thread-{preinitialize}.sqlite"
    if preinitialize:
        with open_initialized_database(path):
            pass

    barrier = threading.Barrier(8)

    def open_concurrently() -> tuple[str, int]:
        barrier.wait()
        with open_initialized_database(path) as connection:
            return (
                str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower(),
                int(connection.execute("PRAGMA user_version").fetchone()[0]),
            )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(open_concurrently) for _ in range(8)]
        results = [future.result(timeout=20) for future in futures]

    assert results == [("wal", SCHEMA_VERSION)] * 8


@pytest.mark.parametrize("preinitialize", [False, True], ids=["fresh", "already-wal"])
def test_concurrent_process_database_open_is_wal_safe_and_closes_handles(
    tmp_path: Path,
    preinitialize: bool,
) -> None:
    path = tmp_path / f"process-{preinitialize}.sqlite"
    if preinitialize:
        with open_initialized_database(path):
            pass

    context = multiprocessing.get_context("spawn")
    start_at = time.time() + 1.0
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as executor:
        futures = [
            executor.submit(
                _open_initialized_database_in_process,
                str(path),
                start_at,
            )
            for _ in range(4)
        ]
        results = [future.result(timeout=30) for future in futures]

    assert results == [("wal", SCHEMA_VERSION)] * 4
    renamed = path.with_name(f"{path.stem}-closed.sqlite")
    path.rename(renamed)
    assert renamed.exists()


def test_wal_initialization_lock_retry_is_bounded_and_closes_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_pragma_text = database_module._pragma_text

    def retain_lock(connection: sqlite3.Connection, expression: str) -> str:
        if expression.lower() == "journal_mode":
            raise sqlite3.OperationalError("database is locked")
        return real_pragma_text(connection, expression)

    monkeypatch.setattr(database_module, "_pragma_text", retain_lock)
    path = tmp_path / "bounded-wal-lock.sqlite"
    started_at = time.monotonic()
    with pytest.raises(StorageError, match="Timed out while enabling required WAL"):
        connect_database(path, busy_timeout_ms=30)
    elapsed = time.monotonic() - started_at

    assert elapsed < 1
    renamed = tmp_path / "bounded-wal-lock-closed.sqlite"
    path.rename(renamed)
    assert renamed.exists()


def test_wal_initialization_retries_transient_lock_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_pragma_text = database_module._pragma_text
    journal_mode_reads = 0

    def release_transient_lock(connection: sqlite3.Connection, expression: str) -> str:
        nonlocal journal_mode_reads
        if expression.lower() == "journal_mode":
            journal_mode_reads += 1
            if journal_mode_reads <= 2:
                raise sqlite3.OperationalError("database is busy")
        return real_pragma_text(connection, expression)

    monkeypatch.setattr(database_module, "_pragma_text", release_transient_lock)
    path = tmp_path / "transient-wal-lock.sqlite"
    started_at = time.monotonic()
    connection = connect_database(path, busy_timeout_ms=250)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 250
    finally:
        connection.close()

    assert journal_mode_reads == 4
    assert time.monotonic() - started_at < 1
    renamed = tmp_path / "transient-wal-lock-closed.sqlite"
    path.rename(renamed)
    assert renamed.exists()


def test_writable_connection_fails_if_wal_verification_does_not_hold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_pragma_text = database_module._pragma_text
    journal_mode_reads = 0

    def lose_wal_after_enable(connection: sqlite3.Connection, expression: str) -> str:
        nonlocal journal_mode_reads
        normalized = expression.lower()
        if normalized == "journal_mode":
            journal_mode_reads += 1
            return "delete"
        if normalized.startswith("journal_mode ="):
            return "wal"
        return real_pragma_text(connection, expression)

    monkeypatch.setattr(database_module, "_pragma_text", lose_wal_after_enable)
    path = tmp_path / "wal-verification-failure.sqlite"
    with pytest.raises(StorageError, match="did not remain in required WAL"):
        connect_database(path)

    assert journal_mode_reads == 2
    renamed = tmp_path / "wal-verification-failure-closed.sqlite"
    path.rename(renamed)
    assert renamed.exists()


def test_writable_connection_does_not_claim_wal_when_sqlite_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_pragma_text = database_module._pragma_text

    def refuse_wal(connection: sqlite3.Connection, expression: str) -> str:
        if expression.lower().startswith("journal_mode ="):
            return "delete"
        return real_pragma_text(connection, expression)

    monkeypatch.setattr(database_module, "_pragma_text", refuse_wal)
    path = tmp_path / "refuse.sqlite"
    with pytest.raises(StorageError, match="refused required WAL"):
        database_module.connect_database(path)

    renamed = tmp_path / "closed.sqlite"
    path.rename(renamed)
    assert renamed.exists()


def test_connection_cleanup_leaves_no_open_handle(tmp_path: Path) -> None:
    path = _database(tmp_path)
    connection = open_initialized_database(path)
    connection.close()
    renamed = tmp_path / "renamed.sqlite"
    path.rename(renamed)
    assert renamed.exists()


def test_scanner_lifecycle_and_outbox_writers_serialize_safely(tmp_path: Path) -> None:
    path = _database(tmp_path)
    barrier = threading.Barrier(3)

    def scanner_writer() -> None:
        with open_initialized_database(path) as connection:
            barrier.wait()
            for index in range(10):
                _insert_scan_run(connection, f"scan-{index}")
                connection.commit()

    def lifecycle_writer() -> None:
        with open_initialized_database(path) as connection:
            barrier.wait()
            for index in range(10):
                _insert_lifecycle(connection, f"life-{index}")
                connection.commit()

    def outbox_writer() -> None:
        with open_initialized_database(path) as connection:
            barrier.wait()
            for index in range(10):
                _insert_outbox_attempt(connection, f"signal-{index}")
                connection.commit()

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [
            executor.submit(scanner_writer),
            executor.submit(lifecycle_writer),
            executor.submit(outbox_writer),
        ]
        for future in futures:
            future.result(timeout=20)

    with open_read_only_database(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 10
        assert connection.execute("SELECT COUNT(*) FROM setup_lifecycle_records").fetchone()[0] == 10
        assert connection.execute("SELECT COUNT(*) FROM telegram_alert_attempts").fetchone()[0] == 10


def test_telegram_repository_ends_write_transaction_before_network(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with SQLiteTelegramAlertAttemptRepository(path) as repository:
        _insert_outbox_attempt(repository._connection, "pre-network")
        assert repository._connection.in_transaction is True
        repository.commit_before_network_activity()
        assert repository._connection.in_transaction is False



def test_read_only_open_missing_database_fails_without_creation(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite"
    with pytest.raises(DatabaseMissingError, match="does not exist"):
        open_read_only_database(path)
    assert not path.exists()
    assert not path.parent.joinpath("missing.sqlite-wal").exists()
    assert not path.parent.joinpath("missing.sqlite-shm").exists()


def test_read_only_history_missing_database_fails_without_initialization(tmp_path: Path) -> None:
    path = tmp_path / "missing-history.sqlite"
    with pytest.raises(DatabaseMissingError, match="does not exist"):
        list_scan_history(path)
    assert not path.exists()


def test_read_only_open_does_not_migrate_older_schema(tmp_path: Path) -> None:
    path = tmp_path / "v15.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("CREATE TABLE legacy(value TEXT); PRAGMA user_version = 15;")
    before = _state(path)

    with open_read_only_database(path) as connection:
        assert identify_schema_version(connection) == 15
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'scan_runs'"
        ).fetchone()[0] == 0

    assert _state(path) == before


def test_read_only_inspection_preserves_size_mtime_hash_and_sidecars(tmp_path: Path) -> None:
    path = _database(tmp_path)
    before = _state(path)
    report = inspect_database(path)
    after = _state(path)
    assert report["inspection_mode"] == "read-only"
    assert report["journal_mode"] == "wal"
    assert before == after


def test_read_only_open_enforces_query_only(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with open_read_only_database(path) as connection:
        assert database_module.read_only_connection_safety_proof(connection) == {
            "sqlite_uri_mode": "ro",
            "query_only_readback": 1,
            "query_only_verified": True,
        }
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden(value TEXT)")


def test_read_only_open_uses_uri_mode_and_proves_query_only_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "query-only-proof.sqlite"
    with sqlite3.connect(path):
        pass
    connection = _ReadOnlySafetyConnection((1,))
    connect_call: dict[str, object] = {}

    def fake_connect(*args: object, **kwargs: object) -> _ReadOnlySafetyConnection:
        connect_call["database"] = args[0]
        connect_call["kwargs"] = kwargs
        return connection

    monkeypatch.setattr(database_module.sqlite3, "connect", fake_connect)

    opened = open_read_only_database(path, require_supported_schema=False)

    assert opened is connection
    assert str(connect_call["database"]).startswith("file:")
    assert "mode=ro" in str(connect_call["database"])
    assert "immutable=1" in str(connect_call["database"])
    assert connect_call["kwargs"] == {
        "uri": True,
        "timeout": WRITABLE_BUSY_TIMEOUT_MS / 1_000,
        "factory": database_module.ManagedSQLiteConnection,
    }
    assert connection.statements[:2] == ["PRAGMA query_only = ON", "PRAGMA query_only"]
    assert database_module.read_only_connection_safety_proof(opened) == {
        "sqlite_uri_mode": "ro",
        "query_only_readback": 1,
        "query_only_verified": True,
    }
    opened.close()


def test_required_immutable_source_proves_immutable_without_changing_default_contract(tmp_path: Path) -> None:
    path = _database(tmp_path, "quiescent-immutable.sqlite")
    before = _state(path)

    with open_read_only_database(
        path,
        require_immutable_source=True,
        include_immutable_safety_proof=True,
    ) as connection:
        proof = database_module.read_only_connection_safety_proof(connection)
        assert proof["immutable_requested"] is True
        assert proof["live_mutable_source"] is False
        assert proof["sqlite_uri_mode"] == "ro"
        assert proof["query_only_verified"] is True

    assert _state(path) == before


def test_required_immutable_source_refuses_existing_sidecars_before_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _database(tmp_path, "immutable-sidecar.sqlite")
    Path(f"{path}-wal").write_bytes(b"fixture-sidecar")
    Path(f"{path}-shm").write_bytes(b"fixture-sidecar")

    def must_not_connect(*args: object, **kwargs: object) -> object:
        del args, kwargs
        pytest.fail("required immutable source must refuse before sqlite3.connect")

    monkeypatch.setattr(database_module.sqlite3, "connect", must_not_connect)
    with pytest.raises(StorageError, match="required immutable source"):
        open_read_only_database(path, require_immutable_source=True)


def test_live_mutable_read_only_option_never_requests_immutable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "live-mutable.sqlite"
    with sqlite3.connect(path):
        pass
    connection = _ReadOnlySafetyConnection((1,))
    connect_call: dict[str, object] = {}

    def fake_connect(*args: object, **kwargs: object) -> _ReadOnlySafetyConnection:
        connect_call["database"] = args[0]
        connect_call["kwargs"] = kwargs
        return connection

    monkeypatch.setattr(database_module.sqlite3, "connect", fake_connect)
    opened = open_read_only_database(
        path,
        require_supported_schema=False,
        assume_immutable_when_sidecars_absent=False,
    )

    assert "mode=ro" in str(connect_call["database"])
    assert "immutable=1" not in str(connect_call["database"])
    assert database_module.read_only_connection_safety_proof(opened)["immutable_requested"] is False
    assert database_module.read_only_connection_safety_proof(opened)["live_mutable_source"] is True
    opened.close()


def test_live_mutable_snapshot_allows_wal_writer_and_remains_coherent(tmp_path: Path) -> None:
    path = _database(tmp_path, "live-wal.sqlite")
    with connect_database(path) as writer:
        _insert_scan_run(writer, "before")
        writer.commit()
        with open_read_only_database(
            path,
            assume_immutable_when_sidecars_absent=False,
            require_consistent_snapshot=True,
        ) as reader:
            before = reader.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
            _insert_scan_run(writer, "after")
            writer.commit()
            assert reader.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == before
            proof = database_module.read_only_connection_safety_proof(reader)
            assert proof["consistent_read_snapshot"] == "transaction_read_snapshot"
            assert proof["immutable_requested"] is False
    with open_read_only_database(path) as reader:
        assert reader.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 2


@pytest.mark.parametrize("query_only_row", (None, (0,), ("1",), (1, 0)))
def test_read_only_open_fails_closed_without_exact_query_only_readback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    query_only_row: object,
) -> None:
    path = tmp_path / "invalid-query-only-proof.sqlite"
    with sqlite3.connect(path):
        pass
    connection = _ReadOnlySafetyConnection(query_only_row)

    def fake_connect(*args: object, **kwargs: object) -> _ReadOnlySafetyConnection:
        del args, kwargs
        return connection

    monkeypatch.setattr(database_module.sqlite3, "connect", fake_connect)

    with pytest.raises(StorageError, match="query_only"):
        open_read_only_database(path, require_supported_schema=False)

    assert connection.statements == ["PRAGMA query_only = ON", "PRAGMA query_only"]
    assert connection.closed is True

def test_read_only_open_reports_unsupported_schema_without_migration(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE future(value TEXT)")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    before = _state(path)
    with pytest.raises(UnsupportedSchemaVersionError, match="Unsupported database schema version"):
        open_read_only_database(path)
    assert _state(path) == before


def test_inspect_reports_unsupported_schema_without_changing_it(tmp_path: Path) -> None:
    path = tmp_path / "future-inspect.sqlite"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE future(value TEXT)")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    before = _state(path)
    report = inspect_database(path)
    assert report["schema_status"] == "unsupported-newer"
    assert _state(path) == before


def test_healthy_database_passes_quick_and_foreign_key_checks(tmp_path: Path) -> None:
    report = check_database(_database(tmp_path))
    assert report["integrity_result"] == ["ok"]
    assert report["foreign_key_check"] == []
    assert report["ok"] is True


def test_full_integrity_check_runs_only_when_explicit(tmp_path: Path) -> None:
    path = _database(tmp_path)
    assert check_database(path, full=False)["integrity_pragma"] == "quick_check"
    assert check_database(path, full=True)["integrity_pragma"] == "integrity_check"


def test_foreign_key_violation_is_reported_precisely(tmp_path: Path) -> None:
    path = _database(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO setup_lifecycle_events (
                lifecycle_id, timestamp, symbol, to_state, reason
            ) VALUES ('missing-parent', '2026-07-18T10:00:00Z', 'BTCUSDT', 'WATCHLISTED', 'test')
            """
        )
        connection.commit()
    report = check_database(path)
    assert report["ok"] is False
    assert report["foreign_key_check"] == [
        {
            "table": "setup_lifecycle_events",
            "rowid": 1,
            "parent": "setup_lifecycle_records",
            "foreign_key_id": 0,
        }
    ]


def test_corrupt_database_check_fails_without_modification(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.sqlite"
    path.write_bytes(b"not a sqlite database\x00" * 20)
    before = _state(path)
    with pytest.raises(StorageError):
        check_database(path)
    assert _state(path) == before


def test_corrupt_database_cli_returns_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "corrupt-cli.sqlite"
    path.write_bytes(b"corrupt")
    assert sqlite_maintenance.main(["quick-check", "--database-path", str(path)]) == 1
    assert "failed" in capsys.readouterr().err.lower()


def test_online_backup_contains_committed_wal_records_with_writer_open(tmp_path: Path) -> None:
    source = _database(tmp_path)
    archive = tmp_path / "archives"
    writer = open_initialized_database(source)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        _insert_scan_run(writer, "wal-committed")
        writer.commit()
        assert Path(f"{source}-wal").exists()
        result = _backup(source, archive)
    finally:
        writer.close()

    snapshot = Path(str(result["snapshot_path"]))
    with open_read_only_database(snapshot) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM scan_runs WHERE run_id = 'wal-committed'"
        ).fetchone()[0] == 1


def test_backup_uses_partial_path_before_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _database(tmp_path)
    archive = tmp_path / "archives"
    original_verify = maintenance_module._verify_snapshot
    observed: dict[str, object] = {}

    def verify_partial(path: Path, **kwargs: object) -> dict[str, object]:
        observed["path"] = path
        observed["exists"] = path.exists()
        observed["is_partial"] = str(path).endswith(".partial")
        return original_verify(path, **kwargs)

    monkeypatch.setattr(maintenance_module, "_verify_snapshot", verify_partial)
    result = _backup(source, archive)
    assert observed == {
        "path": Path(str(result["partial_snapshot_path"])),
        "exists": True,
        "is_partial": True,
    }


def test_verified_snapshot_is_promoted_with_manifest_and_no_partial(tmp_path: Path) -> None:
    source = _database(tmp_path)
    result = _backup(source, tmp_path / "archives")
    snapshot = Path(str(result["snapshot_path"]))
    manifest = Path(str(result["manifest_path"]))
    assert snapshot.exists()
    assert manifest.exists()
    assert not Path(str(result["partial_snapshot_path"])).exists()
    assert not Path(str(result["partial_manifest_path"])).exists()


def test_snapshot_sha_schema_and_core_counts_match_manifest(tmp_path: Path) -> None:
    source = _database(tmp_path)
    with open_initialized_database(source) as connection:
        _insert_scan_run(connection, "manifest-row")
    result = _backup(source, tmp_path / "archives")
    snapshot = Path(str(result["snapshot_path"]))
    payload = json.loads(Path(str(result["manifest_path"])).read_text(encoding="utf-8"))
    assert payload["sha256"] == sha256_file(snapshot)
    assert payload["source_schema_version"] == payload["snapshot_schema_version"] == SCHEMA_VERSION
    assert payload["core_table_counts"]["scan_runs"] == 1
    assert set(payload["core_table_counts"]) == set(CORE_TABLES)
    assert verify_backup(snapshot)["ok"] is True


def test_backup_snapshot_boundary_counts_ignore_later_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _database(tmp_path)
    archive = tmp_path / "archives"
    with open_initialized_database(source) as connection:
        _insert_scan_run(connection, "before-boundary")

    original_run = maintenance_module._run_online_backup

    def commit_then_backup(
        source_connection: sqlite3.Connection,
        destination_connection: sqlite3.Connection,
        *,
        progress: object,
    ) -> None:
        with open_initialized_database(source) as writer:
            _insert_scan_run(writer, "after-boundary")
        original_run(source_connection, destination_connection, progress=progress)

    monkeypatch.setattr(maintenance_module, "_run_online_backup", commit_then_backup)
    result = _backup(source, archive)
    payload = result["manifest"]
    assert payload["core_table_counts"]["scan_runs"] == 1
    with open_read_only_database(Path(str(result["snapshot_path"]))) as connection:
        assert connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 1
    with open_read_only_database(source) as connection:
        assert connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 2


def test_existing_backup_destination_is_never_overwritten(tmp_path: Path) -> None:
    source = _database(tmp_path)
    archive = tmp_path / "archives"
    created = datetime(2026, 7, 18, 12, tzinfo=UTC)
    plan = plan_backup(
        source,
        archive,
        allow_unsafe_temp=True,
        now=created,
        unique_suffix="abcdef123456",
    )
    archive.mkdir()
    destination = Path(str(plan["snapshot_path"]))
    destination.write_bytes(b"do-not-overwrite")
    with pytest.raises(MaintenanceError, match="will not be overwritten"):
        _backup(source, archive, now=created, unique_suffix="abcdef123456")
    assert destination.read_bytes() == b"do-not-overwrite"


def test_source_equals_generated_destination_is_rejected(tmp_path: Path) -> None:
    archive = tmp_path / "archives"
    archive.mkdir()
    created = datetime(2026, 7, 18, 12, tzinfo=UTC)
    source = archive / "cci-20260718T120000Z-schema-v16-abcdef123456.sqlite"
    with open_initialized_database(source):
        pass
    before = sha256_file(source)
    with pytest.raises(MaintenanceError, match="live source"):
        _backup(source, archive, now=created, unique_suffix="abcdef123456")
    assert sha256_file(source) == before


def test_insufficient_free_space_prevents_backup_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _database(tmp_path)
    archive = tmp_path / "archives"
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(
        maintenance_module.shutil,
        "disk_usage",
        lambda path: type(usage)(usage.total, usage.used, 0),
    )
    with pytest.raises(MaintenanceError, match="Insufficient free space"):
        _backup(source, archive)
    assert not archive.exists()


def test_verification_failure_never_promotes_snapshot_or_changes_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _database(tmp_path)
    archive = tmp_path / "archives"
    before = sha256_file(source)
    monkeypatch.setattr(
        maintenance_module,
        "_verify_snapshot",
        lambda *args, **kwargs: {"ok": False, "errors": ["fault injected"]},
    )
    with pytest.raises(MaintenanceError, match="not promoted"):
        _backup(source, archive)
    assert sha256_file(source) == before
    assert not tuple(archive.glob("*.sqlite"))
    assert not tuple(archive.glob("*.partial"))


def test_interrupted_backup_leaves_source_unchanged_and_no_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _database(tmp_path)
    archive = tmp_path / "archives"
    before = sha256_file(source)

    def interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(maintenance_module, "_run_online_backup", interrupt)
    with pytest.raises(KeyboardInterrupt):
        _backup(source, archive)
    assert sha256_file(source) == before
    assert not tuple(archive.iterdir())


def test_backup_dry_run_creates_no_files_or_directories(tmp_path: Path) -> None:
    source = _database(tmp_path)
    archive = tmp_path / "archives"
    result = create_verified_backup(
        source,
        archive,
        dry_run=True,
        allow_unsafe_temp=True,
    )
    assert result["status"] == "dry-run"
    assert result["writes_performed"] is False
    assert not archive.exists()


def test_second_backup_creates_a_new_unique_snapshot(tmp_path: Path) -> None:
    source = _database(tmp_path)
    archive = tmp_path / "archives"
    first = _backup(source, archive)
    second = _backup(source, archive)
    assert first["snapshot_path"] != second["snapshot_path"]
    assert Path(str(first["snapshot_path"])).exists()
    assert Path(str(second["snapshot_path"])).exists()


def test_manifest_does_not_capture_environment_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "never-include-this-token")
    monkeypatch.setenv("BINANCE_API_SECRET", "never-include-this-secret")
    source = _database(tmp_path)
    result = _backup(source, tmp_path / "archives")
    text = Path(str(result["manifest_path"])).read_text(encoding="utf-8")
    assert "never-include-this-token" not in text
    assert "never-include-this-secret" not in text
    assert ".env" not in text


def test_backup_verify_detects_manifest_checksum_mismatch(tmp_path: Path) -> None:
    source = _database(tmp_path)
    result = _backup(source, tmp_path / "archives")
    manifest = Path(str(result["manifest_path"]))
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["sha256"] = "0" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    report = verify_backup(Path(str(result["snapshot_path"])), manifest_path=manifest)
    assert report["ok"] is False
    assert "manifest SHA-256 does not match" in report["errors"]


def test_unsafe_temporary_archive_requires_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _database(tmp_path)
    monkeypatch.setattr(maintenance_module.tempfile, "gettempdir", lambda: str(tmp_path))
    with pytest.raises(MaintenanceError, match="unsafe temporary"):
        plan_backup(source, tmp_path / "archives")


def test_growth_report_includes_main_wal_shm_and_disk_usage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _database(tmp_path)
    writer = open_initialized_database(source)
    try:
        writer.execute("PRAGMA wal_autocheckpoint = 0")
        _insert_scan_run(writer, "footprint")
        writer.commit()
        usage = shutil.disk_usage(tmp_path)
        fake_usage = type(usage)(1_000_000, 600_000, 400_000)
        monkeypatch.setattr(maintenance_module.shutil, "disk_usage", lambda path: fake_usage)
        report = inspect_database(source)
        expected_main = source.stat().st_size
        expected_wal = Path(f"{source}-wal").stat().st_size
        expected_shm = Path(f"{source}-shm").stat().st_size
        assert report["database_size_bytes"] == expected_main
        assert report["wal_size_bytes"] == expected_wal
        assert report["shm_size_bytes"] == expected_shm
        assert report["sqlite_footprint_bytes"] == expected_main + expected_wal + expected_shm
        assert report["filesystem_total_bytes"] == 1_000_000
        assert report["filesystem_free_bytes"] == 400_000
        assert report["filesystem_percent_free"] == 40.0
    finally:
        writer.close()


def test_growth_estimate_is_honest_without_manifest_history(tmp_path: Path) -> None:
    report = inspect_database(_database(tmp_path))
    assert report["recent_daily_growth"]["status"] == "not enough data"
    assert report["estimated_days_until_warning_threshold"] == "not enough data"


def test_growth_report_counts_scan_lifecycle_and_outbox_rows(tmp_path: Path) -> None:
    source = _database(tmp_path)
    with open_initialized_database(source) as connection:
        _insert_scan_run(connection, "counts")
        _insert_lifecycle(connection, "life-counts")
        connection.execute(
            """
            INSERT INTO setup_lifecycle_events (
                lifecycle_id, timestamp, symbol, to_state, reason
            ) VALUES ('life-counts', '2026-07-18T10:00:00Z', 'BTCUSDT', 'WATCHLISTED', 'test')
            """
        )
        _insert_outbox_attempt(connection, "signal-counts")
    report = inspect_database(source)
    assert report["scan_run_count"] == 1
    assert report["lifecycle_counts"]["records"] == 1
    assert report["lifecycle_counts"]["events"] == 1
    assert report["outbox_state_counts"]["telegram_alert_attempts"] == {"pending": 1}
    assert report["oldest_scan_timestamp"] == "2026-07-18T10:00:00+00:00"
    assert report["newest_scan_timestamp"] == "2026-07-18T10:00:00+00:00"


def test_warning_thresholds_are_diagnostic_only(tmp_path: Path) -> None:
    source = _database(tmp_path)
    thresholds = WarningThresholds(
        low_free_bytes=10**30,
        low_free_percent=100.0,
        large_wal_bytes=0,
        rapid_growth_bytes_per_day=0,
        integrity_max_age_hours=0,
        backup_max_age_hours=0,
    )
    report = inspect_database(source, thresholds=thresholds)
    assert report["automatic_deletion"] is False
    assert report["warnings"]
    assert all(item["diagnostic_only"] is True for item in report["warnings"])


def test_two_verified_manifests_enable_growth_estimate(tmp_path: Path) -> None:
    source = _database(tmp_path)
    archive = tmp_path / "archives"
    first_time = datetime(2026, 7, 16, 12, tzinfo=UTC)
    _backup(source, archive, now=first_time, unique_suffix="first001")
    with open_initialized_database(source) as connection:
        for index in range(20):
            _insert_scan_run(connection, f"growth-{index}")
    _backup(
        source,
        archive,
        now=first_time + timedelta(days=2),
        unique_suffix="second02",
    )
    report = inspect_database(
        source,
        archive_directory=archive,
        now=first_time + timedelta(days=2, hours=1),
    )
    assert report["recent_daily_growth"]["status"] == "estimated"
    assert report["backup_metadata"]["verified_backup_count"] == 2


def test_checkpoint_is_explicit_and_reports_status(tmp_path: Path) -> None:
    source = _database(tmp_path)
    report = checkpoint_database(source, mode="PASSIVE", busy_timeout_ms=250)
    assert report["checkpoint_mode"] == "PASSIVE"
    assert report["busy"] >= 0
    assert report["wal_log_pages"] >= 0
    assert report["checkpointed_pages"] >= 0


def test_cli_full_check_is_explicit_and_healthy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = _database(tmp_path)
    assert sqlite_maintenance.main(["full-check", "--database-path", str(source)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["check_mode"] == "full"
    assert payload["ok"] is True


def test_schema_remains_v16_after_maintenance_workflows(tmp_path: Path) -> None:
    source = _database(tmp_path)
    before = inspect_database(source)["schema_version"]
    check_database(source)
    result = _backup(source, tmp_path / "archives")
    after = inspect_database(source)["schema_version"]
    assert before == after == SCHEMA_VERSION
    assert result["manifest"]["snapshot_schema_version"] == SCHEMA_VERSION


def test_no_maintenance_api_exposes_delete_or_prune_operation() -> None:
    public_names = set(maintenance_module.__all__)
    assert not {name for name in public_names if "delete" in name or "prune" in name or "rotate" in name}
    assert "automatic_deletion" not in public_names


def test_actual_corrupt_partial_snapshot_is_not_promoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _database(tmp_path)
    archive = tmp_path / "archives"
    source_before = sha256_file(source)
    original_verify = maintenance_module._verify_snapshot

    def corrupt_before_verification(path: Path, **kwargs: object) -> dict[str, object]:
        path.write_bytes(b"corrupt snapshot")
        return original_verify(path, **kwargs)

    monkeypatch.setattr(
        maintenance_module,
        "_verify_snapshot",
        corrupt_before_verification,
    )
    with pytest.raises(MaintenanceError, match="Verified online backup failed"):
        _backup(source, archive)

    assert sha256_file(source) == source_before
    assert not tuple(archive.glob("*.sqlite"))
    assert not tuple(archive.glob("*.manifest.json"))
    assert not tuple(archive.glob("*.partial"))


def test_empty_archive_path_is_rejected_before_writing(tmp_path: Path) -> None:
    source = _database(tmp_path)
    with pytest.raises(MaintenanceError, match="explicit non-empty path"):
        plan_backup(source, "")
    assert list(tmp_path.glob("*.manifest.json")) == []


def test_disk_io_storage_failure_is_visible_and_fatal_to_watch_loop() -> None:
    try:
        try:
            raise sqlite3.OperationalError("disk I/O error")
        except sqlite3.OperationalError as cause:
            raise StorageError("runtime database disconnected") from cause
    except StorageError as exc:
        assert classify_watch_exception(exc) == WatchFailureDisposition.FATAL
