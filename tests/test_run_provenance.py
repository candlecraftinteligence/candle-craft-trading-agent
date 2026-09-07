from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from app.pipeline.scanner_runner import ScannerRuntimeStats
from app.storage.repositories import store_scan_result
from app.storage.run_provenance import (
    PROVENANCE_RUNTIME_STATS_KEY,
    canonical_json_dumps,
    collect_run_provenance,
    collect_run_provenance_safe,
    reset_git_info_cache,
    safe_configuration_hash,
    unavailable_provenance,
)
from tests.test_storage_database import _replay_summary, _scan_result


def _settings(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "environment": "test",
        "local_manual_mode": True,
        "order_execution_enabled": False,
        "telegram_dry_run": True,
        "telegram_signals_enabled": False,
        "telegram_admin_enabled": False,
        "telegram_public_watchlist_enabled": False,
        "telegram_research_watch_enabled": False,
        "telegram_watchlist_outcome_tracking_enabled": True,
        "telegram_public_signal_policy": "lifecycle",
        "scanner_confirmation_cycles": 2,
        "scanner_setup_merge_tolerance_pct": 0.5,
        "global_context_enabled": True,
        "btc_context_enabled": True,
        "btc_d_context_enabled": True,
        "macro_event_context_enabled": True,
        "microstructure_flow_enabled": False,
        "liquidation_flow_enabled": False,
        "order_book_liquidity_enabled": False,
        "public_watchlist_min_grade": "A",
        "public_watchlist_min_score": 88,
        "public_watchlist_min_rr": "3.0",
        "public_watchlist_max_per_scan": 1,
        "public_watchlist_dedupe_across_modes": True,
        "public_watchlist_require_plan": True,
        "public_watchlist_require_entry_zone": True,
        "public_watchlist_require_invalidation": True,
    }
    payload.update(overrides)
    return payload


def _scanner(**overrides: object) -> SimpleNamespace:
    payload = dict(
        exchange="binance",
        interval="15m",
        candle_limit=250,
        dry_run_alerts=True,
        min_score_for_idea="80",
        min_rr="2.5",
        strategy_name="liquidity_grab_pullback",
        strategy_modes=("swing", "scalp"),
        aggressive_toggle=False,
        htf_timeframe="2d",
        bias_timeframe="12h",
        structure_timeframe="4h",
        execution_timeframe="15m",
        confirmation_timeframe="5m",
        market_regime_enabled=False,
        regime_risk_mode="balanced",
        regime_strictness="normal",
        global_context_enabled=False,
        btc_context_enabled=True,
        btc_d_context_enabled=True,
        macro_event_context_enabled=False,
        microstructure_flow_enabled=False,
        liquidation_flow_enabled=False,
        order_book_liquidity_enabled=False,
        fast_mode=False,
    )
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _git() -> dict[str, object]:
    return {
        "status": "captured",
        "commit_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "dirty_working_tree": False,
    }


def test_safe_configuration_hash_stable_across_key_order() -> None:
    left = {"order_execution_enabled": False, "local_manual_mode": True, "environment": "test"}
    right = {"environment": "test", "local_manual_mode": True, "order_execution_enabled": False}
    assert safe_configuration_hash(settings_values=left, scanner_config=_scanner()) == safe_configuration_hash(
        settings_values=right,
        scanner_config=_scanner(),
    )
    modes_left = _scanner(strategy_modes=("scalp", "swing", "challenge"))
    modes_right = _scanner(strategy_modes=("challenge", "swing", "scalp"))
    assert safe_configuration_hash(settings_values=_settings(), scanner_config=modes_left) == safe_configuration_hash(
        settings_values=_settings(),
        scanner_config=modes_right,
    )


def test_safe_configuration_hash_changes_when_allowlisted_value_changes() -> None:
    baseline = safe_configuration_hash(settings_values=_settings(), scanner_config=_scanner())
    changed = safe_configuration_hash(
        settings_values=_settings(local_manual_mode=False),
        scanner_config=_scanner(),
    )
    assert baseline != changed
    rr_changed = safe_configuration_hash(
        settings_values=_settings(),
        scanner_config=_scanner(min_rr="2.7"),
    )
    assert baseline != rr_changed


