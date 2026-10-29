"""Explicit rejection taxonomy: operational failure is never directional evidence."""
from enum import Enum


class RejectionCategory(str, Enum):
    NO_VALID_SETUP = "NO_VALID_SETUP"
    MISSING_CONFIRMATION = "MISSING_CONFIRMATION"
    INVALID_RISK_REWARD = "INVALID_RISK_REWARD"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    DATA_STALE = "DATA_STALE"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    INTERNAL_ERROR = "INTERNAL_ERROR"


def rejection_category(gate: str, *, provider_failure: bool = False,
                       internal_error: bool = False, valid: bool = False) -> str:
    if valid:
        return "N/A"
    if provider_failure:
        return RejectionCategory.PROVIDER_FAILURE.value
    if internal_error:
        return RejectionCategory.INTERNAL_ERROR.value
    code = str(gate).strip().lower().split(":", 1)[0]
    if code in {"data_stale", "stale_mandatory_candles", "selective_timeframes_stale_or_unverified"}:
        return RejectionCategory.DATA_STALE.value
    if code in {"missing_rr", "rr_below_minimum", "challenge_rr_below_3", "trade_plan_integrity",
                "target_integrity", "missing_stop", "unjustified_structural_stop", "unverified_execution_cost"}:
        return RejectionCategory.INVALID_RISK_REWARD.value
    if code in {"entry_zone_not_touched", "entry_confirmation_missing", "canonical_confirmation_missing",
                "structure_unconfirmed", "missing_confirmation_structure_shift", "confirmation_pending"}:
        return RejectionCategory.MISSING_CONFIRMATION.value
    if (code.startswith("candles_") or code in {"instrument_precision_unverified", "missing_confirmation_candles",
        "missing_stop_volatility", "selective_candle_provenance", "selective_timeframe_hierarchy_mismatch",
        "selective_structure_analysis_required", "not_enough_candles", "atr_unavailable", "essential_candle_freshness"}):
        return RejectionCategory.DATA_UNAVAILABLE.value
    return RejectionCategory.NO_VALID_SETUP.value
