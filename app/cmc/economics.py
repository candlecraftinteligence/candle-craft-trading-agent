"""Conservative planning, separate from provider balances and durable reservations."""
from math import ceil

from app.cmc.endpoints import reserved_credits


def collection_plan(profile, *, days=31, attempts=3, daily_cap=388, monthly_cap=11988):
    if not 1 <= days <= 31 or not 1 <= attempts <= 5:
        raise ValueError("bounded planning horizon required")
    families = [{"endpoint":f.endpoint.value, "seconds":f.seconds, "pages":f.max_pages,
        "credits_per_poll":reserved_credits(f.endpoint, {"limit":f.limit} if f.limit else {})*f.max_pages,
        "polls_per_day":ceil(86400/f.seconds)} for f in profile.families]
    normal = sum(f["credits_per_poll"]*f["polls_per_day"] for f in families)
    retry = normal*attempts
    return {"basis":"planning_upper_bound_before_provider_receipts; not current account usage",
        "families":families,"normal_daily":normal,"retry_daily":retry,"days":days,
        "normal_month":normal*days,"retry_month":retry*days,
        "daily_reservation_cap":daily_cap,"monthly_reservation_cap":monthly_cap,
        "retry_daily_headroom":daily_cap-retry,"retry_month_headroom":monthly_cap-retry*days,
        "account_entitlement_staging":15000,"account_remaining_last_staging_observation":14984,
        "account_remaining_now":"N/A", "provider_usage_since_staging":"N/A",
        "separate_validation_ledger_spending":"N/A: requires reconciliation, not a second provider debit",
        "existing_runtime_reservations":"N/A: retain ledger; reconcile before activation",
        "application_allowances_previous":{"daily":400,"monthly":12000},
        "account_and_application_ledgers_added_together":False}


def expected_freshness(*, cadence_seconds=14400, source_max_age_seconds=300):
    if cadence_seconds <= 0 or source_max_age_seconds <= 0:
        raise ValueError("positive cadence and freshness required")
    return {"cadence_seconds":cadence_seconds,"unchanged_source_max_age_seconds":source_max_age_seconds,
        "ideal_time_coverage_upper_bound":min(1,source_max_age_seconds/cadence_seconds),
        "expected_worst_age_before_next_poll_seconds":cadence_seconds,
        "assumptions":"zero source lag, immediate success, eligible approved mapping; actual coverage can be zero",
        "measured_metrics":["verified_global_results / analyzed_results",
            "verified_contract_results / analyzed_results", "approved_mapped_symbols / eligible_symbols",
            "complete_pair_batches / attempted_pair_batches", "source_age_p50_p95_max_seconds",
            "acquisition_latency_p95_seconds", "budget_exhausted_results / analyzed_results"]}
