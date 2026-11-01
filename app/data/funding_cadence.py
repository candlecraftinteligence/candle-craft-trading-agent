"""Binance funding schedule evidence, separate from settled funding values."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from app.data.derivatives_source_evidence import QUALITY_VERIFIED, RATE_SETTLED_FUNDING


def attach_funding_cadence(instrument, evidence, snapshot):
    if instrument is None or getattr(instrument, "quality", None) != QUALITY_VERIFIED or not snapshot:
        return instrument
    try:
        at, adjustments, schedules = snapshot
        if not isinstance(at, datetime) or at.tzinfo is None or not isinstance(adjustments, list) or not isinstance(schedules, list):
            raise ValueError("invalid schedule envelope")
    except (TypeError, ValueError):
        return replace(instrument, funding_interval_ms=None, funding_cadence_source="malformed_funding_schedule")
    symbol = instrument.symbol
    matches = [r for r in adjustments if isinstance(r, dict) and r.get("symbol") == symbol]
    explicit = None
    if len(matches) == 1:
        hours = matches[0].get("fundingIntervalHours")
        if type(hours) is int and 1 <= hours <= 24:
            explicit = hours*3600000
    if len(matches) > 1 or (matches and explicit is None):
        return replace(instrument, funding_interval_ms=None, funding_cadence_source="conflicting_funding_info")
    rows = [r for r in schedules if isinstance(r, dict) and r.get("symbol") == symbol]
    settled = [o.metric_time.at for slot in ("funding_rate", "funding_history")
        for o in getattr((evidence or {}).get(slot), "observations", ())
        if o.identity.requested_symbol == symbol and o.semantic_quality == QUALITY_VERIFIED
        and o.rate_kind == RATE_SETTLED_FUNDING and o.metric_time.at is not None
        and o.metric_time.at <= at]
    scheduled, source_at = None, None
    if len(rows) == 1 and settled:
        row = rows[0]
        try:
            if type(row.get("time")) is not int or type(row.get("nextFundingTime")) is not int:
                raise ValueError("invalid schedule timestamps")
            source_at = datetime.fromtimestamp(row["time"]/1000, UTC)
            next_at = datetime.fromtimestamp(row["nextFundingTime"]/1000, UTC)
            last_at = max(settled)
            interval = int((next_at-last_at).total_seconds()*1000)
            if (last_at <= source_at <= at < next_at and 0 <= (at-source_at).total_seconds() <= 60
                    and 0 < interval <= 86400000 and interval % 3600000 == 0):
                scheduled = interval
        except (ValueError, OverflowError, OSError):
            pass
    if explicit is not None and scheduled is not None and explicit != scheduled:
        return replace(instrument, funding_interval_ms=None, funding_cadence_source="conflicting_funding_schedule")
    interval = explicit or scheduled
    return replace(instrument, funding_interval_ms=interval,
        funding_cadence_source=("/fapi/v1/fundingInfo" if explicit else
            "/fapi/v1/premiumIndex:nextFundingTime-minus-verified-settlement" if scheduled else "funding_cadence_not_reported"),
        funding_cadence_acquired_at=at, funding_cadence_source_at=source_at)
