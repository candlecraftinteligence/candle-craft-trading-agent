from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any

import pytest

from app.analytics.market_regime import (
    MarketRegimeInput,
    MarketRegimeResult,
    RegimeAdjustment,
    RegimeCompatibility,
    RegimeConfidenceBand,
    RegimeRiskLevel,
    RegimeState,
    evaluate_market_regime,
)
from app.analytics.setup_quality import SetupQualityGrade, SetupQualityResult, SetupQualityState, validate_setup_quality
from app.alerts.telegram_lifecycle import (
    PUBLIC_WATCHLIST_REGIME_PENDING_GATE_CODES,
    TIMING_CONFIRMATION_PENDING,
    TelegramAlertType,
    TelegramEligibilityContext,
    _public_signal_gate_result,
    _public_watchlist_failed_gate_codes,
    _public_watchlist_gate_result,
    classify_failed_gate_code,
    telegram_alert_decision_for_symbol,
    telegram_signal_message_from_symbol,
)
from app.data.dtos import NA
from app.lifecycle.eligibility import active_signal_eligible
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState, SetupTransitionReason, SetupTransitionResult
from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerRunConfig,
    ScannerRunResult,
    ScannerSymbolResult,
    _apply_market_regime_to_results,
)
from app.research.queries import build_research_report
from app.storage.database import open_initialized_database
from app.storage.repositories import store_scan_result

REGIME_INTERVAL_MS = 12 * 60 * 60_000


def _trend_candles(*, start: str = "100", step: str = "1", count: int = 90, wick: str = "1") -> list[dict[str, Decimal | int]]:
    candles: list[dict[str, Decimal | int]] = []
    start_value = Decimal(start)
    step_value = Decimal(step)
    wick_value = Decimal(wick)
    for index in range(count):
        price = start_value + step_value * Decimal(index)
        candles.append(
            {
                "timestamp": index * REGIME_INTERVAL_MS,
                "open": price - step_value / Decimal("2"),
                "high": price + wick_value,
                "low": price - wick_value,
                "close": price,
                "volume": Decimal("100"),
            }
        )
    return candles


def _compression_candles() -> list[dict[str, Decimal | int]]:
    candles = _trend_candles(step="0.05", count=70, wick="2")
    for index in range(70, 90):
        close = Decimal("104") + Decimal(index % 2) * Decimal("0.01")
        candles.append(
            {
                "timestamp": index * REGIME_INTERVAL_MS,
                "open": close,
                "high": close + Decimal("0.08"),
                "low": close - Decimal("0.08"),
                "close": close,
                "volume": Decimal("100"),
            }
        )
    return candles


def _config() -> ScannerRunConfig:
    return ScannerRunConfig.model_validate(
        {"symbols": ["BTCUSDT"], "exchange": "binance", "account_equity": Decimal("1000"), "risk_per_trade_pct": Decimal("1")}
    )


def _valid_symbol() -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.JOURNAL_ENTRY_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED, ScannerPipelineStatus.JOURNAL_ENTRY_CREATED),
        valid_strategy_modes=("challenge",),
        strategy_diagnostics={
            "challenge": {
                "mode": "challenge",
                "is_valid": True,
                "bias": "long",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "rr_to_tp2": Decimal("3.2"),
                "trust_percentage": 88,
                "gates_passed": ("sweep", "bos_choch", "pullback_zone", "rr", "trust_meter"),
            }
        },
        setup_quality=validate_setup_quality(
            {
                "setup_valid": True,
                "mode": "challenge",
                "bias": "long",
                "rr_to_tp2": Decimal("3.2"),
                "best_rr": Decimal("3.2"),
                "sweep_passed": True,
                "confirmation_passed": True,
                "pullback_valid": True,
                "trust_percentage": 88,
                "first_failed_gate": "N/A",
            }
        ),
    )


SCANNER_CONTRACT_MODES = ("challenge", "swing", "scalp")
SCANNER_CONTRACT_NON_REGIME_FAILURES = (
    ("liquidity", "missing_confirmed_sweep"),
    ("reclaim", "wick_sweep_reclaim"),
    ("structure", "missing_confirmation_structure_shift"),
    ("pullback", "no_ob_or_fvg_zone"),
    ("target_integrity", "target_integrity"),
)


