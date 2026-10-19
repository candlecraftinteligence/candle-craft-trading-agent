"""Cadence notes and a finite offline microbenchmark. Not a Runtime capacity claim."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.data.candle_freshness import assess_candle_freshness
from app.watch_supervisor import schedule_after_iteration

from research.market_data_foundation.constants import (
    CLI_DEFAULT_WATCH_INTERVAL_SECONDS,
    FIVE_MINUTE_CYCLES_PER_DAY,
    MONTH_LENGTH_DAYS,
)


def cadence_contract() -> dict[str, Any]:
    decision = schedule_after_iteration(
        scheduled_start_monotonic=0.0,
        actual_start_monotonic=0.0,
        finished_monotonic=12.0,
        interval_seconds=CLI_DEFAULT_WATCH_INTERVAL_SECONDS,
    )
    overrun = schedule_after_iteration(
        scheduled_start_monotonic=0.0,
        actual_start_monotonic=0.0,
        finished_monotonic=90.0,
        interval_seconds=CLI_DEFAULT_WATCH_INTERVAL_SECONDS,
    )
    return {
        "scheduler": "fixed_start_to_start",
        "cli_default_interval_seconds": CLI_DEFAULT_WATCH_INTERVAL_SECONDS,
        "five_minute_interval_is_code_default": False,
        "example_duration_seconds": decision.duration_seconds,
        "example_sleep_seconds": decision.sleep_seconds,
        "overrun_missed_intervals": overrun.missed_interval_count,
        "overrun_next_is_not_finish_plus_interval": overrun.next_scheduled_monotonic != 90.0 + 60.0,
        "runtime_cadence": "unverified",
    }


def freshness_microbenchmark(*, symbols: int = 30, repeats: int = 3) -> dict[str, Any]:
    """Cold means the first pass. Warm means the next passes over the same objects.

    This does not fetch, write a database, or call the scanner.
    """

    candles = _series()
    decision = datetime(1970, 1, 1, 0, 10, tzinfo=UTC)
    durations: list[float] = []
    statuses: list[str] = []
    for _pass in range(repeats):
        started = time.perf_counter()
        for index in range(symbols):
            result = assess_candle_freshness(
                candles,
                symbol=f"SYM{index}USDT",
                timeframe="1m",
                role="benchmark",
                decision_timestamp=decision,
            )
            if _pass == 0:
                statuses.append(result.status.value)
        durations.append(time.perf_counter() - started)
    return {
        "label": "dev_measured_microbenchmark",
        "symbols": symbols,
        "repeats": repeats,
        "cold_seconds": durations[0],
        "warm_seconds": durations[1:],
        "statuses": sorted(set(statuses)),
        "scanner_invoked": False,
        "candles_manufactured_to_force_gate_pass": False,
        "runtime_capacity": "unverified",
    }


def project_acquisition(
    *,
    measured_latency_seconds: list[float],
    requests_per_cycle: int,
    concurrency: int = 2,
) -> dict[str, Any]:
    if not measured_latency_seconds or requests_per_cycle < 1:
        return {
            "label": "insufficient_evidence",
            "projected_cycle_seconds": None,
            "within_300_seconds": None,
        }
    mean = sum(measured_latency_seconds) / len(measured_latency_seconds)
    workers = max(1, concurrency)
    projected = (mean * requests_per_cycle) / workers
    return {
        "label": "projected_from_sample_mean",
        "sample_size": len(measured_latency_seconds),
        "mean_latency_seconds": mean,
        "requests_per_cycle_assumption": requests_per_cycle,
        "concurrency_assumption": workers,
        "projected_cycle_seconds": projected,
        "within_300_seconds": projected <= 300,
        "runtime_capacity": "unverified",
        "note": "Mean of a tiny sample is descriptive. It is not a latency percentile.",
    }


def usage_projection(
    *,
    measured_calls_per_cycle: int | None,
    measured_weight_per_cycle: str | None,
) -> dict[str, Any]:
    cycles = FIVE_MINUTE_CYCLES_PER_DAY
    month_days = MONTH_LENGTH_DAYS
    daily = None if measured_calls_per_cycle is None else measured_calls_per_cycle * cycles
    monthly = None if daily is None else daily * month_days
    return {
        "assumption": "288 cycles/day only if a 300-second cadence is actually configured",
        "code_default_interval_seconds": CLI_DEFAULT_WATCH_INTERVAL_SECONDS,
        "code_default_cycles_per_day": 86400 / CLI_DEFAULT_WATCH_INTERVAL_SECONDS,
        "month_length_days": month_days,
        "measured_calls_per_cycle": measured_calls_per_cycle,
        "projected_calls_per_day_at_300s": daily,
        "projected_calls_per_month_at_300s": monthly,
        "measured_weight_per_cycle": measured_weight_per_cycle or "not reported by the clients",
        "billing_credits": "request count is not billing credits",
        "pricing": "not assessed from a vendor invoice",
    }


def _series() -> tuple[dict[str, Decimal | int], ...]:
    rows = []
    start = datetime(1970, 1, 1, tzinfo=UTC)
    for index in range(10):
        opened = start + timedelta(minutes=index)
        rows.append(
            {
                "timestamp": int(opened.timestamp() * 1000),
                "open": Decimal("1"),
                "high": Decimal("1"),
                "low": Decimal("1"),
                "close": Decimal("1"),
                "volume": Decimal("1"),
            }
        )
    return tuple(rows)
