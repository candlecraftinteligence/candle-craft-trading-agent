from __future__ import annotations

import inspect
from decimal import Decimal
from math import inf, nan

from app.alerts.public_identity import canonical_public_event_key
from app.data.dtos import NA
from app.lifecycle.economic_identity import (
    PLAN_VERSION_ID_PREFIX,
    PLAN_VERSION_ID_SCHEMA_VERSION,
    REASON_MISSING_INSTRUMENT_VENUE,
    REASON_MISSING_STRUCTURAL_ANCHOR,
    REASON_PLAN_GEOMETRY_UNLOCKED,
    REASON_PLAN_VERSION_INVARIANT_VIOLATION,
    SETUP_ID_PREFIX,
    SETUP_ID_SCHEMA_VERSION,
    latch_economic_identities,
    mint_plan_version_id,
    mint_setup_id,
)
from app.lifecycle.identity import setup_geometry_identity
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState
from app.lifecycle.outcome_policy import canonical_plan_identity
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import apply_lifecycle_to_run_result
from app.lifecycle.state_machine import PLAN_LOCK_STATES, LifecycleObservation, evaluate_lifecycle_transition
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunResult, ScannerSymbolResult


def _setup_kwargs(**overrides) -> dict[str, object]:
    values: dict[str, object] = {
        "instrument_venue": "binance",
        "symbol": "BTCUSDT",
        "direction": "long",
        "mode": "swing",
        "structural_anchor": "setup_generation_anchor|sweep-a",
    }
    values.update(overrides)
    return values


def _plan_kwargs(**overrides) -> dict[str, object]:
    setup = mint_setup_id(**_setup_kwargs())
    assert setup.identity is not None
    values: dict[str, object] = {
        "setup_id": setup.identity,
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "95",
        "tp1": "110",
        "tp2": "117",
        "tp3": "124",
        "invalidation": "Invalid if price accepts below 95.",
    }
    values.update(overrides)
    return values


def _confirmed_observation(**overrides) -> LifecycleObservation:
    data = {
        "symbol": "BTCUSDT",
        "mode": "swing",
        "direction": "long",
        "instrument_venue": "binance",
        "structural_anchor": "setup_generation_anchor|sweep-a",
        "readiness_score": 85,
        "readiness_label": "VALID SETUP",
        "quality_score": 82,
        "quality_grade": "B+",
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "95",
        "tp1": "110",
        "tp2": "117",
        "tp3": "124",
        "rr": "3.2",
        "failed_gate": NA,
        "invalidation_reason": "Invalid if price accepts below 95.",
        "sweep_detected": True,
        "structure_shift_detected": True,
        "pullback_valid": True,
        "rr_valid": True,
        "valid_trade_idea": True,
        "core_status": "idea_created",
        "setup_quality_state": "high_quality_trade",
        "technical_score": "70",
        "opportunity_score": "88",
    }
    data.update(overrides)
    return LifecycleObservation(**data)


def _triggered_observation(**overrides) -> LifecycleObservation:
    return _confirmed_observation(
        valid_trade_idea=False,
        **overrides,
    )


def _record(**overrides) -> SetupLifecycleRecord:
    values = {
        "lifecycle_id": "life-1",
        "symbol": "BTCUSDT",
        "mode": "swing",
        "direction": "long",
        "current_state": SetupLifecycleState.CONFIRMED,
        "first_seen_at": "2026-05-18T09:00:00+00:00",
        "last_seen_at": "2026-05-18T09:00:00+00:00",
        "last_transition_at": "2026-05-18T09:00:00+00:00",
        "invalidation_reason": "Invalid if price accepts below 95.",
        "invalidation_logic": "Invalid if price accepts below 95.",
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "95",
        "tp1": "110",
        "tp2": "117",
        "tp3": "124",
        "rr": "3.2",
        "setup_identity": "BTCUSDT|swing|long|100|102|95|Invalid if price accepts below 95.",
        "structural_anchor": "setup_generation_anchor|sweep-a",
    }
    values.update(overrides)
    return SetupLifecycleRecord(**values)


