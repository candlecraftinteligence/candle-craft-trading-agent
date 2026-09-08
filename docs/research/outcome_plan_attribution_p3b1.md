# CCI P3B1 — Prospective Immutable-Plan Attribution on Outcome Progress

**Status:** Prospective, additive evidence attribution.
**Does not** persist canonical plan outcomes, migrate analytics, mint fill/trade ids, or change public delivery.

## Boundary

New eligible `setup_lifecycle_outcome_progress` rows may store the existing P1
`plan_version_id` when `evaluate_closed_candle_outcomes` can prove that identity
remints from the frozen economics actually evaluated. Physical uniqueness remains
`UNIQUE(lifecycle_id, plan_identity)`.

## Write contract

- Attribution is captured only in `_new_progress` (first INSERT).
- Upsert `ON CONFLICT` does not update `plan_version_id` (no NULL backfill, no overwrite).
- Invariant-conflicting replacement geometry stores SQL NULL on the new row.
- Missing/unproven identity stores SQL NULL; ordinary outcome persistence still occurs.
- `plan_version_id` is excluded from `SetupLifecycleOutcomeProgress.model_dump`, so
  analytics `raw_payload_json` and scanner result dumps stay unchanged.

## Explicitly excluded

Analytics persistence, evaluation-context columns, canonical ownership, trade/fill
occurrence identity, and consumer migration.
