import pytest

from app.cmc.forecast import comparisons, simulate
from app.cmc.profiles import profile_for

pytestmark = pytest.mark.no_auto_epoch


def test_forecasts_30_31_days_budget_and_freshness_are_deterministic():
    report = comparisons()
    rows = report["cases"]["decision_focused_10m"]
    assert [r["estimated_credits"] for r in rows] == [339,10170,10509]
    assert all(r["joint_freshness_coverage_percent"] == 40 for r in rows)
    assert rows[-1]["remaining_account_allowance"] == 4491
    assert report["cases"]["two_sources_15m"][0]["estimated_credits"] == 192
    assert report["sensitivity"]["five_minute_sources_unconstrained"]["estimated_credits"] == 17856
    assert report["sensitivity"]["source_already_stale"]["joint_freshness_coverage_percent"] == 0
    assert report["sensitivity"]["one_minute_selection_delay"]["joint_freshness_coverage_percent"] == 30
    assert report["sensitivity"]["default_application_budget"]["denied_reservations"] > 0
    assert report["sensitivity"]["costs_double"]["denied_reservations"] > 0
    assert report["sensitivity"]["six_hour_provider_outage"]["denied_reservations"] > 0


def test_cache_ttls_and_pagination_change_network_forecast():
    profile = profile_for()
    # MAP's one-day cache prevents 288 daily full refreshes even under the
    # legacy five-minute broad polling schedule.
    result = simulate(profile,daily_limit=100000,monthly_limit=100000)
    assert result["requests_per_endpoint"]["/v1/cryptocurrency/map"] == 2
    terminal = simulate(profile,daily_limit=100000,monthly_limit=100000,use_max_pages=False)
    assert terminal["requests"] < result["requests"]
