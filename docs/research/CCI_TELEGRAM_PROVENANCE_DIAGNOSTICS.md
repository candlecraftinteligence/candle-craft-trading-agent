# Telegram provenance diagnostics

The DEV counterexample is an owned pending event without a canonical reservation.
Recovery either silently omitted it or logged it repeatedly without retaining an
event-key disposition. An event-key attempt lookup also has a fallback, which is
not canonical send authority. Existing outbox guards already fail closed.

## Contract

`cci-public-provenance-diagnostics-v1` is read-only. Before delivery reconciliation,
it inspects known public event families in PENDING, RETRYABLE or IN_FLIGHT state.
It reuses the canonical ownership/reservation validation path. Unresolved details
retain event id/key/type/symbol, source update timestamp, epoch, delivery state,
canonical pointer, ownership reason and reservation reason. No message payload,
destination, HTTP receipt or transport credential is collected.

`UNRESOLVED_NO_SEND` means this diagnostic authorizes no send. It is not a new
persisted outbox state, reservation, delivery attempt or database quarantine.
Historical HTTP delivery and completeness remain Unverified. No historical
receipt, outcome, timestamp or reservation is manufactured or repaired.

The default inspection visits at most 256 rows, in SQL pages of 64 and under an
upper event id captured before inspection. It retains at most 50 issue details,
plus counts and omitted-detail count. `coverage_complete=false` means counts are
lower bounds for the declared scope. It is an observation over source rows, not
an atomic historical audit. A persistent large oldest-first backlog may leave
newer diagnostic rows uninspected; incomplete coverage stays explicit.

This inspection is independent of the existing recovery query. Diagnostic limits
cannot limit or starve valid send recovery. Additional diagnostic families do not
become eligible for recovery. Recovery validates canonical association before
using the existing attempt loader; missing/malformed provenance does not authorize
a send. Existing atomic claims, send revalidation and deduplication still apply.

Warnings are limited to ten per inspection with a process-local cache of 1024
fingerprints scoped to database path and event evidence. An unchanged resident
fingerprint is suppressed; changed evidence can warn again. Eviction or process
restart can permit repeats, still within the inspection budget. Suppression never
removes current diagnostic rows or counts.

Delivery summaries carry diagnostics separately from delivery counts. Scan JSON,
SQLite scan payloads, manifests and watch summaries retain them. Unresolved or
incomplete provenance makes the Telegram phase PARTIAL. Disabled collection is
NOT_REQUESTED with N/A count; failed/unavailable collection is explicit and cannot
establish zero unresolved events. One-shot delivery exceptions write diagnostic-only
partial manifests before preserving the original exit; failed persistence reports
both errors. Fatal watch errors retain unavailable coverage in the failed summary.

## Verification and limits

The initial expanded synthetic recovery/ownership/quality/watch/command subset
passed 309 tests. The final targeted provenance and watch-status subset passed
41 tests before retaining the source update timestamp. Full pytest, required CI
and exact-head independent Astra review remain pending. The initial failing test
fixture mixed a SENT attempt with a PENDING event and was corrected to represent
pending state consistently; it did not reproduce an erroneous HTTP send.

No production database was inspected or mutated. The ten historical pending
follow-ups cannot be individually classified from DEV source or supplied counts;
their current state and delivery provenance remain Unverified. Any historical
recovery procedure requires separately approved evidence and must not auto-resend.
