from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.data.dtos import NA
from app.formatters.scanner_display import build_symbol_display, representative_strategy_diagnostics
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState, SetupTransitionResult
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.state_machine import (
    LifecycleObservation,
    WATCH_PRIORITY_STATES,
    evaluate_lifecycle_transition,
    is_watchable_lifecycle_state,
    now_utc_iso,
)
from app.pipeline.scanner_runner import ScannerRunResult, ScannerSymbolResult
from app.storage.database import DEFAULT_DATABASE_PATH


class SetupLifecycleService:
    def __init__(self, database_path: Path | str = DEFAULT_DATABASE_PATH) -> None:
        self.database_path = Path(database_path)

    def apply_to_run_result(
        self,
        result: ScannerRunResult,
        *,
        scan_run_id: str | None = None,
        now: str | None = None,
    ) -> ScannerRunResult:
        timestamp = now or now_utc_iso()
        updated_results: list[ScannerSymbolResult] = []
        with SQLiteSetupLifecycleRepository(self.database_path) as repository:
            for symbol_result in result.results:
                updated_results.append(
                    self.apply_to_symbol_result(
                        symbol_result,
                        repository=repository,
                        scan_run_id=scan_run_id,
                        now=timestamp,
                    )
                )
        return result.model_copy(update={"results": tuple(updated_results)})

    def apply_to_symbol_result(
        self,
        symbol_result: ScannerSymbolResult,
        *,
        repository: SQLiteSetupLifecycleRepository,
        scan_run_id: str | None,
        now: str,
    ) -> ScannerSymbolResult:
        observation = observation_from_symbol_result(symbol_result)
        existing = repository.get_record(
            symbol=observation.symbol,
            mode=observation.mode,
            direction=observation.direction,
        )
        transition = evaluate_lifecycle_transition(
            existing,
            observation,
            lifecycle_id=existing.lifecycle_id if existing is not None else uuid4().hex,
            now=now,
            scan_run_id=scan_run_id,
        )
        if transition.record is not None:
            repository.upsert_record(transition.record)
        if transition.event is not None:
            repository.insert_event(transition.event)
        return symbol_result.model_copy(
            update={
                "lifecycle_state": transition.record,
                "lifecycle_transition": transition,
            }
        )

    def reset(self) -> None:
        with SQLiteSetupLifecycleRepository(self.database_path) as repository:
            repository.reset()


def apply_lifecycle_to_run_result(
    result: ScannerRunResult,
    *,
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    scan_run_id: str | None = None,
    now: str | None = None,
) -> ScannerRunResult:
    return SetupLifecycleService(database_path).apply_to_run_result(result, scan_run_id=scan_run_id, now=now)