def _scan_result(symbol_result: ScannerSymbolResult) -> ScannerRunResult:
    config = ScannerRunConfig.model_validate(
        {
            "symbols": [symbol_result.symbol],
            "exchange": "binance",
            "account_equity": Decimal("10000"),
            "risk_per_trade_pct": Decimal("1"),
        }
    )
    return ScannerRunResult(
        config=config,
        results=(symbol_result,),
        scanned_symbols=1,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )


def _confirmed_candidate(
    *,
    direction: str = "long",
    mode: str = "swing",
    tp1: Decimal = Decimal("110"),
    tp2: Decimal = Decimal("117"),
    tp3: Decimal = Decimal("124"),
    anchor: str = "sweep-a",
) -> ScannerSymbolResult:
    entry_low = Decimal("100")
    entry_high = Decimal("102")
    stop = Decimal("95") if direction == "long" else Decimal("107")
    return ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        latest_high=Decimal("110"),
        latest_low=Decimal("108"),
        technical_score=70,
        valid_strategy_modes=(mode,),
        rejected_strategy_modes=(),
        strategy_diagnostics={
            mode: {
                "mode": mode,
                "bias": direction,
                "setup_generation_anchor": anchor,
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                "gates_failed": (),
                "entry_low": entry_low,
                "entry_high": entry_high,
                "stop": stop,
                "tp1": tp1,
                "tp2": tp2,
                "tp3": tp3,
                "rr_to_tp2": Decimal("3.2"),
                "opportunity_score": Decimal("88"),
                "invalidation": f"Invalid if price accepts beyond {stop}.",
                "quality_grade": "B+",
            }
        },
    )


def test_same_lineage_inputs_mint_the_same_setup_id() -> None:
    first = mint_setup_id(**_setup_kwargs())
    second = mint_setup_id(**_setup_kwargs())
    assert first.available
    assert first.identity == second.identity
    assert first.identity is not None
    assert first.identity.startswith(SETUP_ID_PREFIX)
    assert len(first.identity) == len(SETUP_ID_PREFIX) + 64


def test_different_structural_anchor_mints_different_setup_id() -> None:
    first = mint_setup_id(**_setup_kwargs())
    second = mint_setup_id(**_setup_kwargs(structural_anchor="setup_generation_anchor|sweep-b"))
    assert first.identity != second.identity


def test_long_and_short_mint_different_setup_ids() -> None:
    first = mint_setup_id(**_setup_kwargs(direction="LONG"))
    second = mint_setup_id(**_setup_kwargs(direction="short"))
    assert first.identity != second.identity


def test_scalp_and_swing_mint_different_setup_ids() -> None:
    first = mint_setup_id(**_setup_kwargs(mode="swing"))
    second = mint_setup_id(**_setup_kwargs(mode="scalp"))
    assert first.identity != second.identity


def test_different_venue_mints_different_setup_id() -> None:
    first = mint_setup_id(**_setup_kwargs(instrument_venue="binance"))
    second = mint_setup_id(**_setup_kwargs(instrument_venue="bybit"))
    assert first.identity != second.identity


def test_same_entry_stop_different_tp_ladder_keeps_setup_changes_plan_version() -> None:
    setup = mint_setup_id(**_setup_kwargs())
    first = mint_plan_version_id(**_plan_kwargs(setup_id=setup.identity))
    second = mint_plan_version_id(**_plan_kwargs(setup_id=setup.identity, tp2="130", tp3="145"))
    assert setup.identity == mint_setup_id(**_setup_kwargs()).identity
    assert first.identity != second.identity
    assert first.identity is not None
    assert first.identity.startswith(PLAN_VERSION_ID_PREFIX)


def test_equivalent_decimal_forms_share_plan_version_id() -> None:
    identities = {
        mint_plan_version_id(**_plan_kwargs(entry_low=entry)).identity
        for entry in ("100", "100.0", "100.00", Decimal("100.000"))
    }
    assert identities == {mint_plan_version_id(**_plan_kwargs(entry_low="100")).identity}


def test_different_economic_price_changes_plan_version_id() -> None:
    first = mint_plan_version_id(**_plan_kwargs(stop_loss="95"))
    second = mint_plan_version_id(**_plan_kwargs(stop_loss="94"))
    assert first.identity != second.identity


