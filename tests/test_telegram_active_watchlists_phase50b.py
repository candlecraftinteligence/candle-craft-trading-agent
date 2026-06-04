from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from app.data.dtos import NA
from app.storage.database import open_initialized_database
from app.telegram_admin import TelegramAdminCommandService, TelegramAdminConfig, process_telegram_admin_commands
from app.telegram_admin.commands import SCREEN_FOOTER, SCREEN_HEADER
from tests.test_telegram_admin_commands import FakeCommandTransport, _screen_send_calls, _update


def _insert_attempt(
    db_path: Path,
    *,
    signal_id: str,
    symbol: str,
    direction: str = "long",
    alert_type: str = "WATCHLIST",
    status: str = "sent",
    sent_at: str = "2026-06-04T12:00:00Z",
    scan_run_id: str = "run-active",
    price_level: str = NA,
    entry_low: str = NA,
    entry_high: str = NA,
    stop_loss: str = NA,
    tp1: str = NA,
    tp2: str = NA,
    tp3: str = NA,
) -> None:
    connection = open_initialized_database(db_path)
    try:
        connection.execute(
            """
            INSERT INTO telegram_alert_attempts (
                signal_id, symbol, direction, new_state, alert_type, lifecycle_state,
                sent_at, telegram_status, message_hash, scan_run_id, price_level,
                entry_low, entry_high, stop_loss, tp1, tp2, tp3
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_id,
                symbol,
                direction,
                "WATCHLISTED",
                alert_type,
                "WATCHLISTED",
                sent_at,
                status,
                f"hash-{signal_id}-{alert_type}",
                scan_run_id,
                price_level,
                entry_low,
                entry_high,
                stop_loss,
                tp1,
                tp2,
                tp3,
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

    assert response.text.startswith(f"{SCREEN_HEADER} ACTIVE WATCHLISTS")
    assert response.text.endswith(SCREEN_FOOTER)
    assert "ENAUSDT | LONG" in response.text
    assert "Limit Zone: 0.09402 – 0.09497" in response.text
    assert "BTCUSDT" not in response.text
    assert "scan_runs" not in response.text


def test_active_watchlists_empty_state_when_no_scan_database_exists(tmp_path: Path) -> None:
    response = TelegramAdminCommandService(project_root=tmp_path).public_response_for("/watchlists")

    assert "No local watchlist data found yet. Start the scanner first." in response.text
    assert "System:\nManual tracking only. No order execution." in response.text
    assert "scan_runs" not in response.text


def test_active_watchlists_derive_statuses_and_exclude_terminal_or_non_public_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _insert_attempt(
        db_path,
        signal_id="sig-wait",
        symbol="BTCUSDT",
        entry_low="104250.0000",
        entry_high="104800.0000",
        stop_loss="103700.0000",
    )
    _insert_attempt(
        db_path,
        signal_id="sig-limit",
        symbol="ENAUSDT",
        entry_low="0.09402000",
        entry_high="0.09497000",
        stop_loss="0.09230000",
        tp1="0.10000000",
        tp2="0.10500000",
        tp3="0.11000000",
    )
    _insert_attempt(db_path, signal_id="sig-limit", symbol="ENAUSDT", alert_type="LIMIT_HIT", price_level="0.09402-0.09497")
    _insert_attempt(
        db_path,
        signal_id="sig-tp1",
        symbol="SOLUSDT",
        entry_low="150",
        entry_high="151",
        stop_loss="148",
        tp1="153",
        tp2="156",
        tp3="160",
    )
    _insert_attempt(db_path, signal_id="sig-tp1", symbol="SOLUSDT", alert_type="LIMIT_HIT")
    _insert_attempt(db_path, signal_id="sig-tp1", symbol="SOLUSDT", alert_type="TP1_HIT", price_level="153")
    _insert_attempt(
        db_path,
        signal_id="sig-tp2",
        symbol="XRPUSDT",
        entry_low="0.6000",
        entry_high="0.6100",
        stop_loss="0.5800",
        tp1="0.6300",
        tp2="0.6600",
        tp3="0.7000",
    )
    _insert_attempt(db_path, signal_id="sig-tp2", symbol="XRPUSDT", alert_type="LIMIT_HIT")
    _insert_attempt(db_path, signal_id="sig-tp2", symbol="XRPUSDT", alert_type="TP1_HIT", price_level="0.63")
    _insert_attempt(db_path, signal_id="sig-tp2", symbol="XRPUSDT", alert_type="TP2_HIT", price_level="0.66")
    _insert_attempt(db_path, signal_id="sig-closed-tp3", symbol="ADAUSDT", entry_low="1", entry_high="1.01")
    _insert_attempt(db_path, signal_id="sig-closed-tp3", symbol="ADAUSDT", alert_type="TP3_HIT")
    _insert_attempt(db_path, signal_id="sig-closed-sl", symbol="DOGEUSDT", entry_low="0.2", entry_high="0.21")
    _insert_attempt(db_path, signal_id="sig-closed-sl", symbol="DOGEUSDT", alert_type="SL_HIT")
    _insert_attempt(db_path, signal_id="sig-blocked", symbol="BLOCKUSDT", status="blocked", entry_low="1", entry_high="2")
    _insert_attempt(db_path, signal_id="sig-skipped", symbol="SKIPUSDT", status="skipped", entry_low="1", entry_high="2")

    response = _service(tmp_path, db_path).public_response_for("/watchlists")

    assert "BTCUSDT | LONG" in response.text
    assert "Limit Zone: 104250 – 104800" in response.text
    assert "SL: 103700" in response.text
    assert "Status: Waiting for Limit Zone" in response.text
    assert "ENAUSDT | LONG" in response.text
    assert "Limit Zone: 0.09402 – 0.09497" in response.text
    assert "SL: 0.0923" in response.text
    assert "Status: LIMIT ZONE HIT" in response.text
    assert "TP1: waiting (0.1)" in response.text
    assert "SOLUSDT | LONG" in response.text
    assert "Status: TP1 HIT" in response.text
    assert "TP1: HIT (153)" in response.text
    assert "XRPUSDT | LONG" in response.text
    assert "Status: TP2 HIT" in response.text
    assert "TP2: HIT (0.66)" in response.text
    assert "ADAUSDT" not in response.text
    assert "DOGEUSDT" not in response.text
    assert "BLOCKUSDT" not in response.text
    assert "SKIPUSDT" not in response.text
    assert "Decimal(" not in response.text
    assert "{" not in response.text
    assert "}" not in response.text
    assert "order was placed" not in response.text.lower()
    assert "automatic execution" not in response.text.lower()


def test_active_watchlists_fall_back_to_setup_candidates_and_na_missing_levels(tmp_path: Path) -> None:
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

    assert "LINKUSDT | SHORT" in response.text
    assert "Limit Zone: 18.2 – 18.4" in response.text
    assert "SL: 18.9" in response.text
    assert "Status: Waiting for Limit Zone" in response.text
    assert "TP3" not in response.text
    assert "raw_candidate_json" not in response.text


def test_active_watchlists_limits_public_output_to_newest_ten(tmp_path: Path) -> None:
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

    assert "Showing 10 of 11 active watchlists." in response.text
    assert "SYM10USDT" in response.text
    assert "SYM00USDT" not in response.text


def test_public_watchlist_commands_and_buttons_route_to_active_watchlists(tmp_path: Path) -> None:
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

    messages = [call["message"] for call in _screen_send_calls(transport)]
    assert result.delivery_status == "sent_public"
    assert len(messages) == len(updates)
    assert all(message.startswith(f"{SCREEN_HEADER} ACTIVE WATCHLISTS") for message in messages)
    assert all("BTCUSDT | LONG" in message for message in messages)
    assert "secret-token" not in "\n".join(messages)
