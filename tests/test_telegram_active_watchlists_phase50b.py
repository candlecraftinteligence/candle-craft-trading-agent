from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.data.dtos import NA
from app.storage.database import open_initialized_database
from app.telegram_admin import TelegramAdminCommandService, TelegramAdminConfig, process_telegram_admin_commands
from app.telegram_admin.active_watchlists import (
    WATCHLIST_DASHBOARD_FOOTER,
    WatchlistStageDashboardResult,
    WatchlistStageItem,
    format_watchlist_stage_dashboard,
)
from app.telegram_admin.commands import (
    SCREEN_HEADER,
    WATCHLIST_BACK_BUTTON_LABEL,
    WATCHLIST_REFRESH_BUTTON_LABEL,
)
from tests.test_telegram_admin_commands import (
    FakeCommandTransport,
    _button_labels,
    _callback_data_values,
    _callback_update,
    _screen_send_calls,
    _update,
)


def _insert_attempt(
    db_path: Path,
    *,
    signal_id: str,
    symbol: str,
    direction: str = "long",
    alert_type: str = "WATCHLIST",
    status: str = "sent",
    new_state: str = "WATCHLISTED",
    lifecycle_state: str = "WATCHLISTED",
    sent_at: str = "2026-06-04T12:00:00Z",
    first_seen_at: str = "2026-06-04T12:00:00Z",
    scan_run_id: str = "run-active",
    price_level: str = NA,
    setup_quality_score: str = "B+",
    entry_low: str = NA,
    entry_high: str = NA,
    stop_loss: str = NA,
    tp1: str = NA,
    tp2: str = NA,
    tp3: str = NA,
    blocked_reason: str = NA,
    error_message: str = NA,
    last_error_message: str = NA,
) -> None:
    connection = open_initialized_database(db_path)
    try:
        connection.execute(
            """
            INSERT INTO telegram_alert_attempts (
                signal_id, symbol, direction, new_state, alert_type, lifecycle_state,
                sent_at, telegram_status, message_hash, scan_run_id, setup_quality_score, price_level,
                first_seen_at, entry_low, entry_high, stop_loss, tp1, tp2, tp3,
                blocked_reason, error_message, last_error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_id,
                symbol,
                direction,
                new_state,
                alert_type,
                lifecycle_state,
                sent_at,
                status,
                f"hash-{signal_id}-{alert_type}",
                scan_run_id,
                setup_quality_score,
                price_level,
                first_seen_at,
                entry_low,
                entry_high,
                stop_loss,
                tp1,
                tp2,
                tp3,
                blocked_reason,
                error_message,
                last_error_message,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _insert_scan_run(db_path: Path, *, run_id: str, symbol: str) -> None:
    connection = open_initialized_database(db_path)
    try:
        connection.execute(
            """
            INSERT OR IGNORE INTO scan_runs (
                run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
                strategy, timeframes_json, market_regime, runtime_stats_json,
                command_preset, command_used, total_valid_setups, near_misses,
                rejected, data_issues, data_issues_json, raw_payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "2026-06-04T12:00:00Z",
                "binance",
                "manual",
                1,
                f'["{symbol}"]',
                "liquidity_grab_pullback",
                "{}",
                "mixed",
                "{}",
                "phase46d",
                "test",
                0,
                1,
                0,
                0,
                "[]",
                "{}",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _insert_symbol_result(
    db_path: Path,
    *,
    run_id: str,
    symbol: str,
    failed_gate: str = NA,
    rejection_reason: str = NA,
    next_trigger_needed: str = NA,
    setup_quality_score: str = "B+",
    readiness_score: int = 70,
    raw_result: dict[str, Any] | None = None,
) -> None:
    _insert_scan_run(db_path, run_id=run_id, symbol=symbol)
    connection = open_initialized_database(db_path)
    try:
        connection.execute(
            """
            INSERT INTO symbol_results (
                run_id, symbol, status, display_bucket, readiness_score,
                setup_quality_score, edge_score, failed_gate, rejection_reason,
                next_trigger_needed, action_label, regime_state, derivatives_context_json,
                volume_profile_context_json, pullback_status, portfolio_decision,
                raw_result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                symbol,
                "near_miss",
                "near_miss",
                readiness_score,
                setup_quality_score,
                NA,
                failed_gate,
                rejection_reason,
                next_trigger_needed,
                "watchlist",
                "mixed",
                "{}",
                "{}",
                NA,
                NA,
                json.dumps(raw_result or {}),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _insert_lifecycle_record(
    db_path: Path,
    *,
    lifecycle_id: str,
    symbol: str,
    current_state: str,
    direction: str = "long",
    failed_gate: str = NA,
    invalidation_reason: str = NA,
    quality_score: int = 85,
    readiness_score: int = 70,
    last_seen_at: str = "2026-06-04T12:00:00Z",
    last_transition_at: str = "2026-06-04T12:00:00Z",
) -> None:
    connection = open_initialized_database(db_path)
    try:
        connection.execute(
            """
            INSERT INTO setup_lifecycle_records (
                lifecycle_id, symbol, mode, direction, current_state, previous_state,
                first_seen_at, last_seen_at, last_transition_at, failed_gate,
                readiness_score, quality_score, edge_score, regime_state, action_label,
                invalidation_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lifecycle_id,
                symbol,
                "scalp",
                direction,
                current_state,
                NA,
                "2026-06-04T10:00:00Z",
                last_seen_at,
                last_transition_at,
                failed_gate,
                readiness_score,
                quality_score,
                NA,
                "mixed",
                "watchlist",
                invalidation_reason,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _insert_candidate(
    db_path: Path,
    *,
    run_id: str,
    symbol: str,
    direction: str,
    entry: str,
    stop: str,
    tp1: str = NA,
    tp2: str = NA,
    tp3: str = NA,
) -> None:
    connection = open_initialized_database(db_path)
    try:
        connection.execute(
            """
            INSERT OR IGNORE INTO scan_runs (
                run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
                strategy, timeframes_json, market_regime, runtime_stats_json,
                command_preset, command_used, total_valid_setups, near_misses,
                rejected, data_issues, data_issues_json, raw_payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                "2026-06-04T12:00:00Z",
                "binance",
                "manual",
                1,
                f'["{symbol}"]',
                "liquidity_grab_pullback",
                "{}",
                "mixed",
                "{}",
                "phase50b",
                "test",
                0,
                1,
                0,
                0,
                "[]",
                "{}",
            ),
        )
        connection.execute(
            """
            INSERT INTO setup_candidates (
                run_id, symbol, mode, direction, entry, stop, tp1, tp2, tp3, rr,
                invalidation, quality_grade, trust_meter, risk_warning, raw_candidate_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                symbol,
                "scalp",
                direction,
                entry,
                stop,
                tp1,
                tp2,
                tp3,
                "3",
                "Invalid beyond stop.",
                "B",
                "70",
                "Manual review only.",
                "{}",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _service(tmp_path: Path, db_path: Path | None = None) -> TelegramAdminCommandService:
    return TelegramAdminCommandService(project_root=tmp_path, database_path=db_path or tmp_path / "missing.db")


def test_grouped_watchlist_formatter_orders_buckets_and_empty_rows() -> None:
    text = format_watchlist_stage_dashboard(
        WatchlistStageDashboardResult(
            source_available=True,
            stalking_items=(
                WatchlistStageItem(signal_id="sig-stalk", symbol="STALKUSDT", stage="STALKING", reason="pullback forming"),
            ),
            stalking_total=1,
            watch_items=(
                WatchlistStageItem(signal_id="sig-watch", symbol="WATCHUSDT", stage="WATCH", reason=NA),
            ),
            watch_total=1,
            cooldown_items=(
                WatchlistStageItem(signal_id="sig-cool", symbol="COOLUSDT", stage="COOLDOWN", reason="invalidated, waiting reset"),
            ),
            cooldown_total=1,
        )
    )

    assert text.startswith("🐺🟠 WATCHLISTS")
    assert text.endswith(WATCHLIST_DASHBOARD_FOOTER)
    assert text.index("🔥 STALKING") < text.index("👀 WATCH") < text.index("❄️ COOLDOWN")
    assert "STALKUSDT — pullback forming" in text
    assert "WATCHUSDT — N/A" in text
    assert "COOLUSDT — invalidated, waiting reset" in text
    assert "SOLUSDT.P" not in text
    assert "LINKUSDT.P" not in text


def test_grouped_watchlist_formatter_empty_state() -> None:
    text = format_watchlist_stage_dashboard(WatchlistStageDashboardResult(source_available=False))

    assert text.count("None right now.") == 3
    assert "No forced trades." in text
    assert text.endswith(WATCHLIST_DASHBOARD_FOOTER)


def test_active_watchlists_find_newest_scan_runs_sqlite_with_alert_attempts(tmp_path: Path) -> None:
    scan_dir = tmp_path / "scan_runs"
    scan_dir.mkdir()
    old_db = scan_dir / "old.sqlite"
    new_db = scan_dir / "new.sqlite"
    _insert_attempt(old_db, signal_id="sig-old", symbol="BTCUSDT", entry_low="104250", entry_high="104800")
    _insert_attempt(new_db, signal_id="sig-new", symbol="ENAUSDT", entry_low="0.09402", entry_high="0.09497")
    os.utime(old_db, (1, 1))
    os.utime(new_db, (2, 2))

    response = TelegramAdminCommandService(project_root=tmp_path).public_response_for("/watchlists")

    assert response.text.startswith("🐺🟠 WATCHLISTS")
    assert response.text.endswith(WATCHLIST_DASHBOARD_FOOTER)
    assert "ENAUSDT — N/A" in response.text
    assert "BTCUSDT" not in response.text
    assert "scan_runs" not in response.text


def test_active_watchlists_empty_state_when_no_scan_database_exists(tmp_path: Path) -> None:
    response = TelegramAdminCommandService(project_root=tmp_path).public_response_for("/watchlists")

    assert response.text.startswith("🐺🟠 WATCHLISTS")
    assert response.text.count("None right now.") == 3
    assert "No forced trades." in response.text
    assert "scan_runs" not in response.text


def test_active_signals_use_sent_runtime_signal_attempts(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(
        db_path,
        signal_id="sig-confirmed",
        symbol="BTCUSDT",
        alert_type="SIGNAL_CONFIRMED",
        setup_quality_score="91",
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        tp1="112",
        tp2="120",
        tp3="130",
    )
    _insert_attempt(db_path, signal_id="sig-watch", symbol="WATCHUSDT", alert_type="WATCHLIST", entry_low="1", entry_high="2")
    _insert_attempt(
        db_path,
        signal_id="sig-blocked",
        symbol="BLOCKUSDT",
        alert_type="SIGNAL_CONFIRMED",
        status="blocked",
        entry_low="1",
        entry_high="2",
    )

    response = _service(tmp_path, db_path).public_response_for("/signals")

    assert response.text.startswith(f"{SCREEN_HEADER} Active Signals")
    assert "Confirmed Candle Craft setups." in response.text
    assert "Symbol: BTCUSDT" in response.text
    assert "Direction: Long" in response.text
    assert "Grade: 91" in response.text
    assert "Entry: 100 – 102" in response.text
    assert "Stop: 95" in response.text
    assert "Targets: 112, 120, 130" in response.text
    assert "Status: Confirmed setup" in response.text
    assert "Updated: 2026-06-04T12:00:00Z" in response.text
    assert "WATCHUSDT" not in response.text
    assert "BLOCKUSDT" not in response.text
    assert "order was placed" not in response.text.lower()


def test_active_signals_empty_state_does_not_promote_watchlists(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(db_path, signal_id="sig-watch", symbol="BTCUSDT", alert_type="WATCHLIST", entry_low="100", entry_high="102")

    response = _service(tmp_path, db_path).public_response_for("/signals")

    assert "No active confirmed signals right now." in response.text
    assert "The engine is waiting for clean structure." in response.text
    assert "BTCUSDT" not in response.text


def test_active_signals_show_runtime_progress_and_exclude_terminal_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(
        db_path,
        signal_id="sig-active",
        symbol="ENAUSDT",
        alert_type="SIGNAL_CONFIRMED",
        entry_low="0.09402",
        entry_high="0.09497",
        stop_loss="0.0923",
        tp1="0.1",
        tp2="0.105",
    )
    _insert_attempt(db_path, signal_id="sig-active", symbol="ENAUSDT", alert_type="LIMIT_HIT")
    _insert_attempt(db_path, signal_id="sig-active", symbol="ENAUSDT", alert_type="TP1_HIT", sent_at="2026-06-04T12:15:00Z")
    _insert_attempt(
        db_path,
        signal_id="sig-closed",
        symbol="CLOSEDUSDT",
        alert_type="SIGNAL_CONFIRMED",
        entry_low="10",
        entry_high="11",
    )
    _insert_attempt(db_path, signal_id="sig-closed", symbol="CLOSEDUSDT", alert_type="TP3_HIT")

    response = _service(tmp_path, db_path).public_response_for("/signals")

    assert "Symbol: ENAUSDT" in response.text
    assert "Status: TP1 HIT" in response.text
    assert "Updated: 2026-06-04T12:15:00Z" in response.text
    assert "CLOSEDUSDT" not in response.text


def test_active_signals_find_scan_run_sqlite_when_default_runtime_db_is_empty(tmp_path: Path) -> None:
    scan_dir = tmp_path / "scan_runs"
    scan_dir.mkdir()
    default_db = scan_dir / "candle_craft.db"
    open_initialized_database(default_db).close()
    run_db = scan_dir / "runtime.sqlite"
    _insert_attempt(
        run_db,
        signal_id="sig-runtime",
        symbol="SOLUSDT",
        alert_type="SIGNAL_CONFIRMED",
        entry_low="150",
        entry_high="151",
        stop_loss="148",
        tp1="155",
    )

    response = TelegramAdminCommandService(project_root=tmp_path).public_response_for("/signals")

    assert "Symbol: SOLUSDT" in response.text
    assert "No active confirmed signals right now." not in response.text


def test_watchlists_dashboard_groups_stalking_watch_and_cooldown_data(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(
        db_path,
        signal_id="sig-watch",
        symbol="WATCHUSDT",
        scan_run_id="run-watch",
    )
    _insert_symbol_result(
        db_path,
        run_id="run-watch",
        symbol="WATCHUSDT",
        failed_gate="missing_confirmed_sweep",
    )
    _insert_attempt(
        db_path,
        signal_id="sig-stalk",
        symbol="STALKUSDT",
        lifecycle_state="STALKING",
        new_state="STALKING",
        scan_run_id="run-stalk",
    )
    _insert_symbol_result(
        db_path,
        run_id="run-stalk",
        symbol="STALKUSDT",
        failed_gate="missing_confirmation_structure_shift",
        setup_quality_score="A",
    )
    _insert_attempt(
        db_path,
        signal_id="sig-cool",
        symbol="COOLUSDT",
    )
    _insert_attempt(
        db_path,
        signal_id="sig-cool",
        symbol="COOLUSDT",
        alert_type="INVALIDATED",
        lifecycle_state="INVALIDATED",
        new_state="INVALIDATED",
        sent_at="2026-06-04T12:15:00Z",
    )
    _insert_attempt(
        db_path,
        signal_id="sig-unverified",
        symbol="UNVERIFIEDUSDT",
        blocked_reason="Unverified",
    )
    _insert_attempt(
        db_path,
        signal_id="sig-missing",
        symbol="MISSINGUSDT",
    )
    _insert_attempt(db_path, signal_id="sig-closed-tp3", symbol="ADAUSDT", entry_low="1", entry_high="1.01")
    _insert_attempt(db_path, signal_id="sig-closed-tp3", symbol="ADAUSDT", alert_type="TP3_HIT")
    _insert_attempt(db_path, signal_id="sig-closed-sl", symbol="DOGEUSDT", entry_low="0.2", entry_high="0.21")
    _insert_attempt(db_path, signal_id="sig-closed-sl", symbol="DOGEUSDT", alert_type="SL_HIT")
    _insert_attempt(db_path, signal_id="sig-blocked", symbol="BLOCKUSDT", status="blocked", entry_low="1", entry_high="2")
    _insert_attempt(db_path, signal_id="sig-skipped", symbol="SKIPUSDT", status="skipped", entry_low="1", entry_high="2")
    _insert_attempt(db_path, signal_id="sig-active", symbol="ACTIVEUSDT")
    _insert_attempt(
        db_path,
        signal_id="sig-active",
        symbol="ACTIVEUSDT",
        alert_type="SIGNAL_CONFIRMED",
        lifecycle_state="CONFIRMED",
        new_state="CONFIRMED",
    )

    response = _service(tmp_path, db_path).public_response_for("/watchlists")

    assert response.text.startswith("🐺🟠 WATCHLISTS")
    assert response.text.index("🔥 STALKING") < response.text.index("👀 WATCH") < response.text.index("❄️ COOLDOWN")
    assert "STALKUSDT — sweep done, waiting BOS/CHoCH" in response.text
    assert "WATCHUSDT — waiting liquidity sweep" in response.text
    assert "UNVERIFIEDUSDT — Unverified" in response.text
    assert "MISSINGUSDT — N/A" in response.text
    assert "COOLUSDT — invalidated, waiting reset" in response.text
    assert "ACTIVEUSDT" not in response.text
    assert "ADAUSDT" not in response.text
    assert "DOGEUSDT" not in response.text
    assert "BLOCKUSDT" not in response.text
    assert "SKIPUSDT" not in response.text
    assert "Decimal(" not in response.text
    assert "{" not in response.text
    assert "}" not in response.text
    assert "order was placed" not in response.text.lower()
    assert "automatic execution" not in response.text.lower()


def test_watchlists_dashboard_retains_invalidated_rows_in_cooldown(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(db_path, signal_id="sig-invalidated", symbol="BTCUSDT", entry_low="100", entry_high="102")
    _insert_attempt(
        db_path,
        signal_id="sig-invalidated",
        symbol="BTCUSDT",
        alert_type="INVALIDATED",
        lifecycle_state="INVALIDATED",
        new_state="INVALIDATED",
    )

    response = _service(tmp_path, db_path).public_response_for("/watchlists")

    assert "❄️ COOLDOWN" in response.text
    assert "BTCUSDT — invalidated, waiting reset" in response.text


def test_active_watchlist_older_than_48h_expires_from_public_output(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    old_seen = (datetime.now(UTC) - timedelta(hours=49)).isoformat().replace("+00:00", "Z")
    _insert_attempt(
        db_path,
        signal_id="sig-old-watch",
        symbol="OLDUSDT",
        first_seen_at=old_seen,
        sent_at=old_seen,
        entry_low="100",
        entry_high="102",
    )

    response = _service(tmp_path, db_path).public_response_for("/watchlists")

    assert response.text.count("None right now.") == 3
    assert "No forced trades." in response.text
    assert "OLDUSDT" not in response.text


def test_active_watchlist_younger_than_48h_remains_visible(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    fresh_seen = (datetime.now(UTC) - timedelta(hours=47)).isoformat().replace("+00:00", "Z")
    _insert_attempt(
        db_path,
        signal_id="sig-fresh-watch",
        symbol="FRESHUSDT",
        first_seen_at=fresh_seen,
        sent_at=fresh_seen,
        entry_low="100",
        entry_high="102",
    )

    response = _service(tmp_path, db_path).public_response_for("/watchlists")

    assert "FRESHUSDT — N/A" in response.text


def test_triggered_watchlist_does_not_expire_due_to_watch_ttl(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    old_seen = (datetime.now(UTC) - timedelta(hours=96)).isoformat().replace("+00:00", "Z")
    _insert_attempt(
        db_path,
        signal_id="sig-triggered-watch",
        symbol="TRIGGERUSDT",
        new_state="TRIGGERED",
        lifecycle_state="TRIGGERED",
        first_seen_at=old_seen,
        sent_at=old_seen,
        entry_low="100",
        entry_high="102",
    )

    response = _service(tmp_path, db_path).public_response_for("/watchlists")

    assert "🔥 STALKING" in response.text
    assert "TRIGGERUSDT — N/A" in response.text


def test_confirmed_active_signal_does_not_expire_due_to_watch_ttl(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    old_seen = (datetime.now(UTC) - timedelta(hours=96)).isoformat().replace("+00:00", "Z")
    _insert_attempt(
        db_path,
        signal_id="sig-confirmed-old",
        symbol="CONFIRMUSDT",
        alert_type="SIGNAL_CONFIRMED",
        new_state="CONFIRMED",
        lifecycle_state="CONFIRMED",
        first_seen_at=old_seen,
        sent_at=old_seen,
        setup_quality_score="B+",
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        tp1="110",
    )

    response = _service(tmp_path, db_path).public_response_for("/signals")

    assert "Symbol: CONFIRMUSDT" in response.text


def test_grade_b_is_hidden_from_public_active_watchlist_output(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(
        db_path,
        signal_id="sig-grade-b",
        symbol="LOWGRADEUSDT",
        setup_quality_score="B",
        entry_low="100",
        entry_high="102",
    )

    response = _service(tmp_path, db_path).public_response_for("/watchlists")

    assert "LOWGRADEUSDT" not in response.text
    assert response.text.count("None right now.") == 3


def test_watchlists_dashboard_missing_reason_displays_na(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(db_path, signal_id="sig-candidate", symbol="LINKUSDT", direction="short", scan_run_id="run-candidate")
    _insert_candidate(
        db_path,
        run_id="run-candidate",
        symbol="LINKUSDT",
        direction="short",
        entry="18.20-18.40",
        stop="18.90",
        tp1="17.80",
        tp2="17.30",
    )

    response = _service(tmp_path, db_path).public_response_for("/watchlists")

    assert "LINKUSDT — N/A" in response.text
    assert "Limit Zone:" not in response.text
    assert "TP3" not in response.text
    assert "raw_candidate_json" not in response.text


def test_watchlists_dashboard_limits_each_bucket_to_eight_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    for index in range(11):
        _insert_attempt(
            db_path,
            signal_id=f"sig-{index:02d}",
            symbol=f"SYM{index:02d}USDT",
            entry_low=str(100 + index),
            entry_high=str(101 + index),
        )

    response = _service(tmp_path, db_path).public_response_for("/watchlists")

    assert "+ 3 more" in response.text
    assert "SYM00USDT — N/A" in response.text
    assert "SYM07USDT — N/A" in response.text
    assert "SYM08USDT" not in response.text
    assert "SYM10USDT" not in response.text


def test_public_watchlist_commands_and_refresh_back_buttons_route_safely(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(db_path, signal_id="sig-btc", symbol="BTCUSDT", entry_low="104250", entry_high="104800")
    service = _service(tmp_path, db_path)
    transport = FakeCommandTransport()
    updates = (
        _update(1, "public-chat", "/watchlists"),
        _update(2, "public-chat", "/watchlist"),
        _update(3, "public-chat", "👁 Watchlist"),
        _update(4, "public-chat", "👁 Watchlists"),
        _update(5, "public-chat", "Active Watchlists"),
        _callback_update(6, "public-chat", "public:watchlist"),
        _callback_update(7, "public-chat", "public:menu"),
    )

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=updates,
        )
    )

    screen_calls = _screen_send_calls(transport)
    messages = [call["message"] for call in screen_calls]
    watchlist_calls = screen_calls[:6]
    assert result.delivery_status == "sent_public"
    assert len(messages) == len(updates)
    assert [call["callback_query_id"] for call in transport.answer_callback_calls] == ["callback-6", "callback-7"]
    assert all(call["message"].startswith("🐺🟠 WATCHLISTS") for call in watchlist_calls)
    assert all("BTCUSDT — N/A" in call["message"] for call in watchlist_calls)
    assert all(
        _button_labels(call["reply_markup"]) == [WATCHLIST_REFRESH_BUTTON_LABEL, WATCHLIST_BACK_BUTTON_LABEL]
        for call in watchlist_calls
    )
    assert all(_callback_data_values(call["reply_markup"]) == ["public:watchlist", "public:menu"] for call in watchlist_calls)
    assert "Candle Craft Intelligence" in messages[-1]
    assert "secret-token" not in "\n".join(messages)