def test_missing_structural_anchor_does_not_mint_setup_id() -> None:
    result = mint_setup_id(**_setup_kwargs(structural_anchor=NA))
    assert result.available is False
    assert result.identity is None
    assert result.reason == REASON_MISSING_STRUCTURAL_ANCHOR


def test_missing_venue_does_not_invent_setup_id() -> None:
    result = mint_setup_id(**_setup_kwargs(instrument_venue=None))
    assert result.available is False
    assert result.identity is None
    assert result.reason == REASON_MISSING_INSTRUMENT_VENUE


def test_missing_required_economics_do_not_mint_plan_version_id() -> None:
    for field in ("entry_low", "entry_high", "stop_loss", "tp1", "tp2", "tp3"):
        result = mint_plan_version_id(**_plan_kwargs(**{field: NA}))
        assert result.available is False
        assert result.identity is None
        assert result.reason == f"missing_{field}"


def test_non_finite_and_malformed_prices_are_rejected() -> None:
    for value in ("NaN", "Infinity", "-Infinity", inf, nan, "not-a-price", 100.0, True):
        result = mint_plan_version_id(**_plan_kwargs(tp1=value))
        assert result.available is False
        assert result.identity is None
        assert result.reason in {"malformed_tp1", "non_finite_tp1", "rejected_tp1"}


def test_off_tick_prices_are_not_silently_merged() -> None:
    tick = Decimal("0.1")
    on_tick = mint_plan_version_id(**_plan_kwargs(entry_low="100.0", tick_size=tick))
    off_tick = mint_plan_version_id(**_plan_kwargs(entry_low="100.05", tick_size=tick))
    other_off_tick = mint_plan_version_id(**_plan_kwargs(entry_low="100.06", tick_size=tick))
    assert on_tick.available is True
    assert off_tick.available is False
    assert other_off_tick.available is False
    assert off_tick.identity is None
    assert off_tick.reason == "off_tick_entry_low"
    assert other_off_tick.reason == "off_tick_entry_low"


def test_plan_version_id_does_not_depend_on_lifecycle_id() -> None:
    source = inspect.getsource(mint_plan_version_id)
    assert "lifecycle_id" not in source
    first = mint_plan_version_id(**_plan_kwargs())
    second = mint_plan_version_id(**_plan_kwargs())
    assert first.identity == second.identity


def test_identity_functions_do_not_depend_on_quality_readiness_or_research() -> None:
    setup_source = inspect.getsource(mint_setup_id)
    plan_source = inspect.getsource(mint_plan_version_id)
    banned = (
        "research_provenance",
        "quality_score",
        "readiness_score",
        "current_state",
        "failed_gate",
        "valid_activations",
        "message_hash",
        "event_key",
    )
    for token in banned:
        assert token not in setup_source
        assert token not in plan_source


def test_successful_identity_does_not_embed_literal_na() -> None:
    setup = mint_setup_id(**_setup_kwargs())
    plan = mint_plan_version_id(**_plan_kwargs(setup_id=setup.identity))
    assert setup.identity is not None
    assert plan.identity is not None
    assert NA not in setup.identity
    assert NA not in plan.identity
    assert "N/A" not in "".join(field.canonical or "" for field in setup.fields)
    assert "N/A" not in "".join(field.canonical or "" for field in plan.fields)


def test_confirmed_lifecycle_latches_both_identities() -> None:
    result = evaluate_lifecycle_transition(
        None,
        _confirmed_observation(),
        lifecycle_id="life-confirmed",
        now="2026-05-18T09:00:00+00:00",
        required_confirmation_cycles=1,
    )
    record = result.record
    assert record is not None
    assert record.current_state == SetupLifecycleState.CONFIRMED
    assert record.setup_id == mint_setup_id(**_setup_kwargs()).identity
    assert record.plan_version_id == mint_plan_version_id(**_plan_kwargs()).identity
    assert record.economic_identity_reason is None
    assert record.setup_identity == setup_geometry_identity(
        symbol="BTCUSDT",
        mode="swing",
        direction="long",
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        invalidation_reason="Invalid if price accepts below 95.",
    )


