from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.context.macro_events import (
    MacroCalendarStatus,
    MacroEventRiskContext,
    MacroEventRiskSnapshot,
    MacroSourceStatus,
    MacroVerificationStatus,
)


def apply_macro_event_context_to_symbol_result(
    symbol_result: Any,
    snapshot: MacroEventRiskSnapshot,
) -> Any:
    """Attach optional research metadata after all trading decisions are complete."""

    context = snapshot.symbol_context()
    strategy_results = {
        mode: _with_health_in_strategy_result(strategy_result, context, snapshot)
        for mode, strategy_result in symbol_result.strategy_results.items()
    }
    missing_data, unverified_data = _macro_health_values(
        symbol_result.missing_data,
        symbol_result.unverified_data,
        context,
        snapshot,
    )
    strategy_missing, strategy_unverified = _macro_health_values(
        symbol_result.strategy_missing_data,
        symbol_result.strategy_unverified_data,
        context,
        snapshot,
    )
    return symbol_result.model_copy(
        update={
            "event_risk_context": context,
            "missing_data": missing_data,
            "unverified_data": unverified_data,
            "strategy_missing_data": strategy_missing,
            "strategy_unverified_data": strategy_unverified,
            "strategy_results": strategy_results,
        }
    )


def _with_health_in_strategy_result(
    strategy_result: Any,
    context: MacroEventRiskContext | None,
    snapshot: MacroEventRiskSnapshot,
) -> Any:
    missing_data, unverified_data = _macro_health_values(
        strategy_result.missing_data,
        strategy_result.unverified_data,
        context,
        snapshot,
    )
    return strategy_result.model_copy(
        update={
            "missing_data": missing_data,
            "unverified_data": unverified_data,
        }
    )


def _macro_health_values(
    missing_values: Sequence[str],
    unverified_values: Sequence[str],
    context: MacroEventRiskContext | None,
    snapshot: MacroEventRiskSnapshot,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    missing = [value for value in missing_values if not _is_macro_health_value(value)]
    unverified = [
        value for value in unverified_values if not _is_macro_health_value(value)
    ]
    if context is None:
        missing.append(f"event_risk_context: N/A ({_unavailable_reason(snapshot)})")
    elif context.status == MacroVerificationStatus.UNVERIFIED:
        unverified.append(
            "event_risk_context: Unverified "
            f"({_degraded_reason(snapshot, context.calendar_status)})"
        )
    return _dedupe(missing), _dedupe(unverified)


def _unavailable_reason(snapshot: MacroEventRiskSnapshot) -> str:
    reasons = tuple(
        f"{source.source}={source.reason or source.status.value.lower()}"
        for source in snapshot.sources
        if source.status == MacroSourceStatus.UNAVAILABLE
    )
    return "; ".join(reasons) if reasons else "macro calendar unavailable"


def _degraded_reason(
    snapshot: MacroEventRiskSnapshot,
    status: MacroCalendarStatus,
) -> str:
    degraded = tuple(
        f"{source.source}={source.status.value.lower()}"
        for source in snapshot.sources
        if source.status != MacroSourceStatus.VERIFIED
    )
    prefix = "stale macro calendar" if status == MacroCalendarStatus.STALE else "partial macro calendar"
    return f"{prefix}: {', '.join(degraded)}" if degraded else prefix


def _is_macro_health_value(value: Any) -> bool:
    return str(value).split(":", 1)[0].strip().lower() == "event_risk_context"


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return tuple(result)


__all__ = ["apply_macro_event_context_to_symbol_result"]
