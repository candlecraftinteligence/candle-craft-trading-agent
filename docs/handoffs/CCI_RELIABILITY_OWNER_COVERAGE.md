# CCI reliability: owner-monitoring accounting handoff

Date: 2026-10-23. State: **CODE_COMPLETE**, pending owner integration/release and
natural Runtime verification. This document does not authorize merging or rollout.

- Branch: `codex/reliability-owner-coverage`.
- PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/141.
- Base: `a88ed55d8923c1af7078e8c612de4cec106c623b`.
- Verified implementation: `735d0b4dbe85e79f082e7cb6b6560c7890262f10`.
- Implementation commits: `fc170b0`, `b2d8251`, `735d0b4`.
- The documentation-only commit containing this handoff changes no implementation
  or tests. Its exact hash is recorded in the PR after publication.

## Problem and correction

P5A already monitors existing economic obligations outside discovery. Its report
counted lag rows rather than all obligations, omitting evaluator/acquisition
subsystem failures. The caller discarded continuation diagnostics after phase
classification. One-shot failure exits also preceded normal artifact persistence.

The correction retains per-owner dispositions and a complete denominator, combines
both passes without double counting, and separates verified current evaluations
from gaps/errors/uncovered owners. Historical completeness remains Unverified.
Reports survive scan JSON, SQLite scan payloads, manifests and watch summaries.
One-shot failures write explicitly partial diagnostic records before exiting;
these records are not scan completions. Enumeration failures report N/A population,
never an assumed empty population. Diagnostic-write failure preserves both errors.
Synthetic command-test artifact defaults now use pytest temporary directories.

No eligibility rule, state progression, economics, schema, public delivery policy,
strategy gate or P4 shadow configuration changed. No historical candle acquisition,
production access, scanner/listener operation, merge or deployment occurred.

## Verification

- Full existing-environment pytest on `735d0b4`: **3390 passed, 1 warning**, 612.93 s.
  No failures or skips. Warning: FastAPI/Starlette TestClient's deprecated httpx
  adapter. Dependencies were not changed.
- Corrected command/watch/accounting/binding subset: 167 passed.
- Independent Astra exact-code review: APPROVE for `735d0b4`, 41 independently
  verified targeted tests, no remaining actionable findings.
- Required CI: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/39923605763
  succeeded for `735d0b4`.
- Dependency, compile and whitespace checks passed. Static diff confirms no change
  to public score 88, grade A, RR 3, strategy, lifecycle-outcome, outbox or P4 policy
  code; the existing gate-invariance regression remains covered by full pytest.
- Earlier partial full-suite runs were interrupted after review corrections;
  they are not counted as successful runs. The final full run is the one above.

Bounded synthetic diagnostic CPU/size measurements and limits are in the objective
tracker. They exclude disk/SQLite and do not prove Runtime cadence/storage impact.

## Remaining risks and continuation

The nine historical uncovered owners have not been individually diagnosed. The
73 historical continuity gaps and 20 missing boundaries remain unresolved; current
monitoring success is not historical completeness. Runtime conditions and current
delivery provenance are N/A / Unverified from DEV. PR #141 is a diagnostic integrity
improvement, not a verified operational repair or an approved release.

Continue with the synthetic outcome-report counterexample recorded under C in
`docs/research/CCI_RELIABILITY_OBJECTIVE_TRACKER.md`: separate supplied execution
integrity from verified plan identity. Then address A's start/exit coverage, D's
event-key provenance visibility/repeated logging, and E's phase/resource timing.
Keep all work on focused DEV branches and require independent exact-head review.
Any Runtime deployment/measurement needs its separately approved operational package.
