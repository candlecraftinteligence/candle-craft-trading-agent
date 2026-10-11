"""Prospective reference-stop management; never an exchange fill or PnL claim."""
from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from app.data.candle_integrity import normalize_utc_timestamp
from app.lifecycle.models import SetupLifecycleOutcomeProgress
from app.lifecycle.outcome_policy import StoredPlanGeometry

POLICY = "tp1_entry_zone_midpoint_breakeven_v1"
KEY = "stop_management"


def reference_price(geometry: StoredPlanGeometry) -> Decimal:
    return (geometry.entry_low + geometry.entry_high) / Decimal(2)


def bind_stop_policy(
    progress: SetupLifecycleOutcomeProgress,
    geometry: StoredPlanGeometry,
    *,
    existing: bool,
    decision_timestamp: object,
    evaluated_at: str,
) -> SetupLifecycleOutcomeProgress:
    metadata = json.loads(progress.metadata_json)
    if not isinstance(metadata, dict):
        raise ValueError("invalid_stop_policy_metadata")
    binding = metadata.get(KEY)
    if KEY in metadata:
        if not isinstance(binding, dict) or binding.get("policy") != POLICY:
            raise ValueError("unsupported_stop_management_policy")
        try:
            stored_reference = Decimal(str(binding.get("reference_price")))
        except InvalidOperation as exc:
            raise ValueError("invalid_stop_management_reference") from exc
        if not stored_reference.is_finite() or stored_reference != reference_price(geometry):
            raise ValueError("stop_management_reference_changed")
        if binding.get("reference_type") != "entry_zone_midpoint":
            raise ValueError("invalid_stop_management_reference_type")
        if set(binding) != {
            "policy", "reference_type", "reference_price", "adopted_at",
            "effective_from_candle_open_at", "prior_policy", "actual_fill_or_net_pnl_claim",
        }:
            raise ValueError("incomplete_stop_management_binding")
        normalize_utc_timestamp(binding["adopted_at"], field_name="stop_policy_adopted_at")
        if binding["actual_fill_or_net_pnl_claim"] is not False:
            raise ValueError("invalid_stop_management_claim")
        if binding["prior_policy"] not in (None, "fixed_original_stop"):
            raise ValueError("unsupported_prior_stop_policy")
        boundary = binding["effective_from_candle_open_at"]
        if binding["prior_policy"] == "fixed_original_stop" and boundary is None:
            raise ValueError("missing_stop_policy_adoption_boundary")
        if boundary is not None:
            normalize_utc_timestamp(boundary, field_name="stop_policy_effective_from")
        return progress
    cutoff = None
    if existing:
        cutoff = normalize_utc_timestamp(decision_timestamp, field_name="stop_policy_adoption_cutoff")
        if progress.evaluation_cursor_close_at is not None:
            cutoff = max(cutoff, normalize_utc_timestamp(
                progress.evaluation_cursor_close_at, field_name="stop_policy_prior_cursor"
            ))
    metadata[KEY] = {
        "policy": POLICY,
        "reference_type": "entry_zone_midpoint",
        "reference_price": str(reference_price(geometry)),
        "adopted_at": evaluated_at,
        "effective_from_candle_open_at": cutoff.isoformat() if cutoff is not None else None,
        "prior_policy": "fixed_original_stop" if existing else None,
        "actual_fill_or_net_pnl_claim": False,
    }
    return progress.model_copy(update={"metadata_json": json.dumps(metadata, sort_keys=True, separators=(",", ":"))})


def protection_active(progress: SetupLifecycleOutcomeProgress, candle_open: object) -> bool:
    if progress.tp1_at is None:
        return False
    binding = json.loads(progress.metadata_json).get(KEY)
    if not isinstance(binding, dict) or binding.get("policy") != POLICY:
        return False
    opened = normalize_utc_timestamp(candle_open, field_name="breakeven_candle_open")
    # OHLC does not prove an intrabar TP1-before-retracement sequence.
    if opened < normalize_utc_timestamp(progress.tp1_at, field_name="breakeven_tp1_at"):
        return False
    boundary = binding.get("effective_from_candle_open_at")
    return boundary is None or opened >= normalize_utc_timestamp(boundary, field_name="breakeven_effective_from")


def reference_stop_touched(high: Decimal, low: Decimal, geometry: StoredPlanGeometry) -> bool:
    price = reference_price(geometry)
    return low <= price if geometry.direction == "long" else high >= price


def validate_breakeven_outcome(progress: SetupLifecycleOutcomeProgress, geometry: StoredPlanGeometry) -> None:
    """Reject an unsupported BE label before a public update is reserved."""
    metadata = json.loads(progress.metadata_json)
    if not isinstance(metadata, dict) or KEY not in metadata:
        raise ValueError("missing_stop_management_binding")
    bind_stop_policy(progress, geometry, existing=True, decision_timestamp=progress.last_evaluated_at,
                     evaluated_at=progress.last_evaluated_at)
    if progress.entry_at is None or progress.tp1_at is None or progress.stop_at is None:
        raise ValueError("missing_breakeven_milestones")
    if progress.tp3_at is not None or progress.invalidated_at is not None:
        raise ValueError("conflicting_breakeven_terminal")
    if progress.evaluation_cursor_open_at is None or not protection_active(progress, progress.evaluation_cursor_open_at):
        raise ValueError("breakeven_precedes_protection")
    entry = normalize_utc_timestamp(progress.entry_at, field_name="breakeven_entry")
    tp1 = normalize_utc_timestamp(progress.tp1_at, field_name="breakeven_tp1")
    stop = normalize_utc_timestamp(progress.stop_at, field_name="breakeven_stop")
    if not entry < tp1 < stop:
        raise ValueError("invalid_breakeven_milestone_order")
    if progress.outcome_at != progress.stop_at or progress.evaluation_cursor_close_at != progress.stop_at:
        raise ValueError("breakeven_terminal_cursor_mismatch")
    if progress.tp2_at is not None:
        tp2 = normalize_utc_timestamp(progress.tp2_at, field_name="breakeven_tp2")
        if not tp1 <= tp2 < stop:
            raise ValueError("invalid_breakeven_tp2_order")
