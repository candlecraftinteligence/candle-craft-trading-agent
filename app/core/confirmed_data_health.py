from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


# These domains are optional at the final CONFIRMED data-health boundary. Some of
# them still affect trust, derivatives, setup-quality, or opportunity scores; the
# existing strategy and scoring gates remain authoritative for those effects.
_OPTIONAL_FIELDS = frozenset(
    {
        "btc_context",
        "btc_d_context",
        "catalyst",
        "current_open_interest",
        "cvd",
        "derivatives",
        "derivatives_summary",
        "event_risk_context",
        "funding",
        "funding_history",
        "funding_rate",
        "liquidation_data",
        "liquidation_heatmap",
        "liquidity",
        "liquidity_above",
        "liquidity_below",
        "long_short_ratio",
        "narrative",
        "open_interest",
        "open_interest_change_pct",
        "open_interest_history",
        "orderflow_summary",
        "poc",
        "previous_open_interest",
        "price_change_percentage",
        "price_direction",
        "price_oi_relationship",
        "sector_rotation",
        "ticker",
        "token_classification",
        "usdt_d_context",
        "value_area_high",
        "value_area_low",
        "volume",
        "volume_profile",
        "volume_z_score",
        "weekend_filter",
    }
)

_OPTIONAL_PREFIXES = (
    "funding_history_",
    "open_interest_history_",
    "volume_profile_",
)


@dataclass(frozen=True)
class ConfirmedDataHealth:
    required_missing: tuple[str, ...] = ()
    optional_missing: tuple[str, ...] = ()
    required_unverified: tuple[str, ...] = ()
    optional_unverified: tuple[str, ...] = ()

    @property
    def blocked(self) -> bool:
        return bool(self.required_missing or self.required_unverified)

    @property
    def blocking_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.required_missing:
            reasons.append(f"required_data_missing:{','.join(self.required_missing)}")
        if self.required_unverified:
            reasons.append(f"required_data_unverified:{','.join(self.required_unverified)}")
        return tuple(reasons)

    @property
    def diagnostic_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.optional_missing:
            reasons.append(f"optional_data_missing:{','.join(self.optional_missing)}")
        if self.optional_unverified:
            reasons.append(f"optional_data_unverified:{','.join(self.optional_unverified)}")
        return tuple(reasons)


def confirmed_data_health_for_symbol(
    symbol_result: Any,
    *,
    diagnostics: Mapping[str, Any] | None = None,
) -> ConfirmedDataHealth:
    """Classify all data-health sources used by lifecycle/public CONFIRMED.

    Unknown fields intentionally fail closed. A field becomes optional only when
    its current producer and consumers establish that it is enrichment or an
    input already governed by independent scoring/quality gates.
    """

    score_result = getattr(symbol_result, "score_result", None)
    diagnostics = diagnostics or {}
    return classify_confirmed_data_health(
        missing_values=(
            getattr(symbol_result, "missing_data", ()),
            getattr(symbol_result, "strategy_missing_data", ()),
            getattr(symbol_result, "derivatives_missing_data", ()),
            getattr(score_result, "missing_data", ()) if score_result is not None else (),
            diagnostics.get("missing_data"),
        ),
        unverified_values=(
            getattr(symbol_result, "unverified_data", ()),
            getattr(symbol_result, "strategy_unverified_data", ()),
            getattr(symbol_result, "derivatives_unverified_data", ()),
            getattr(score_result, "unverified_data", ()) if score_result is not None else (),
            diagnostics.get("unverified_data"),
        ),
    )


def classify_confirmed_data_health(
    *,
    missing_values: Sequence[Any] = (),
    unverified_values: Sequence[Any] = (),
) -> ConfirmedDataHealth:
    required_missing, optional_missing = _classify_values(missing_values)
    required_unverified, optional_unverified = _classify_values(unverified_values)
    return ConfirmedDataHealth(
        required_missing=required_missing,
        optional_missing=optional_missing,
        required_unverified=required_unverified,
        optional_unverified=optional_unverified,
    )


def _classify_values(values: Sequence[Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    required: list[str] = []
    optional: list[str] = []
    for raw_value in values:
        for value in _sequence_values(raw_value):
            field = _field_name(value)
            if not field:
                continue
            target = optional if _is_optional_field(field) else required
            if field not in target:
                target.append(field)
    return tuple(required), tuple(optional)


def _sequence_values(value: Any) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, Mapping):
        return tuple(value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return tuple(value)
    return (value,)


def _field_name(value: Any) -> str:
    text = str(value).strip()
    if not text:
        return ""
    label = text.split(":", 1)[0]
    label = re.split(
        r"\s+is\s+(?:n/?a|missing|unverified)\b",
        label,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def _is_optional_field(field: str) -> bool:
    if field in _OPTIONAL_FIELDS:
        return True
    return field.startswith(_OPTIONAL_PREFIXES)


__all__ = [
    "ConfirmedDataHealth",
    "classify_confirmed_data_health",
    "confirmed_data_health_for_symbol",
]
