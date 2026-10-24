# CCI reliability: resource-observations handoff

Date: 2026-10-24. Implementation **CODE_COMPLETE**, independently approved with
successful full pytest and CI. Final documentation review/CI, owner integration,
release and natural Runtime verification remain pending. No operational approval.

- Branch: `codex/reliability-resource-observations`.
- PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/145.
- Base: `1830aa87b2c47ca5b2e51df1cc5a12c453c464fc` (PR #144).
- Implementation: `8ca919ab158e92ec04c5e45eb94bb1233ec043d4`.
- Corrected implementation: `d063c3f8b9ed2b1cad998094671ba1373023089e`.
- Final implementation/boundary correction: `3aaac3bfeeaf1ba68e8722de373ac92d63d0bd59`.
- Final documentation-only head receives separate review/CI and is recorded in PR.

## Problem and correction

Existing statistics do not attribute downstream lifecycle, monitoring, storage
and reporting work. Historical Runtime capacity points are not current resource
evidence. A's session journal bounds individual rows but needs a total budget.

Nineteen fixed phase names record inclusive elapsed-call time with high-resolution
clock metadata, counts, active calls and raised calls. Missing/invalid timing is
N/A / Unverified. Returned calls do not imply business success; nested timings
overlap. Resource sampling records this process's RSS, Windows physical memory
and only a proven fixed-local system disk. The existing planning reserve floor
is advisory; no capacity GO, new RAM threshold or scan backoff is introduced.

Earlier artifact snapshots name their boundaries. The final receipt/journal
profile reaches reporting/presentation, before its own final persistence. Primary
scan storage is measured as a whole call, not attributed to the SQLite engine.
Ordinary telemetry errors cannot replace scan exceptions or halt iteration work;
critical lease/receipt/history integrity failures remain fail-closed.

The journal admits rows only within 32 MiB, reserving 256 bytes for a final
exhaustion marker. Marker fsync prevents recording from resuming after abrupt exit
with a stale receipt. Established-history ownership separately prevents missing
normal journals from being recreated, across restarts or callback changes.
Unknown/torn/missing history is preserved without repair. Budget exhaustion emits
one advisory per session, retains bounded incomplete-coverage metadata and keeps
the current receipt. No archive, deletion, compression or retention daemon exists.

## Verification checkpoint

- Expanded initial subset: 238 passed in 45.97 seconds.
- Corrected expanded subset: 242 passed in 46.05 seconds.
- Final corrected profile/resource/session/inventory subset: 111 passed in
  16.64 seconds, including legacy and contradictory-ownership cases.
- Astra CHANGES_REQUIRED on `8ca919a` for missing normal-history recreation;
  70 independent tests passed. Its full run was stopped at 10% without observed
  failures to apply the review correction. This is not a completed full pass.
- Astra **APPROVE** on exact corrected `d063c3f`; independently **77 passed in
  14.63 seconds**, no remaining actionable findings. Its cache-disabled test run
  produced a pytest configuration warning. Whitespace check passed.
- Final full existing-environment Windows pytest on `3aaac3b`:
  **3507 passed, one existing dependency warning in 646.34 seconds**, no failures
  or skips. The warning is FastAPI/Starlette's deprecated httpx TestClient adapter.
  Dependency, compile and invariant checks passed. Source inventory anchor/artifacts
  were regenerated after imports moved the watch-default line, preserving checks.
- Final boundary review found the initial SQLite PENDING payload's legacy
  duration scope described the later manifest boundary. Only that label was
  corrected; a real synthetic storage-path test checks both snapshots. The
  `d063c3f` full run was stopped at 71% without observed failures for this fix.
  Final watch/session/resource/inventory subset: 139 passed in 29.44 seconds.
  Astra APPROVE for exact `3aaac3b`; the new regression independently passed
  in 1.12 seconds. [Required CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40032395331)
  succeeded for that exact source. Final full Windows pytest passed as above.
- Matched alternating DEV persistence probes and exact byte sizes are retained in
  `docs/research/CCI_RESOURCE_PHASE_OBSERVATIONS.md`. Corrected-source median
  increment: 0.684 ms in 100 synthetic pairs; no matched Runtime claim.

## Risks and next step

Current Runtime RAM, C: growth, safe sustained headroom and actual phase latency
remain Unverified. The synthetic journal projects roughly 18 days at five-minute
iterations or 3.7 days at one minute; extra session boundaries reduce that horizon.
Existing artifact copies also consume space. Plan capacity and an independently
approved archive/generation procedure; restarting does not clear the cap.

Full natural iteration latency includes costs outside individual measured
boundaries; final observation persistence and service cleanup are excluded from
the final profile. No database bottleneck or historical outage cause is claimed.
The earlier 4 GB / 221 MiB / 1.51 GB Runtime observations may require hardware or
storage remediation; DEV measurements cannot establish current Runtime safety.

Public 88/A/3, confirmation, economics, freshness, lifecycle outcomes, outbox
deduplication, schema 27 and disabled/non-enforcing P4 remain unchanged. No live
scanner/listener, exchange request, production database or Telegram send occurred.

Checkpoint final documentation with its own exact-head review/CI, then request
separate owner approval for the integration sequence and Runtime packet:
`docs/handoffs/CCI_RELIABILITY_INTEGRATION_RUNTIME_PACKET.md`. Do not merge or
deploy automatically. Overall mission remains PARTIALLY_RESOLVED until authorized
release and genuine natural Runtime evidence satisfy the objective tracker.
