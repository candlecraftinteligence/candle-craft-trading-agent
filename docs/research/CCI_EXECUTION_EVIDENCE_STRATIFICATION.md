# Supplied execution-evidence strata

This is an additive, read-only classification for `project_outcome_ownership`.
It consumes caller-supplied records. It does not acquire candles, repair source
rows, change lifecycle outcomes, or authorize outcome/expectancy research.

## Verified counterexample

A synthetic progress row with a remintable plan id, a coherent supplied anchor,
an asserted TP3 terminal, and `integrity_status=Unverified` could receive
`verified_attribution` and an interpretable `tp3_terminal` snapshot label. Those
fields describe identity and diagnostic interpretation. The projection already
keeps expectancy, P&L and unique-trade metrics unavailable, but did not stratify
the snapshot interpretation by its supplied execution-integrity uncertainty.

## Additive contract

`execution_evidence_policy` names `cci-supplied-execution-evidence-v1`. Each
evaluation and plan interpretation now includes `execution_evidence`:

- `reported_verified`: every supplied status reports Verified and has no diagnostic.
- `reported_verified_with_diagnostic`: a Verified claim retains a diagnostic.
- `reported_unverified` / `reported_failed`: supplied status as classified.
- `unavailable`: missing/N/A status. Missing values are N/A, never zero.
- `unrecognized`: an unknown or malformed status, retained for inspection.
- `mixed`: retained snapshots report different status strata.
- `no_progress_supplied`: no progress evidence supplied for the lifecycle.

These are source claims, not independently verified candle coverage. Historical
completeness stays Unverified even when every source row reports Verified.
`source_status_counts` counts retained supplied rows. `supplied_rows` retains
status, diagnostic, plan binding, cursor/processing timestamps and supplied
metadata/lineage. Existing source references/digests remain unchanged. Strings
and timestamp offsets are preserved; non-JSON Mapping metadata follows the
existing source-digest string representation. No receipt or timestamp is invented.

Counts now expose strata for supplied evaluation groups, diagnostic plan
interpretations and the subset with interpretable snapshot labels. Namespace
boundaries remain intact; reconstructed source metadata is retained and never
relabelled prospectively observed. Counts are not trades or fills. Existing
identity/economic/context classification, unavailable metrics and source records
retain their prior meanings.

## Verification and limits

The targeted report, attribution, context and cutoff subset passed 107 tests.
The counterexample is a deterministic regression. Tests cover missing/malformed
status, contradictory Verified+diagnostic claims, conflicting snapshots,
namespace/lineage/timestamp preservation, input immutability and deterministic
ordering. Full pytest, CI and exact-head independent Astra review are pending.

The first independent review requested correction of a boundary-field typo.
Retained snapshots now include the actual `tracking_start_at`, lifecycle id,
namespace and original source reference/digest. A regression preserves two
conflicting boundary strings and source references, without changing the inputs.

The branch is `codex/reliability-research-evidence`, stacked on the independently
approved owner-accounting head `bdcc90e649fdcd4d5b542f867ba27264a6d038d9`.
No Runtime evidence was inspected or recovered. The historical 73 continuity gaps
and 20 missing confirmation boundaries remain unresolved. This classification
supports honest stratification; it does not establish research-grade completeness.
