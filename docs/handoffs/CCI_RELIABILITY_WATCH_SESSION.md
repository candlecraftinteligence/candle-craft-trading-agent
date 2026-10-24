# CCI reliability: watch-session handoff

Date: 2026-10-23. Implementation CODE_COMPLETE; final documentation review,
owner integration, release and natural Runtime verification remain pending.

- Branch: `codex/reliability-watch-session`.
- PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/144.
- Base: `2cfd608d5744b6f2dfcce53cfdc568dcd6471223` (PR #143).
- Implementation head: `c8d2a2fdc78f7b31c5d93152c8ed4b4c0b3ed65b`.
- Final documentation head is recorded in the PR after publication.

## Correction

The cooperating watch CLI lacked a shared startup lease and durable start
observation. Reporting work could follow its last duration/status calculation.
A nonblocking OS lease now covers reset, discovery, startup, every iteration and
all service cleanup attempts. A bounded atomic receipt and separate journal
record session starts, iteration starts/finalization and prior unobserved
shutdown/completion. Unknown sidecars fail closed without repair. PIDs and ages
never break leases. No restart supervisor or process termination was added.

Finalization includes manifest, reporting and presentation work; watch-output
failures update final status and consecutive-failure policy. Cancellation and
startup failure remain distinct. Original errors survive temporary-file,
descriptor and service cleanup errors. The receipt's finalization boundary
excludes its own subsequent persistence and final service cleanup. Historical
duration fields retain their declared earlier scope.

## Verification

- Expanded watch/command/owner/provenance/inventory subset: 232 passed.
- Final corrected session/cadence/inventory subset: 76 passed in 27.73 seconds.
- Full Windows pytest on the implementation head: **3455 passed, one existing
  FastAPI/Starlette dependency warning in 1641.43 seconds**, no failures/skips.
- Astra APPROVE for the exact implementation head; 44 independent tests passed.
- Required CI succeeded:
  https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/39994803059.
- Dependency, compile, whitespace and invariant checks passed. Synthetic helper
  process exit, concurrent starts, reporting delays, repeated failures and cleanup
  failure injection are covered. An early oversized fixture ID caused Windows
  setup errors; short explicit IDs fixed that fixture, without weakening checks.
- No strategy gates, schema, lifecycle economics, outbox identity or P4 policy
  changed. No production, scanner service, listener or live transport was run.

## Risks and next work

The historical 16h54m interval is missing observation coverage. Its exact UTC
boundaries and cause remain N/A / Unverified; P4 causation is not established.
Only cooperating processes sharing a canonical path honor this lease. Hard-link
aliases, other binaries and unverified filesystem locking remain limitations.
An independent observer is still needed to notice absence while the process is
down. The receipt does not prove the OS process subsequently exited.

Each receipt and journal row is bounded, but the journal is append-only with no
automatic retention. Objective E must bound total diagnostic collection without
deleting evidence and measure timing/resource overhead. Capacity planning must
retain the existing reserve, growth and restore requirements. Current Runtime
resources and natural continuity remain Unverified.

The final documentation checkpoint requires its own exact-head review and CI.
A later independent E design review was unavailable because Astra hit the account
usage limit; it is not treated as an approval. Continue authorized E development,
then obtain the missing review before any release. No merge or deployment is
authorized by this handoff.
