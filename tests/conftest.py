from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import get_settings
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
import app.storage.database as database_module
from tests.runtime_epoch_support import (
    SYNTHETIC_EPOCH_ID,
    attach_synthetic_origin_to_record,
    bootstrap_operational_test_database,
    ensure_synthetic_test_epoch,
)


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def synthetic_runtime_epoch(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
):
    if request.node.get_closest_marker("no_auto_epoch") is not None:
        return
    monkeypatch.setenv("RUNTIME_EPOCH_ID", SYNTHETIC_EPOCH_ID)
    original_initialize = database_module.initialize_database

    def wrapped_initialize(connection):
        original_initialize(connection)
        ensure_synthetic_test_epoch(connection)

    monkeypatch.setattr(database_module, "initialize_database", wrapped_initialize)

    original_upsert = SQLiteSetupLifecycleRepository.upsert_record

    def wrapped_upsert(self, record):
        stamped = attach_synthetic_origin_to_record(self._connection, record)
        return original_upsert(self, stamped)

    monkeypatch.setattr(SQLiteSetupLifecycleRepository, "upsert_record", wrapped_upsert)

    default_db = tmp_path_factory.mktemp("cci-operational") / "candle_craft.db"
    bootstrap_operational_test_database(default_db)
    import scripts.run_scan as run_scan_module

    monkeypatch.setattr(run_scan_module, "DEFAULT_DATABASE_PATH", default_db)
    original_prepare = run_scan_module._prepare_operational_runtime_if_needed

    def wrapped_prepare(args, settings):
        path = Path(args.database_path)
        if not _is_pytest_temp_database(path):
            return original_prepare(args, settings)
        if not path.exists():
            bootstrap_operational_test_database(path)
            return original_prepare(args, settings)
        from app.runtime_epoch.startup import migrate_existing_database
        from app.storage.database import SCHEMA_VERSION, connect_database, identify_schema_version

        connection = connect_database(path)
        try:
            schema_version = identify_schema_version(connection)
        finally:
            connection.close()
        if schema_version < SCHEMA_VERSION:
            migrate_existing_database(path)
        return original_prepare(args, settings)

    monkeypatch.setattr(run_scan_module, "_prepare_operational_runtime_if_needed", wrapped_prepare)


def _is_pytest_temp_database(path: Path) -> bool:
    rendered = str(path).replace("\\", "/").lower()
    return any(
        token in rendered
        for token in (
            ".pytest_tmp/",
            "/pytest_tmp/",
            "/cci-operational/",
            "/pytest-of-",
        )
    )
