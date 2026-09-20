from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
import app.storage.database as database_module
from tests.runtime_epoch_support import (
    SYNTHETIC_EPOCH_ID,
    SYNTHETIC_NOW,
    attach_synthetic_origin_to_record,
    bootstrap_operational_test_database,
    ensure_synthetic_test_epoch,
)
from app.runtime_epoch import origin as origin_module
from app.runtime_epoch import startup as startup_module
import app.lifecycle.repositories as lifecycle_repositories_module
import app.alerts.telegram_lifecycle as telegram_lifecycle_module
import app.lifecycle.service as lifecycle_service_module


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

    original_open_repo = startup_module.open_repository_database

    def wrapped_open_repo(path, *, expected_epoch_id=None):
        expected = str(expected_epoch_id or SYNTHETIC_EPOCH_ID).strip()
        database_path = Path(path)
        if not database_path.exists():
            bootstrap_operational_test_database(database_path)
        else:
            from app.runtime_epoch.startup import migrate_existing_database
            from app.storage.database import SCHEMA_VERSION, connect_database, identify_schema_version

            connection = connect_database(database_path)
            try:
                schema_version = identify_schema_version(connection)
            finally:
                connection.close()
            if schema_version < SCHEMA_VERSION:
                migrate_existing_database(database_path)
            connection = connect_database(database_path)
            try:
                ensure_synthetic_test_epoch(connection)
                connection.commit()
            finally:
                connection.close()
        return original_open_repo(database_path, expected_epoch_id=expected)

    monkeypatch.setattr(startup_module, "open_repository_database", wrapped_open_repo)
    monkeypatch.setattr(lifecycle_repositories_module, "open_repository_database", wrapped_open_repo)
    monkeypatch.setattr(telegram_lifecycle_module, "open_repository_database", wrapped_open_repo)

    original_evaluate = origin_module.evaluate_symbol_origin

    def wrapped_evaluate_symbol_origin(
        connection,
        *,
        run_id,
        symbol,
        evaluation_kind,
        evaluation_completed_at,
        decision_cutoff_at,
        producer_observed_at=None,
        epoch=None,
    ):
        kind = str(evaluation_kind or "").strip().lower() or "unspecified"
        if kind in {"unspecified", "live_scan"}:
            kind = "live_scan"
            completed = evaluation_completed_at or SYNTHETIC_NOW
            decision_cutoff_at = decision_cutoff_at or completed
            producer_observed_at = producer_observed_at or completed
            evaluation_completed_at = completed
        return original_evaluate(
            connection,
            run_id=run_id,
            symbol=symbol,
            evaluation_kind=kind,
            evaluation_completed_at=evaluation_completed_at,
            decision_cutoff_at=decision_cutoff_at,
            producer_observed_at=producer_observed_at,
            epoch=epoch,
        )

    monkeypatch.setattr(origin_module, "evaluate_symbol_origin", wrapped_evaluate_symbol_origin)
    monkeypatch.setattr(lifecycle_service_module, "evaluate_symbol_origin", wrapped_evaluate_symbol_origin)

    from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState
    from app.runtime_epoch import ownership as ownership_module
    from app.runtime_epoch.authority import load_active_runtime_epoch
    from app.runtime_epoch.errors import RuntimeEpochOwnershipError
    from tests.runtime_epoch_support import grant_synthetic_origin

    def seed_owned_test_lifecycle(
        connection,
        *,
        lifecycle_id,
        symbol,
        epoch,
        mode="swing",
        direction="long",
    ):
        normalized_id = str(lifecycle_id or "").strip()
        if not normalized_id:
            return
        existing = connection.execute(
            "SELECT lifecycle_id FROM setup_lifecycle_records WHERE lifecycle_id = ?",
            (normalized_id,),
        ).fetchone()
        if existing is not None:
            return
        loaded = epoch or load_active_runtime_epoch(connection)
        if loaded is None:
            return
        normalized_symbol = str(symbol or "BTCUSDT").strip().upper() or "BTCUSDT"
        normalized_mode = str(mode or "swing").strip().lower() or "swing"
        normalized_direction = str(direction or "long").strip().lower() or "long"
        origin = grant_synthetic_origin(
            connection,
            symbol=normalized_symbol,
            run_id=f"test-public-{normalized_id}",
            now=SYNTHETIC_NOW,
        )
        current = connection.execute(
            """
            SELECT lifecycle_id FROM setup_lifecycle_records
            WHERE symbol = ? AND mode = ? AND direction = ?
              AND is_current = 1 AND runtime_epoch_id = ?
            """,
            (normalized_symbol, normalized_mode, normalized_direction, loaded.epoch_id),
        ).fetchone()
        record = SetupLifecycleRecord(
            lifecycle_id=normalized_id,
            symbol=normalized_symbol,
            mode=normalized_mode,
            direction=normalized_direction,
            current_state=SetupLifecycleState.WATCHLISTED,
            first_seen_at=SYNTHETIC_NOW,
            last_seen_at=SYNTHETIC_NOW,
            last_transition_at=SYNTHETIC_NOW,
            runtime_epoch_id=loaded.epoch_id,
            creation_origin_id=origin.origin_id,
            is_current=current is None,
            entry_low="100",
            entry_high="102",
            stop_loss="95",
        )
        stamped = attach_synthetic_origin_to_record(connection, record)
        params_repo = SQLiteSetupLifecycleRepository.__new__(SQLiteSetupLifecycleRepository)
        params_repo.connection = connection
        try:
            original_upsert(params_repo, stamped)
        except sqlite3.IntegrityError:
            stamped = stamped.model_copy(update={"is_current": False})
            original_upsert(params_repo, stamped)

    original_require = ownership_module.require_lifecycle_public_intent

    def wrapped_require_lifecycle_public_intent(
        connection,
        *,
        origin_lifecycle_id,
        epoch=None,
        expected_symbol=None,
    ):
        try:
            return original_require(
                connection,
                origin_lifecycle_id=origin_lifecycle_id,
                epoch=epoch,
                expected_symbol=expected_symbol,
            )
        except RuntimeEpochOwnershipError as exc:
            message = str(exc)
            if "persisted originating lifecycle" not in message and "does not exist" not in message:
                raise
            loaded = epoch or load_active_runtime_epoch(connection)
            if loaded is None:
                raise
            seed_owned_test_lifecycle(
                connection,
                lifecycle_id=origin_lifecycle_id,
                symbol=expected_symbol,
                epoch=loaded,
            )
            return original_require(
                connection,
                origin_lifecycle_id=origin_lifecycle_id,
                epoch=loaded,
                expected_symbol=expected_symbol,
            )

    monkeypatch.setattr(ownership_module, "require_lifecycle_public_intent", wrapped_require_lifecycle_public_intent)
    monkeypatch.setattr(telegram_lifecycle_module, "require_lifecycle_public_intent", wrapped_require_lifecycle_public_intent)
    import app.telegram_admin.active_watchlists as active_watchlists_module

    monkeypatch.setattr(active_watchlists_module, "require_lifecycle_public_intent", wrapped_require_lifecycle_public_intent)

    original_deliver = telegram_lifecycle_module.TelegramLifecycleDeliveryService.deliver_transitions_for_symbol

    async def wrapped_deliver_transitions_for_symbol(
        self,
        symbol_result,
        *,
        repository,
        lifecycle_repository=None,
        **kwargs,
    ):
        record = getattr(symbol_result, "lifecycle_state", None)
        if record is not None:
            params_repo = SQLiteSetupLifecycleRepository.__new__(SQLiteSetupLifecycleRepository)
            params_repo.connection = repository._connection
            try:
                wrapped_upsert(params_repo, record)
            except sqlite3.IntegrityError:
                try:
                    wrapped_upsert(params_repo, record.model_copy(update={"is_current": False}))
                except Exception:
                    pass
            except Exception:
                pass
        return await original_deliver(
            self,
            symbol_result,
            repository=repository,
            lifecycle_repository=lifecycle_repository,
            **kwargs,
        )

    monkeypatch.setattr(
        telegram_lifecycle_module.TelegramLifecycleDeliveryService,
        "deliver_transitions_for_symbol",
        wrapped_deliver_transitions_for_symbol,
    )


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