def _scanner_contract_diagnostics(
    mode: str,
    *,
    first_failed_gate: Any = NA,
    gates_failed: tuple[Any, ...] = (),
    rr: Decimal = Decimal("3.2"),
    **overrides: Any,
) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {
        "mode": mode,
        "is_valid": first_failed_gate == NA and not gates_failed,
        "bias": "long",
        "entry_low": Decimal("100"),
        "entry_high": Decimal("102"),
        "entry_zone": {"low": Decimal("100"), "high": Decimal("102")},
        "watch_zone": "100 - 102",
        "entry": Decimal("101"),
        "stop": Decimal("95"),
        "stop_loss": Decimal("95"),
        "tp1": Decimal("110"),
        "tp2": Decimal("115"),
        "tp3": Decimal("120"),
        "rr_to_tp2": rr,
        "planned_rr": rr,
        "invalidation": "Invalid if price accepts below 95.",
        "invalidation_reason": "Invalid if price accepts below 95.",
        "structure_reason": "Sweep, reclaim, BOS/CHoCH, pullback zone, target map, and RR are intact.",
        "confirmation_needed": "Market regime must turn supportive.",
        "execution_sweep_status": "passed",
        "confirmation_structure_shift_status": "passed",
        "pullback_zone_status": "valid",
        "selected_zone_type": "OB valid",
        "target_integrity_status": "passed",
        "target_integrity_failed": False,
        "trust_percentage": 88,
        "quality_grade": "A",
        "regime_state": "HIGH_VOLATILITY",
        "regime_compatibility_label": "Hostile",
        "regime_compatibility_reason": "Setup is structurally valid, but market regime is hostile.",
        "gates_passed": ("sweep", "wick_reclaim", "bos_choch", "pullback_zone", "ob_fvg", "target_integrity", "rr"),
        "first_failed_gate": first_failed_gate,
        "gates_failed": gates_failed,
    }
    diagnostics.update(overrides)
    return diagnostics


def _scanner_contract_quality(
    state: SetupQualityState = SetupQualityState.HIGH_QUALITY_TRADE,
    *,
    score: int = 88,
) -> SetupQualityResult:
    if score >= 90:
        grade = SetupQualityGrade.A_PLUS
    elif score >= 85:
        grade = SetupQualityGrade.A
    elif score >= 75:
        grade = SetupQualityGrade.B_PLUS
    else:
        grade = SetupQualityGrade.B
    return SetupQualityResult(
        quality_state=state,
        quality_grade=grade,
        quality_score=score,
        tradeability_score=score,
        profitability_edge_score=score,
        execution_risk_score=max(0, 100 - score),
        strongest_factors=("structure", "target map"),
        weakest_factors=(),
        decision_reason="Synthetic scanner-mode contract setup quality.",
        action_label="Valid setup" if state != SetupQualityState.WATCHLIST_NEAR_MISS else "Wait for cleaner regime",
    )


def _scanner_contract_symbol(
    mode: str,
    *,
    diagnostics: dict[str, Any] | None = None,
    rr: Decimal = Decimal("3.2"),
    setup_quality: SetupQualityResult | None = None,
    status: ScannerPipelineStatus = ScannerPipelineStatus.JOURNAL_ENTRY_CREATED,
    rejection_stage: str = NA,
) -> ScannerSymbolResult:
    mode_diagnostics = diagnostics or _scanner_contract_diagnostics(mode, rr=rr)
    return ScannerSymbolResult(
        symbol="BTCUSDT",
        status=status,
        status_history=(ScannerPipelineStatus.IDEA_CREATED, status),
        rejection_stage=rejection_stage,
        current_price=Decimal("101"),
        technical_score=Decimal("70"),
        valid_strategy_modes=(mode,),
        strategy_diagnostics={mode: mode_diagnostics},
        setup_quality=setup_quality or _scanner_contract_quality(),
    )


