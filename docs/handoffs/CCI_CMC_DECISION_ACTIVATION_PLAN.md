# CCI CMC decision activation plan

Status: **PENDING validation and named owner approval**. This file is a procedure,
not an approval. No rule is enabled by this release. See the
[rule review](CCI_CMC_ACTIVE_DECISION_RULE_REVIEW.md) and
[Runtime plan](CCI_CMC_RUNTIME_ROLLOUT_PLAN.md).

## Ordered activation gates

1. Obtain separate approval for the pinned Runtime rollout package. Verify its
   existing epoch/schema/capacity gates before starting consumers.
2. Verify actual provider access, source freshness and credit accounting with
   bounded samples. Retain original source and acquisition times; mark missing
   facts N/A and unreliable facts Unverified. Do not infer USD/USDT parity,
   funding intervals, OI conventions or index currency.
3. Establish unchanged CMC OFF baseline. Confirm the actual single scanner and
   listener identities, normal canonical decision counts, event IDs and latency.
4. Observe bounded COLLECT operation with a dedicated sidecar. Fix a period,
   universe, credit budget and resource ceilings in advance. Exhausted credits,
   stale data or sidecar failures must remove optional authority.
5. Run SHADOW with the activation manifest empty. Record every original eligible
   candidate, its original setup/plan ID, direction, mode, regime and canonical
   rank, original CCI cutoff, CMC selection time, raw/normalized evidence IDs,
   timestamps, formula hash and hypothetical action. Include insufficient-evidence
   and non-triggering rows; do not sample only successful CMC recommendations.
6. Evaluate `adverse_regime/1` first. Freeze the rule before an independent
   prospective/out-of-sample period. Current delivery effect is exact-tie order;
   measure that effect rather than claiming a selection change that did not occur.
7. Obtain reviewed economic attribution for all retained, displaced, introduced
   and vetoed plans in any finite-capacity comparison, including winners rejected
   by CMC. An actual entry activation and attributable close, fees and slippage
   are required for verified net R. Candle touches and Telegram sends are not fills.
8. Review long/short, scalp/swing, market regime, coverage and outage cohorts.
   Keep every repeated observation attached to one original economic-plan unit;
   do not double-count the same outcome. Avoid post-hoc threshold or cohort choice.
9. Require a signed evidence packet showing causal data, independent evaluation,
   uncertainty and no material negative regression under prespecified criteria.
   If it cannot support that conclusion, keep the rule disabled.
10. Obtain owner authorization naming exactly one rule/version/hash, approved
    evidence references, release SHA, environment, operating budget, expiry,
    observation window and rollback criteria. A merge or `CMC_MODE=decision`
    alone is insufficient.
11. Install only that reviewed activation entry, revalidate it with the existing
    parser and `Activation.permits`, then restart the existing single scanner
    through the approved Runtime procedure. Confirm the loaded effective mode,
    manifest and release without displaying secrets.
12. Verify durable provenance and monitor. Retain or disable based on the frozen
    criteria; do not relabel an experiment as a proven improvement.

## Evidence packet and statistical limits

No adequate real outcome population is supplied. Sample size, detectable effect,
minimum tie/trigger count, minimum verified-outcome coverage, study duration and
uncertainty method must be chosen before observing the evaluation outcomes.
There is no defensible numerical minimum sample size or profitable threshold to
invent here. The existing thresholds are hypotheses, not demonstrated alpha.

The review must account for multiple rule/cohort comparisons, regime dependence,
selection opportunity costs, fill/nonfill censoring and missing outcomes. Show
mean and median net R, profit factor (N/A without losses), actual verified fill
rate, retained/displaced winners and losers, and additional fee/slippage
sensitivity. Unverified outcomes stay out of economic statistics and remain
visible in coverage denominators. Real incremental expectancy remains N/A.

## Manifest preparation and local validation

Use a separate owner-reviewed manifest, not the shipped disabled file. Required
fields are the exact existing `Activation` fields in `app/cmc/policy.py`:

`rule_id`, `version`, `specification_hash`, `enabled`, `source_contract_ref`,
`causal_evaluation_ref`, `out_of_sample_or_prospective_ref`, `tests_ref`,
`hard_gate_compatibility_ref`, `baseline_comparison_ref`,
`no_material_negative_regression`, `owner_approval_ref`, `approved_at`, `expires_at`.

Do not populate those references with synthetic fixture evidence or mere paths
to incomplete studies. Keep actual dates; never backdate approval. For the first
priority candidate the identity is `adverse_regime/1`, hash
`0cd7878eb32d7a66f4b5a62a6f423b5762d16694b3a6ca37fae93c2583fbcf44`.

After the packet is approved, validate without provider calls or a production DB:

```powershell
# On the approved checkout; reads only the specified non-secret approval file.
$env:CMC_ACTIVATION_MANIFEST = $ApprovedActivationManifest
@'
import os
from datetime import UTC, datetime
from pathlib import Path
from app.cmc.integration import load_activations
from app.cmc.policy import RULES
items=load_activations(Path(os.environ['CMC_ACTIVATION_MANIFEST']))
assert len(items)==1 and items[0].rule_id=='adverse_regime'
rule=next(r for r in RULES if r.id=='adverse_regime')
assert items[0].permits(rule,datetime.now(UTC))
print('Exact single-rule activation contract passed.')
'@ | & $Python -
if ($LASTEXITCODE -ne 0) { throw 'Activation contract failed' }
```

This checks the contract, not the truth of the evidence references. Owner review
of those references remains required. No manifest was generated or activated here.

## Monitoring and rollback contract

| Requirement | Authoritative evidence / interpretation |
| --- | --- |
| Original eligible candidates and evaluations | `policy_batch`, candidate eligibility and original setup/plan IDs |
| Hypothetical vs authorized recommendations | `Evaluation` mode; hypothetical fields; each recommendation's `enforced` flag and exact hash |
| Actual order or selection change | Pre-application `selection_intent`: baseline/actual delivery order, selected IDs, retained/displaced/introduced IDs |
| Completed delivery boundary / veto | Linked `selection_application`, actual delivery statuses, CMC veto marker; cross-check canonical Telegram attempts |
| Fills, winners/losers and net R | Independently verified plan-owned outcomes; never inferred from selection or Telegram statistics |
| Mean/median, profit factor, fill rate, style/regime/side | Offline `compare_policies` with complete verified outcome inputs; missing style remains N/A |
| Freshness, coverage and provider outages | Snapshot states/timestamps/errors and `collection_batch` source/capability records |
| API credits | Dedicated persistent UTC sidecar usage plus sanitized account quota reconciliation |
| Scanner latency/memory | Existing scanner process-memory/run diagnostics plus approved Runtime process measurements |

The intent is persisted before CMC authority reaches the loop. Failure to persist
it clears enforcement and restores baseline. Post-delivery audit failure cannot
undo a send: preserve the intent and canonical attempt ledger, label the
application observation missing, and do not count it as proven influence.

Stop optional authority immediately for a gate violation, wrong identity/hash,
unapproved rule, unexpected signal promotion/count increase, lost follow-up,
missing provenance, stale/future evidence being used, resource ceiling breach or
unexplained credit drift. Disable the single entry or set CMC mode OFF through
the established launcher and approved restart procedure. Preserve all sidecar,
outcome and Telegram evidence. Never delete or restore old outbox/history to
undo an experiment. Review economic deterioration at the prespecified checkpoints;
insufficient evidence is a reason to stay disabled, not to lower gates.
