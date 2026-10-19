"""Offline comparisons and the configured-universe request plan. No network."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Sequence

from app.data.candle_freshness import assess_candle_freshness
from app.data.timeframes import resample_ohlcv_candles
from app.pipeline.scanner_runner import (
    DIRECT_STRATEGY_TIMEFRAMES,
    SYNTHETIC_2D_SOURCE_TIMEFRAME,
    ScannerRunConfig,
    _synthetic_2d_source_limit,
    _timeframe_fetch_limit,
)

from research.market_data_foundation.top100 import select_shadow_sample


def configured_request_plan(symbols: Sequence[str]) -> dict[str, object]:
    """Count the scanner's configured public reads. This does not perform them."""

    config = ScannerRunConfig.model_validate(
        {
            "symbols": [{"symbol": symbol} for symbol in symbols] or [{"symbol": "BTCUSDT"}],
            "exchange": "binance",
            "interval": "15m",
            "candle_limit": 250,
            "dry_run_alerts": True,
            "account_equity": Decimal("10000"),
            "risk_per_trade_pct": Decimal("1"),
        }
    )
    warnings: list[str] = []
    timeframes = ("15m", *DIRECT_STRATEGY_TIMEFRAMES, SYNTHETIC_2D_SOURCE_TIMEFRAME)
    per_symbol: list[dict[str, object]] = []
    for symbol in symbols:
        reads = []
        for timeframe in dict.fromkeys(timeframes):
            if timeframe == SYNTHETIC_2D_SOURCE_TIMEFRAME:
                limit = _synthetic_2d_source_limit(config, warnings)
            else:
                limit = _timeframe_fetch_limit(config, timeframe, warnings)
            reads.append({"timeframe": timeframe, "limit": limit, "kind": "klines"})
        reads.extend(
            {"timeframe": label, "limit": None, "kind": "optional"}
            for label in (
                "ticker",
                "funding_rate",
                "funding_history",
                "open_interest",
                "open_interest_history",
                "long_short_ratio",
            )
        )
        per_symbol.append({"symbol": symbol, "reads": reads, "read_count": len(reads)})
    total = sum(item["read_count"] for item in per_symbol)
    return {
        "label": "synthetic_workload",
        "executed": False,
        "candle_limit": config.candle_limit,
        "strategy_timeframes": list(DIRECT_STRATEGY_TIMEFRAMES),
        "synthetic_2d_source": SYNTHETIC_2D_SOURCE_TIMEFRAME,
        "symbols": len(symbols),
        "requests": total,
        "per_symbol": per_symbol,
        "warnings": warnings,
        "network": "not_executed",
    }


def offline_metric_comparison() -> dict[str, object]:
    """Representative price, volume, open interest, and funding rows. All synthetic."""

    queued = [(f"S{index}USDT", str(1000 - index)) for index in range(6)]
    queued.extend([("BTCUSDT", "5000"), ("ETHUSDT", "4000")])
    selected = select_shadow_sample(queued)["selected"]
    rows = []
    for index, symbol in enumerate(selected):
        open_ms = 1_700_000_000_000 + index * 900_000
        binance_price = Decimal("100") + Decimal(index)
        bybit_price = binance_price + Decimal("0.5")
        rows.append(
            {
                "symbol": symbol,
                "open_ms": open_ms,
                "label": "synthetic",
                "binance_close_usdt": format(binance_price, "f"),
                "bybit_close_usdt": format(bybit_price, "f"),
                "close_delta_usdt": format(bybit_price - binance_price, "f"),
                "binance_base_volume": "10",
                "bybit_base_volume": "11",
                "binance_open_interest": "2.5",
                "bybit_open_interest": "2.5",
                "open_interest_unit": "unverified",
                "binance_funding_fraction": "0.0001",
                "bybit_funding_fraction": "0.0002",
                "usdt_converted_to_usd": False,
                "delta_is_error": False,
            }
        )
    return {
        "label": "synthetic",
        "captured": "unavailable_raw_bodies_were_not_retained",
        "live": "not_executed",
        "selected": list(selected),
        "metrics": ["close", "base_volume", "open_interest", "funding_fraction"],
        "rows": rows,
        "same_venue_vendor_comparison": "not_applicable",
    }


def selected_universe_benchmark(symbols: Sequence[str]) -> dict[str, object]:
    """Time local freshness and 2d resample for the configured history. No scanner run."""

    plan = configured_request_plan(symbols)
    started = time.perf_counter()
    passes = 0
    for _symbol in symbols:
        candles = [
            {
                "timestamp": index * 60_000,
                "open": Decimal("10"),
                "high": Decimal("11"),
                "low": Decimal("9"),
                "close": Decimal("10"),
                "volume": Decimal("1"),
            }
            for index in range(250)
        ]
        assess_candle_freshness(
            candles,
            symbol="BTCUSDT",
            timeframe="1m",
            role="benchmark",
            decision_timestamp=datetime(1970, 1, 1, 4, 11, tzinfo=UTC),
        )
        daily = [
            {
                "timestamp": index * 86_400_000,
                "open": "10",
                "high": "11",
                "low": "9",
                "close": "10",
                "volume": "1",
            }
            for index in range(40)
        ]
        resample_ohlcv_candles(
            daily,
            target_interval="2d",
            decision_timestamp=datetime(1970, 1, 21, tzinfo=UTC),
        )
        passes += 1
    elapsed = time.perf_counter() - started
    return {
        "label": "synthetic_workload",
        "live_network": "not_executed",
        "scanner_invoked": False,
        "symbols": len(symbols),
        "configured_requests": plan["requests"],
        "local_stage_passes": passes,
        "local_stage_seconds": elapsed,
        "five_minute_headroom": "not_measured",
        "plan": plan,
    }