def _blocking_regime() -> MarketRegimeResult:
    compatibility = {
        mode: RegimeCompatibility(
            mode=mode,
            score=20,
            label="Hostile",
            allowed=False,
            regime_compatibility=20,
            volatility_suitability=20,
            trend_suitability=20,
            execution_quality_suitability=20,
            risk_multiplier=Decimal("1"),
            notes=("Market/regime condition is not supportive yet.",),
        )
        for mode in SCANNER_CONTRACT_MODES
    }
    adjustment = RegimeAdjustment(
        allow_challenge=False,
        allow_swings=False,
        allow_scalps=False,
        min_quality_score_adjustment=0,
        risk_multiplier=Decimal("1"),
        regime_penalty=10,
        compatibility_scores={mode: 20 for mode in SCANNER_CONTRACT_MODES},
        explanation="Market/regime condition is pending.",
    )
    return MarketRegimeResult(
        state=RegimeState.HIGH_VOLATILITY,
        risk_level=RegimeRiskLevel.HIGH,
        confidence_score=20,
        confidence_band=RegimeConfidenceBand.HOSTILE,
        compatibility_scores=compatibility,
        adjustment=adjustment,
        environment_notes=("Market/regime condition is pending.",),
    )


def _scanner_regime_blocked_symbol(
    mode: str,
    *,
    diagnostics: dict[str, Any] | None = None,
    rr: Decimal = Decimal("3.2"),
) -> ScannerSymbolResult:
    symbol = _scanner_contract_symbol(mode, diagnostics=diagnostics, rr=rr)
    return _apply_market_regime_to_results((symbol,), _blocking_regime())[0]


def _contract_lifecycle_record(
    state: SetupLifecycleState,
    *,
    mode: str,
    signal_id: str = "scanner-contract",
    rr: Decimal = Decimal("3.2"),
) -> SetupLifecycleRecord:
    return SetupLifecycleRecord(
        lifecycle_id=signal_id,
        symbol="BTCUSDT",
        mode=mode,
        direction="long",
        current_state=state,
        previous_state=SetupLifecycleState.TRIGGERED if state == SetupLifecycleState.CONFIRMED else SetupLifecycleState.DISCOVERED,
        first_seen_at="2026-06-02T00:00:00+00:00",
        last_seen_at="2026-06-02T00:00:00+00:00",
        last_transition_at="2026-06-02T00:00:00+00:00",
        readiness_score=88,
        quality_score=88,
        regime_state="HIGH_VOLATILITY",
        action_label="Wait for cleaner regime",
        invalidation_reason="Invalid if price accepts below 95.",
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        tp1="110",
        tp2="115",
        tp3="120",
        rr=str(rr),
        invalidation_logic="Invalid if price accepts below 95.",
        quality_grade_current="A",
    )


def _symbol_mode(symbol: ScannerSymbolResult) -> str:
    if symbol.valid_strategy_modes:
        return symbol.valid_strategy_modes[0]
    if symbol.rejected_strategy_modes:
        return symbol.rejected_strategy_modes[0]
    for diagnostics in symbol.strategy_diagnostics.values():
        if isinstance(diagnostics, dict) and diagnostics.get("mode"):
            return str(diagnostics["mode"])
    return "swing"


def _symbol_rr(symbol: ScannerSymbolResult) -> Decimal:
    diagnostics = next(iter(symbol.strategy_diagnostics.values()), {})
    if isinstance(diagnostics, dict):
        value = diagnostics.get("rr_to_tp2", Decimal("3.2"))
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return Decimal("3.2")
    return Decimal("3.2")


def _with_lifecycle(
    symbol: ScannerSymbolResult,
    *,
    state: SetupLifecycleState = SetupLifecycleState.WATCHLISTED,
    signal_id: str = "scanner-contract",
) -> ScannerSymbolResult:
    mode = _symbol_mode(symbol)
    record = _contract_lifecycle_record(state, mode=mode, signal_id=signal_id, rr=_symbol_rr(symbol))
    transition = SetupTransitionResult(
        lifecycle_id=signal_id,
        symbol=symbol.symbol,
        from_state=record.previous_state,
        to_state=state,
        reason=SetupTransitionReason.READINESS_IMPROVED,
        transitioned=True,
        record=record,
    )
    return symbol.model_copy(update={"lifecycle_state": record, "lifecycle_transition": transition})


