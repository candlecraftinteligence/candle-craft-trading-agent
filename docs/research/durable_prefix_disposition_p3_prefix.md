# CCI P3_PREFIX — Durable Supplied-Prefix Disposition Evidence

**Status:** Additive nullable progress evidence. Schema v23 → v24.
**Does not** prove complete coverage, mint evaluation/occurrence ids, or
advance canonical plan-outcome reconciliation.

## Durable fact

`setup_lifecycle_outcome_progress.last_eligibility_prefix_evidence_json` is a
compact envelope for the most recent **completed** `closed_candles_as_of(...)`
application whose same-pass progress write was accepted and committed for that
`(lifecycle_id, plan_identity)` physical owner.

It is written atomically with `last_eligibility_decision_at` under the existing
P3B2B pass-local witness. “Most recent” means accepted durable application
order, not `MAX(timestamp)`.

The envelope may answer:

> For that last qualifying application, what eligible window was supplied, what
> pending suffix was established, how much completed, and why did that pass stop?

## Non-claims

The envelope does **not** mean:

- complete acquired market history through the cutoff
- a completely evaluated trade or unique occurrence
- that a legal early TP/SL terminal is missing coverage
- that `pending_suffix_exhausted=true` is an economic win/loss
- a canonical outcome for one immutable plan

NULL means no observation under this contract. It does not distinguish legacy
origin, a skipped pre-filter pass, an invalid new envelope, or an unpersisted
write.

## Write rule

Capture the envelope from fresh execution state when the eligibility filter
returns. Start a new pass-local envelope at each newly returned filter; never
inherit a previous envelope as this pass’s proof. Reload/import does not grant
authority to overwrite stored evidence.

If the filter returns but a valid envelope cannot be built, store cutoff with
envelope NULL rather than pairing a new application with an older envelope.

Pre-filter shortcuts, omitted-witness upserts, rejected writes, and rolled-back
transactions preserve the previously committed tuple. Do not backfill history.

## Read diagnostic

`diagnose_prefix_evidence()` reports owner, cutoff, or timeframe mismatch as
`conflicting`. Internally contradictory disposition, exhaustion, count, or
chronological bounds are `malformed`. A `known` status never carries an
impossible combination. This still does not prove complete coverage.