def test_triggered_state_does_not_latch_plan_version_id() -> None:
    result = evaluate_lifecycle_transition(
        None,
        _triggered_observation(),
        lifecycle_id="life-triggered",
        now="2026-05-18T09:00:00+00:00",
    )
    record = result.record
    assert record is not None
    assert record.current_state == SetupLifecycleState.TRIGGERED
    assert record.setup_id == mint_setup_id(**_setup_kwargs()).identity
    assert record.plan_version_id is None
    assert record.economic_identity_reason == REASON_PLAN_GEOMETRY_UNLOCKED


def test_repeated_scan_does_not_churn_identities() -> None:
    first = evaluate_lifecycle_transition(
        None,
        _confirmed_observation(),
        lifecycle_id="life-stable",
        now="2026-05-18T09:00:00+00:00",
        required_confirmation_cycles=1,
    )
    second = evaluate_lifecycle_transition(
        first.record,
        _confirmed_observation(readiness_score=90, quality_score=91),
        lifecycle_id="life-stable",
        now="2026-05-18T09:05:00+00:00",
        required_confirmation_cycles=1,
    )
    assert first.record is not None
    assert second.record is not None
    assert second.record.setup_id == first.record.setup_id
    assert second.record.plan_version_id == first.record.plan_version_id
    assert second.record.lifecycle_id == first.record.lifecycle_id


def test_missing_anchor_leaves_setup_id_unavailable_until_known() -> None:
    first = evaluate_lifecycle_transition(
        None,
        _confirmed_observation(structural_anchor=NA),
        lifecycle_id="life-anchor",
        now="2026-05-18T09:00:00+00:00",
        required_confirmation_cycles=1,
    )
    assert first.record is not None
    assert first.record.setup_id is None
    assert first.record.plan_version_id is None
    second = evaluate_lifecycle_transition(
        first.record,
        _confirmed_observation(),
        lifecycle_id="life-anchor",
        now="2026-05-18T09:05:00+00:00",
        required_confirmation_cycles=1,
    )
    assert second.record is not None
    assert second.record.setup_id == mint_setup_id(**_setup_kwargs()).identity
    assert second.record.plan_version_id == mint_plan_version_id(**_plan_kwargs()).identity


def test_latched_plan_version_is_not_silently_overwritten() -> None:
    locked = latch_economic_identities(
        _record(),
        instrument_venue="binance",
        plan_locked=True,
    )
    mutated = locked.model_copy(update={"tp2": "999"})
    result = latch_economic_identities(
        mutated,
        instrument_venue="binance",
        plan_locked=True,
        previous=locked,
    )
    assert result.setup_id == locked.setup_id
    assert result.plan_version_id == locked.plan_version_id
    assert result.plan_version_id != mint_plan_version_id(**_plan_kwargs(tp2="999")).identity
    assert REASON_PLAN_VERSION_INVARIANT_VIOLATION in (result.economic_identity_reason or "")


def test_unlocked_complete_geometry_waits_for_lock_before_plan_version() -> None:
    unlocked = latch_economic_identities(
        _record(current_state=SetupLifecycleState.TRIGGERED),
        instrument_venue="binance",
        plan_locked=False,
    )
    assert unlocked.setup_id is not None
    assert unlocked.plan_version_id is None
    assert unlocked.economic_identity_reason == REASON_PLAN_GEOMETRY_UNLOCKED
    locked = latch_economic_identities(
        unlocked.model_copy(update={"current_state": SetupLifecycleState.CONFIRMED}),
        instrument_venue="binance",
        plan_locked=True,
        previous=unlocked,
    )
    assert locked.plan_version_id == mint_plan_version_id(**_plan_kwargs()).identity
    assert locked.economic_identity_reason is None


