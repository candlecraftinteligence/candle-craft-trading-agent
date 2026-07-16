from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


DEFAULT_CONFIGURED_MINIMUM_RR = Decimal("2.5")
SCALP_HARD_MINIMUM_RR = Decimal("2.5")
SWING_HARD_MINIMUM_RR = Decimal("2.5")
CHALLENGE_HARD_MINIMUM_RR = Decimal("3.0")


class MinimumRRConfigurationError(ValueError):
    """Raised when the configured global minimum-RR value is unsafe or invalid."""


@dataclass(frozen=True)
class MinimumRRPolicy:
    mode: str
    configured_global_minimum_rr: Decimal
    hard_mode_floor: Decimal
    effective_minimum_rr: Decimal


def validate_configured_minimum_rr(value: Any) -> Decimal:
    """Return a finite Decimal configuration that cannot weaken the 2.5R base floor."""

    try:
        minimum_rr = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError) as exc:
        raise MinimumRRConfigurationError(
            "configured minimum RR must be a finite decimal greater than or equal to 2.5"
        ) from exc
    if not minimum_rr.is_finite() or minimum_rr < DEFAULT_CONFIGURED_MINIMUM_RR:
        raise MinimumRRConfigurationError(
            "configured minimum RR must be a finite decimal greater than or equal to 2.5"
        )
    return minimum_rr


def hard_mode_minimum_rr(mode: Any) -> Decimal:
    mode_name = _mode_name(mode)
    if mode_name == "challenge":
        return CHALLENGE_HARD_MINIMUM_RR
    if mode_name == "scalp":
        return SCALP_HARD_MINIMUM_RR
    if mode_name == "swing":
        return SWING_HARD_MINIMUM_RR
    raise ValueError(f"unsupported minimum-RR mode: {mode_name!r}")


def minimum_rr_policy(configured_global_minimum_rr: Any, mode: Any) -> MinimumRRPolicy:
    configured = validate_configured_minimum_rr(configured_global_minimum_rr)
    mode_name = _mode_name(mode)
    hard_floor = hard_mode_minimum_rr(mode_name)
    return MinimumRRPolicy(
        mode=mode_name,
        configured_global_minimum_rr=configured,
        hard_mode_floor=hard_floor,
        effective_minimum_rr=max(configured, hard_floor),
    )


def candidate_rr_meets_minimum(candidate_rr: Any, policy: MinimumRRPolicy) -> bool:
    candidate = _candidate_decimal(candidate_rr)
    return candidate is not None and candidate >= policy.effective_minimum_rr


def rr_rejection_reason(candidate_rr: Any, policy: MinimumRRPolicy) -> str:
    candidate = _candidate_decimal(candidate_rr)
    policy_details = (
        f"configured_global_minimum_rr={_display(policy.configured_global_minimum_rr)};"
        f"hard_mode_floor={_display(policy.hard_mode_floor)};"
        f"effective_minimum_rr={_display(policy.effective_minimum_rr)};"
        f"mode={policy.mode}"
    )
    if candidate is None:
        return f"missing_rr:candidate_rr=N/A;{policy_details}"
    if not candidate_rr_meets_minimum(candidate, policy):
        return f"rr_below_minimum:candidate_rr={_display(candidate)};{policy_details}"
    return "N/A"


def _mode_name(mode: Any) -> str:
    return str(getattr(mode, "value", mode)).strip().lower()


def _candidate_decimal(value: Any) -> Decimal | None:
    if value is None or str(value).strip().upper() in {"", "N/A", "NA", "NONE", "NAN"}:
        return None
    try:
        candidate = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError, AttributeError):
        return None
    return candidate if candidate.is_finite() else None


def _display(value: Decimal) -> str:
    return format(value, "f")


__all__ = [
    "CHALLENGE_HARD_MINIMUM_RR",
    "DEFAULT_CONFIGURED_MINIMUM_RR",
    "MinimumRRConfigurationError",
    "MinimumRRPolicy",
    "SCALP_HARD_MINIMUM_RR",
    "SWING_HARD_MINIMUM_RR",
    "candidate_rr_meets_minimum",
    "hard_mode_minimum_rr",
    "minimum_rr_policy",
    "rr_rejection_reason",
    "validate_configured_minimum_rr",
]