def _with_regime_metadata(symbol: ScannerSymbolResult) -> ScannerSymbolResult:
    return symbol.model_copy(
        update={
            "regime_state": "HIGH_VOLATILITY",
            "regime_confidence_score": 20,
            "regime_compatibility_label": "Hostile",
            "regime_diagnostics": {"state": "HIGH_VOLATILITY", "compatibility_label": "Hostile"},
        }
    )


def _public_watchlist_gate(symbol: ScannerSymbolResult):
    message = telegram_signal_message_from_symbol(symbol)
    return _public_watchlist_gate_result(symbol, message, TelegramEligibilityContext())


def test_regime_confidence_and_compatibility_scores() -> None:
    result = evaluate_market_regime(
        MarketRegimeInput(
            btc_candles=_trend_candles(),
            eth_candles=_trend_candles(start="80", step="0.8"),
            bullish_bias_pct=Decimal("72"),
            bearish_bias_pct=Decimal("15"),
            confirmation_pct=Decimal("66"),
            htf_agreement_pct=Decimal("80"),
            average_rr=Decimal("3.4"),
            setup_density_pct=Decimal("35"),
            rejection_clustering_pct=Decimal("8"),
            btc_d_context="stable",
            usdt_d_context="stable",
        )
    )

    assert result.state == RegimeState.TREND_EXPANSION
    assert result.confidence_score >= 71
    assert result.compatibility_scores["swing"].score >= result.compatibility_scores["challenge"].score
    assert "HTF alignment boost" in result.boosts


def test_regime_strictness_changes_trade_permission() -> None:
    base = {
        "btc_candles": _compression_candles(),
        "eth_candles": _compression_candles(),
        "bullish_bias_pct": Decimal("42"),
        "bearish_bias_pct": Decimal("35"),
    }

    low = evaluate_market_regime(MarketRegimeInput(**base, strictness="low"))
    high = evaluate_market_regime(MarketRegimeInput(**base, strictness="high"))

    assert low.state == RegimeState.RANGE_COMPRESSION
    assert low.compatibility_scores["challenge"].allowed is True
    assert high.compatibility_scores["challenge"].allowed is False
    assert high.adjustment.regime_penalty >= low.adjustment.regime_penalty


def test_weak_regime_blocks_high_confidence_setup_with_diagnostics() -> None:
    regime = evaluate_market_regime(
        MarketRegimeInput(
            btc_candles=_trend_candles(step="0.4"),
            eth_candles=_trend_candles(start="140", step="-0.4"),
            valid_sweep_pct=Decimal("60"),
            confirmation_pct=Decimal("20"),
            failed_confirmation_pct=Decimal("60"),
            rejection_clustering_pct=Decimal("65"),
            strictness="high",
        )
    )

    adjusted = _apply_market_regime_to_results((_valid_symbol(),), regime)[0]

    assert adjusted.status == ScannerPipelineStatus.REJECTED_BY_REGIME
    assert adjusted.valid_strategy_modes == ()
    assert adjusted.rejected_strategy_modes == ("challenge",)
    assert adjusted.regime_blocked is True
    assert adjusted.setup_quality.quality_state == SetupQualityState.WATCHLIST_NEAR_MISS
    assert adjusted.regime_diagnostics["confidence_score"] == regime.confidence_score
    assert "penalty" in adjusted.rejection_reason


@pytest.mark.parametrize("mode", SCANNER_CONTRACT_MODES)
def test_strategy_mode_blocks_public_watchlist_when_only_regime_blocks(mode: str) -> None:
    blocked = _scanner_regime_blocked_symbol(mode, rr=Decimal("2.5"))
    candidate = _with_lifecycle(blocked, signal_id=f"{mode}-regime-only")
    diagnostics = blocked.strategy_diagnostics[mode]
    raw_codes = _public_watchlist_failed_gate_codes(candidate)
    gate = _public_watchlist_gate(candidate)

    assert blocked.status == ScannerPipelineStatus.REJECTED_BY_REGIME
    assert blocked.rejected_strategy_modes == (mode,)
    assert diagnostics["first_failed_gate"] == "regime_compatibility"
    assert diagnostics["gates_failed"] == ("regime_compatibility",)
    assert raw_codes == ("regime_compatibility",)
    assert raw_codes[0] not in PUBLIC_WATCHLIST_REGIME_PENDING_GATE_CODES
    assert {classify_failed_gate_code(code) for code in raw_codes} == {"FATAL_PUBLIC_WATCHLIST_GATE"}
    assert gate.allowed is False
    assert gate.allowed_missing_gate is None
    assert "public_watchlist_fatal_failed_gates=regime_compatibility" in gate.blocking_reasons
    assert gate.rr == 2.5


