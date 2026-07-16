from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data.candle_integrity import CausalCandle
from app.data.dtos import NA
from app.lifecycle.models import SetupLifecycleOutcomeProgress, SetupLifecycleRecord


@dataclass(frozen=True)
class StoredPlanGeometry:
    direction: str
    entry_low: Decimal
    entry_high: Decimal
    stop_loss: Decimal
    targets: tuple[Decimal, Decimal, Decimal]


def canonical_plan_identity(record: SetupLifecycleRecord) -> str:
    values = (
        record.lifecycle_id,
        record.symbol,
        record.mode,
        record.direction,
        record.entry_low,
        record.entry_high,
        record.stop_loss,
        record.tp1,
        record.tp2,
        record.tp3,
        record.invalidation_logic,
    )
    digest = hashlib.sha256("\x1f".join(_text(value) for value in values).encode("utf-8")).hexdigest()
    return f"plan-{digest}"


def stored_plan_geometry(record: SetupLifecycleRecord) -> StoredPlanGeometry:
    direction = _text(record.direction).lower()
    if direction not in {"long", "short"}:
        raise ValueError(f"unsupported_direction:{record.direction}")
    values = {
        "entry_low": _required_decimal(record.entry_low, "entry_low"),
        "entry_high": _required_decimal(record.entry_high, "entry_high"),
        "stop_loss": _required_decimal(record.stop_loss, "stop_loss"),
        "tp1": _required_decimal(record.tp1, "tp1"),
        "tp2": _required_decimal(record.tp2, "tp2"),
        "tp3": _required_decimal(record.tp3, "tp3"),
    }
    invalidation = _text(record.invalidation_logic or record.invalidation_reason)
    if invalidation == NA:
        raise ValueError("missing_invalidation")
    low = values["entry_low"]
    high = values["entry_high"]
    stop = values["stop_loss"]
    targets = (values["tp1"], values["tp2"], values["tp3"])
    if low > high:
        raise ValueError("entry_low_above_entry_high")
    if direction == "long" and not (stop < low <= high < targets[0] < targets[1] < targets[2]):
        raise ValueError("invalid_long_level_order")
    if direction == "short" and not (stop > high >= low > targets[0] > targets[1] > targets[2]):
        raise ValueError("invalid_short_level_order")
    return StoredPlanGeometry(
        direction=direction,
        entry_low=low,
        entry_high=high,
        stop_loss=stop,
        targets=targets,
    )


def candle_range(causal: CausalCandle) -> tuple[Decimal, Decimal]:
    high = _required_decimal(_field(causal.source, "high"), "candle_high")
    low = _required_decimal(_field(causal.source, "low"), "candle_low")
    if high < low:
        raise ValueError(
            "candle_integrity:invalid_ohlc_range "
            f"open={causal.open_timestamp.isoformat()} high={high} low={low}"
        )
    return high, low


def entry_touched(high: Decimal, low: Decimal, geometry: StoredPlanGeometry) -> bool:
    return high >= geometry.entry_low and low <= geometry.entry_high


def stop_touched(high: Decimal, low: Decimal, geometry: StoredPlanGeometry) -> bool:
    if geometry.direction == "long":
        return low <= geometry.stop_loss
    return high >= geometry.stop_loss


def newly_touched_targets(
    high: Decimal,
    low: Decimal,
    geometry: StoredPlanGeometry,
    progress: SetupLifecycleOutcomeProgress,
) -> tuple[tuple[int, Decimal], ...]:
    reached: list[tuple[int, Decimal]] = []
    for target_number, target in enumerate(geometry.targets, start=1):
        if getattr(progress, f"tp{target_number}_at") is not None:
            continue
        touched = high >= target if geometry.direction == "long" else low <= target
        if touched:
            reached.append((target_number, target))
    return tuple(reached)


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _required_decimal(value: Any, name: str) -> Decimal:
    if value in (None, "", NA):
        raise ValueError(f"missing_{name}")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid_{name}") from exc
    if not number.is_finite():
        raise ValueError(f"non_finite_{name}")
    return number


def _text(value: Any) -> str:
    if value is None or value == "":
        return NA
    text = str(value).strip()
    return text if text else NA


__all__ = [
    "StoredPlanGeometry",
    "candle_range",
    "canonical_plan_identity",
    "entry_touched",
    "newly_touched_targets",
    "stop_touched",
    "stored_plan_geometry",
]
