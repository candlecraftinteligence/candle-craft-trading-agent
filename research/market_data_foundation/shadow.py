"""Offline shadow checks. Differences are not win-rate or profit claims."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

from app.analytics.derivatives_enrichment import enrich_derivatives
from app.analytics.public_signal_quality import canonical_public_setup_quality_decision
from app.cache.market_data_cache import MarketDataCache
from app.data.candle_batch_evidence import CACHE_KIND_HIT, CACHE_KIND_MISS
from app.data.candle_freshness import PUBLICATION_ALLOWANCE, assess_candle_freshness

from research.market_data_foundation.integrity import linear_position_versus_notional


def public_gate_probe() -> dict[str, bool]:
    def decision(score: str, grade: str, *, evaluated: bool = True) -> bool:
        record = SimpleNamespace(
            setup_quality=SimpleNamespace(
                is_evaluated=evaluated,
                quality_score=Decimal(score),
                quality_grade=grade,
            )
        )
        return canonical_public_setup_quality_decision(record).passed

    return {
        "score_88_grade_A": decision("88", "A"),
        "score_87_grade_A": decision("87", "A"),
        "score_90_grade_B": decision("90", "B"),
        "unevaluated": decision("100", "A+", evaluated=False),
    }


def freshness_sensitivity() -> dict[str, str]:
    decision = datetime(1970, 1, 1, 0, 2, tzinfo=UTC)
    first = _minute_candle(0)
    second = _minute_candle(60_000)
    covered = assess_candle_freshness(
        (first, second),
        symbol="BTCUSDT",
        timeframe="1m",
        role="research_probe",
        decision_timestamp=decision,
        allowance=PUBLICATION_ALLOWANCE,
    )
    stale = assess_candle_freshness(
        (first,),
        symbol="BTCUSDT",
        timeframe="1m",
        role="research_probe",
        decision_timestamp=decision,
        allowance=PUBLICATION_ALLOWANCE,
    )
    return {"covered": covered.status.value, "missing_latest_bar": stale.status.value}


def derivatives_sensitivity() -> dict[str, str]:
    changed = enrich_derivatives(
        symbol="BTCUSDT",
        exchange="binance_futures",
        current_open_interest=Decimal("2.5"),
        previous_open_interest=Decimal("2.0"),
    )
    missing = enrich_derivatives(
        symbol="BTCUSDT",
        exchange="binance_futures",
        current_open_interest=Decimal("2.5"),
    )
    price_only = linear_position_versus_notional(
        base_quantity=Decimal("2.5"),
        mark_before=Decimal("100000"),
        mark_after=Decimal("110000"),
    )
    return {
        "oi_change_pct": str(changed.open_interest_change_pct),
        "missing_previous_oi": str(missing.open_interest_change_pct),
        "price_only_position_growth": price_only["position_growth"],
    }


def pair_exact_opens(
    left: Sequence[dict[str, Any]],
    right: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Pair only equal open timestamps. Unmatched rows stay unmatched."""

    right_by_open = {int(row["timestamp"]): row for row in right}
    paired = []
    unmatched_left = []
    used: set[int] = set()
    for row in left:
        opened = int(row["timestamp"])
        other = right_by_open.get(opened)
        if other is None:
            unmatched_left.append(opened)
            continue
        used.add(opened)
        paired.append(
            {
                "timestamp": opened,
                "close_delta": str(Decimal(str(row["close"])) - Decimal(str(other["close"]))),
            }
        )
    unmatched_right = [opened for opened in right_by_open if opened not in used]
    return {
        "tolerance_ms": 0,
        "paired": paired,
        "unmatched_left": unmatched_left,
        "unmatched_right": unmatched_right,
        "comparison_class": "cross_venue_context_unless_same_venue_declared",
    }


def cache_provenance(root: Path) -> dict[str, Any]:
    return asyncio.run(_cache_provenance(root))


async def _cache_provenance(root: Path) -> dict[str, Any]:
    path = root / "cache.json"
    clock = {"now": 1_000.0}

    def now() -> float:
        return clock["now"]

    cache = MarketDataCache(file_path=path, ttl_seconds=60, now=now)
    calls = {"n": 0}

    async def fetch() -> dict[str, str]:
        calls["n"] += 1
        return {"measurement_at": "1970-01-01T00:00:00+00:00", "value": "1"}

    miss = await cache.get_or_fetch_with_local_delivery(
        data_type="ticker",
        key_parts={"exchange": "binance_futures", "symbol": "BTCUSDT", "endpoint": "probe"},
        fetch=fetch,
    )
    hit = await cache.get_or_fetch_with_local_delivery(
        data_type="ticker",
        key_parts={"exchange": "binance_futures", "symbol": "BTCUSDT", "endpoint": "probe"},
        fetch=fetch,
    )
    failed = False
    try:
        async def boom() -> dict[str, str]:
            raise RuntimeError("upstream failed")

        await cache.get_or_fetch_with_local_delivery(
            data_type="ticker",
            key_parts={"exchange": "bybit_linear", "symbol": "BTCUSDT", "endpoint": "probe"},
            fetch=boom,
        )
    except RuntimeError:
        failed = True
    reloaded = MarketDataCache(file_path=path, ttl_seconds=60, now=now)
    reloaded_hit = await reloaded.get_or_fetch_with_local_delivery(
        data_type="ticker",
        key_parts={"exchange": "binance_futures", "symbol": "BTCUSDT", "endpoint": "probe"},
        fetch=fetch,
    )
    bybit_present = any(
        isinstance(entry.get("key"), dict) and entry["key"].get("exchange") == "bybit_linear"
        for entry in reloaded._entries.values()
    )
    return {
        "miss_kind": miss.delivery_kind,
        "hit_kind": hit.delivery_kind,
        "fetch_calls_before_reload": calls["n"],
        "failed_fetch_raised": failed,
        "failed_venue_stored": bybit_present,
        "reload_kind": reloaded_hit.delivery_kind,
        "reload_file_loaded": reloaded_hit.file_loaded_entry,
        "measurement_at": hit.value["measurement_at"],
        "cache_created_at": hit.bookkeeping_created_at,
        "measurement_is_cache_time": False,
        "expected_miss": CACHE_KIND_MISS,
        "expected_hit": CACHE_KIND_HIT,
    }


def _minute_candle(open_ms: int) -> dict[str, Any]:
    return {
        "timestamp": open_ms,
        "open": Decimal("1"),
        "high": Decimal("1"),
        "low": Decimal("1"),
        "close": Decimal("1"),
        "volume": Decimal("1"),
    }


def acquisition_skew_example() -> dict[str, str]:
    """A decision cutoff cannot be backdated to a snapshot start after later fetches."""

    snapshot_start = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)
    fetched_at = snapshot_start + timedelta(seconds=40)
    return {
        "snapshot_start": snapshot_start.isoformat(),
        "feature_decision_cutoff": fetched_at.isoformat(),
        "acquisition_skew_seconds": "40",
        "label": "cutoff_is_after_admissible_inputs",
    }
