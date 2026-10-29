"""Bounded acceptance for the signal-engine recovery product path."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.analytics.discrete_fib_levels import evaluate_discrete_levels, protected_stop_price
from app.analytics.public_alert_funnel import DEFAULT_PUBLIC_RR_MIN
from app.analytics.public_signal_quality import MIN_PUBLIC_SETUP_QUALITY_SCORE, MIN_PUBLIC_SIGNAL_GRADE
from app.core.minimum_rr import SWING_HARD_MINIMUM_RR
from app.core.trade_plan_integrity import validate_trade_plan
from app.lifecycle.eligibility import PUBLIC_WATCHLIST_MIN_RR
from app.lifecycle.state_machine import CONFIRMED_MIN_RR
from app.watch_supervisor import discovery_retry_backoff_seconds, failure_backoff_seconds
from scripts import run_scan


def test_public_trade_plan_requires_tp2_rr_at_least_three() -> None:
    weak = validate_trade_plan(
        direction="long",
        entry_low="100",
        entry_high="101",
        entry_reference="100",
        stop_loss="99",
        tp1="101.5",
        tp2="102",
        tp3="103",
        minimum_rr=DEFAULT_PUBLIC_RR_MIN,
    )
    strong = validate_trade_plan(
        direction="long",
        entry_low="100",
        entry_high="101",
        entry_reference="100",
        stop_loss="99",
        tp1="102",
        tp2="103",
        tp3="104",
        minimum_rr=DEFAULT_PUBLIC_RR_MIN,
    )
    assert weak.valid is False
    assert strong.valid is True
    assert strong.rr == Decimal("3")
    assert DEFAULT_PUBLIC_RR_MIN == Decimal("3")
    assert PUBLIC_WATCHLIST_MIN_RR == Decimal("3")
    assert CONFIRMED_MIN_RR == Decimal("3")
    assert SWING_HARD_MINIMUM_RR == Decimal("2.5")
    assert MIN_PUBLIC_SIGNAL_GRADE == "A"
    assert MIN_PUBLIC_SETUP_QUALITY_SCORE == Decimal("88")


def test_wick_touch_cannot_masquerade_as_body_confirmation() -> None:
    levels = evaluate_discrete_levels(
        direction="short",
        sweep=Decimal("200"),
        bos=Decimal("100"),
        zones=(("OB", Decimal("160"), Decimal("170"), "fresh"),),
        candles=(
            SimpleNamespace(
                open=Decimal("160"),
                high=Decimal("171"),
                low=Decimal("159"),
                close=Decimal("160.5"),
            ),
        ),
    )
    deep = next(item for item in levels if item.ratio == Decimal("0.618"))
    assert deep.wick_touched is True
    assert deep.body_confirmed is False
    assert deep.execution_eligible is False


def test_protected_stop_does_not_widen_for_distant_liquidity() -> None:
    stop = protected_stop_price(
        direction="long",
        entry=Decimal("100"),
        structure_extreme=Decimal("95"),
        liquidity_levels=(Decimal("50"), Decimal("94")),
        atr_buffer=Decimal("1"),
        spread=Decimal("0.25"),
        tick_size=Decimal("0.1"),
        local_search=Decimal("10"),
    )
    # Nearby 94 is included; distant 50 is ignored.
    assert stop == Decimal("93")


def test_discovery_retry_stays_inside_finite_backoff_ceiling() -> None:
    base = failure_backoff_seconds(4)
    low = discovery_retry_backoff_seconds(4, jitter=0.0)
    high = discovery_retry_backoff_seconds(4, jitter=1.0)
    assert 0 < low <= high <= base
    assert high == base
    assert low == pytest.approx(base * 0.5)


def test_unverified_discovery_placeholder_cannot_enter_a_scan_queue() -> None:
    watchlist = run_scan._discovery_unavailable_watchlist(
        type(
            "Args",
            (),
            {"universe": "binance_usdt_perp_top_market_cap", "universe_size": 100},
        )(),
        RuntimeError("synthetic discovery outage"),
    )
    assert watchlist.symbols == ()
    assert run_scan._verified_discovery_watchlist(watchlist) is False
    assert watchlist.universe.diagnostics["verified_membership"] is False
    assert watchlist.universe.diagnostics["new_setup_admission"] == "blocked"
    assert run_scan._DISCOVERY_CONFIG_PLACEHOLDER not in watchlist.symbols
