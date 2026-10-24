# CCI reliability: provenance-only cadence and backup safety handoff

Date: 2026-10-24. Implementation CODE_COMPLETE: combined source has full Windows
pytest, successful CI and independent Astra approval. Final documentation head
and its separate checks are recorded in PR #146. No merge, deployment or Runtime
authorization.

- Branch: `codex/reliability-provenance-cadence`.
- PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/146.
- Base: `8fa80b62d45b4e8c8bdfb0b149b28fa239f5d048` (PR #145).
- Initial implementation: `d4983b3be215c785d65e2ea362fd7217ba910b66`.
- Cadence correction: `bd897491fb490fcfe1fea817927f2c9311bde2f0`.
- Combined backup-source correction: `0c01aa8a4213d7980fc3ccc898791230ff68aab0`.
- The final documentation head receives separate review/CI and is recorded in PR.

## Problem and correction

The final acceptance audit found that D's truthful provenance PARTIAL status
fed the supervisor's all-PARTIAL operational retry streak. Real pure functions
reproduced a 300-second backoff and next start at 540 seconds for a synthetic
240-second cycle on a 300-second cadence, with zero delivery failures.
This is a prospective DEV interaction, not a cause of historical Runtime gaps.

Keep PARTIAL, provenance uncertainty/counts and bounded warnings. A small pure
policy exempts backoff only when Telegram is the sole adverse phase, original
delivery evidence proves no failure, and a successful bounded provenance
observation explicitly explains the partial result. Missing, malformed, defaulted,
unavailable or failed evidence retains backoff. Known benign blocked_repeat
suppression qualifies; failure markers still do not. Every other operational
failure retains the existing backoff policy and constants.

The policy is recomputed at all four storage/reporting schedule boundaries from
the original prior streak. Later storage/manifest/watch-output failures cannot
inherit a provisional zero-backoff decision. Summaries and the final receipt
retain eligibility, reason and the explicit backoff-eligible operational failure
scope. Historical counters are not retrospectively reinterpreted. Cancellation
and fatal stops remain distinct from retry requests.

## Verification checkpoint

- First subset: 136 passed, one frozen-dataclass fixture error; corrected with
  dataclasses.replace, without relaxing production invariants.
- Initial expanded subset: 201 passed in 37.96 seconds; focused subset after
  explicit inspection-error coverage: 91 passed in 17.53 seconds.
- Astra CHANGES_REQUIRED on `d4983b3` for the omitted existing healthy
  blocked_repeat status; 83 independent tests passed and its CI succeeded.
  The first full run was stopped at 14% without observed failures for this fix.
- Corrected expanded watch/cadence/session/resource/Telegram/inventory subset:
  **208 passed in 39.65 seconds**. Real delivery dataclasses cover benign
  suppression versus failed/uncertain/retryable markers. Real synthetic storage,
  manifest and watch-output failures follow two diagnostic-only cycles and recover.
- Astra **APPROVE** for exact `bd89749`; **45 independent tests passed in
  5.91 seconds**, clean whitespace/working tree, no remaining actionable findings.
- Corrected full Windows pytest: **3552 passed, 1 existing dependency warning in
  651.59 seconds**, no failures or skips. The warning is the existing
  Starlette/httpx TestClient deprecation. The first
  corrected-head CI attempt had 3551 passes and one failure in the unchanged
  backup snapshot-boundary test: quick_check failed. The exact test passed on
  fresh synthetic DEV data (1 passed in 0.62 seconds); cause remains Unverified.
  One retry reproduced the same sole failure (3551 passed / 1 failed in 340.83
  seconds); no integrity check is waived. Independent diagnosis
  confirmed a pre-existing immutable assumption on a potentially changing backup
  source; its relation to this CI failure is unproven. PR #146 includes the
  narrow actual-acquisition correction, read-only locking proof and coherent
  snapshot tests with both absent and existing sidecars. Complete maintenance
  subset: 61 passed in 14.99 seconds. Combined maintenance/readiness/cadence
  subset: **162 passed in 29.21 seconds**. Dependency, compilation and whitespace
  checks passed. Exact combined source `0c01aa8` received Astra APPROVE with
  **61 independent maintenance tests passed in 15.65 seconds**, clean tree and
  whitespace; no actionable findings. [Combined-source CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40063440798)
  succeeded: **3553 passed, 1 existing dependency warning in 221.43 seconds**,
  no failures or skips. Full Windows pytest on exact combined source `0c01aa8`:
  **3553 passed, 1 existing dependency warning in 646.33 seconds**, no failures
  or skips. The warning is the existing Starlette/httpx TestClient deprecation.
  Dependency, compilation, whitespace and invariant checks passed. The source
  inventory line reference/artifacts were regenerated after an import shifted it.

## Limits and continuation

No delivery/recovery selection, canonical reservation, public quality 88/A/3,
confirmation, economics, lifecycle outcome, schema 27 or P4 policy changed.
No live scanner/listener, production database, exchange request or Telegram send.
The historical ten events remain individually Unverified; no event is adopted,
repaired, resent or given a synthetic receipt. Natural Runtime cadence is unknown.

Planning still requires independently established source quiescence; absent
sidecars do not prove it. Dry-run no-artifact behavior is preserved and tested
during and after source access. Actual backup coordination may create WAL/SHM;
never delete them. The retained planner limitation and scope are recorded in
`docs/research/CCI_MUTABLE_BACKUP_SOURCE_SAFETY.md`.

Finish final documentation exact-head review/CI before requesting owner approval
for integration in
`docs/handoffs/CCI_RELIABILITY_INTEGRATION_RUNTIME_PACKET.md`. Runtime preflight,
cutover and natural verification remain separate. The mission is not complete
based on DEV results alone.
