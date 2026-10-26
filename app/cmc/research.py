"""Reproducible policy comparison from independently verified economic-plan outcomes."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import Decimal

from pydantic import Field

from app.cmc.models import Frozen, Mode
from app.cmc.policy import Action, Evaluation, rank


class Outcome(Frozen):
    economic_plan_id: str
    verified: bool = False
    attribution_ref: str
    closed_at: datetime | None = None
    entry_activated: bool | None = None
    realized_net_r: Decimal | None = None
    max_adverse_excursion_r: Decimal | None = None
    fill_probability: Decimal | None = Field(default=None, ge=0, le=1)
    fees_r: Decimal | None = None
    slippage_r: Decimal | None = None


class ResearchRow(Frozen):
    selection_group: str
    evaluation: Evaluation
    outcome: Outcome
    regime: str = "N/A"
    cohort: str = "unverified"  # historical / out_of_sample / prospective / synthetic
    trading_style: str = "N/A"  # independently supplied scalp/swing; never inferred


def _mean(values):
    return str(sum(values, Decimal(0))/len(values)) if values else "N/A"


def _distribution(values):
    ordered = sorted(values)
    size = len(ordered)
    median = (ordered[(size-1)//2] + ordered[size//2])/2 if size else None
    gains = sum((x for x in values if x > 0), Decimal(0))
    losses = -sum((x for x in values if x < 0), Decimal(0))
    return {"verified_filled": size, "net_expectancy_r": _mean(values),
        "median_net_r": str(median) if median is not None else "N/A",
        "profit_factor": str(gains/losses) if losses else "N/A",
        "profit_factor_note": "net winning R / absolute net losing R; N/A without observed losses",
        "winners": sum(x > 0 for x in values), "losers": sum(x < 0 for x in values)}


def compare_policies(rows: tuple[ResearchRow, ...], *, delivery_limit=1,
                     additional_cost_r=(Decimal("0"), Decimal("0.05"), Decimal("0.10"))):
    if delivery_limit < 1:
        raise ValueError("delivery limit must be positive")
    groups, seen = defaultdict(list), set()
    for row in rows:
        evaluation, outcome = row.evaluation, row.outcome
        plan = evaluation.candidate.economic_plan_id
        if plan in seen:
            raise ValueError("duplicate economic-plan outcome unit; reconstruct one original decision per plan")
        seen.add(plan)
        if outcome.economic_plan_id != plan:
            raise ValueError("outcome ownership mismatch")
        if outcome.closed_at is not None and (outcome.closed_at.tzinfo is None or outcome.closed_at < evaluation.decision_at):
            raise ValueError("outcome predates decision / has no timezone")
        for recommendation in evaluation.recommendations:
            if any(t > evaluation.decision_at for t in (*recommendation.data_timestamps,*recommendation.acquisition_timestamps)):
                raise ValueError("historical look-ahead in policy evidence")
        groups[row.selection_group].append(row)
    selected = {name:[] for name in ("CCI_BASELINE", "CMC_SHADOW_POLICY", "CMC_DECISION_POLICY")}
    for group in groups.values():
        valid = [r.evaluation for r in group if r.evaluation.candidate.cci_eligible and r.evaluation.candidate.is_new_signal]
        baseline = sorted(valid, key=lambda e:(e.candidate.canonical_rank,e.candidate.baseline_tie_rank,e.candidate.symbol,
                                              e.candidate.economic_plan_id,e.candidate.setup_id))[:delivery_limit]
        for name, evaluations in (("CCI_BASELINE",baseline), ("CMC_SHADOW_POLICY",rank(tuple(valid),hypothetical=True)[:delivery_limit]),
                                  ("CMC_DECISION_POLICY",rank(tuple(valid))[:delivery_limit])):
            selected[name].extend(e.candidate.economic_plan_id for e in evaluations)
    by_plan = {r.evaluation.candidate.economic_plan_id:r for r in rows}

    def trusted(row):
        o = row.outcome
        return (o.verified and o.attribution_ref.strip().upper() not in {"", "N/A","UNVERIFIED","PENDING"} and o.closed_at is not None
                and o.entry_activated is not None)

    results = {}
    for name, plans in selected.items():
        included = [by_plan[p] for p in plans]
        outcomes = [r.outcome for r in included if trusted(r)]
        realized = [o.realized_net_r for o in outcomes if o.entry_activated and o.realized_net_r is not None
                    and o.fees_r is not None and o.slippage_r is not None]
        cohorts = {}
        for axis in ("direction", "regime", "cohort", "trading_style"):
            values = defaultdict(list)
            for r in included:
                key = r.evaluation.candidate.direction if axis == "direction" else getattr(r,axis)
                if trusted(r) and r.outcome.entry_activated and r.outcome.realized_net_r is not None and r.outcome.fees_r is not None and r.outcome.slippage_r is not None:
                    values[key].append(r.outcome.realized_net_r)
            cohorts[axis] = {key:_distribution(v) for key,v in sorted(values.items())}
        results[name] = {
            "selected_plan_ids":plans, "selected_count":len(plans), "verified_outcomes":len(outcomes),
            "verified_realized_net_r":str(sum(realized,Decimal(0))) if realized else "N/A",
            "net_expectancy_r":_mean(realized), "entry_activation_rate":_mean([Decimal(int(o.entry_activated)) for o in outcomes]),
            "verified_fill_rate":_mean([Decimal(int(o.entry_activated)) for o in outcomes]),
            "realized_distribution":_distribution(realized),
            "mean_fill_probability":_mean([o.fill_probability for o in outcomes if o.fill_probability is not None]),
            "mean_max_adverse_excursion_r":_mean([o.max_adverse_excursion_r for o in outcomes if o.max_adverse_excursion_r is not None]),
            "additional_fee_slippage_sensitivity_r":{str(cost):_mean([value-cost for value in realized]) for cost in additional_cost_r},
            "cohorts":cohorts,
        }
    baseline_ids = set(selected["CCI_BASELINE"])
    original_valid = [r for r in rows if r.evaluation.candidate.cci_eligible and r.evaluation.candidate.is_new_signal]
    for name in ("CMC_SHADOW_POLICY", "CMC_DECISION_POLICY"):
        removed = baseline_ids-set(selected[name])
        removed_verified = [by_plan[p] for p in removed if trusted(by_plan[p]) and by_plan[p].outcome.entry_activated
                            and by_plan[p].outcome.realized_net_r is not None and by_plan[p].outcome.fees_r is not None and by_plan[p].outcome.slippage_r is not None]
        results[name].update(
            removed_baseline_opportunities=len(removed),
            prevented_losing_trades=sum(r.outcome.realized_net_r<0 for r in removed_verified),
            rejected_winning_trades=sum(r.outcome.realized_net_r>0 for r in removed_verified),
            lost_winning_opportunities=sum(r.outcome.realized_net_r>0 for r in removed_verified),
        )
        def population(plans):
            values = [by_plan[p].outcome.realized_net_r for p in plans if trusted(by_plan[p])
                and by_plan[p].outcome.entry_activated and by_plan[p].outcome.realized_net_r is not None
                and by_plan[p].outcome.fees_r is not None and by_plan[p].outcome.slippage_r is not None]
            return {"plan_ids": sorted(plans), "count": len(plans), **_distribution(values)}
        selected_ids = set(selected[name])
        results[name]["retained"] = population(baseline_ids & selected_ids)
        results[name]["displaced"] = population(baseline_ids - selected_ids)
        results[name]["introduced"] = population(selected_ids - baseline_ids)
        a,b = results[name]["net_expectancy_r"], results["CCI_BASELINE"]["net_expectancy_r"]
        results[name]["incremental_expectancy_r"] = str(Decimal(a)-Decimal(b)) if "N/A" not in (a,b) else "N/A"
    return {
        "schema_version":"cmc-policy-research/1", "original_valid_setups":len(original_valid),
        "veto_recommendations":sum(r.evaluation.hypothetical_veto for r in original_valid),
        "priority_changes":sum(r.evaluation.hypothetical_priority_delta != 0 for r in original_valid),
        "additional_evidence_support":sum(any(x.action == Action.CONFIRMED_SUPPORT for x in r.evaluation.recommendations) for r in original_valid),
        "evidence_coverage":_mean([Decimal(int(any(x.evidence_status.value == "VERIFIED" for x in r.evaluation.recommendations))) for r in original_valid]),
        "independent_validation_available":any(r.cohort in {"out_of_sample","prospective"} and trusted(r) for r in rows),
        "improvement_proven":False,
        "limitation":"Descriptive comparison only. Full unselected outcomes, causal attribution and independent prospective/out-of-sample validation required; synthetic fixtures establish behavior only.",
        "policies":results,
    }