def test_secrets_and_nested_fields_are_excluded_from_hash_and_payload() -> None:
    secrets = _settings()
    secrets.update(
        {
            "telegram_bot_token": "123:secret-token",
            "api_key": "cmc-key",
            "password": "hunter2",
            "database_url": "postgresql+psycopg://user:pass@localhost/db",
            "nested": {"token": "still-secret", "enabled": True},
        }
    )
    scanner = _scanner()
    setattr(scanner, "telegram_bot_token", "should-not-be-read")
    setattr(scanner, "cache_file", "/secrets/path")
    encoded = canonical_json_dumps(
        collect_run_provenance(
            scanner_config=scanner,
            settings_values=secrets,
            git_info=_git(),
            package_version="0.1.0",
        )
    )
    assert "secret-token" not in encoded
    assert "cmc-key" not in encoded
    assert "hunter2" not in encoded
    assert "user:pass" not in encoded
    assert "still-secret" not in encoded
    assert "telegram_bot_token" not in encoded
    assert "password" not in encoded
    assert "database_url" not in encoded
    assert "nested" not in encoded
    with_secrets = safe_configuration_hash(settings_values=secrets, scanner_config=scanner)
    without_secrets = safe_configuration_hash(settings_values=_settings(), scanner_config=_scanner())
    assert with_secrets == without_secrets


def test_dirty_tree_is_not_reproducible_from_commit_alone() -> None:
    payload = collect_run_provenance(
        scanner_config=_scanner(),
        settings_values=_settings(),
        git_info={
            "status": "captured",
            "commit_sha": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            "dirty_working_tree": True,
        },
        package_version="0.1.0",
    )
    assert payload["git"]["dirty_working_tree"] is True
    assert payload["git"]["reproducible_from_commit_alone"] is False
    assert payload["versions"]["strategy_version"]["status"] == "unavailable"


def test_unavailable_provenance_uses_reason_codes_only() -> None:
    payload = unavailable_provenance("collection failed!!!")
    assert payload["status"] == "unavailable"
    assert payload["reason"] == "collection_failed"
    assert collect_run_provenance_safe(scanner_config=object())["schema_version"] == payload["schema_version"]


def test_store_scan_result_adds_provenance_without_changing_identity_surfaces(
    tmp_path,
    monkeypatch,
) -> None:
    reset_git_info_cache()
    monkeypatch.setattr(
        "app.storage.run_provenance.collect_run_provenance",
        lambda **kwargs: collect_run_provenance(
            scanner_config=kwargs.get("scanner_config"),
            settings_values=_settings(),
            git_info=_git(),
            package_version="0.1.0",
        ),
    )
    db_path = tmp_path / "provenance.sqlite"
    result = _scan_result(runtime_stats=ScannerRuntimeStats(total_runtime_seconds=1.5, completed_symbols=3))
    replay = _replay_summary()
    run_id = store_scan_result(db_path, result, replay_summary=replay)
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT runtime_stats_json, raw_payload_json FROM scan_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        fingerprints = [
            item[0]
            for item in connection.execute(
                "SELECT setup_fingerprint FROM replay_results WHERE run_id = ?",
                (run_id,),
            ).fetchall()
        ]
        symbols = connection.execute(
            "SELECT COUNT(*) FROM symbol_results WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
    runtime = json.loads(row[0])
    raw_payload = row[1]
    assert runtime["total_runtime_seconds"] == 1.5
    assert runtime["completed_symbols"] == 3
    provenance = runtime[PROVENANCE_RUNTIME_STATS_KEY]
    assert provenance["schema_version"] == "cci-run-provenance-v1"
    assert provenance["git"]["commit_sha"] == "a" * 40
    assert PROVENANCE_RUNTIME_STATS_KEY not in raw_payload
    assert "research_provenance" not in raw_payload
    assert symbols == 3
    assert fingerprints
    assert all(len(item) == 64 for item in fingerprints)


def test_store_scan_result_survives_provenance_failure(tmp_path, monkeypatch, caplog) -> None:
    def boom(**kwargs: object) -> dict[str, object]:
        del kwargs
        raise RuntimeError("telegram_bot_token=MUST-NOT-APPEAR")

    monkeypatch.setattr("app.storage.run_provenance.collect_run_provenance_safe", boom)
    db_path = tmp_path / "provenance-fail.sqlite"
    run_id = store_scan_result(db_path, _scan_result())
    with sqlite3.connect(db_path) as connection:
        stored = json.loads(
            connection.execute(
                "SELECT runtime_stats_json FROM scan_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        )
    assert stored["research_provenance"]["status"] == "unavailable"
    assert stored["research_provenance"]["reason"] == "collection_failed"
    combined = caplog.text + json.dumps(stored)
    assert "MUST-NOT-APPEAR" not in combined
    assert "telegram_bot_token=" not in combined
