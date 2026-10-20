"""Read-only probes of existing scanner and microstructure consumers.

These functions do not change gates. A helper result is not a production-aggregator result.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.analytics.derivatives_enrichment import DerivativesEnrichmentResult
from app.context.models import ContextStatus
from app.microstructure.liquidation_aggregator import SymbolLiquidationAggregator
from app.pipeline.scanner_runner import (
    _OptionalMarketData,
    _derivatives_enrichment_input,
    _derivatives_rejection,
    _previous_open_interest_from_history,
)


def derivatives_timestamp_probe() -> dict[str, object]:
    """Show that current funding and OI numbers reach rejection without their source times."""

    stale = datetime(2020, 1, 1, tzinfo=UTC)
    future = datetime(2030, 1, 1, tzinfo=UTC)
    decision = datetime(2026, 10, 18, 12, tzinfo=UTC)
    history = (
        {"open_interest": Decimal("9"), "timestamp": decision - timedelta(hours=8)},
        {"open_interest": Decimal("11"), "timestamp": future},
    )
    optional = _OptionalMarketData(
        funding={"funding_rate": Decimal("0.01"), "timestamp": stale},
        open_interest={"open_interest": Decimal("11"), "timestamp": future},
        previous_open_interest=_previous_open_interest_from_history(history),
        open_interest_history=history,
        funding_history=({"funding_rate": Decimal("0.01"), "timestamp": stale},),
        long_short_ratio=Decimal("1.2"),
    )
    produced = _derivatives_enrichment_input(
        symbol="BTCUSDT",
        exchange="binance_futures",
        candles=(),
        current_price=Decimal("100"),
        optional_data=optional,
        interval="15m",
    )
    fields = set(produced.model_fields)
    rejection = _derivatives_rejection(
        "long",
        DerivativesEnrichmentResult(
            symbol="BTCUSDT",
            exchange="binance_futures",
            funding_status="extreme_positive",
            oi_direction="rising",
            crowding_risk="high",
            squeeze_risk="balanced",
            supports_long=False,
        ),
    )
    return {
        "consumer": "ScannerRunner._derivatives_enrichment_input and _derivatives_rejection",
        "previous_open_interest_selector": "history[-2]",
        "previous_open_interest": str(produced.previous_open_interest),
        "current_funding_rate": str(produced.current_funding_rate),
        "current_open_interest": str(produced.current_open_interest),
        "source_timestamps_on_enrichment_input": "timestamp" in fields or "measurement_at" in fields,
        "stale_funding_timestamp": stale.isoformat(),
        "future_open_interest_timestamp": future.isoformat(),
        "decision_cutoff": decision.isoformat(),
        "rejection_without_timestamp_check": rejection,
        "production_incident": "not established",
        "gate_change": "none",
        "gap": "numeric funding and open interest can reach REJECTED_BY_DERIVATIVES without a source-time check",
    }


def production_liquidation_coverage_probe() -> dict[str, object]:
    """Exercise the production aggregator. This is not liquidation_observation()."""

    start = datetime(2026, 10, 18, 12, tzinfo=UTC)
    disconnected = SymbolLiquidationAggregator("BTCUSDT", stale_after_seconds=30)
    never_connected = disconnected.snapshot(as_of=start)
    disconnected.mark_connected(start)
    disconnected.mark_disconnected(start + timedelta(minutes=1))
    gapped = disconnected.snapshot(as_of=start + timedelta(minutes=2))
    covered = SymbolLiquidationAggregator("BTCUSDT", stale_after_seconds=30)
    covered.mark_connected(start - timedelta(minutes=20))
    empty = covered.snapshot(as_of=start)
    window = empty.windows["15m"]
    return {
        "consumer": "SymbolLiquidationAggregator",
        "helper_liquidation_observation_is_not_this_consumer": True,
        "disconnected_status": never_connected.status.value,
        "disconnected_reason": never_connected.reason,
        "gapped_status": gapped.status.value,
        "gapped_reason": gapped.reason,
        "gapped_disconnect_count": gapped.disconnect_count,
        "covered_empty_status": window.status.value,
        "covered_empty_complete": window.coverage_complete,
        "covered_empty_event_count": window.event_count,
        "covered_empty_is_observed_zero": window.coverage_complete and window.event_count == 0,
        "connected_observed_at_is_snapshot_clock": empty.observed_at == start,
        "fresh_snapshot_clock_is_not_an_event_time": True,
        "production_incident": "not established",
        "unavailable_status": ContextStatus.UNAVAILABLE.value,
    }
