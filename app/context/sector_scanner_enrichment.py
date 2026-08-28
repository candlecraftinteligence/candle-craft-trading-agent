from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.context.sector_rotation import SectorContextStatus, SectorRotationContext


def apply_sector_context_to_symbol_result(
    symbol_result: Any,
    context: SectorRotationContext,
) -> Any:
    """Attach research context without recalculating any trading decision field."""

    strategy_results = {
        mode: _with_context_in_strategy_result(strategy_result, context)
        for mode, strategy_result in symbol_result.strategy_results.items()
    }
    missing_data, unverified_data = _sector_health_values(
        symbol_result.missing_data,
        symbol_result.unverified_data,
        context,
    )
    strategy_missing, strategy_unverified = _sector_health_values(
        symbol_result.strategy_missing_data,
        symbol_result.strategy_unverified_data,
        context,
    )
    return symbol_result.model_copy(
        update={
            "sector_rotation": context,
            "missing_data": missing_data,
            "unverified_data": unverified_data,
            "strategy_missing_data": strategy_missing,
            "strategy_unverified_data": strategy_unverified,
            "strategy_results": strategy_results,
            "formatted_strategy_output": _replace_sector_display(
                symbol_result.formatted_strategy_output,
                context.display_text(),
            ),
        }
    )


def _with_context_in_strategy_result(
    strategy_result: Any,
    context: SectorRotationContext,
) -> Any:
    display = context.display_text()
    setup_updates: dict[str, Any] = {}
    for mode in ("challenge", "swing", "scalp"):
        setup = getattr(strategy_result, mode)
        setup_updates[mode] = setup.model_copy(
            update={
                "rotation": setup.rotation.model_copy(
                    update={"sector_rotation": display}
                )
            }
        )
    missing_data, unverified_data = _sector_health_values(
        strategy_result.missing_data,
        strategy_result.unverified_data,
        context,
    )
    formatted = strategy_result.formatted_output
    setup_updates.update(
        {
            "missing_data": missing_data,
            "unverified_data": unverified_data,
            "formatted_output": formatted.model_copy(
                update={
                    "challenge_setup": _replace_sector_display(
                        formatted.challenge_setup, display
                    ),
                    "swing_setup": _replace_sector_display(
                        formatted.swing_setup, display
                    ),
                    "scalp_setup": _replace_sector_display(
                        formatted.scalp_setup, display
                    ),
                    "full_text": _replace_sector_display(formatted.full_text, display),
                }
            ),
        }
    )
    return strategy_result.model_copy(update=setup_updates)


def _sector_health_values(
    missing_values: Sequence[str],
    unverified_values: Sequence[str],
    context: SectorRotationContext,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    missing = [value for value in missing_values if not _is_sector_health_value(value)]
    unverified = [
        value for value in unverified_values if not _is_sector_health_value(value)
    ]
    if context.status in (SectorContextStatus.STALE, SectorContextStatus.ERROR):
        unverified.append(
            f"sector_rotation: Unverified ({context.reason or context.status.value.lower()})"
        )
    elif context.status != SectorContextStatus.VERIFIED:
        missing.append(
            f"sector_rotation: N/A ({context.reason or context.status.value.lower()})"
        )
    return _dedupe(missing), _dedupe(unverified)


def _replace_sector_display(value: str, display: str) -> str:
    if not isinstance(value, str):
        return value
    return value.replace(
        "• Sector rotation: [N/A].",
        f"• Sector rotation: [{display}].",
    )


def _is_sector_health_value(value: Any) -> bool:
    return str(value).split(":", 1)[0].strip().lower() == "sector_rotation"


def _dedupe(values: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return tuple(result)


__all__ = ["apply_sector_context_to_symbol_result"]
