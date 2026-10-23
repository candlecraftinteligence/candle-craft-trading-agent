from __future__ import annotations

import asyncio
import json

import pytest

from app.core.phase_profile import PHASES, PhaseProfile, current_observations, profiled


def test_nested_elapsed_is_inclusive_and_unobserved_is_not_zero():
    clock = iter([0, 1, 2, 5, 8, 10])
    profile = PhaseProfile(lambda: next(clock))
    with profile.measure("scan_core"):
        with profile.measure("scanner"):
            pass
    report = profile.snapshot(boundary="synthetic_end")
    assert report["phases"]["scan_core"]["elapsed_seconds"] == 7
    assert report["phases"]["scanner"]["elapsed_seconds"] == 3
    assert report["phases"]["lifecycle"]["elapsed_seconds"] == "N/A"
    assert report["phases"]["lifecycle"]["measurement_status"] == "NOT_OBSERVED"
    assert report["elapsed_seconds"] == 10
    assert "do_not_sum" in report["scope"]
    assert report["business_success_inferred"] is False


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "bad", None, -10], ids=["nan", "inf", "bool", "text", "none", "regression"])
def test_invalid_clock_cannot_turn_into_zero_or_replace_business_error(bad):
    values = iter([0, 1, bad, 3])
    profile = PhaseProfile(lambda: next(values))
    error = RuntimeError("original operation")
    with pytest.raises(RuntimeError) as raised:
        with profile.measure("scan_storage"):
            raise error
    assert raised.value is error
    row = profile.snapshot(boundary="synthetic_end")["phases"]["scan_storage"]
    assert row["raised_calls"] == 1 and row["unavailable_calls"] == 1
    assert row["elapsed_seconds"] == "N/A" and row["measurement_status"] == "Unverified"


def test_failed_clock_with_cancellation_retains_exact_exception():
    def broken():
        raise OSError("clock unavailable")
    profile = PhaseProfile(broken)
    error = asyncio.CancelledError("synthetic cancel")
    with pytest.raises(asyncio.CancelledError) as raised:
        with profile.measure("scanner"):
            raise error
    assert raised.value is error
    assert profile.snapshot(boundary="cancelled")["elapsed_seconds"] == "N/A"


def test_active_snapshot_is_in_progress_and_detached():
    profile = PhaseProfile(lambda: 1)
    with profile.activate():
        with profile.measure("scan_core"):
            current = current_observations(boundary="during_core")
            row = current["phase_profile"]["phases"]["scan_core"]
            assert row["active_calls"] == 1 and row["elapsed_seconds"] == "N/A"
            assert row["measurement_status"] == "IN_PROGRESS"
    assert current["phase_profile"]["phases"]["scan_core"]["active_calls"] == 1
    assert current_observations(boundary="inactive") == {}


def test_decorators_keep_sync_async_behavior_and_context_isolated():
    @profiled("lifecycle")
    def synchronous(value):
        return value
    @profiled("owner_monitoring")
    async def asynchronous(value):
        await asyncio.sleep(0)
        return synchronous(value)
    async def run():
        first, second = PhaseProfile(), PhaseProfile()
        async def sample(profile, value):
            with profile.activate():
                return await asynchronous(value)
        assert await asyncio.gather(sample(first, 7), sample(second, 9)) == [7, 9]
        return first, second
    assert synchronous(3) == 3
    first, second = asyncio.run(run())
    for profile in (first, second):
        rows = profile.snapshot(boundary="complete")["phases"]
        assert rows["lifecycle"]["calls"] == rows["owner_monitoring"]["calls"] == 1
    assert current_observations(boundary="outside") == {}


def test_fixed_vocabulary_prevents_per_symbol_growth_and_snapshot_size_is_bounded():
    profile = PhaseProfile(lambda: 1)
    for _ in range(1000):
        profile.call("scanner", lambda: None)
    with pytest.raises(ValueError, match="unknown phase"):
        with profile.measure("BTCUSDT"):
            pass
    report = profile.snapshot(boundary="synthetic")
    assert set(report["phases"]) == set(PHASES)
    assert report["phases"]["scanner"]["calls"] == 1000
    assert len(json.dumps(report).encode()) < 7000