def observation_from_symbol_result(symbol_result: ScannerSymbolResult) -> LifecycleObservation:
    display = build_symbol_display(symbol_result)
    diagnostics = representative_strategy_diagnostics(symbol_result)
    gates_passed = _sequence_values(diagnostics.get("gates_passed"))
    gates_failed = _sequence_values(diagnostics.get("gates_failed"))
    failed_gate = _first_non_na(display.failed_gate, diagnostics.get("first_failed_gate"), symbol_result.rejection_stage)
    pullback_failure_type = _pullback_failure_type(symbol_result, diagnostics)
    acceptance_status = _acceptance_status(symbol_result, diagnostics)
    mode = _mode_from_result(symbol_result, diagnostics)
    direction = _direction_from_result(symbol_result, diagnostics)
    rr = _decimal_or_none(_first_non_na(diagnostics.get("rr_to_tp2"), _risk_best_rr(symbol_result)))
    required_rr = Decimal("3.0") if mode == "challenge" else Decimal("2.5")
    pullback_status = _display(diagnostics.get("pullback_zone_status")).lower()
    valid_trade_idea = _valid_trade_idea_exists(symbol_result, display.display_status)
    pullback_valid = (
        valid_trade_idea
        or pullback_status in {"valid", "passed"}
        or "pullback_zone" in gates_passed
    )
    rr_valid = valid_trade_idea or (rr is not None and rr >= required_rr and not set(gates_failed) & _rr_failure_gates())
    sweep_detected = (
        bool(symbol_result.sweep_detected)
        or _display(diagnostics.get("execution_sweep_status")) == "passed"
        or "sweep" in gates_passed
    )
    structure_shift_detected = (
        bool(symbol_result.bos_detected or symbol_result.choch_detected)
        or _display(diagnostics.get("confirmation_structure_shift_status")) == "passed"
        or "bos_choch" in gates_passed
    )
    quality_score = _int_or_zero(getattr(symbol_result.setup_quality, "quality_score", 0))
    edge_score = _first_non_na(
        getattr(symbol_result.setup_quality, "profitability_edge_score", NA),
        symbol_result.historical_expectancy,
        symbol_result.expectancy_metrics.get("expectancy") if symbol_result.expectancy_metrics else NA,
    )

    return LifecycleObservation(
        symbol=symbol_result.symbol,
        mode=mode,
        direction=direction,
        readiness_score=display.readiness_score,
        readiness_label=display.readiness_label,
        quality_score=quality_score,
        edge_score=_display(edge_score),
        failed_gate=failed_gate,
        regime_state=_first_non_na(symbol_result.regime_state, symbol_result.regime_diagnostics.get("state")),
        action_label=display.action_label,
        invalidation_reason=_invalidation_reason(
            symbol_result,
            diagnostics,
            failed_gate,
            pullback_failure_type,
            acceptance_status,
        ),
        sweep_detected=sweep_detected,
        structure_shift_detected=structure_shift_detected,
        pullback_valid=pullback_valid,
        rr_valid=rr_valid,
        valid_trade_idea=valid_trade_idea,
        entry_filled=False,
        invalidated=_structural_acceptance_invalidated(pullback_failure_type, acceptance_status, failed_gate),
        expired=failed_gate == "entry_window_expired",
    )


def prioritize_watch_symbols(
    symbols: Sequence[str],
    *,
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    now: str | None = None,
) -> tuple[str, ...]:
    ordered = tuple(dict.fromkeys(_display(symbol).upper() for symbol in symbols if _display(symbol) != NA))
    if not ordered:
        return ()

    with SQLiteSetupLifecycleRepository(database_path) as repository:
        records = repository.get_records_for_symbols(ordered)

    records_by_symbol: dict[str, list[SetupLifecycleRecord]] = {}
    for record in records:
        records_by_symbol.setdefault(record.symbol, []).append(record)

    priority_index = {state: index for index, state in enumerate(WATCH_PRIORITY_STATES)}
    prioritized: list[tuple[int, int, str]] = []
    passthrough: list[tuple[int, str]] = []
    for original_index, symbol in enumerate(ordered):
        symbol_records = records_by_symbol.get(symbol, [])
        if not symbol_records:
            passthrough.append((original_index, symbol))
            continue
        watchable = [record for record in symbol_records if is_watchable_lifecycle_state(record, now=now)]
        if not watchable:
            continue
        best = min(
            watchable,
            key=lambda record: priority_index.get(record.current_state, len(priority_index)),
        )
        prioritized.append((priority_index.get(best.current_state, len(priority_index)), original_index, symbol))

    prioritized.sort()
    passthrough.sort()
    output: list[str] = []
    for _priority, _index, symbol in prioritized:
        if symbol not in output:
            output.append(symbol)
    for _index, symbol in passthrough:
        if symbol not in output:
            output.append(symbol)
    return tuple(output)


