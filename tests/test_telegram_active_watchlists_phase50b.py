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
    SIGNAL_DETAIL_BACK_BUTTON_LABEL,
    SIGNAL_DETAIL_LIFECYCLE_BUTTON_LABEL,
    SIGNAL_DETAIL_REFRESH_BUTTON_LABEL,
    SIGNAL_DETAIL_WHY_VALID_BUTTON_LABEL,
    WATCHLIST_BACK_BUTTON_LABEL,
    WATCHLIST_REFRESH_BUTTON_LABEL,
    command_for_callback_data,
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
    rr_planned: str = NA,
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
                sent_at, telegram_status, message_hash, scan_run_id, setup_quality_score, rr_planned, price_level,
                first_seen_at, entry_low, entry_high, stop_loss, tp1, tp2, tp3,
                blocked_reason, error_message, last_error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                rr_planned,
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


def _insert_lifecycle_event(
    db_path: Path,
    *,
    lifecycle_id: str,
    symbol: str,
    from_state: str,
    to_state: str,
    reason: str = "Existing lifecycle transition.",
    timestamp: str = "2026-06-04T12:00:00Z",
) -> None:
    connection = open_initialized_database(db_path)
    try:
        connection.execute(
            """
            INSERT INTO setup_lifecycle_events (
                lifecycle_id, timestamp, symbol, from_state, to_state, reason,
                readiness_score, quality_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (lifecycle_id, timestamp, symbol, from_state, to_state, reason, 75, 90),
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
    invalidation: str = "Invalid beyond stop.",
    quality_grade: str = "B",
    raw_candidate: dict[str, Any] | None = None,
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
                invalidation,
                quality_grade,
                "70",
                "Manual review only.",
                json.dumps(raw_candidate or {}),
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _service(tmp_path: Path, db_path: Path | None = None) -> TelegramAdminCommandService:
    return TelegramAdminCommandService(project_root=tmp_path, database_path=db_path or tmp_path / "missing.db")


def _callback_update_with_chat_type(update_id: int, chat_id: str, callback_data: str, chat_type: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": chat_id},
            "message": {"chat": {"id": chat_id, "type": chat_type}},
            "data": callback_data,
        },
    }


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
        rr_planned="3.1",
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
    assert "Current active signal records." in response.text
    assert "Select a symbol for details." in response.text
    assert "Active signals: 1" in response.text
    assert _button_labels(response.reply_markup) == ["BTCUSDT"]
    assert _callback_data_values(response.reply_markup) == ["public:signal:BTCUSDT"]
    assert "Symbol: BTCUSDT" not in response.text
    assert "WATCHUSDT" not in response.text
    assert "BLOCKUSDT" not in response.text
    assert "order was placed" not in response.text.lower()


def test_active_signals_dedupe_same_symbol_bias_and_hide_expired_duplicate(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(
        db_path,
        signal_id="sig-old-expired",
        symbol="BTCUSDT",
        alert_type="SIGNAL_CONFIRMED",
        setup_quality_score="A",
        rr_planned="3.1",
        entry_low="99",
        entry_high="101",
        stop_loss="95",
        tp1="110",
        tp2="117",
        tp3="124",
        sent_at="2026-06-04T11:45:00Z",
    )
    _insert_lifecycle_record(
        db_path,
        lifecycle_id="sig-old-expired",
        symbol="BTCUSDT",
        current_state="EXPIRED",
        invalidation_reason="no price reaction or lifecycle progress",
    )
    _insert_attempt(
        db_path,
        signal_id="sig-new-current",
        symbol="BTCUSDT",
        alert_type="SIGNAL_CONFIRMED",
        setup_quality_score="A-",
        rr_planned="3.2",
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        tp1="110",
        tp2="117",
        tp3="124",
        sent_at="2026-06-04T12:00:00Z",
    )

    response = _service(tmp_path, db_path).public_response_for("/signals")

    assert "Active signals: 1" in response.text
    assert _button_labels(response.reply_markup) == ["BTCUSDT"]
    assert _callback_data_values(response.reply_markup) == ["public:signal:BTCUSDT"]


def test_active_signals_show_direct_limit_zone_hit_setups(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(
        db_path,
        signal_id="sig-limit",
        symbol="BTCUSDT",
        alert_type="LIMIT_HIT",
        new_state="EXECUTING",
        lifecycle_state="EXECUTING",
        setup_quality_score="A+",
        rr_planned="3.2",
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        tp1="110",
        tp2="115",
        tp3="120",
    )
    _insert_lifecycle_record(
        db_path,
        lifecycle_id="sig-limit",
        symbol="BTCUSDT",
        current_state="EXECUTING",
        invalidation_reason="Invalid if price accepts below 95.",
    )

    response = _service(tmp_path, db_path).public_response_for("/signals")

    assert "Active signals: 1" in response.text
    assert _button_labels(response.reply_markup) == ["BTCUSDT"]
    assert "Symbol: BTCUSDT" not in response.text

    detail = _service(tmp_path, db_path).public_response_for("/signal BTCUSDT")
    assert "Status: LIMIT ZONE HIT" in detail.text
    assert "Quality: A+" in detail.text
    assert "RR: 3.20R" in detail.text
    assert "Entry Zone: 100 – 102" in detail.text
    assert "Stop: 95" in detail.text
    assert "TP1: 110" in detail.text
    assert "TP2: 115" in detail.text
    assert "TP3: 120" in detail.text
    assert "Invalid if price accepts below 95." in detail.text
    assert "Lifecycle: EXECUTING" in detail.text


def test_crclusdt_limit_zone_hit_active_signal_detail_regression(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(
        db_path,
        signal_id="crcl-limit-zone-hit",
        symbol="CRCLUSDT",
        alert_type="LIMIT_HIT",
        new_state="LIMIT_ZONE_HIT",
        lifecycle_state="LIMIT_ZONE_HIT",
        setup_quality_score="A-",
        rr_planned="2.7244673",
        entry_low="42.123456",
        entry_high="42.987654",
        stop_loss="40.75",
        tp1="45.25",
        tp2="47.5",
        tp3="50",
    )
    _insert_lifecycle_record(
        db_path,
        lifecycle_id="crcl-limit-zone-hit",
        symbol="CRCLUSDT",
        current_state="EXECUTING",
        invalidation_reason="Invalid if price accepts below 40.75.",
    )
    _insert_lifecycle_event(
        db_path,
        lifecycle_id="crcl-limit-zone-hit",
        symbol="CRCLUSDT",
        from_state="CONFIRMED",
        to_state="LIMIT_ZONE_HIT",
        reason="Limit zone touched by persisted lifecycle data.",
        timestamp="2026-06-04T12:05:00Z",
    )
    service = _service(tmp_path, db_path)

    active = service.public_response_for("/signals")

    assert _button_labels(active.reply_markup) == ["CRCLUSDT"]
    assert _callback_data_values(active.reply_markup) == ["public:signal:CRCLUSDT"]

    detail = service.public_response_for("/signal CRCLUSDT")

    assert detail.text.startswith("🐺🟠 CRCLUSDT — SIGNAL DETAIL")
    assert "Status: LIMIT ZONE HIT" in detail.text
    assert "Quality: A-" in detail.text
    assert "RR: 2.72R" in detail.text
    assert "Lifecycle: CONFIRMED → LIMIT ZONE HIT" in detail.text
    assert "Entry Zone: 42.12 – 42.99" in detail.text
    assert "Stop: 40.75" in detail.text
    assert "TP1: 45.25" in detail.text
    assert "TP2: 47.5" in detail.text
    assert "TP3: 50" in detail.text
    assert "Invalid if price accepts below 40.75." in detail.text
    assert "No active signal detail found." not in detail.text
    assert "No data was changed." not in detail.text


def test_confirmed_signal_opens_detail_from_active_signals_and_refresh_reloads(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(
        db_path,
        signal_id="sig-detail",
        symbol="BTCUSDT",
        alert_type="SIGNAL_CONFIRMED",
        new_state="CONFIRMED",
        lifecycle_state="CONFIRMED",
        setup_quality_score="A-",
        rr_planned="2.72",
        scan_run_id="run-detail",
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        tp1="110",
        tp2="118",
        tp3="125",
    )
    _insert_candidate(
        db_path,
        run_id="run-detail",
        symbol="BTCUSDT",
        direction="long",
        entry="100-102",
        stop="95",
        tp1="110",
        tp2="118",
        tp3="125",
        invalidation="Price accepts below 95.",
        quality_grade="A-",
        raw_candidate={
            "reason_for_trade": "Liquidity swept and structure shifted.",
            "confirmed_facts": ["Sweep confirmed.", "Structure shift confirmed."],
            "quality_gate_result": {"passed": True},
        },
    )
    _insert_lifecycle_record(
        db_path,
        lifecycle_id="sig-detail",
        symbol="BTCUSDT",
        current_state="EXECUTING",
        direction="long",
        quality_score=90,
    )
    _insert_lifecycle_event(
        db_path,
        lifecycle_id="sig-detail",
        symbol="BTCUSDT",
        from_state="WATCHLISTED",
        to_state="CONFIRMED",
        reason="Confirmed by existing lifecycle data.",
        timestamp="2026-06-04T11:00:00Z",
    )
    _insert_lifecycle_event(
        db_path,
        lifecycle_id="sig-detail",
        symbol="BTCUSDT",
        from_state="CONFIRMED",
        to_state="EXECUTING",
        reason="Waiting limit fill from persisted lifecycle.",
        timestamp="2026-06-04T12:00:00Z",
    )
    service = _service(tmp_path, db_path)

    active = service.public_response_for("/signals")

    assert "Active signals: 1" in active.text
    assert "Symbol: BTCUSDT" not in active.text
    assert _button_labels(active.reply_markup) == ["BTCUSDT"]
    assert _callback_data_values(active.reply_markup) == ["public:signal:BTCUSDT"]

    detail = service.public_response_for("/signal BTCUSDT")

    assert detail.text.startswith("🐺🟠 BTCUSDT — SIGNAL DETAIL")
    assert "Bias: LONG" in detail.text
    assert "Status: CONFIRMED" in detail.text
    assert "Quality: A-" in detail.text
    assert "RR: 2.72R" in detail.text
    assert "Lifecycle: WATCHLISTED → CONFIRMED → EXECUTING" in detail.text
    assert "Entry Zone: 100 – 102" in detail.text
    assert "Stop: 95" in detail.text
    assert "TP1: 110" in detail.text
    assert "TP2: 118" in detail.text
    assert "TP3: 125" in detail.text
    assert "Liquidity swept and structure shifted." in detail.text
    assert "Price accepts below 95." in detail.text
    assert detail.text.endswith("Candle Craft | Signal. Structure. Execution.")
    assert _button_labels(detail.reply_markup) == [
        SIGNAL_DETAIL_REFRESH_BUTTON_LABEL,
        SIGNAL_DETAIL_LIFECYCLE_BUTTON_LABEL,
        SIGNAL_DETAIL_WHY_VALID_BUTTON_LABEL,
        SIGNAL_DETAIL_BACK_BUTTON_LABEL,
    ]
    assert _callback_data_values(detail.reply_markup) == [
        "public:signal:BTCUSDT",
        "public:signal_lifecycle:BTCUSDT",
        "public:signal_why:BTCUSDT",
        "public:signals",
    ]

    lifecycle = service.public_response_for("/signal_lifecycle BTCUSDT")
    assert lifecycle.text.startswith("🐺🟠 BTCUSDT — LIFECYCLE")
    assert "WATCHLISTED → CONFIRMED → EXECUTING" in lifecycle.text
    assert "Latest reason: Waiting limit fill from persisted lifecycle." in lifecycle.text

    why = service.public_response_for("/signal_why BTCUSDT")
    assert why.text.startswith("🐺🟠 BTCUSDT — WHY VALID?")
    assert "Confirmed facts\nSweep confirmed.\nStructure shift confirmed." in why.text
    assert "Confirmed gates\nLifecycle state EXECUTING." in why.text
    assert "strategy_diagnostics" not in why.text
    assert "{" not in why.text

    with open_initialized_database(db_path) as connection:
        connection.execute(
            "UPDATE telegram_alert_attempts SET tp1 = ? WHERE signal_id = ? AND alert_type = ?",
            ("111", "sig-detail", "SIGNAL_CONFIRMED"),
        )
        connection.commit()
    scope, refresh_command = command_for_callback_data("public:signal:BTCUSDT")
    assert scope == "public"
    refreshed = service.public_response_for(refresh_command)
    assert "TP1: 111" in refreshed.text
    assert "TP1: 110" not in refreshed.text


def test_signal_detail_callbacks_route_safely_and_back_to_active_signals(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(
        db_path,
        signal_id="sig-callback",
        symbol="ETHUSDT",
        alert_type="SIGNAL_CONFIRMED",
        setup_quality_score="A",
        rr_planned="3",
        entry_low="10",
        entry_high="11",
        stop_loss="9",
        tp1="12",
        tp2="13",
        tp3="14",
    )
    service = _service(tmp_path, db_path)
    transport = FakeCommandTransport()
    updates = (
        _callback_update(20, "public-chat", "public:signal:ETHUSDT"),
        _callback_update(21, "public-chat", "public:signal_why:ETHUSDT"),
        _callback_update(22, "public-chat", "public:signal_lifecycle:ETHUSDT"),
        _callback_update(23, "public-chat", "public:signals"),
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
    assert result.delivery_status == "sent_public"
    assert result.sent_count == len(updates)
    assert [call["callback_query_id"] for call in transport.answer_callback_calls] == [
        "callback-20",
        "callback-21",
        "callback-22",
        "callback-23",
    ]
    assert screen_calls[0]["message"].startswith("🐺🟠 ETHUSDT — SIGNAL DETAIL")
    assert screen_calls[1]["message"].startswith("🐺🟠 ETHUSDT — WHY VALID?")
    assert screen_calls[2]["message"].startswith("🐺🟠 ETHUSDT — LIFECYCLE")
    assert "Active Signals" in screen_calls[3]["message"]
    assert _callback_data_values(screen_calls[0]["reply_markup"]) == [
        "public:signal:ETHUSDT",
        "public:signal_lifecycle:ETHUSDT",
        "public:signal_why:ETHUSDT",
        "public:signals",
    ]
    assert "order was placed" not in "\n".join(call["message"].lower() for call in screen_calls)


def test_signal_detail_ui_is_not_sent_to_public_channel_group_or_supergroup(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(
        db_path,
        signal_id="sig-channel-safe",
        symbol="ADAUSDT",
        alert_type="SIGNAL_CONFIRMED",
        setup_quality_score="A",
        entry_low="1",
        entry_high="1.1",
    )
    service = _service(tmp_path, db_path)
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                public_channel_id="public-channel",
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(
                _callback_update_with_chat_type(30, "public-channel", "public:signal:ADAUSDT", "channel"),
                _callback_update_with_chat_type(31, "group-chat", "public:signal:ADAUSDT", "group"),
                _callback_update_with_chat_type(32, "supergroup-chat", "public:signal:ADAUSDT", "supergroup"),
            ),
        )
    )

    assert result.delivery_status == "ignored_unauthorized"
    assert result.sent_count == 0
    assert transport.send_calls == []
    assert [call["callback_query_id"] for call in transport.answer_callback_calls] == [
        "callback-30",
        "callback-31",
        "callback-32",
    ]


def test_active_signals_empty_state_does_not_promote_watchlists(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(db_path, signal_id="sig-watch", symbol="BTCUSDT", alert_type="WATCHLIST", entry_low="100", entry_high="102")

    response = _service(tmp_path, db_path).public_response_for("/signals")

    assert "No active confirmed signals right now." in response.text
    assert "The engine is waiting for clean structure." in response.text
    assert "BTCUSDT" not in response.text


def test_watchlists_dedupe_same_symbol_bias_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(
        db_path,
        signal_id="sig-watch-old",
        symbol="BTCUSDT",
        direction="long",
        entry_low="100",
        entry_high="102",
        sent_at="2026-06-04T11:55:00Z",
    )
    _insert_attempt(
        db_path,
        signal_id="sig-watch-new",
        symbol="BTCUSDT",
        direction="long",
        entry_low="100.10",
        entry_high="102.10",
        sent_at="2026-06-04T12:00:00Z",
    )

    response = _service(tmp_path, db_path).public_response_for("/watchlists")

    assert response.text.count("BTCUSDT") == 1
    assert "👀 WATCH" in response.text


def test_expired_lifecycle_watchlist_is_hidden_from_public_active_output(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(
        db_path,
        signal_id="sig-expired-watch",
        symbol="BTCUSDT",
        direction="long",
        entry_low="100",
        entry_high="102",
        setup_quality_score="A+",
    )
    _insert_lifecycle_record(
        db_path,
        lifecycle_id="sig-expired-watch",
        symbol="BTCUSDT",
        current_state="EXPIRED",
        invalidation_reason="no price reaction or lifecycle progress",
    )

    watch_response = _service(tmp_path, db_path).public_response_for("/watchlists")
    signal_response = _service(tmp_path, db_path).public_response_for("/signals")

    assert "BTCUSDT" not in watch_response.text
    assert "BTCUSDT" not in signal_response.text


def test_watchlists_show_a_grade_waiting_candidate_from_lifecycle_fallback(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_lifecycle_record(
        db_path,
        lifecycle_id="life-a-watch",
        symbol="ETHUSDT",
        current_state="A_GRADE_WATCH",
        direction="long",
        invalidation_reason="Invalid if price accepts below 95.",
        quality_score=92,
    )
    _insert_candidate(
        db_path,
        run_id="run-a-watch",
        symbol="ETHUSDT",
        direction="long",
        entry="100-102",
        stop="95",
        tp1="110",
        tp2="115",
        tp3="120",
        invalidation="Invalid if price accepts below 95.",
        quality_grade="A+",
    )

    watch_response = _service(tmp_path, db_path).public_response_for("/watchlists")
    signal_response = _service(tmp_path, db_path).public_response_for("/signals")

    assert "ETHUSDT" in watch_response.text
    assert "Status: A-GRADE WATCH — Waiting Limit Zone" in watch_response.text
    assert "Direction: Long" in watch_response.text
    assert "Entry Zone: 100 – 102" in watch_response.text
    assert "Stop: 95" in watch_response.text
    assert "Targets: 110, 115, 120" in watch_response.text
    assert "RR: 3" in watch_response.text
    assert "Quality: A+" in watch_response.text
    assert "Invalidation: Invalid if price accepts below 95." in watch_response.text
    assert "ETHUSDT" not in signal_response.text


def test_active_signals_show_runtime_progress_and_exclude_terminal_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(
        db_path,
        signal_id="sig-active",
        symbol="ENAUSDT",
        alert_type="SIGNAL_CONFIRMED",
        rr_planned="3",
        entry_low="0.09402",
        entry_high="0.09497",
        stop_loss="0.0923",
        tp1="0.1",
        tp2="0.105",
        tp3="0.11",
    )
    _insert_attempt(db_path, signal_id="sig-active", symbol="ENAUSDT", alert_type="LIMIT_HIT")
    _insert_attempt(db_path, signal_id="sig-active", symbol="ENAUSDT", alert_type="TP1_HIT", sent_at="2026-06-04T12:15:00Z")
    _insert_attempt(
        db_path,
        signal_id="sig-open-tp3",
        symbol="TP3USDT",
        alert_type="SIGNAL_CONFIRMED",
        rr_planned="3",
        entry_low="20",
        entry_high="21",
        stop_loss="19",
        tp1="23",
        tp2="25",
        tp3="27",
        sent_at="2026-06-04T12:20:00Z",
    )
    _insert_attempt(
        db_path,
        signal_id="sig-open-tp3",
        symbol="TP3USDT",
        alert_type="TP3_HIT",
        lifecycle_state="TP3_HIT",
        new_state="TP3_HIT",
        sent_at="2026-06-04T12:30:00Z",
    )
    _insert_lifecycle_record(db_path, lifecycle_id="sig-open-tp3", symbol="TP3USDT", current_state="TP3_HIT")
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

    assert "Active signals: 2" in response.text
    assert _button_labels(response.reply_markup) == ["TP3USDT", "ENAUSDT"]
    detail = _service(tmp_path, db_path).public_response_for("/signal ENAUSDT")
    assert "Status: TP1 HIT" in detail.text
    assert "Updated: 2026-06-04T12:15:00Z" in detail.text
    tp3_detail = _service(tmp_path, db_path).public_response_for("/signal TP3USDT")
    assert "Status: TP3 HIT" in tp3_detail.text
    assert "Updated: 2026-06-04T12:30:00Z" in tp3_detail.text
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
        rr_planned="3",
        entry_low="150",
        entry_high="151",
        stop_loss="148",
        tp1="155",
        tp2="160",
        tp3="165",
    )

    response = TelegramAdminCommandService(project_root=tmp_path).public_response_for("/signals")

    assert _button_labels(response.reply_markup) == ["SOLUSDT"]
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
    assert "TRIGGERUSDT" not in response.text
    assert "None right now." in response.text.split("🔥 STALKING", 1)[1].split("👀 WATCH", 1)[0]


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
        rr_planned="3",
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        tp1="110",
        tp2="115",
        tp3="120",
    )

    response = _service(tmp_path, db_path).public_response_for("/signals")

    assert _button_labels(response.reply_markup) == ["CONFIRMUSDT"]


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