@pytest.mark.parametrize(("failure_type", "gate_code"), SCANNER_CONTRACT_NON_REGIME_FAILURES)
def test_strategy_mode_non_regime_failure_not_regime_pending(failure_type: str, gate_code: str) -> None:
    diagnostics = _scanner_contract_diagnostics(
        "swing",
        first_failed_gate=gate_code,
        gates_failed=(gate_code,),
        target_integrity_status="blocked" if failure_type == "target_integrity" else "passed",
        target_integrity_failed=failure_type == "target_integrity",
        invalid_target_fields="tp1" if failure_type == "target_integrity" else NA,
    )
    candidate = _with_lifecycle(
        _with_regime_metadata(_scanner_contract_symbol("swing", diagnostics=diagnostics)),
        signal_id=f"non-regime-{failure_type}",
    )
    gate = _public_watchlist_gate(candidate)

    assert "REGIME_MARKET_CONDITION_PENDING" not in gate.failed_gate_classes
    if classify_failed_gate_code(gate_code) == TIMING_CONFIRMATION_PENDING:
        assert gate.allowed is False
        assert gate.allowed_missing_gate == TIMING_CONFIRMATION_PENDING
        assert "public_block_non_actionable_state" in gate.blocking_reasons
        return
    assert gate.allowed is False
    assert any(
        reason.startswith("public_watchlist_non_regime_failed_gates=")
        or reason.startswith("public_watchlist_fatal_failed_gates=")
        or reason.startswith("target_integrity_failed")
        for reason in gate.blocking_reasons
    )


def test_strategy_mode_mixed_failures_block_public_watchlist() -> None:
    target_failure = _scanner_contract_diagnostics(
        "swing",
        first_failed_gate="target_integrity",
        gates_failed=("target_integrity",),
        target_integrity_status="blocked",
        target_integrity_failed=True,
        invalid_target_fields="tp1",
    )
    blocked = _scanner_regime_blocked_symbol("swing", diagnostics=target_failure)
    candidate = _with_lifecycle(blocked, signal_id="mixed-regime-target")
    gate = _public_watchlist_gate(candidate)

    assert _public_watchlist_failed_gate_codes(candidate) == ("regime_compatibility", "target_integrity")
    assert gate.failed_gate_classes == ("FATAL_PUBLIC_WATCHLIST_GATE",)
    assert gate.allowed is False
    assert "public_watchlist_fatal_failed_gates=regime_compatibility,target_integrity" in gate.blocking_reasons


def test_strategy_mode_rr_failure_blocks_public_watchlist() -> None:
    blocked = _scanner_regime_blocked_symbol("swing", rr=Decimal("1.99"))
    candidate = _with_lifecycle(blocked, signal_id="rr-below-watchlist")
    gate = _public_watchlist_gate(candidate)

    assert gate.allowed is False
    assert gate.failed_gate_classes == ("FATAL_PUBLIC_WATCHLIST_GATE",)
    assert "public_watchlist_fatal_failed_gates=regime_compatibility" in gate.blocking_reasons
    assert "public_watchlist_rr_below_min:1.99<3" in gate.blocking_reasons


def test_strategy_mode_missing_regime_data_still_blocks_as_regime_failure() -> None:
    blocked = _scanner_regime_blocked_symbol("swing")
    diagnostics = dict(blocked.strategy_diagnostics["swing"])
    diagnostics["regime_state"] = NA
    diagnostics["regime_compatibility_label"] = NA
    candidate = _with_lifecycle(
        blocked.model_copy(update={"strategy_diagnostics": {"swing": diagnostics}}),
        signal_id="missing-regime-data",
    ).model_copy(
        update={"regime_state": NA, "regime_compatibility_label": NA, "regime_diagnostics": {}}
    )
    gate = _public_watchlist_gate(candidate)

    assert gate.allowed is False
    assert "public_watchlist_fatal_failed_gates=regime_compatibility" in gate.blocking_reasons