def _mode_from_result(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    if symbol_result.valid_strategy_modes:
        return symbol_result.valid_strategy_modes[0].lower()
    if symbol_result.rejected_strategy_modes:
        return symbol_result.rejected_strategy_modes[0].lower()
    mode = _display(diagnostics.get("mode")).lower()
    if mode != NA.lower():
        return mode
    setup_type = _display(getattr(symbol_result.trade_idea, "setup_type", NA))
    for candidate in ("challenge", "swing", "scalp"):
        if candidate in setup_type:
            return candidate
    return NA


def _direction_from_result(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    trade_direction = _display(getattr(symbol_result.trade_idea, "direction", NA)).lower()
    if trade_direction in {"long", "short"}:
        return trade_direction
    for key in ("bias", "direction"):
        value = _display(diagnostics.get(key)).lower()
        if value in {"long", "short"}:
            return value
    return NA


def _valid_trade_idea_exists(symbol_result: ScannerSymbolResult, display_status: str) -> bool:
    trade_idea = symbol_result.trade_idea
    if trade_idea is None:
        return False
    quality_gate = getattr(trade_idea, "quality_gate_result", None)
    if quality_gate is not None and getattr(quality_gate, "passed", True) is not True:
        return False
    return display_status == "valid_setup"


def _risk_best_rr(symbol_result: ScannerSymbolResult) -> Any:
    risk_decision = symbol_result.risk_decision
    return getattr(risk_decision, "best_risk_reward_ratio", NA) if risk_decision is not None else NA


def _invalidation_reason(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    failed_gate: str,
    pullback_failure_type: str = NA,
    acceptance_status: str = NA,
) -> str:
    if acceptance_status == "STRUCTURAL_BREAKDOWN" or failed_gate == "structural_breakdown":
        return "structure broke after body acceptance beyond 0.786"
    if acceptance_status == "BODY_ACCEPTANCE_FAILURE" or failed_gate == "body_acceptance_failure":
        return "body accepted beyond 0.786 invalidation zone"
    if pullback_failure_type == "TOO_DEEP" or failed_gate in {"pullback_too_deep", "pullback_beyond_786"}:
        return "pullback exceeded valid structure depth"
    trade_idea = symbol_result.trade_idea
    for value in (
        getattr(trade_idea, "invalidation", NA) if trade_idea is not None else NA,
        diagnostics.get("invalidation"),
        diagnostics.get("pullback_failure_reason"),
        symbol_result.rejection_reason,
        failed_gate,
    ):
        text = _display(value)
        if text != NA:
            return text
    return NA


def _pullback_failure_type(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    intelligence = symbol_result.pullback_intelligence
    if intelligence is not None:
        return _display(getattr(intelligence.pullback_failure_type, "value", intelligence.pullback_failure_type))
    payload = diagnostics.get("pullback_intelligence")
    if isinstance(payload, Mapping):
        return _display(payload.get("pullback_failure_type"))
    return NA


def _acceptance_status(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    intelligence = symbol_result.pullback_intelligence
    if intelligence is not None:
        return _display(getattr(intelligence, "acceptance_status", NA))
    payload = diagnostics.get("pullback_intelligence")
    if isinstance(payload, Mapping):
        status = _display(payload.get("acceptance_status"))
        if status != NA:
            return status
        structure = payload.get("wick_close_structure")
        if isinstance(structure, Mapping):
            return _display(structure.get("acceptance_status"))
    status = _display(diagnostics.get("acceptance_status"))
    if status != NA:
        return status
    structure = diagnostics.get("wick_close_structure")
    if isinstance(structure, Mapping):
        return _display(structure.get("acceptance_status"))
    return NA


def _structural_acceptance_invalidated(pullback_failure_type: str, acceptance_status: str, failed_gate: str) -> bool:
    return (
        pullback_failure_type == "TOO_DEEP"
        or failed_gate in {"pullback_too_deep", "pullback_beyond_786", "body_acceptance_failure", "structural_breakdown"}
        or acceptance_status in {"BODY_ACCEPTANCE_FAILURE", "STRUCTURAL_BREAKDOWN"}
    )


def _rr_failure_gates() -> set[str]:
    return {
        "missing_rr",
        "missing_target",
        "rr_below_minimum",
        "challenge_rr_below_3",
        "rr_too_low",
    }


def _sequence_values(values: Any) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    return tuple(_display(value) for value in values if _display(value) != NA)


def _first_non_na(*values: Any) -> str:
    for value in values:
        text = _display(value)
        if text != NA:
            return text
    return NA


def _decimal_or_none(value: Any) -> Decimal | None:
    text = _display(value)
    if text == NA:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


def _display(value: Any) -> str:
    if value is None or value == "":
        return NA
    if value == NA:
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


__all__ = [
    "SetupLifecycleService",
    "apply_lifecycle_to_run_result",
    "observation_from_symbol_result",
    "prioritize_watch_symbols",
]
