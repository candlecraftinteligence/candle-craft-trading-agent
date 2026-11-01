"""Three-process synthetic release upgrade/rollback. Never accepts a live DB path."""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from decimal import Decimal


def main():
    source, state, phase = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
    assert ".pytest_tmp" in state.parts or "pytest-of-" in str(state)
    sys.path.insert(0, str(source))
    import httpx
    def deny(*a, **k):
        raise AssertionError("Real HTTP forbidden in release roundtrip")
    httpx.HTTPTransport.handle_request = deny
    httpx.AsyncHTTPTransport.handle_async_request = deny
    from app.storage.database import connect_database
    from app.storage.repositories import store_scan_result
    from app.storage.scan_payloads import load_logical_scan_payload
    from app.pipeline.scanner_runner import ScannerSymbolResult
    from app.watch_mode import _snapshot_from_payload
    from app.telegram_admin.wolf_briefing import load_latest_db_scan_artifacts
    from app.telegram_admin import TelegramAdminCommandService, TelegramAdminConfig, process_telegram_admin_commands
    from app.alerts.telegram_outbox import SQLitePublicTelegramOutbox, SENT, RETRYABLE, UNCERTAIN
    from app.alerts.telegram_outbox_protocol import TelegramOutboxWorker, deliver_single_reserved_part
    from tests.runtime_epoch_support import bootstrap_operational_test_database, register_synthetic_scan_run, SYNTHETIC_IDENTITY
    from tests.test_integrated_signal_product import _scan
    from tests.test_p0b3b1_telegram_outbox_protocol import _seed_canonical_event, FakeTelegramTransport, NOW
    from tests.test_telegram_admin_commands import FakeCommandTransport
    state.mkdir(exist_ok=True)
    db = state / "SYNTHETIC-release-roundtrip.db"
    ledger = state / "expected.json"
    offset = state / "listener-state.json"

    def rows(table, where="", args=()):
        with sqlite3.connect(db) as c:
            return [list(row) for row in c.execute(f'SELECT * FROM "{table}" {where}', args)]
    def all_rows():
        with sqlite3.connect(db) as c:
            tables = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return {name: rows(name) for name in tables}
    def write_scans(label, unverified=False):
        result, _ = _scan(Decimal(".750"), "long", scenario="before_contact", room=True)
        assert result.trade_ideas_created == 0
        if unverified:
            from app.microstructure.models import MicrostructureFlowSnapshot
            from app.microstructure.order_book_models import OrderBookLiquiditySnapshot
            from app.microstructure.liquidation_models import LiquidationFlowSnapshot
            common = dict(symbol="BTCUSDT", status="UNVERIFIED", reason="SYNTHETIC:identity_not_verified")
            item = result.results[0].model_copy(update={
                "microstructure_flow": MicrostructureFlowSnapshot(**common, source="SYNTHETIC", orderflow_summary="Unverified"),
                "order_book_liquidity": OrderBookLiquiditySnapshot(**common),
                "liquidation_flow": LiquidationFlowSnapshot(**common, liquidation_summary="Unverified")})
            result = result.model_copy(update={"results": (item,)})
        for inline in (True, False):
            run_id = f"SYNTHETIC-{label}-{'inline' if inline else 'compact'}"
            register_synthetic_scan_run(db, run_id)
            store_scan_result(db, result, run_id=run_id, inline_raw_payload=inline, expected_identity=SYNTHETIC_IDENTITY)

    if phase == "baseline":
        assert not db.exists()
        bootstrap_operational_test_database(db)
        write_scans("baseline")
        ids = {}
        for label in ("sent", "pending", "uncertain", "partial"):
            # Earlier plans remain in history; only the latest plan is current.
            with connect_database(db) as c:
                c.execute("UPDATE setup_lifecycle_records SET is_current=0")
                c.commit()
            event, attempt = _seed_canonical_event(db, event_key=f"SYNTHETIC-{label}|initial_watchlist",
                signal_id=f"SYNTHETIC-{label}", message="AAAAA BBBBB CCCCC", max_message_length=5 if label == "partial" else 4096)
            with connect_database(db) as c:
                c.execute("UPDATE public_alert_events SET canonical_plan_id=? WHERE id=?", (f"SYNTHETIC-{label}", event))
                c.execute("UPDATE telegram_alert_attempts SET public_watchlist_plan_id=? WHERE id=?", (f"SYNTHETIC-{label}", attempt))
                c.commit()
                if label != "pending":
                    outbox = SQLitePublicTelegramOutbox(c)
                    claim = outbox.claim(event_id=event, reservation_id=attempt, now=NOW, attempt_id=f"seed-{label}").claim
                    assert claim is not None
                    part = claim.parts[0]
                    assert outbox.mark_part_in_flight(part_id=part.id, attempt_id=claim.attempt_id)
                    outcome = {"delivery_state": UNCERTAIN, "error_category": "synthetic_unknown"} if label == "uncertain" else {
                        "delivery_state": SENT, "message_id": 901 if label == "sent" else 902, "chat_id": "synthetic-chat", "http_status": 200, "sent_at": NOW}
                    outbox.record_part_result(event_id=event, reservation_id=attempt, part_id=part.id,
                        attempt_id=claim.attempt_id, now=NOW, result=outcome)
                    if label == "partial":
                        part = claim.parts[1]
                        assert outbox.mark_part_in_flight(part_id=part.id, attempt_id=claim.attempt_id)
                        assert outbox.record_part_result(event_id=event, reservation_id=attempt, part_id=part.id,
                            attempt_id=claim.attempt_id, now=NOW, result={"delivery_state": RETRYABLE,
                                "http_status": 429, "retry_after": 1, "error_category": "synthetic_rate_limit"}) == RETRYABLE
            ids[label] = [event, attempt]
        offset.write_text(json.dumps({"latest_processed_update_id": 700, "synthetic": True}))
        expected = {"ids": ids, "epoch": rows("runtime_epoch_control"),
                    "lifecycle": rows("setup_lifecycle_records"), "before": all_rows()}
    else:
        expected = json.loads(ledger.read_text())
        assert all_rows() == expected["before"], "State changed between release processes"
        assert rows("runtime_epoch_control") == expected["epoch"]
        assert rows("setup_lifecycle_records") == expected["lifecycle"]
        if phase == "repair":
            write_scans("repair", unverified=True)

    before_read = all_rows()
    with connect_database(db) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == 27
        formats = {r[0] for r in c.execute("SELECT raw_payload_format FROM scan_runs")}
        assert formats == {"inline_v1", "symbol_refs_v1"}
        scan_ids = [r[0] for r in c.execute("SELECT run_id FROM scan_runs")]
        for run_id in scan_ids:
            payload = load_logical_scan_payload(c, run_id)
            item = ScannerSymbolResult.model_validate(payload["results"][0])
            _snapshot_from_payload(payload["results"][0])
            if "repair" in run_id:
                from app.context.models import ContextValue
                unreliable = ContextValue(value=123, source="SYNTHETIC", status="UNVERIFIED")
                assert not unreliable.usable_for_research and unreliable.research_payload() is None
                assert all(not s.verified and s.status.value == "UNVERIFIED" for s in (
                    item.microstructure_flow, item.order_book_liquidity, item.liquidation_flow))
                assert item.order_book_liquidity.liquidity_below_context() is None
    artifact = load_latest_db_scan_artifacts(project_root=state, database_path=db)
    assert artifact.scan_payload is not None
    service = TelegramAdminCommandService(project_root=state, database_path=db)
    transport = FakeCommandTransport(updates=({"update_id":700},))
    config = TelegramAdminConfig(admin_enabled=True, commands_enabled=True, dry_run=False,
                                bot_token="SYNTHETIC-NOT-A-KEY", admin_chat_id="synthetic-chat")
    async def listener_read():
        result = await process_telegram_admin_commands(config=config, command_service=service,
            transport=transport, state_path=offset, audit_path=state/"commands.jsonl")
        assert result.processed_count == result.sent_count == 0
        assert transport.get_calls[0]["offset"] == 701 and not transport.send_calls
    asyncio.run(listener_read())
    assert json.loads(offset.read_text())["latest_processed_update_id"] == 700
    after_read = all_rows()
    assert after_read == before_read, "Scanner/listener reads mutated populated state"

    async def prove_delivery():
        worker = TelegramOutboxWorker()
        transport = FakeTelegramTransport(mode="success")
        try:
            for label in ("sent", "uncertain"):
                event, attempt = expected["ids"][label]
                result = await deliver_single_reserved_part(worker=worker, database_path=db,
                    event_id=event, reservation_id=attempt, expected_identity=SYNTHETIC_IDENTITY,
                    now="2026-07-16T11:00:00Z", transport=transport)
                expected_error = "event_already_sent" if label == "sent" else "event_delivery_uncertain"
                assert result.status == "blocked" and result.error_message == expected_error and not transport.calls, (label, result, transport.calls)
            if phase == "rollback":
                original_receipts = rows("public_alert_delivery_parts", "WHERE delivery_state='SENT'")
                for label, parts in (("pending", (1,)), ("partial", (2, 3))):
                    event, attempt = expected["ids"][label]
                    continuation = None
                    for index in parts:
                        result = await deliver_single_reserved_part(worker=worker, database_path=db,
                            event_id=event, reservation_id=attempt, expected_identity=SYNTHETIC_IDENTITY,
                            now="2026-07-16T11:00:00Z", part_index=index, attempt_id=continuation, transport=transport)
                        expected_state = "sent" if index == parts[-1] else "in_flight"
                        assert result.status == expected_state and result.transport_outcome.delivery_state == SENT, result
                        assert result.durable_receipt.transaction_result == "COMMITTED"
                        continuation = result.reservation_receipt.attempt_id
                    again = await deliver_single_reserved_part(worker=worker, database_path=db,
                        event_id=event, reservation_id=attempt, expected_identity=SYNTHETIC_IDENTITY,
                        now="2026-07-16T11:01:00Z", transport=transport)
                    assert again.status == "blocked" and again.error_message == "event_already_sent"
                assert len(transport.calls) == 3
                assert len({call.part_id for call in transport.calls}) == 3
                for receipt in original_receipts:
                    assert receipt in rows("public_alert_delivery_parts", "WHERE delivery_state='SENT'")
        finally:
            await worker.aclose()
    asyncio.run(prove_delivery())
    expected["before"] = all_rows()
    ledger.write_text(json.dumps(expected))
    proof = {"phase": phase, "schema": 27, "epoch_preserved": True,
                      "mixed_payload_reads": len(scan_ids), "command_offset": 700,
                      "state_before_reads_sha256": hashlib.sha256(json.dumps(before_read,sort_keys=True).encode()).hexdigest(),
                      "state_after_reads_sha256": hashlib.sha256(json.dumps(after_read,sort_keys=True).encode()).hexdigest(),
                      "real_http_calls": 0, "database": db.name}
    (state/f"{phase}-proof.json").write_text(json.dumps(proof,indent=2))
    print(json.dumps(proof))


if __name__ == "__main__":
    main()
