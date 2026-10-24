# Provenance-only diagnostics and watch cadence

## Reproduced DEV interaction

The D correction truthfully marks unresolved or incomplete Telegram provenance
PARTIAL. The existing supervisor treats every repeated PARTIAL as an operational
retry failure. With ten unresolved historical rows and zero delivery failures,
the real pure functions produce a 300-second backoff after repeated cycles. A
synthetic 240-second cycle on a 300-second cadence then schedules the next start
at 540 seconds, despite useful scanner work completing within the interval.

This is a prospective interaction in the unmerged DEV reliability stack. It does
not explain the historical four missed slots or the 16h54m outage. The production
ten events and their actual current state remain individually Unverified.

## Narrow correction

Keep Telegram and overall PARTIAL, provenance counts, incomplete-coverage markers
and bounded warnings. Separate retry eligibility from diagnostic status. Only a
positively established provenance-only PARTIAL avoids operational backoff:

- Telegram is the sole adverse phase; every other phase is recognized SUCCESS
  or SKIPPED.
- A delivery summary exists; all four failure counters are explicit nonnegative
  integers and zero. Its original failed count and delivery list must also exist;
  default-to-zero conversions cannot establish health. Unknown, failed, uncertain
  or retryable raw delivery statuses/details retain backoff.
- Provenance has the recognized contract and OBSERVED status without a reported
  inspection error, valid bounded counts and an explicit completeness boolean.
  A completed inspection with unresolved rows qualifies. Incomplete inspection
  qualifies only at its explicitly observed inspection limit. Missing, unavailable,
  failed or inconsistent evidence retains backoff.

Any other failed/partial phase or actual delivery failure preserves the existing
backoff constants and policy. Recompute the decision before primary storage,
after storage, on manifest failure and after watch-output/reporting work. Each
calculation starts from the iteration's original prior streak, so provisional
diagnostic-only decisions cannot hide a later operational failure or double count.

`retry_backoff_eligible`, `retry_backoff_reason` and `consecutive_failure_scope`
are additive summary/receipt evidence. The scope explicitly describes consecutive
backoff-eligible operational iterations. Historical counts are not reinterpreted;
missing scope in old records remains unavailable. Earlier snapshots retain their
declared boundaries; the final receipt retains the final decision. Cancellation
and fatal stops are not presented as retry requests.

No send/recovery selection, canonical claim, deduplication, public eligibility,
strategy, economic identity, history or resource threshold changes. This does not
approve delivery or mutate/quarantine the historical events.

## Verification checkpoint

The real pure-function regression retains PARTIAL and moves the synthetic next
start from 540 to 300 seconds. A four-iteration mocked watch test keeps statuses
PARTIAL and operational streaks 0, 0, 1, 0 when a watch-output failure follows two
diagnostic-only cycles and then recovers. Final eligibility and reasons persist.
Malformed/defaulted counters, unavailable/failed provenance, bounded inspection,
raw delivery failures and combined scanner/lifecycle/owner/storage/manifest/output
failures are covered. No live scanner, listener, Telegram or market request runs.

The first subset had 136 passes and one fixture failure: it tried to assign to
the frozen WatchScanExecution dataclass. The fixture now uses dataclasses.replace;
no production invariant was relaxed. Corrected expanded subset: **201 passed in
37.96 seconds**. An explicit OBSERVED-with-error regression was added afterward.
Final targeted/full pytest, exact-head Astra review and CI remain pending.
The final focused cadence/Telegram/session subset passed **91 tests in 17.53
seconds**, including real synthetic storage, manifest and watch-output failures
after two diagnostic-only cycles. Dependency, compile, whitespace and invariant
checks passed. Final full pytest and exact-head review/CI remain pending.
Astra requested correction on `d4983b3`: the conservative raw-status allowlist
omitted existing healthy `blocked_repeat`, used for suppression/compaction. The
first full run was stopped at 14% without observed failures. The allowlist now
includes that existing status while retaining failure-marker rejection. Real
delivery dataclasses cover benign suppression and failed/retryable/uncertain
markers; repeated mixed provenance/suppression cycles exercise final receipts.
Corrected expanded watch/cadence/session/resource/Telegram/inventory subset:
**208 passed in 39.65 seconds**. Compilation and whitespace checks passed.

The five previous focused PR heads remain unchanged and independently reviewed.
This follow-up must be included in the integration sequence before requesting
owner approval for the complete reliability stack. No Runtime improvement is
claimed before an authorized rollout and natural measurements.
