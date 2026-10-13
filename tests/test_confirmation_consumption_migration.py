"""Genuine schema-26 catalog, synthetic rows only; never an existing database."""
from pathlib import Path
import sqlite3

import pytest

from app.runtime_epoch.startup import migrate_existing_database
from app.storage import database as db
from tests.runtime_epoch_support import (
    SYNTHETIC_IDENTITY, ensure_synthetic_test_epoch, seed_legacy_lifecycle,
)
from tests.test_confirmation_observation_consumption import (
    _apply, _candidate, _register, _snapshot, _state,
)

pytestmark = pytest.mark.no_auto_epoch


def _v26(path):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.executescript(
            Path(__file__).with_name("fixtures").joinpath("schema_v26.sql").read_text(encoding="utf-8")
        )
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 26
        assert "lifecycle_consumption_json" not in {
            r[1] for r in connection.execute("PRAGMA table_info(runtime_operational_origins)")
        }
        ensure_synthetic_test_epoch(connection)
        connection.execute(
            "INSERT INTO runtime_operational_runs VALUES (?, ?, ?, ?, ?)",
            ("historical", SYNTHETIC_IDENTITY.epoch_id, "2026-03-01T12:00:00Z", "registered", None),
        )
        connection.execute(
            "INSERT INTO runtime_operational_origins VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("old-origin", SYNTHETIC_IDENTITY.epoch_id, "historical", "BTCUSDT",
             "2026-03-01T13:00:00Z", "2026-03-01T13:00:00Z", "2026-03-01T13:00:00Z",
             "live_fresh", "granted", None, "2026-03-01T13:00:00Z"),
        )
        seed_legacy_lifecycle(connection, lifecycle_id="historical-owner", symbol="BTCUSDT",
                              current_state="TRIGGERED")
        connection.execute(
            """UPDATE setup_lifecycle_records
               SET runtime_epoch_id=?, creation_origin_id='old-origin', confirmation_count=1,
                   invalidation_logic='Invalid if price accepts below 95.',
                   invalidation_reason='Invalid if price accepts below 95.'
               WHERE lifecycle_id='historical-owner'""",
            (SYNTHETIC_IDENTITY.epoch_id,),
        )
    return path


def _catalog(connection):
    return connection.execute(
        "SELECT name, type, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()


def test_migration_adds_only_nullable_receipts_and_preserves_historical_rows(tmp_path):
    path = _v26(tmp_path / "genuine-v26.db")
    before = _snapshot(path)
    with sqlite3.connect(path) as connection:
        catalog = _catalog(connection)
    migrate_existing_database(path)
    after = _snapshot(path)
    for table in before:
        if table == "runtime_operational_origins":
            assert [row[:-1] for row in after[table]] == before[table]
            assert all(row[-1] is None for row in after[table])
        else:
            assert after[table] == before[table]
    with sqlite3.connect(path) as connection:
        changed = _catalog(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 27
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    assert [r for r in changed if r[0] != "runtime_operational_origins"] == [
        r for r in catalog if r[0] != "runtime_operational_origins"]
    migrate_existing_database(path)
    assert _snapshot(path) == after
    with sqlite3.connect(path) as connection:
        assert _catalog(connection) == changed


@pytest.mark.parametrize("fail_after", ["column", "version"])
def test_migration_failure_rolls_back_column_and_version_then_retries(tmp_path, monkeypatch, fail_after):
    path = _v26(tmp_path / "failed-v26.db")
    before = _snapshot(path)
    with sqlite3.connect(path) as connection:
        catalog = _catalog(connection)
    original = db._ensure_column
    def fail(connection, table, column, declaration):
        original(connection, table, column, declaration)
        if column == "lifecycle_consumption_json":
            if fail_after == "version":
                connection.execute("PRAGMA user_version = 27")
            raise sqlite3.OperationalError("injected v27 migration failure")
    with monkeypatch.context() as patch:
        patch.setattr(db, "_ensure_column", fail)
        with pytest.raises(db.StorageError):
            migrate_existing_database(path)
    assert _snapshot(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 26
        assert _catalog(connection) == catalog
    migrate_existing_database(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 27


def test_unmarked_origin_is_unknown_and_new_scan_preserves_existing_counter(tmp_path):
    path = _v26(tmp_path / "legacy.db")
    migrate_existing_database(path)
    before = _snapshot(path)
    old = _apply(path, _candidate(), "historical")
    assert _state(old).confirmation_count == 1
    assert old.scanner_process_summary["unverified_confirmation_observations"] == 1
    assert _snapshot(path) == before
    _register(path, "new")
    new = _state(_apply(path, _candidate(index=1), "new"))
    assert new.lifecycle_id == "historical-owner"
    assert new.confirmation_count == 2
    assert new.confirmed_at is not None
    confirmed = _snapshot(path)
    retry = _state(_apply(path, _candidate(), "historical"))
    assert retry == new
    assert _snapshot(path) == confirmed


def test_old_writer_is_not_silently_compatible(tmp_path):
    path = _v26(tmp_path / "old-writer.db")
    migrate_existing_database(path)
    from tests.fixtures import baseline_v25_database
    with sqlite3.connect(path) as connection:
        with pytest.raises(baseline_v25_database.UnsupportedSchemaVersionError):
            baseline_v25_database.initialize_database(connection)


def test_readiness_accepts_genuine_v26_source_but_requires_v27_target(tmp_path):
    from app.storage import runtime_checkpoint_readiness as readiness
    from tests.test_runtime_checkpoint_readiness import _complete_packet, _measured_collector
    path = _v26(tmp_path / "readiness-source.db")
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        lineage = readiness.inspect_epoch_lineage(connection, tables)
        assert lineage["tables"]["runtime_operational_origins"]["matches_expected"] is True
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 26
    packet = _complete_packet(_measured_collector(26))
    packet["collector_report"]["sqlite"]["epoch_lineage"] = lineage
    result = readiness.assess_runtime_checkpoint_evidence(packet)
    assert result["overall_disposition"] == "PACKET_REVIEWABLE_NOT_AUTHORIZED"
    target = packet["operator"]["target_rehearsal"]["epoch_lineage_tables"]["runtime_operational_origins"]
    target.remove("lifecycle_consumption_json")
    result = readiness.assess_runtime_checkpoint_evidence(packet)
    assert result["overall_disposition"] == "ADVERSE_MEASURED_RESULT"
    assert "target_rehearsal.epoch_lineage_definition_mismatch:runtime_operational_origins" in result["adverse_results"]


def test_readiness_requires_preservation_of_consumption_evidence():
    from app.storage import runtime_checkpoint_readiness as readiness
    from tests.test_runtime_checkpoint_readiness import _complete_packet, _measured_collector
    packet = _complete_packet(_measured_collector(27))
    del packet["operator"]["target_rehearsal"]["preservation"]["confirmation_consumption_receipts"]
    result = readiness.assess_runtime_checkpoint_evidence(packet)
    assert result["overall_disposition"] == "INCOMPLETE_PREREQUISITES"
    assert any("confirmation_consumption_receipts" in item for item in result["missing_prerequisites"])
