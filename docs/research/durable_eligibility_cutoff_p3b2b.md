# CCI P3B2B — Prospective Durable Eligibility-Cutoff Evidence

**Status:** Additive nullable progress evidence. Schema v22 → v23.
**Does not** prove complete coverage, mint evaluation/occurrence ids, or
advance P3C canonical reconciliation.

## Durable fact

`setup_lifecycle_outcome_progress.last_eligibility_decision_at` is the exact
cutoff used by the most recent **completed** outcome candle-eligibility
computation (`closed_candles_as_of` returned an eligible prefix, possibly empty)
whose same-pass progress write was accepted and committed for that
`(lifecycle_id, plan_identity)` economic owner.

“Most recent” means the order of accepted durable writes carrying fresh
eligibility evidence. It is not `MAX(timestamp)`, the latest scanner request, or
an arbitrary database update.

## Non-claims

A stored cutoff does **not** mean:

- all required history through that cutoff was evaluated
- outcome evaluation succeeded or the eligible prefix was exhausted
- the value is a processing clock, scan id, or report `as_of`
- a unique evaluation / trade / fill occurrence exists
- complete durable evaluation context or a canonical plan-outcome

NULL means no observation under this contract. It does not distinguish legacy
origin, early return, unavailable input, or an unpersisted pass.

## Write rule

Record the filter’s normalized `decision_timestamp` when
`closed_candles_as_of(...)` returns and the existing progress upsert commits.
Preserve the stored value when the current pass did not complete that filter
(terminal shortcut, missing candles/timeframe, exception, omitted-witness
upsert). Do not add extra progress writes for skipped passes. Do not backfill
history.

P3B1 `plan_version_id` remains INSERT-only. This field may update
prospectively on a later qualifying application for the same economic owner.