@pytest.mark.parametrize(
    ("label", "first_failed_gate", "gates_failed", "expected_reason"),
    (
        ("absent", NA, (), "public_watchlist_missing_explicit_timing_gate"),
        ("none", None, (), "public_watchlist_missing_explicit_timing_gate"),
        ("empty", "", (), "public_watchlist_missing_explicit_timing_gate"),
        ("unknown", "mystery_gate", ("mystery_gate",), "public_watchlist_unknown_failed_gates=mystery_gate"),
        ("malformed", {"bad": "gate"}, ({"bad": "gate"},), "public_watchlist_malformed_failed_gate_diagnostics"),
    ),
)
def test_strategy_mode_missing_or_malformed_diagnostics_block_public_watchlist(
    label: str,
    first_failed_gate: Any,
    gates_failed: tuple[Any, ...],
    expected_reason: str,
) -> None:
    diagnostics = _scanner_contract_diagnostics("swing", first_failed_gate=first_failed_gate, gates_failed=gates_failed)
    candidate = _with_lifecycle(
        _with_regime_metadata(_scanner_contract_symbol("swing", diagnostics=diagnostics)),
        signal_id=f"bad-diagnostics-{label}",
    )
    gate = _public_watchlist_gate(candidate)

    assert gate.allowed is False
    assert expected_reason in gate.blocking_reasons


def test_strategy_mode_regime_blocked_candidate_is_not_public_watchlist_copy() -> None:
    candidate = _with_lifecycle(_scanner_regime_blocked_symbol("scalp"), signal_id="scalp-copy")
    decision = telegram_alert_decision_for_symbol(candidate)

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "public_watchlist_fatal_failed_gates=regime_compatibility" in decision.reason


def test_strategy_mode_public_watchlist_does_not_create_active_execution_signal() -> None:
    candidate = _with_lifecycle(_scanner_regime_blocked_symbol("challenge"), signal_id="watch-not-active")
    decision = telegram_alert_decision_for_symbol(candidate)

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert candidate.lifecycle_transition is not None
    assert candidate.lifecycle_transition.to_state == SetupLifecycleState.WATCHLISTED
    assert candidate.lifecycle_state is not None
    assert active_signal_eligible(candidate.lifecycle_state) is False


def test_limit_hit_still_not_public_execution_eligible_after_scanner_contracts() -> None:
    candidate = _with_lifecycle(
        _with_regime_metadata(_scanner_contract_symbol("swing")),
        state=SetupLifecycleState.MANAGING,
        signal_id="limit-hit-contract",
    )
    message = telegram_signal_message_from_symbol(candidate)

    gate = _public_signal_gate_result(candidate, TelegramAlertType.LIMIT_HIT, message)

    assert gate.allowed is False
    assert "limit_hit_requires_prior_public_signal" in gate.blocking_reasons


def test_regime_metadata_is_persisted(tmp_path) -> None:
    db_path = tmp_path / "regime.db"
    regime = evaluate_market_regime(
        MarketRegimeInput(
            btc_candles=_trend_candles(),
            eth_candles=_trend_candles(start="80", step="0.8"),
            bullish_bias_pct=Decimal("72"),
            confirmation_pct=Decimal("65"),
        )
    )
    symbol = _valid_symbol().model_copy(
        update={
            "regime_state": regime.state.value,
            "regime_confidence_score": regime.confidence_score,
            "regime_compatibility_score": regime.compatibility_scores["challenge"].score,
            "regime_compatibility_label": regime.compatibility_scores["challenge"].label,
            "regime_penalty": regime.adjustment.regime_penalty,
            "regime_notes": regime.environment_notes,
        }
    )
    scan = ScannerRunResult(
        config=_config(),
        results=(symbol,),
        scanned_symbols=1,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
        market_regime=regime,
        regime_adjustments=regime.adjustment,
    )

    run_id = store_scan_result(db_path, scan)

    with sqlite3.connect(db_path) as connection:
        run = connection.execute(
            "SELECT regime_confidence, regime_compatibility_json, environment_notes_json FROM scan_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        symbol_row = connection.execute(
            "SELECT regime_confidence, regime_compatibility_score, regime_compatibility_label, regime_penalty FROM symbol_results WHERE run_id = ?",
            (run_id,),
        ).fetchone()

    assert run[0] == regime.confidence_score
    assert json.loads(run[1])["challenge"]["score"] == regime.compatibility_scores["challenge"].score
    assert json.loads(run[2])
    assert symbol_row[0] == str(regime.confidence_score)
    assert symbol_row[2] == regime.compatibility_scores["challenge"].label


