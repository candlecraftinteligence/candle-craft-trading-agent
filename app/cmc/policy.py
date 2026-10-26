"""Experimental recommendations and individually authorized enforcement, never setup promotion."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from pydantic import Field

from app.cmc.models import EvidenceState as S, Feature, Frozen, Mode, stable_id


class Action(str, Enum):
    NO_EFFECT = "NO_EFFECT"
    CONFIRMED_SUPPORT = "CONFIRMED_SUPPORT"
    PRIORITY_INCREASE = "PRIORITY_INCREASE"
    PRIORITY_DECREASE = "PRIORITY_DECREASE"
    VETO_CANDIDATE = "VETO_CANDIDATE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class Candidate(Frozen):
    setup_id: str
    economic_plan_id: str
    symbol: str
    direction: str
    cci_eligible: bool
    canonical_rank: tuple[Decimal, ...] = ()
    baseline_tie_rank: tuple[Decimal, ...] = ()
    is_new_signal: bool = True
    original_decision_at: datetime | None = None


class Rule(Frozen):
    id: str
    version: str = "1"
    action: Action
    features: tuple[str, ...]
    specification: str

    @property
    def specification_hash(self):
        return stable_id(self)


RULES = (
    Rule(id="data_divergence", action=Action.VETO_CANDIDATE, features=("CmcDataConsistency",),
         specification="Verified equivalent contract comparison has value -1 (material divergence)."),
    Rule(id="liquidation_stress", action=Action.VETO_CANDIDATE,
         features=("CmcLiquidationPressure", "CmcLiquidationImbalance"),
         specification="Global hourly stress ratio >=4 and direction-signed liquidation imbalance >=0.75; no sweep inference."),
    Rule(id="adverse_regime", action=Action.PRIORITY_DECREASE,
         features=("CmcGlobalMarketRisk", "CmcLiquidationPressure"),
         specification="Direction-signed negative market-cap return >3 percent and global stress ratio >=2."),
    Rule(id="sector_support", action=Action.PRIORITY_INCREASE,
         features=("CmcSectorRelativeStrength", "CmcGlobalMarketRisk", "CmcBasisContext"),
         specification="Direction-signed sector relative return >=2 percentage points; market return supports direction; absolute basis <=0.005."),
    Rule(id="consistent_context", action=Action.CONFIRMED_SUPPORT,
         features=("CmcDataConsistency", "CmcBasisContext"),
         specification="Verified equivalent sources consistent (value 1) and absolute basis <=0.005; evidence support only."),
)


class Activation(Frozen):
    rule_id: str
    version: str
    specification_hash: str
    enabled: bool = False
    source_contract_ref: str
    causal_evaluation_ref: str
    out_of_sample_or_prospective_ref: str
    tests_ref: str
    hard_gate_compatibility_ref: str
    baseline_comparison_ref: str
    no_material_negative_regression: bool = False
    owner_approval_ref: str
    approved_at: datetime
    expires_at: datetime

    def permits(self, rule: Rule, as_of: datetime) -> bool:
        refs = (self.source_contract_ref, self.causal_evaluation_ref, self.out_of_sample_or_prospective_ref,
                self.tests_ref, self.hard_gate_compatibility_ref, self.baseline_comparison_ref, self.owner_approval_ref)
        return (self.enabled and self.no_material_negative_regression and self.rule_id == rule.id
                and self.version == rule.version and self.specification_hash == rule.specification_hash
                and all(ref.strip() and ref.strip().upper() not in {"N/A", "UNVERIFIED", "PENDING"} for ref in refs)
                and self.approved_at.tzinfo is not None and self.expires_at.tzinfo is not None
                and self.approved_at <= as_of < self.expires_at)


class Recommendation(Frozen):
    original_setup_id: str
    economic_plan_id: str
    direction: str
    cci_eligible: bool
    rule_id: str
    rule_version: str
    specification_hash: str
    action: Action
    evidence_refs: tuple[str, ...]
    data_timestamps: tuple[datetime, ...]
    acquisition_timestamps: tuple[datetime, ...]
    reason: str
    evidence_status: S
    enforced: bool = False
    final_decision: str


class Evaluation(Frozen):
    candidate: Candidate
    mode: Mode
    decision_at: datetime
    recommendations: tuple[Recommendation, ...] = ()
    priority_delta: int = Field(default=0, ge=-1, le=1)
    hypothetical_priority_delta: int = Field(default=0, ge=-1, le=1)
    vetoed: bool = False
    hypothetical_veto: bool = False
    final_eligible: bool


def _triggered(rule: Rule, values: dict, direction: int) -> bool:
    if rule.id == "data_divergence":
        return values["CmcDataConsistency"] == -1
    if rule.id == "liquidation_stress":
        return values["CmcLiquidationPressure"] >= 4 and values["CmcLiquidationImbalance"] * direction >= Decimal("0.75")
    if rule.id == "adverse_regime":
        return values["CmcGlobalMarketRisk"] * direction > 3 and values["CmcLiquidationPressure"] >= 2
    if rule.id == "sector_support":
        return (values["CmcSectorRelativeStrength"] * direction >= 2 and
                values["CmcGlobalMarketRisk"] * direction < 0 and abs(values["CmcBasisContext"]) <= Decimal("0.005"))
    return values["CmcDataConsistency"] == 1 and abs(values["CmcBasisContext"]) <= Decimal("0.005")


def evaluate(candidate: Candidate, features: tuple[Feature, ...], *, mode: Mode,
             as_of: datetime, activations: tuple[Activation, ...] = ()) -> Evaluation:
    mode = Mode(mode)
    if as_of.tzinfo is None:
        raise ValueError("decision timestamp must include timezone")
    eligible = (candidate.cci_eligible and candidate.is_new_signal and candidate.direction in {"long", "short"}
                and (candidate.original_decision_at is None or candidate.original_decision_at.tzinfo is not None
                     and candidate.original_decision_at<=as_of)
                and all(x and x.upper() != "N/A" for x in (candidate.setup_id, candidate.economic_plan_id)))
    if mode in {Mode.OFF, Mode.COLLECT} or not eligible:
        return Evaluation(candidate=candidate, mode=mode, decision_at=as_of, final_eligible=candidate.cci_eligible)
    grouped = {}
    for feature in features:
        grouped.setdefault(feature.name, []).append(feature)
    evidence = {name:fs[0] for name,fs in grouped.items() if len(fs) == 1}
    recs, enforced_actions, hypothetical = [], [], []
    for rule in RULES:
        selected = [evidence.get(name) for name in rule.features]
        valid = all(f is not None and f.version == "1" and f.state == S.VERIFIED and f.value is not None and
                    f.evidence_ids and f.input_timestamps and f.acquired_timestamps and
                    all(t.tzinfo is not None and t <= as_of for t in (*f.input_timestamps, *f.acquired_timestamps))
                    and all((as_of-t).total_seconds() <= 300 for t in f.input_timestamps)
                    for f in selected)
        triggered = valid and _triggered(rule, {f.name:f.value for f in selected}, 1 if candidate.direction == "long" else -1)
        action = rule.action if triggered else Action.NO_EFFECT if valid else Action.INSUFFICIENT_EVIDENCE
        approvals = [a for a in activations if a.rule_id == rule.id]
        enforced = (triggered and mode == Mode.DECISION and len(approvals) == 1 and approvals[0].permits(rule, as_of))
        if triggered:
            hypothetical.append(action)
        if enforced:
            enforced_actions.append(action)
        recs.append(Recommendation(original_setup_id=candidate.setup_id, economic_plan_id=candidate.economic_plan_id,
            direction=candidate.direction, cci_eligible=candidate.cci_eligible, rule_id=rule.id, rule_version=rule.version,
            specification_hash=rule.specification_hash, action=action,
            evidence_refs=tuple(dict.fromkeys(ref for f in selected if f for ref in f.evidence_ids)),
            data_timestamps=tuple(t for f in selected if f for t in f.input_timestamps),
            acquisition_timestamps=tuple(t for f in selected if f for t in f.acquired_timestamps),
            reason=rule.specification if valid else "N/A: required verified, fresh, causal rule evidence missing",
            evidence_status=S.VERIFIED if valid else S.MISSING, enforced=enforced,
            final_decision="VETOED" if enforced and action == Action.VETO_CANDIDATE else "CANONICAL_ELIGIBILITY_PRESERVED"))
    delta = lambda actions: max(-1, min(1, actions.count(Action.PRIORITY_INCREASE)-actions.count(Action.PRIORITY_DECREASE)))
    vetoed = Action.VETO_CANDIDATE in enforced_actions
    final = "VETOED" if vetoed else "ELIGIBLE_PRIORITY_" + str(delta(enforced_actions))
    recs = [r.model_copy(update={"final_decision": final}) for r in recs]
    return Evaluation(candidate=candidate, mode=mode, decision_at=as_of, recommendations=tuple(recs),
        priority_delta=delta(enforced_actions), hypothetical_priority_delta=delta(hypothetical),
        vetoed=vetoed, hypothetical_veto=Action.VETO_CANDIDATE in hypothetical, final_eligible=not vetoed)


def rank(evaluations: tuple[Evaluation, ...], *, hypothetical=False):
    """Canonical rank wins; CMC breaks ties only, then stable economic identities."""
    return tuple(sorted((e for e in evaluations if e.candidate.cci_eligible and e.candidate.is_new_signal and
        not (e.hypothetical_veto if hypothetical else e.vetoed)), key=lambda e:(
            e.candidate.canonical_rank, -(e.hypothetical_priority_delta if hypothetical else e.priority_delta),
            e.candidate.baseline_tie_rank,
            e.candidate.symbol, e.candidate.economic_plan_id, e.candidate.setup_id)))