def test_lifecycle_rotation_on_new_anchor_changes_setup_lineage(tmp_path) -> None:
    db_path = tmp_path / "identity-rotation.db"
    first = apply_lifecycle_to_run_result(
        _scan_result(_confirmed_candidate(anchor="sweep-a")),
        database_path=db_path,
        scan_run_id="gen-a",
        now="2026-05-18T09:00:00+00:00",
        confirmation_cycles=1,
    )
    generation_a = first.results[0].lifecycle_state
    assert generation_a is not None
    completed = generation_a.model_copy(
        update={
            "current_state": SetupLifecycleState.COOLDOWN,
            "previous_state": SetupLifecycleState.SL_HIT,
            "cooldown_until": "2026-06-08T10:00:00+00:00",
        }
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(completed)

    second = apply_lifecycle_to_run_result(
        _scan_result(_confirmed_candidate(anchor="sweep-b")),
        database_path=db_path,
        scan_run_id="gen-b",
        now="2026-06-08T09:00:00+00:00",
        confirmation_cycles=1,
    )
    generation_b = second.results[0].lifecycle_state
    assert generation_b is not None
    assert generation_b.lifecycle_id != generation_a.lifecycle_id
    assert generation_b.setup_identity == generation_a.setup_identity
    assert generation_b.setup_id != generation_a.setup_id
    assert generation_b.structural_anchor == "setup_generation_anchor|sweep-b"


def test_repeated_scan_observation_does_not_churn_persisted_identities(tmp_path) -> None:
    db_path = tmp_path / "identity-repeat.db"
    candidate = _confirmed_candidate()
    first = apply_lifecycle_to_run_result(
        _scan_result(candidate),
        database_path=db_path,
        scan_run_id="scan-1",
        now="2026-05-18T09:00:00+00:00",
        confirmation_cycles=1,
    )
    second = apply_lifecycle_to_run_result(
        _scan_result(candidate),
        database_path=db_path,
        scan_run_id="scan-2",
        now="2026-05-18T09:05:00+00:00",
        confirmation_cycles=1,
    )
    first_record = first.results[0].lifecycle_state
    second_record = second.results[0].lifecycle_state
    assert first_record is not None
    assert second_record is not None
    assert second_record.lifecycle_id == first_record.lifecycle_id
    assert second_record.setup_id == first_record.setup_id
    assert second_record.plan_version_id == first_record.plan_version_id
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        restored = repository.get_record(symbol="BTCUSDT", mode="swing", direction="long")
    assert restored is not None
    assert restored.setup_id == first_record.setup_id
    assert restored.plan_version_id == first_record.plan_version_id


def test_current_setup_identity_remains_compatible() -> None:
    assert setup_geometry_identity(
        symbol="btcusdt",
        mode="SWING",
        direction="LONG",
        entry_low="100.00",
        entry_high="102",
        stop_loss="95",
        invalidation_reason="Invalid below 95.",
    ) == "BTCUSDT|swing|long|100.00|102|95|Invalid below 95."


def test_current_canonical_plan_identity_remains_compatible() -> None:
    record = _record(lifecycle_id="plan-compat")
    expected = canonical_plan_identity(record)
    assert expected == canonical_plan_identity(record)
    assert expected.startswith("plan-")
    assert expected != record.plan_version_id
    equivalent = _record(lifecycle_id="plan-compat", entry_low="100.0", entry_high="102.00")
    assert canonical_plan_identity(equivalent) == expected


def test_public_event_key_semantics_remain_unchanged() -> None:
    assert canonical_public_event_key("plan-abc", "initial_watchlist") == "plan-abc|initial_watchlist"
    assert canonical_public_event_key(NA, "initial_watchlist") == NA


def test_plan_lock_states_remain_unchanged() -> None:
    assert PLAN_LOCK_STATES == {
        SetupLifecycleState.WATCHLISTED,
        SetupLifecycleState.STALKING,
        SetupLifecycleState.CONFIRMED,
        SetupLifecycleState.ACTIONABLE_A_GRADE,
        SetupLifecycleState.A_GRADE_WATCH,
        SetupLifecycleState.EXECUTING,
        SetupLifecycleState.MANAGING,
    }
    assert SetupLifecycleState.TRIGGERED not in PLAN_LOCK_STATES


def test_identity_schema_constants_are_explicit() -> None:
    assert SETUP_ID_SCHEMA_VERSION == "cci-setup-id-v1"
    assert PLAN_VERSION_ID_SCHEMA_VERSION == "cci-plan-version-v1"
    assert SETUP_ID_PREFIX == "setup-"
    assert PLAN_VERSION_ID_PREFIX == "plan-version-"
