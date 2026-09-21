"""Sixth bounded repair proofs R141–R150 for PROSPECTIVE_RUNTIME_EPOCH_ISOLATION.

DEV-only temp databases. No Runtime filesystem, live Telegram, exchange calls,
or scanner watch loops.

These tests encode the architecture-review reproduction from HEAD
9360b1054119875a6f376271c4e02312a018e72d:
copied lifecycle/setup/plan ids and identical prices do not prove ACTIVE
enrichment when invalidation contradicts the owned economic plan.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.lifecycle.economic_identity import mint_plan_version_id
from app.runtime_epoch.ownership import active_enrichment_belongs_to_owned_chain
from tests.test_prospective_runtime_epoch_isolation_repair4 import (
    FOREIGN_FACT,
    FOREIGN_GATE,
    FOREIGN_INVALIDATION,
    FOREIGN_QUALITY,
    FOREIGN_RATIONALE,
    OWNED_FACT,
    OWNED_GATE,
    OWNED_INVALIDATION,
    OWNED_RATIONALE,
    _bootstrap,
    _insert_candidate,
    _insert_scan_run,
    _insert_symbol_result,
    _joined_detail_text,
    _load_detail,
    _operational_fields,
    _seed_positive_active_detail,
)
from tests.test_prospective_runtime_epoch_isolation_repair5 import (
    OWNED_SETUP_ID,
    _stamp_lifecycle_identities,
)

pytestmark = pytest.mark.no_auto_epoch

PLAN_ENTRY_LOW = "100"
PLAN_ENTRY_HIGH = "102"
PLAN_STOP = "95"
PLAN_TP1 = "110"
PLAN_TP2 = "120"
PLAN_TP3 = "130"
TERMINAL_LIFECYCLE_REASON = "TERMINAL_NOT_PLAN_INVALIDATION"
OWNED_PLAN_LOGIC = "Close below 95 invalidates the setup"


def _minted_plan_version(invalidation: str, *, setup_id: str = OWNED_SETUP_ID) -> str:
    result = mint_plan_version_id(
        setup_id=setup_id,
        entry_low=PLAN_ENTRY_LOW,
        entry_high=PLAN_ENTRY_HIGH,
        stop_loss=PLAN_STOP,
        tp1=PLAN_TP1,
        tp2=PLAN_TP2,
        tp3=PLAN_TP3,
        invalidation=invalidation,
    )
    assert result.available is True
    assert result.identity
    return result.identity


def _bind_owned_plan(db_path: Path, lifecycle_id: str, *, invalidation: str = OWNED_INVALIDATION) -> str:
    plan_version_id = _minted_plan_version(invalidation)
    _stamp_lifecycle_identities(
        db_path,
        lifecycle_id,
        setup_id=OWNED_SETUP_ID,
        plan_version_id=plan_version_id,
    )
    return plan_version_id


def _geometry(*, entry_low: str | None = PLAN_ENTRY_LOW) -> dict[str, object]:
    payload: dict[str, object] = {
        "entry_high": PLAN_ENTRY_HIGH,
        "entry": f"{PLAN_ENTRY_LOW}-{PLAN_ENTRY_HIGH}",
        "stop_loss": PLAN_STOP,
        "tp1": PLAN_TP1,
        "tp2": PLAN_TP2,
        "tp3": PLAN_TP3,
    }
    if entry_low is not None:
        payload["entry_low"] = entry_low
    return payload


def _copied_identity_raw(
    *,
    lifecycle_id: str,
    plan_version_id: str,
    invalidation: str | None,
    rationale: str,
    fact: str,
    gate: str,
    quality: str,
    entry_low: str | None = PLAN_ENTRY_LOW,
    nested_invalidation: str | None = None,
) -> dict[str, object]:
    geometry = _geometry(entry_low=entry_low)
    nested_invalidation_value = invalidation if nested_invalidation is None else nested_invalidation
    trade_idea: dict[str, object] = {
        "direction": "long",
        "mode": "swing",
        "grade": quality,
        "reason_for_trade": rationale,
        "confirmed_facts": [fact],
        **geometry,
    }
    if nested_invalidation_value is not None:
        trade_idea["invalidation"] = nested_invalidation_value
    raw: dict[str, object] = {
        "symbol": "BTCUSDT",
        "direction": "long",
        "mode": "swing",
        "lifecycle_id": lifecycle_id,
        "setup_id": OWNED_SETUP_ID,
        "plan_version_id": plan_version_id,
        "reason_for_trade": rationale,
        "setup_quality": {"quality_grade": quality, "decision_reason": gate},
        "confirmed_facts": [fact],
        "trade_idea": trade_idea,
        **geometry,
    }
    if invalidation is not None:
        raw["invalidation"] = invalidation
    return raw


def _insert_later_raw(db_path: Path, *, run_id: str, raw_result: dict[str, object]) -> None:
    with sqlite3.connect(db_path) as connection:
        _insert_scan_run(connection, run_id=run_id, symbol="BTCUSDT", now="2026-09-21T18:00:00Z")
        _insert_symbol_result(connection, run_id=run_id, symbol="BTCUSDT", raw_result=raw_result)
        connection.commit()


def _assert_foreign_narrative_absent(detail) -> None:
    joined = _joined_detail_text(detail)
    assert FOREIGN_INVALIDATION not in joined
    assert FOREIGN_RATIONALE not in joined
    assert FOREIGN_FACT not in joined
    assert FOREIGN_GATE not in joined
    assert FOREIGN_QUALITY not in joined


def test_mint_plan_version_id_changes_when_only_invalidation_changes() -> None:
    owned = mint_plan_version_id(
        setup_id=OWNED_SETUP_ID,
        entry_low=PLAN_ENTRY_LOW,
        entry_high=PLAN_ENTRY_HIGH,
        stop_loss=PLAN_STOP,
        tp1=PLAN_TP1,
        tp2=PLAN_TP2,
        tp3=PLAN_TP3,
        invalidation=OWNED_INVALIDATION,
    )
    foreign = mint_plan_version_id(
        setup_id=OWNED_SETUP_ID,
        entry_low=PLAN_ENTRY_LOW,
        entry_high=PLAN_ENTRY_HIGH,
        stop_loss=PLAN_STOP,
        tp1=PLAN_TP1,
        tp2=PLAN_TP2,
        tp3=PLAN_TP3,
        invalidation=FOREIGN_INVALIDATION,
    )
    assert owned.available is True
    assert foreign.available is True
    assert owned.identity
    assert foreign.identity
    assert owned.identity != foreign.identity


def test_r141_copied_ids_same_prices_contradictory_nested_invalidation_rejected(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r141.db")
    _seed_positive_active_detail(db_path, label="r141")
    plan_version_id = _bind_owned_plan(db_path, "life-r141")
    baseline = _load_detail(tmp_path, db_path)
    assert baseline is not None
    before = _operational_fields(baseline)
    _insert_later_raw(
        db_path,
        run_id="r141-foreign-invalidation",
        raw_result=_copied_identity_raw(
            lifecycle_id="life-r141",
            plan_version_id=plan_version_id,
            invalidation=FOREIGN_INVALIDATION,
            rationale=FOREIGN_RATIONALE,
            fact=FOREIGN_FACT,
            gate=FOREIGN_GATE,
            quality=FOREIGN_QUALITY,
            entry_low=None,
        ),
    )
    after = _load_detail(tmp_path, db_path)
    assert after is not None
    assert str(after.bias).upper() == "LONG"
    _assert_foreign_narrative_absent(after)
    assert _operational_fields(after) == before


def test_r142_copied_plan_version_id_cannot_override_invalidation_mismatch(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r142.db")
    _seed_positive_active_detail(db_path, label="r142")
    plan_version_id = _bind_owned_plan(db_path, "life-r142")
    assert plan_version_id != _minted_plan_version(FOREIGN_INVALIDATION)
    baseline = _load_detail(tmp_path, db_path)
    assert baseline is not None
    before = _operational_fields(baseline)
    raw_result = _copied_identity_raw(
        lifecycle_id="life-r142",
        plan_version_id=plan_version_id,
        invalidation=FOREIGN_INVALIDATION,
        rationale=FOREIGN_RATIONALE,
        fact=FOREIGN_FACT,
        gate=FOREIGN_GATE,
        quality=FOREIGN_QUALITY,
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        lifecycle = connection.execute(
            "SELECT * FROM setup_lifecycle_records WHERE lifecycle_id = ?",
            ("life-r142",),
        ).fetchone()
        attempt = connection.execute(
            "SELECT * FROM telegram_alert_attempts WHERE public_watchlist_event_key = ?",
            ("r142:owned",),
        ).fetchone()
        assert active_enrichment_belongs_to_owned_chain(
            connection,
            enrichment={},
            raw=raw_result,
            lifecycle_row=lifecycle,
            attempt_row=attempt,
        ) is False
    _insert_later_raw(db_path, run_id="r142-copied-plan", raw_result=raw_result)
    after = _load_detail(tmp_path, db_path)
    assert after is not None
    _assert_foreign_narrative_absent(after)
    assert _operational_fields(after) == before
    assert str(after.invalid_if) == str(before[2])


def test_r143_raw_and_nested_invalidation_contradiction_rejected(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r143.db")
    _seed_positive_active_detail(db_path, label="r143")
    plan_version_id = _bind_owned_plan(db_path, "life-r143")
    baseline = _load_detail(tmp_path, db_path)
    assert baseline is not None
    before = _operational_fields(baseline)
    _insert_later_raw(
        db_path,
        run_id="r143-split-invalidation",
        raw_result=_copied_identity_raw(
            lifecycle_id="life-r143",
            plan_version_id=plan_version_id,
            invalidation=OWNED_INVALIDATION,
            nested_invalidation=FOREIGN_INVALIDATION,
            rationale=FOREIGN_RATIONALE,
            fact=FOREIGN_FACT,
            gate=FOREIGN_GATE,
            quality=FOREIGN_QUALITY,
        ),
    )
    after = _load_detail(tmp_path, db_path)
    assert after is not None
    _assert_foreign_narrative_absent(after)
    assert _operational_fields(after) == before


def test_r144_same_prices_and_owned_invalidation_remain_accepted(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r144.db")
    _seed_positive_active_detail(db_path, label="r144")
    plan_version_id = _bind_owned_plan(db_path, "life-r144")
    later_rationale = "OWNED_LATER_R144_RATIONALE"
    _insert_later_raw(
        db_path,
        run_id="r144-owned-invalidation",
        raw_result=_copied_identity_raw(
            lifecycle_id="life-r144",
            plan_version_id=plan_version_id,
            invalidation=OWNED_INVALIDATION,
            rationale=later_rationale,
            fact=OWNED_FACT,
            gate=OWNED_GATE,
            quality="A",
            entry_low=None,
        ),
    )
    detail = _load_detail(tmp_path, db_path)
    assert detail is not None
    assert str(detail.bias).upper() == "LONG"
    assert later_rationale in _joined_detail_text(detail)
    assert OWNED_INVALIDATION in str(detail.invalid_if)
    assert FOREIGN_INVALIDATION not in _joined_detail_text(detail)
    assert str(detail.entry_low) != "N/A"
    assert str(detail.stop_loss) != "N/A"


def test_r145_exact_owned_plan_later_rationale_and_facts_still_enrich(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r145.db")
    _seed_positive_active_detail(db_path, label="r145")
    plan_version_id = _bind_owned_plan(db_path, "life-r145")
    later_rationale = "OWNED_LATER_R145_RATIONALE"
    later_fact = "OWNED_LATER_R145_FACT"
    raw_result = _copied_identity_raw(
        lifecycle_id="life-r145",
        plan_version_id=plan_version_id,
        invalidation=OWNED_INVALIDATION,
        rationale=later_rationale,
        fact=later_fact,
        gate=OWNED_GATE,
        quality="A",
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        lifecycle = connection.execute(
            "SELECT * FROM setup_lifecycle_records WHERE lifecycle_id = ?",
            ("life-r145",),
        ).fetchone()
        attempt = connection.execute(
            "SELECT * FROM telegram_alert_attempts WHERE public_watchlist_event_key = ?",
            ("r145:owned",),
        ).fetchone()
        assert active_enrichment_belongs_to_owned_chain(
            connection,
            enrichment={},
            raw=raw_result,
            lifecycle_row=lifecycle,
            attempt_row=attempt,
        ) is True
    _insert_later_raw(db_path, run_id="r145-owned-plan", raw_result=raw_result)
    detail = _load_detail(tmp_path, db_path)
    assert detail is not None
    joined = _joined_detail_text(detail)
    assert later_rationale in joined
    assert later_fact in joined
    assert OWNED_GATE in joined
    assert OWNED_INVALIDATION in str(detail.invalid_if)
    assert str(detail.bias).upper() == "LONG"
    assert FOREIGN_RATIONALE not in joined
    assert FOREIGN_INVALIDATION not in joined


def test_r146_foreign_invalidation_cannot_change_active_narrative_fields(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r146.db")
    _seed_positive_active_detail(db_path, label="r146")
    plan_version_id = _bind_owned_plan(db_path, "life-r146")
    baseline = _load_detail(tmp_path, db_path)
    assert baseline is not None
    before = _operational_fields(baseline)
    _insert_later_raw(
        db_path,
        run_id="r146-review-attack",
        raw_result=_copied_identity_raw(
            lifecycle_id="life-r146",
            plan_version_id=plan_version_id,
            invalidation=FOREIGN_INVALIDATION,
            rationale=FOREIGN_RATIONALE,
            fact=FOREIGN_FACT,
            gate=FOREIGN_GATE,
            quality=FOREIGN_QUALITY,
        ),
    )
    after = _load_detail(tmp_path, db_path)
    assert after is not None
    assert str(after.bias).upper() == "LONG"
    assert _operational_fields(after) == before
    _assert_foreign_narrative_absent(after)
    assert str(after.invalid_if) == str(baseline.invalid_if)
    assert str(after.why_it_matters) == str(baseline.why_it_matters)
    assert tuple(after.confirmed_facts) == tuple(baseline.confirmed_facts)
    assert tuple(after.confirmed_gates) == tuple(baseline.confirmed_gates)
    assert str(after.quality) == str(baseline.quality)


def test_r147_candidate_same_prices_foreign_invalidation_cannot_enrich(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r147.db")
    _seed_positive_active_detail(db_path, label="r147")
    plan_version_id = _bind_owned_plan(db_path, "life-r147")
    baseline = _load_detail(tmp_path, db_path)
    assert baseline is not None
    before = _operational_fields(baseline)
    with sqlite3.connect(db_path) as connection:
        _insert_scan_run(connection, run_id="r147-foreign-candidate", symbol="BTCUSDT", now="2026-09-21T18:00:00Z")
        _insert_candidate(
            connection,
            run_id="r147-foreign-candidate",
            symbol="BTCUSDT",
            direction="long",
            quality=FOREIGN_QUALITY,
            raw_candidate={
                "lifecycle_id": "life-r147",
                "setup_id": OWNED_SETUP_ID,
                "plan_version_id": plan_version_id,
                "invalidation": FOREIGN_INVALIDATION,
                "reason_for_trade": FOREIGN_RATIONALE,
                "confirmed_facts": [FOREIGN_FACT],
                "quality_grade": FOREIGN_QUALITY,
                "decision_reason": FOREIGN_GATE,
            },
        )
        connection.commit()
    after = _load_detail(tmp_path, db_path)
    assert after is not None
    _assert_foreign_narrative_absent(after)
    assert _operational_fields(after) == before


def test_r148_candidate_exact_owned_invalidation_can_enrich(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r148.db")
    _seed_positive_active_detail(db_path, label="r148")
    plan_version_id = _bind_owned_plan(db_path, "life-r148")
    later_fact = "OWNED_LATER_R148_FACT"
    with sqlite3.connect(db_path) as connection:
        _insert_scan_run(connection, run_id="r148-owned-candidate", symbol="BTCUSDT", now="2026-09-21T18:00:00Z")
        _insert_candidate(
            connection,
            run_id="r148-owned-candidate",
            symbol="BTCUSDT",
            direction="long",
            quality="A",
            raw_candidate={
                "lifecycle_id": "life-r148",
                "setup_id": OWNED_SETUP_ID,
                "plan_version_id": plan_version_id,
                "invalidation": OWNED_INVALIDATION,
                "reason_for_trade": OWNED_RATIONALE,
                "confirmed_facts": [later_fact],
                "quality_grade": "A",
            },
        )
        connection.commit()
    detail = _load_detail(tmp_path, db_path)
    assert detail is not None
    assert later_fact in _joined_detail_text(detail)
    assert OWNED_INVALIDATION in str(detail.invalid_if)
    assert str(detail.bias).upper() == "LONG"
    assert FOREIGN_INVALIDATION not in _joined_detail_text(detail)


def test_r149_unproven_invalidation_omits_enrichment_and_keeps_active_detail(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r149.db")
    _seed_positive_active_detail(db_path, label="r149")
    plan_version_id = _bind_owned_plan(db_path, "life-r149")
    baseline = _load_detail(tmp_path, db_path)
    assert baseline is not None
    before = _operational_fields(baseline)
    _insert_later_raw(
        db_path,
        run_id="r149-unproven",
        raw_result=_copied_identity_raw(
            lifecycle_id="life-r149",
            plan_version_id=plan_version_id,
            invalidation=None,
            rationale=FOREIGN_RATIONALE,
            fact=FOREIGN_FACT,
            gate=FOREIGN_GATE,
            quality=FOREIGN_QUALITY,
        ),
    )
    after = _load_detail(tmp_path, db_path)
    assert after is not None
    assert str(after.bias).upper() == "LONG"
    assert str(after.entry_low) != "N/A"
    assert str(after.stop_loss) != "N/A"
    _assert_foreign_narrative_absent(after)
    assert _operational_fields(after) == before
    assert OWNED_INVALIDATION in str(after.invalid_if)


def test_r150_owned_lifecycle_invalidation_fallback_without_optional_enrichment(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r150.db")
    _seed_positive_active_detail(db_path, label="r150")
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM symbol_results")
        connection.execute("DELETE FROM setup_candidates")
        connection.execute(
            """
            UPDATE setup_lifecycle_records
            SET invalidation_logic = ?, invalidation_reason = ?
            WHERE lifecycle_id = ?
            """,
            (OWNED_PLAN_LOGIC, TERMINAL_LIFECYCLE_REASON, "life-r150"),
        )
        connection.commit()
    detail = _load_detail(tmp_path, db_path)
    assert detail is not None
    assert str(detail.bias).upper() == "LONG"
    assert str(detail.symbol) == "BTCUSDT"
    assert str(detail.entry_low) != "N/A"
    assert str(detail.stop_loss) != "N/A"
    assert str(detail.tp1) != "N/A"
    assert OWNED_PLAN_LOGIC in str(detail.invalid_if)
    assert TERMINAL_LIFECYCLE_REASON not in str(detail.invalid_if)
    assert FOREIGN_INVALIDATION not in _joined_detail_text(detail)
