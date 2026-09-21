"""Fifth bounded repair proofs R123–R140 for PROSPECTIVE_RUNTIME_EPOCH_ISOLATION.

DEV-only temp databases, mocked senders, and deterministic clocks.
No Runtime filesystem, live Telegram, exchange calls, or scanner watch loops.

These tests encode the architecture-review reproductions from HEAD
64db64349760c4b8906b204821c10cf440aaed1b:
ACTIVE nested-contradiction provenance, same-run foreign candidate,
unregistered scan_run_id replacement, and post-claim send/recovery
revalidation.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from app.alerts.telegram_lifecycle import (
    PublicWatchlistReservationResult,
    SQLiteTelegramAlertAttemptRepository,
    TelegramAlertType,
    _deliver_committed_public_alert_intent,
)
from app.alerts.telegram_outbox import (
    IN_FLIGHT,
    PENDING,
    SENT,
    UNCERTAIN,
    SQLitePublicTelegramOutbox,
)
from app.alerts.telegram_routing import TelegramMessageType
from app.runtime_epoch.ownership import public_reservation_record_mismatch_reason
from test_telegram_lifecycle_delivery_phase42 import FakeSender
from tests.runtime_epoch_support import SYNTHETIC_IDENTITY
from tests.test_prospective_runtime_epoch_isolation import NOW
from tests.test_prospective_runtime_epoch_isolation_repair2 import (
    _owned_event_reservation,
    _row,
)
from tests.test_prospective_runtime_epoch_isolation_repair4 import (
    FOREIGN_FACT,
    FOREIGN_GATE,
    FOREIGN_INVALIDATION,
    FOREIGN_QUALITY,
    FOREIGN_RATIONALE,
    OWNED_FACT,
    OWNED_INVALIDATION,
    OWNED_RATIONALE,
    _bootstrap,
    _insert_candidate,
    _insert_scan_run,
    _insert_symbol_result,
    _joined_detail_text,
    _load_detail,
    _operational_fields,
    _owned_raw_result,
    _register_current_run,
    _replacement_base,
    _seed_positive_active_detail,
    _snapshot_attempt,
)

pytestmark = pytest.mark.no_auto_epoch

UNREGISTERED_FOREIGN_RUN = "UNREGISTERED-FOREIGN-RUN"
FOREIGN_PLAN_VERSION = "plan-version-FOREIGN-B"
OWNED_PLAN_VERSION = "plan-version-OWNED-A"
OWNED_SETUP_ID = "setup-owned-exact"


def _stamp_lifecycle_identities(
    db_path: Path,
    lifecycle_id: str,
    *,
    setup_id: str | None = None,
    plan_version_id: str | None = None,
) -> None:
    assignments: list[str] = []
    values: list[object] = []
    if setup_id is not None:
        assignments.append("setup_id = ?")
        values.append(setup_id)
    if plan_version_id is not None:
        assignments.append("plan_version_id = ?")
        values.append(plan_version_id)
    if not assignments:
        return
    values.append(lifecycle_id)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            f"UPDATE setup_lifecycle_records SET {', '.join(assignments)} WHERE lifecycle_id = ?",
            values,
        )
        connection.commit()


def _insert_candidate_custom(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    symbol: str,
    direction: str,
    mode: str,
    entry: str,
    stop: str,
    tp1: str,
    tp2: str,
    tp3: str,
    rr: str,
    raw_candidate: dict[str, object],
    quality: str = "A",
) -> None:
    connection.execute(
        """
        INSERT INTO setup_candidates (
            run_id, symbol, mode, direction, entry, stop, tp1, tp2, tp3, rr,
            invalidation, quality_grade, trust_meter, risk_warning, raw_candidate_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'N/A', 'N/A', ?)
        """,
        (
            run_id,
            symbol,
            mode,
            direction,
            entry,
            stop,
            tp1,
            tp2,
            tp3,
            rr,
            str(raw_candidate.get("invalidation", "N/A")),
            quality,
            json.dumps(raw_candidate),
        ),
    )


def _part_state(connection: sqlite3.Connection, part_id: int) -> str:
    row = connection.execute(
        "SELECT delivery_state FROM public_alert_delivery_parts WHERE id = ?",
        (part_id,),
    ).fetchone()
    assert row is not None
    return str(row[0])


def _event_state(connection: sqlite3.Connection, event_id: int) -> dict[str, object]:
    row = connection.execute(
        """
        SELECT delivery_state, attempt_id, canonical_reservation_attempt_id, lease_expires_at
        FROM public_alert_events WHERE id = ?
        """,
        (event_id,),
    ).fetchone()
    assert row is not None
    return {
        "delivery_state": row[0],
        "attempt_id": row[1],
        "canonical_reservation_attempt_id": row[2],
        "lease_expires_at": row[3],
    }


def _claim_owned(db_path: Path, event_id: int, reservation_id: int, *, attempt_id: str = "claim-1"):
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        outbox = SQLitePublicTelegramOutbox(repository._connection)
        result = outbox.claim(
            event_id=event_id,
            reservation_id=reservation_id,
            now=NOW,
            attempt_id=attempt_id,
            lease_seconds=60,
        )
        return result, int(repository._connection.execute(
            "SELECT id FROM public_alert_delivery_parts WHERE public_alert_event_id = ? ORDER BY id LIMIT 1",
            (event_id,),
        ).fetchone()[0])


def _mismatch_after_corrupt(db_path: Path, event_id: int, reservation_id: int) -> str | None:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        event = connection.execute(
            "SELECT * FROM public_alert_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        reservation = connection.execute(
            "SELECT * FROM telegram_alert_attempts WHERE id = ?",
            (reservation_id,),
        ).fetchone()
        return public_reservation_record_mismatch_reason(connection, reservation, event)


def _reservation_payload(event_id: int, reservation_id: int, event_key: str) -> PublicWatchlistReservationResult:
    return PublicWatchlistReservationResult(
        granted=True,
        event_key=event_key,
        reservation_id=reservation_id,
        event_id=event_id,
        status="reserved",
    )


def test_r123_copied_lifecycle_nested_short_raw_cannot_enrich_active(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r123.db")
    _seed_positive_active_detail(db_path, label="r123")
    baseline = _load_detail(tmp_path, db_path)
    assert baseline is not None
    before = _operational_fields(baseline)
    with sqlite3.connect(db_path) as connection:
        _insert_scan_run(connection, run_id="r123-nested-short", symbol="BTCUSDT", now=NOW)
        _insert_symbol_result(
            connection,
            run_id="r123-nested-short",
            symbol="BTCUSDT",
            quality="A",
            raw_result={
                "symbol": "BTCUSDT",
                "direction": "long",
                "lifecycle_id": "life-r123",
                "setup_id": "setup-foreign",
                "plan_version_id": FOREIGN_PLAN_VERSION,
                "reason_for_trade": FOREIGN_RATIONALE,
                "invalidation": FOREIGN_INVALIDATION,
                "setup_quality": {"quality_grade": "A", "decision_reason": FOREIGN_GATE},
                "trade_idea": {
                    "direction": "short",
                    "grade": "A",
                    "entry": "900",
                    "entry_high": "901",
                    "stop_loss": "999",
                    "tp1": "800",
                    "tp2": "700",
                    "tp3": "600",
                    "reason_for_trade": FOREIGN_RATIONALE,
                    "invalidation": FOREIGN_INVALIDATION,
                    "confirmed_facts": [FOREIGN_FACT],
                },
            },
        )
        connection.commit()
    after = _load_detail(tmp_path, db_path)
    assert after is not None
    joined = _joined_detail_text(after)
    assert str(after.bias).upper() == "LONG"
    assert FOREIGN_RATIONALE not in joined
    assert FOREIGN_INVALIDATION not in joined
    assert FOREIGN_FACT not in joined
    assert FOREIGN_GATE not in joined
    assert FOREIGN_QUALITY not in joined
    assert _operational_fields(after) == before


def test_r124_foreign_plan_version_raw_enrichment_omitted(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r124.db")
    _seed_positive_active_detail(db_path, label="r124")
    _stamp_lifecycle_identities(db_path, "life-r124", plan_version_id=OWNED_PLAN_VERSION)
    baseline = _load_detail(tmp_path, db_path)
    assert baseline is not None
    with sqlite3.connect(db_path) as connection:
        _insert_scan_run(connection, run_id="r124-plan-b", symbol="BTCUSDT", now=NOW)
        _insert_symbol_result(
            connection,
            run_id="r124-plan-b",
            symbol="BTCUSDT",
            raw_result={
                **_owned_raw_result(lifecycle_id="life-r124"),
                "plan_version_id": FOREIGN_PLAN_VERSION,
                "reason_for_trade": FOREIGN_RATIONALE,
                "invalidation": FOREIGN_INVALIDATION,
                "setup_quality": {"quality_grade": "B", "decision_reason": FOREIGN_GATE},
                "trade_idea": {
                    "direction": "long",
                    "grade": "B",
                    "reason_for_trade": FOREIGN_RATIONALE,
                    "invalidation": FOREIGN_INVALIDATION,
                    "confirmed_facts": [FOREIGN_FACT],
                },
            },
        )
        connection.commit()
    after = _load_detail(tmp_path, db_path)
    assert after is not None
    joined = _joined_detail_text(after)
    assert FOREIGN_RATIONALE not in joined
    assert FOREIGN_GATE not in joined
    assert FOREIGN_FACT not in joined


def test_r125_contradictory_nested_raw_economics_omitted(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r125.db")
    _seed_positive_active_detail(db_path, label="r125")
    baseline = _load_detail(tmp_path, db_path)
    assert baseline is not None
    with sqlite3.connect(db_path) as connection:
        _insert_scan_run(connection, run_id="r125-econ", symbol="BTCUSDT", now=NOW)
        _insert_symbol_result(
            connection,
            run_id="r125-econ",
            symbol="BTCUSDT",
            raw_result={
                "direction": "long",
                "lifecycle_id": "life-r125",
                "reason_for_trade": FOREIGN_RATIONALE,
                "invalidation": FOREIGN_INVALIDATION,
                "entry": "900",
                "entry_high": "901",
                "stop_loss": "999",
                "tp1": "800",
                "tp2": "700",
                "tp3": "600",
                "setup_quality": {"quality_grade": "A", "decision_reason": FOREIGN_GATE},
                "trade_idea": {
                    "direction": "long",
                    "entry": "900",
                    "entry_high": "901",
                    "stop_loss": "999",
                    "tp1": "800",
                    "tp2": "700",
                    "tp3": "600",
                    "reason_for_trade": FOREIGN_RATIONALE,
                    "invalidation": FOREIGN_INVALIDATION,
                    "confirmed_facts": [FOREIGN_FACT],
                },
            },
        )
        connection.commit()
    after = _load_detail(tmp_path, db_path)
    assert after is not None
    joined = _joined_detail_text(after)
    assert FOREIGN_RATIONALE not in joined
    assert FOREIGN_INVALIDATION not in joined
    assert FOREIGN_FACT not in joined
    assert "900" not in str(after.entry_low)
    assert "999" not in str(after.stop_loss)


def test_r126_same_run_foreign_mode_plan_economics_candidate_omitted(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r126.db")
    _reservation_id, _event_key, run_id = _seed_positive_active_detail(db_path, label="r126")
    baseline = _load_detail(tmp_path, db_path)
    assert baseline is not None
    before = _operational_fields(baseline)
    with sqlite3.connect(db_path) as connection:
        _insert_candidate_custom(
            connection,
            run_id=run_id,
            symbol="BTCUSDT",
            direction="long",
            mode="scalp",
            entry="900-901",
            stop="999",
            tp1="800",
            tp2="700",
            tp3="600",
            rr="9.9",
            raw_candidate={
                "plan_version_id": FOREIGN_PLAN_VERSION,
                "reason_for_trade": FOREIGN_GATE,
                "invalidation": FOREIGN_INVALIDATION,
                "quality_grade": FOREIGN_QUALITY,
                "confirmed_facts": [FOREIGN_FACT],
            },
        )
        connection.commit()
    after = _load_detail(tmp_path, db_path)
    assert after is not None
    joined = _joined_detail_text(after)
    assert FOREIGN_FACT not in joined
    assert FOREIGN_QUALITY not in joined
    assert FOREIGN_GATE not in joined
    assert FOREIGN_INVALIDATION not in joined
    assert _operational_fields(after) == before


def test_r127_unregistered_foreign_scan_run_id_replacement_rejected(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r127.db")
    event_id, reservation_id, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r127", event_key="r127:owned", signal_id="r127-owned", run_id="r127"
    )
    replacement = _replacement_base(
        "r127-owned",
        "r127:owned",
        "life-r127",
        entry_low="100.00",
        entry_high="102.0",
        stop_loss="95.00",
        attempted_alert_type="WATCHLIST",
        scan_run_id=UNREGISTERED_FOREIGN_RUN,
    )
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        before = _snapshot_attempt(repository._connection, reservation_id)
        event_before = _row(repository._connection, "public_alert_events", event_id)
        accepted = repository.replace_attempt_with_reservation(attempt_id=reservation_id, record=replacement)
        after = _snapshot_attempt(repository._connection, reservation_id)
        event_after = _row(repository._connection, "public_alert_events", event_id)
    assert accepted is False
    assert after == before
    assert after["scan_run_id"] != UNREGISTERED_FOREIGN_RUN
    assert event_after == event_before


def test_r128_registered_unrelated_run_cannot_authorize_foreign_enrichment(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r128.db")
    _seed_positive_active_detail(db_path, label="r128")
    baseline = _load_detail(tmp_path, db_path)
    assert baseline is not None
    _register_current_run(db_path, "r128-unrelated")
    with sqlite3.connect(db_path) as connection:
        _insert_scan_run(connection, run_id="r128-unrelated", symbol="BTCUSDT", now=NOW)
        connection.execute(
            "UPDATE telegram_alert_attempts SET scan_run_id = ? WHERE public_watchlist_event_key = ?",
            ("r128-unrelated", "r128:owned"),
        )
        _insert_symbol_result(
            connection,
            run_id="r128-unrelated",
            symbol="BTCUSDT",
            raw_result={
                "direction": "long",
                "reason_for_trade": FOREIGN_RATIONALE,
                "invalidation": FOREIGN_INVALIDATION,
                "confirmed_facts": [FOREIGN_FACT],
                "setup_quality": {"quality_grade": FOREIGN_QUALITY, "decision_reason": FOREIGN_GATE},
            },
        )
        connection.commit()
    after = _load_detail(tmp_path, db_path)
    assert after is not None
    joined = _joined_detail_text(after)
    assert FOREIGN_RATIONALE not in joined
    assert FOREIGN_FACT not in joined
    assert FOREIGN_QUALITY not in joined
    assert FOREIGN_GATE not in joined


def test_r129_legitimate_later_same_setup_raw_may_enrich(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r129.db")
    _seed_positive_active_detail(db_path, label="r129")
    _stamp_lifecycle_identities(
        db_path,
        "life-r129",
        setup_id=OWNED_SETUP_ID,
        plan_version_id=OWNED_PLAN_VERSION,
    )
    later_run = "r129-later"
    _register_current_run(db_path, later_run)
    later_rationale = "OWNED_LATER_RATIONALE_MARKER"
    later_fact = "OWNED_LATER_FACT_MARKER"
    with sqlite3.connect(db_path) as connection:
        _insert_scan_run(connection, run_id=later_run, symbol="BTCUSDT", now=NOW)
        _insert_symbol_result(
            connection,
            run_id=later_run,
            symbol="BTCUSDT",
            raw_result={
                "symbol": "BTCUSDT",
                "direction": "long",
                "lifecycle_id": "life-r129",
                "setup_id": OWNED_SETUP_ID,
                "plan_version_id": OWNED_PLAN_VERSION,
                "mode": "swing",
                "reason_for_trade": later_rationale,
                "invalidation": OWNED_INVALIDATION,
                "entry": "100",
                "entry_high": "102",
                "stop_loss": "95",
                "tp1": "110",
                "tp2": "120",
                "tp3": "130",
                "setup_quality": {"quality_grade": "A", "decision_reason": "OWNED_GATE_MARKER"},
                "trade_idea": {
                    "direction": "long",
                    "grade": "A",
                    "entry": "100",
                    "entry_high": "102",
                    "stop_loss": "95",
                    "tp1": "110",
                    "tp2": "120",
                    "tp3": "130",
                    "reason_for_trade": later_rationale,
                    "invalidation": OWNED_INVALIDATION,
                    "confirmed_facts": [later_fact],
                },
                "confirmed_facts": [later_fact],
            },
        )
        connection.commit()
    detail = _load_detail(tmp_path, db_path)
    assert detail is not None
    joined = _joined_detail_text(detail)
    assert later_rationale in joined
    assert later_fact in joined
    assert str(detail.bias).upper() == "LONG"


def test_r130_legitimate_exact_candidate_enrichment_still_works(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r130.db")
    _seed_positive_active_detail(db_path, label="r130")
    detail = _load_detail(tmp_path, db_path)
    assert detail is not None
    joined = _joined_detail_text(detail)
    assert OWNED_RATIONALE in joined
    assert OWNED_INVALIDATION in joined
    assert OWNED_FACT in joined
    assert str(detail.bias).upper() == "LONG"
    assert detail.quality not in {None, "", "N/A"}


def test_r131_post_claim_corrupt_alert_type_blocks_part_in_flight(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r131.db")
    event_id, reservation_id, part_id = _owned_event_reservation(
        db_path, lifecycle_id="life-r131", event_key="r131:owned", signal_id="r131-owned", run_id="r131"
    )
    claim, part_id = _claim_owned(db_path, event_id, reservation_id, attempt_id="r131-claim")
    assert claim.claim is not None
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "UPDATE telegram_alert_attempts SET alert_type = 'TP1_HIT' WHERE id = ?",
            (reservation_id,),
        )
        connection.commit()
        before_part = _part_state(connection, part_id)
        before_event = _event_state(connection, event_id)
        before_attempt = _snapshot_attempt(connection, reservation_id)
    mismatch = _mismatch_after_corrupt(db_path, event_id, reservation_id)
    assert mismatch is not None
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        mutated = SQLitePublicTelegramOutbox(repository._connection).mark_part_in_flight(
            part_id=part_id,
            attempt_id=claim.claim.attempt_id,
            now=NOW,
        )
        after_part = _part_state(repository._connection, part_id)
        after_event = _event_state(repository._connection, event_id)
        after_attempt = _snapshot_attempt(repository._connection, reservation_id)
    assert mutated is False
    assert before_part == PENDING
    assert after_part == PENDING
    assert after_event["delivery_state"] == before_event["delivery_state"] == IN_FLIGHT
    assert after_event["attempt_id"] == before_event["attempt_id"]
    assert after_attempt["alert_type"] == before_attempt["alert_type"] == "TP1_HIT"
    assert after_attempt["telegram_status"] == before_attempt["telegram_status"]


def test_r132_post_claim_corrupt_economics_blocks_part_in_flight(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r132.db")
    event_id, reservation_id, part_id = _owned_event_reservation(
        db_path, lifecycle_id="life-r132", event_key="r132:owned", signal_id="r132-owned", run_id="r132"
    )
    claim, part_id = _claim_owned(db_path, event_id, reservation_id, attempt_id="r132-claim")
    assert claim.claim is not None
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET entry_low = '900', entry_high = '901', stop_loss = '999'
            WHERE id = ?
            """,
            (reservation_id,),
        )
        connection.commit()
        before_part = _part_state(connection, part_id)
    assert _mismatch_after_corrupt(db_path, event_id, reservation_id) is not None
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        mutated = SQLitePublicTelegramOutbox(repository._connection).mark_part_in_flight(
            part_id=part_id,
            attempt_id=claim.claim.attempt_id,
            now=NOW,
        )
        after_part = _part_state(repository._connection, part_id)
        after_attempt = _snapshot_attempt(repository._connection, reservation_id)
    assert mutated is False
    assert before_part == after_part == PENDING
    assert after_attempt["entry_low"] == "900"
    assert after_attempt["telegram_status"] == "in_flight"


