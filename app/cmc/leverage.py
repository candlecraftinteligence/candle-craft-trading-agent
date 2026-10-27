"""Observable opportunity for existing exact-rank tie breaking; no new ranking authority."""
from collections import Counter


def selection_leverage(evaluations):
    eligible = [e for e in evaluations if e.candidate.cci_eligible and e.candidate.is_new_signal]
    groups = {}
    for e in eligible:
        if e.candidate.canonical_rank:
            groups.setdefault(e.candidate.canonical_rank, []).append(e)
    ties = [g for g in groups.values() if len(g) > 1]
    actionable = [g for g in ties if len({e.hypothetical_priority_delta for e in g}) > 1]
    return {"eligible_new_candidates":len(eligible), "exact_rank_tie_groups":len(ties),
        "candidates_in_exact_rank_ties":sum(map(len,ties)),
        "ties_with_different_hypothetical_priorities":len(actionable),
        "alert_types":dict(Counter(e.candidate.alert_type for e in eligible)),
        "confirmed_priority_mechanism":"delivery ordering only; no membership cap introduced",
        "actual_delivery_change_proven":False, "economic_outcome":"N/A"}
