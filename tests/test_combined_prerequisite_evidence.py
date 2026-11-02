from datetime import timedelta
from pathlib import Path

import pytest

from app.cmc.economics import collection_plan, expected_freshness
from app.cmc.profiles import profile_for
from app.cmc.storage import Sidecar
from tests.fixtures.cmc import AT
from scripts.cci_v2_prerequisite_evidence import build_evidence

pytestmark = pytest.mark.no_auto_epoch


def test_bounded_owner_inventory_never_turns_research_into_approval():
    evidence = build_evidence()
    assert evidence["currently_approved_mappings"] == 0
    assert len(evidence["identities"]) == 55
    assert all(not row["approved"] and row["approved_at"] is None and row["owner_approval_ref"] is None
        for row in evidence["identities"])
    for symbol in ("1000LUNCUSDT","1000PEPEUSDT","1000SHIBUSDT","ARBUSDT","币安人生USDT"):
        assert len(next(row for row in evidence["identities"] if row["symbol"] == symbol)["withholding_reasons"]) >= 3
    assert evidence["staging_usage_reconciliation"]["remaining_now"] == "N/A"


def test_exact_staged_workload_and_caps_do_not_double_count_provider_usage():
    profile = profile_for("CUSTOM",custom_path=Path(__file__).resolve().parents[1]/"config/cmc/combined-shadow.collection.json")
    plan = collection_plan(profile)
    assert plan["normal_daily"] == 121 and plan["normal_month"] == 3751
    assert plan["retry_daily"] == 363 and plan["retry_month"] == 11253
    assert plan["retry_daily_headroom"] == 25 and plan["retry_month_headroom"] == 735
    assert not plan["account_and_application_ledgers_added_together"]
    assert plan["account_remaining_now"] == "N/A"
    assert profile.concurrency == 1 and profile.requests_per_minute == 20 and profile.max_observations == 1000
    assert expected_freshness()["ideal_time_coverage_upper_bound"] == 300/14400
    assert expected_freshness(cadence_seconds=1800,source_max_age_seconds=240)["ideal_time_coverage_upper_bound"] == 240/1800


def test_effective_caps_boundary_restarts_existing_reservations_and_replay(tmp_path):
    path = tmp_path/"cmc_accounting.sqlite"
    store = Sidecar(path)
    previous = AT.replace(day=31,hour=23,minute=59)
    assert store.reserve(previous,388,daily=388,monthly=11988)
    assert not store.reserve(previous,1,daily=388,monthly=11988)
    # Unknown/failed reservations stay charged across restart; provider usage is
    # neither added again nor reconciled merely because cached evidence replays.
    restarted = Sidecar(path)
    assert restarted.usage(previous) == store.usage(previous)
    assert restarted.credit_summary(previous)["m:2026-01"]["FAILED_ATTEMPT_RESERVATIONS"] == 388
    following = previous+timedelta(minutes=1)
    assert restarted.reserve(following,388,daily=388,monthly=11988)
    with restarted.connect() as db:
        db.execute("UPDATE usage SET credits=11988 WHERE bucket='m:2026-02'")
    assert not restarted.reserve(following+timedelta(days=1),1,daily=388,monthly=11988)
    assert restarted.usage(previous)["m:2026-01"]["reserved_credits"] == 388


def test_acquisition_failure_and_provider_cost_overshoot_cannot_reset_caps(tmp_path):
    store = Sidecar(tmp_path/"cmc_failure.sqlite")
    assert store.reserve(AT,363,daily=388,monthly=11988)
    assert store.reserve(AT,25,daily=388,monthly=11988)
    assert not Sidecar(store.path).reserve(AT,1,daily=388,monthly=11988)
    store.reconcile(AT,25,30)
    assert store.usage(AT)["d:2026-01-01"]["reserved_credits"] == 393
    assert not store.reserve(AT,1,daily=388,monthly=11988)