def test_r133_post_claim_corrupt_direction_plan_blocks_part_in_flight(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r133.db")
    event_id, reservation_id, part_id = _owned_event_reservation(
        db_path, lifecycle_id="life-r133", event_key="r133:owned", signal_id="r133-owned", run_id="r133"
    )
    claim, part_id = _claim_owned(db_path, event_id, reservation_id, attempt_id="r133-claim")
    assert claim.claim is not None
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET direction = 'short', public_watchlist_plan_id = 'foreign-plan'
            WHERE id = ?
            """,
            (reservation_id,),
        )
        connection.commit()
        before_part = _part_state(connection, part_id)
    assert _mismatch_after_corrupt(db_path, event_id, reservation_id) is not None
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        mutated = SQLitePublicTelegramOutbox(repository._connection).mark_part_in_flight(
            part_id=part_id,
            attempt_id=claim.claim.attempt_id,
            now=NOW,
        )
        after_part = _part_state(repository._connection, part_id)
        after_attempt = _snapshot_attempt(repository._connection, reservation_id)
    assert mutated is False
    assert before_part == after_part == PENDING
    assert after_attempt["direction"] == "short"
    assert after_attempt["public_watchlist_plan_id"] == "foreign-plan"


def test_r134_post_claim_unregistered_scan_run_blocks_part_in_flight(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r134.db")
    event_id, reservation_id, part_id = _owned_event_reservation(
        db_path, lifecycle_id="life-r134", event_key="r134:owned", signal_id="r134-owned", run_id="r134"
    )
    claim, part_id = _claim_owned(db_path, event_id, reservation_id, attempt_id="r134-claim")
    assert claim.claim is not None
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE telegram_alert_attempts SET scan_run_id = ? WHERE id = ?",
            (UNREGISTERED_FOREIGN_RUN, reservation_id),
        )
        connection.commit()
        before_part = _part_state(connection, part_id)
    assert _mismatch_after_corrupt(db_path, event_id, reservation_id) is not None
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        mutated = SQLitePublicTelegramOutbox(repository._connection).mark_part_in_flight(
            part_id=part_id,
            attempt_id=claim.claim.attempt_id,
            now=NOW,
        )
        after_part = _part_state(repository._connection, part_id)
        after_attempt = _snapshot_attempt(repository._connection, reservation_id)
    assert mutated is False
    assert before_part == after_part == PENDING
    assert after_attempt["scan_run_id"] == UNREGISTERED_FOREIGN_RUN


def test_r135_malformed_expired_reservation_stale_recovery_changes_nothing(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r135.db")
    event_id, reservation_id, part_id = _owned_event_reservation(
        db_path, lifecycle_id="life-r135", event_key="r135:owned", signal_id="r135-owned", run_id="r135"
    )
    claim, part_id = _claim_owned(db_path, event_id, reservation_id, attempt_id="r135-claim")
    assert claim.claim is not None
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "UPDATE telegram_alert_attempts SET alert_type = 'TP1_HIT' WHERE id = ?",
            (reservation_id,),
        )
        connection.execute(
            "UPDATE public_alert_events SET lease_expires_at = ? WHERE id = ?",
            ("2026-09-20T12:00:01Z", event_id),
        )
        connection.commit()
        before_event = _event_state(connection, event_id)
        before_attempt = _snapshot_attempt(connection, reservation_id)
        before_part = _part_state(connection, part_id)
    assert _mismatch_after_corrupt(db_path, event_id, reservation_id) is not None
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        recovered = SQLitePublicTelegramOutbox(repository._connection).recover_stale_in_flight(
            event_id=event_id,
            now="2026-09-20T12:00:02Z",
        )
        after_event = _event_state(repository._connection, event_id)
        after_attempt = _snapshot_attempt(repository._connection, reservation_id)
        after_part = _part_state(repository._connection, part_id)
    assert recovered is False
    assert after_event == before_event
    assert after_attempt == before_attempt
    assert after_part == before_part
    assert after_event["delivery_state"] == IN_FLIGHT
    assert after_attempt["telegram_status"] == "in_flight"
    assert after_attempt["delivery_state"] != UNCERTAIN


def test_r136_malformed_reservation_cannot_reach_fake_sender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _bootstrap(tmp_path / "r136.db")
    event_id, reservation_id, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r136", event_key="r136:owned", signal_id="r136-owned", run_id="r136"
    )

    class _CorruptAfterClaimOutbox(SQLitePublicTelegramOutbox):
        def claim(self, **kwargs):  # type: ignore[no-untyped-def]
            result = super().claim(**kwargs)
            if result.claim is not None:
                self.connection.execute(
                    "UPDATE telegram_alert_attempts SET alert_type = 'TP1_HIT' WHERE id = ?",
                    (int(kwargs["reservation_id"]),),
                )
                self.connection.commit()
            return result

    monkeypatch.setattr(
        "app.alerts.telegram_lifecycle.SQLitePublicTelegramOutbox",
        _CorruptAfterClaimOutbox,
    )
    sender = FakeSender()
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        asyncio.run(
            _deliver_committed_public_alert_intent(
                repository=repository,
                sender=sender,
                reservation=_reservation_payload(event_id, reservation_id, "r136:owned"),
                symbol="BTCUSDT",
                signal_id="r136-owned",
                alert_type=TelegramAlertType.WATCHLIST,
                message_type=TelegramMessageType.PUBLIC_WATCHLIST,
                message_text="owned payload",
                message_hash="hash-r136-owned",
            )
        )
        part_state = _part_state(repository._connection, _part)
        attempt = _snapshot_attempt(repository._connection, reservation_id)
    assert sender.calls == []
    assert sender.messages == []
    assert part_state == PENDING
    assert attempt["alert_type"] == "TP1_HIT"


def test_r137_valid_canonical_reservation_mark_part_in_flight_succeeds(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r137.db")
    event_id, reservation_id, part_id = _owned_event_reservation(
        db_path, lifecycle_id="life-r137", event_key="r137:owned", signal_id="r137-owned", run_id="r137"
    )
    claim, part_id = _claim_owned(db_path, event_id, reservation_id, attempt_id="r137-claim")
    assert claim.claim is not None
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        mutated = SQLitePublicTelegramOutbox(repository._connection).mark_part_in_flight(
            part_id=part_id,
            attempt_id=claim.claim.attempt_id,
            now=NOW,
        )
        after_part = _part_state(repository._connection, part_id)
    assert mutated is True
    assert after_part == IN_FLIGHT


def test_r138_valid_canonical_reservation_fake_sender_persists_sent(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r138.db")
    event_id, reservation_id, part_id = _owned_event_reservation(
        db_path, lifecycle_id="life-r138", event_key="r138:owned", signal_id="r138-owned", run_id="r138"
    )
    sender = FakeSender()
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        delivery = asyncio.run(
            _deliver_committed_public_alert_intent(
                repository=repository,
                sender=sender,
                reservation=_reservation_payload(event_id, reservation_id, "r138:owned"),
                symbol="BTCUSDT",
                signal_id="r138-owned",
                alert_type=TelegramAlertType.WATCHLIST,
                message_type=TelegramMessageType.PUBLIC_WATCHLIST,
                message_text="owned payload",
                message_hash="hash-r138-owned",
            )
        )
        part_state = _part_state(repository._connection, part_id)
        attempt = _snapshot_attempt(repository._connection, reservation_id)
        event = _event_state(repository._connection, event_id)
    assert delivery.status == "sent"
    assert len(sender.calls) == 1
    assert part_state == SENT
    assert attempt["telegram_status"] == "sent"
    assert event["delivery_state"] == SENT


def test_r139_valid_stale_canonical_reservation_enters_uncertain_quarantine(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r139.db")
    event_id, reservation_id, part_id = _owned_event_reservation(
        db_path, lifecycle_id="life-r139", event_key="r139:owned", signal_id="r139-owned", run_id="r139"
    )
    claim, part_id = _claim_owned(db_path, event_id, reservation_id, attempt_id="r139-claim")
    assert claim.claim is not None
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE public_alert_events SET lease_expires_at = ? WHERE id = ?",
            ("2026-09-20T12:00:01Z", event_id),
        )
        connection.commit()
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        recovered = SQLitePublicTelegramOutbox(repository._connection).recover_stale_in_flight(
            event_id=event_id,
            now="2026-09-20T12:00:02Z",
        )
        event = _event_state(repository._connection, event_id)
        attempt = _snapshot_attempt(repository._connection, reservation_id)
    assert recovered is True
    assert event["delivery_state"] == UNCERTAIN
    assert attempt["delivery_state"] == UNCERTAIN
    assert attempt["telegram_status"] == "uncertain"


def test_r140_uncertain_remains_non_auto_retryable(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r140.db")
    event_id, reservation_id, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r140", event_key="r140:owned", signal_id="r140-owned", run_id="r140"
    )
    claim, _part = _claim_owned(db_path, event_id, reservation_id, attempt_id="r140-claim")
    assert claim.claim is not None
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE public_alert_events SET lease_expires_at = ? WHERE id = ?",
            ("2026-09-20T12:00:01Z", event_id),
        )
        connection.commit()
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        outbox = SQLitePublicTelegramOutbox(repository._connection)
        assert outbox.recover_stale_in_flight(event_id=event_id, now="2026-09-20T12:00:02Z") is True
        blocked = outbox.claim(
            event_id=event_id,
            reservation_id=reservation_id,
            now="2026-09-20T12:01:00Z",
            attempt_id="r140-retry",
        )
        event = _event_state(repository._connection, event_id)
    assert blocked.claim is None
    assert blocked.state == UNCERTAIN
    assert event["delivery_state"] == UNCERTAIN
