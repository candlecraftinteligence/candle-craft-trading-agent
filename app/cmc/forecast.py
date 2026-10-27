"""Deterministic acquisition/budget model, not market or economic performance."""
from collections import Counter
from math import ceil

from app.cmc.endpoints import CONTRACTS, Endpoint as E, reserved_credits
from app.cmc.profiles import CollectionProfile, Family, profile_for


def simulate(profile: CollectionProfile, *, days=1, daily_limit=400, monthly_limit=12000,
             account_allowance=15000, source_lag_seconds=60, selection_delay_seconds=0,
             cost_multiplier=1, outage_seconds=0, use_max_pages=True):
    if (days not in {1,30,31} or min(daily_limit,monthly_limit,source_lag_seconds,
            selection_delay_seconds,outage_seconds) < 0 or cost_multiplier < 1):
        raise ValueError("invalid forecast scenario")
    requests, credits = Counter(), Counter()
    next_due, last_success = {}, {}
    used, daily_used, fresh_seconds, denied = 0, 0, 0, 0
    for second in range(0,days*86400,60):
        if second % 86400 == 0: daily_used = 0
        for family in profile.families:
            endpoint = family.endpoint
            if endpoint in {E.CATEGORY,E.CRYPTO_PAIRS}:
                continue  # default empty category/external-venue manifests
            if second < next_due.get(endpoint,0): continue
            pages = family.max_pages if use_max_pages and endpoint in {E.MAP,E.LISTINGS,E.EXCHANGE_PAIRS,E.CRYPTO_PAIRS} else 1
            estimate = reserved_credits(endpoint,{"limit":family.limit} if family.limit else {})
            success = True
            for _ in range(pages):
                attempts = 3 if second < outage_seconds else 1
                for _ in range(attempts):
                    if daily_used+estimate > daily_limit or used+estimate > monthly_limit:
                        denied += 1
                        success = False
                        break
                    # Unknown failed costs retain the reservation. Successful
                    # reported charges may exceed it and stop later requests.
                    charge = estimate if second < outage_seconds else ceil(estimate*cost_multiplier)
                    used += charge
                    daily_used += charge
                    requests[endpoint.value] += 1
                    credits[endpoint.value] += charge
                if second < outage_seconds: success = False
                if not success: break
            if success:
                last_success[endpoint] = second
                next_due[endpoint] = second+max(family.seconds,CONTRACTS[endpoint].ttl)
            else:
                next_due[endpoint] = second+min(300,family.seconds)
        if all(endpoint in last_success for endpoint in (E.GLOBAL,E.LIQUIDATIONS)):
            # Continuous time coverage under uniformly distributed analysis
            # cutoffs; freshness is evaluated at the later selection boundary.
            expires = min(last_success[e]-source_lag_seconds+300-selection_delay_seconds
                for e in (E.GLOBAL,E.LIQUIDATIONS))
            fresh_seconds += max(0,min(60,expires-second))
    coverage = 100*fresh_seconds/(days*86400)
    return {"profile":profile.name,"days":days,"requests_per_endpoint":dict(sorted(requests.items())),
        "credits_per_endpoint":dict(sorted(credits.items())),"requests":sum(requests.values()),
        "estimated_credits":used,"average_credits_per_day":used/days,
        "remaining_account_allowance":account_allowance-used,"remaining_application_budget":monthly_limit-used,
        "denied_reservations":denied,"joint_freshness_coverage_percent":coverage,
        "potential_decisions_with_fresh_global_rule_inputs_percent":coverage,
        "actual_CCI_decision_coverage":"N/A: requires prospective cutoff distribution",
        "provider_cost_multiplier":cost_multiplier,"assumed_source_lag_seconds":source_lag_seconds,
        "selection_delay_seconds":selection_delay_seconds,"outage_seconds":outage_seconds,
        "pagination":"configured upper page bound" if use_max_pages else "one terminal page per family",
        "provider_uncertainty":"Entitlement/cost/source lag/partial pages may change; forecast is conditional",
        "optional_category_and_external_venue_requests":"excluded: no configured IDs",
        "account_usage_outside_CCI":"N/A: not measured"}


def comparisons():
    two = CollectionProfile(name="CUSTOM",families=(Family(endpoint=E.GLOBAL,seconds=900),
        Family(endpoint=E.LIQUIDATIONS,seconds=900)))
    five = two.model_copy(update={"families":tuple(f.model_copy(update={"seconds":300}) for f in two.families)})
    cases = {"existing_broad_4h":profile_for(legacy_seconds=14400), "two_sources_15m":two,
        "decision_focused_10m":profile_for("DECISION_FOCUSED"), "low_credit_15m":profile_for("LOW_CREDIT"),
        "two_sources_5m":five}
    output = {"schema_version":"cmc-budget-forecast/1", "account_allowance":15000,
        "application_daily_limit":400,"application_monthly_limit":12000,
        "headroom_reserved":3000,"network_calls":0,"cases":{}}
    for name,profile in cases.items():
        output["cases"][name] = [simulate(profile,days=d) for d in (1,30,31)]
    focused = cases["decision_focused_10m"]
    output["sensitivity"] = {
        "costs_double":simulate(focused,days=31,cost_multiplier=2),
        "six_hour_provider_outage":simulate(focused,days=31,outage_seconds=21600),
        "default_application_budget":simulate(focused,days=31,daily_limit=100,monthly_limit=3000),
        "one_minute_selection_delay":simulate(focused,days=31,selection_delay_seconds=60),
        "source_already_stale":simulate(focused,days=31,source_lag_seconds=301),
        "five_minute_sources_unconstrained":simulate(five,days=31,daily_limit=100000,monthly_limit=100000)}
    return output