def test_phase_35_research_queries(tmp_path) -> None:
    db_path = tmp_path / "research.db"
    with open_initialized_database(db_path) as connection:
        connection.execute(
            """
            INSERT INTO scan_runs (
                run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
                strategy, timeframes_json, market_regime, regime_confidence,
                regime_compatibility_json, environment_notes_json, runtime_stats_json,
                command_preset, command_used, total_valid_setups, near_misses, rejected,
                data_issues, data_issues_json, raw_payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run_1",
                "2026-05-18T09:00:00+00:00",
                "binance",
                "manual",
                2,
                "[]",
                "liquidity_grab_pullback",
                "{}",
                "CHOP",
                28,
                "{}",
                "[]",
                "{}",
                "N/A",
                "test",
                0,
                1,
                1,
                0,
                "[]",
                "{}",
            ),
        )
        for symbol, bucket, gate, quality in (
            ("BTCUSDT", "near_miss", "regime_compatibility", "68"),
            ("ETHUSDT", "no_setup", "missing_confirmed_sweep", "24"),
        ):
            raw = {
                "setup_quality": {"quality_state": "WATCHLIST_NEAR_MISS", "quality_grade": "C", "quality_score": quality},
                "readiness_label": "HOT WATCH",
                "valid_strategy_modes": [],
                "rejected_strategy_modes": ["challenge"],
            }
            connection.execute(
                """
                INSERT INTO symbol_results (
                    run_id, symbol, status, display_bucket, readiness_score, setup_quality_score,
                    edge_score, failed_gate, rejection_reason, next_trigger_needed, action_label,
                    regime_state, regime_confidence, regime_compatibility_score, regime_compatibility_label,
                    regime_penalty, environment_notes_json, derivatives_context_json, volume_profile_context_json,
                    pullback_status, portfolio_decision, raw_result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "run_1",
                    symbol,
                    "scanned",
                    bucket,
                    55,
                    quality,
                    "N/A",
                    gate,
                    gate,
                    "Wait",
                    "Watchlist only",
                    "CHOP",
                    "28",
                    "24",
                    "Hostile",
                    20,
                    "[]",
                    "{}",
                    "{}",
                    "N/A",
                    "N/A",
                    json.dumps(raw),
                ),
            )
        connection.execute(
            """
            INSERT INTO replay_results (
                run_id, setup_fingerprint, outcome, filled, tp_hit, sl_hit, final_r,
                time_in_trade, regime, symbol, mode, raw_result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("run_1", "fp", "stopped", 1, "N/A", 1, "-1", "10", "CHOP", "BTCUSDT", "challenge", "{}"),
        )
        connection.commit()

    expectancy = build_research_report(db_path, query="regime_expectancy")
    density = build_research_report(db_path, query="regime_setup_density")
    rejections = build_research_report(db_path, query="regime_rejection_patterns")
    quality = build_research_report(db_path, query="regime_quality_distribution")

    assert expectancy["regimes"][0]["regime"] == "CHOP"
    assert density["regimes"][0]["setup_density_pct"] == 50
    assert rejections["regimes"][0]["patterns"][0]["failed_gate"] in {"regime_compatibility", "missing_confirmed_sweep"}
    assert quality["regimes"][0]["compatibility_labels"][0]["regime_compatibility_label"] == "Hostile"
