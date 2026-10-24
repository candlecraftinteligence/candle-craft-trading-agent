# Reliability integration and Runtime verification packet

Integration update, 2026-10-24: the owner separately approved integration only.
PRs #141-146 are now merged, preserving accepted commits. Verified main is
`6785cf6c5a5d4c083f31a280c2ca1555bc7df88c`, with exact-main Astra approval and
successful push CI. See `CCI_RELIABILITY_INTEGRATION_RESULT.md` for the immutable
receipt. The preparation and proposed sequence below are retained as the approved
historical plan. Runtime requirements remain unfulfilled and separately authorized.

Prepared in DEV, 2026-10-24. State: **PARTIALLY_RESOLVED**. This packet authorizes
no merge, deployment, service change or production access. All Runtime evidence
below is still required. The current DEV assignment permits code, synthetic
tests, focused branch pushes, PRs and independent review only.

## Pinned change sequence

Remote PR metadata was rechecked during this preparation: #141 through #145 are
open, ready for review and unmerged. The live `main` base reported by #141 is
`a88ed55d8923c1af7078e8c612de4cec106c623b`. Recheck it immediately before any
approved integration; an unexpected change requires reassessment, not force push.

| Order | PR / objective | Approved final head | Evidence |
| --- | --- | --- | --- |
| 1 | [#141 owner accounting](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/141) / B | `bdcc90e649fdcd4d5b542f867ba27264a6d038d9` | Astra APPROVE; full implementation 3390 passed; [final CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/39925121114) |
| 2 | [#142 evidence strata](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/142) / C | `e4a5fb9693ce78c9194d32dcac0231b109671c74` | Astra APPROVE; full implementation 3405 passed; [final CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/39950075163) |
| 3 | [#143 Telegram provenance](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/143) / D | `2cfd608d5744b6f2dfcce53cfdc568dcd6471223` | Astra APPROVE; full implementation 3429 passed; [final CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/39990861792) |
| 4 | [#144 watch sessions](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/144) / A | `1830aa87b2c47ca5b2e51df1cc5a12c453c464fc` | Astra APPROVE; full implementation 3455 passed; [final CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40028951292) |
| 5 | [#145 resource observations](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/145) / E | `8fa80b62d45b4e8c8bdfb0b149b28fa239f5d048` | Astra APPROVE; full implementation **3507 passed**; [final CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40033691462) |
| 6 | [#146 cadence and backup safety](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/146) / A-D-E | Combined implementation `0c01aa8a4213d7980fc3ccc898791230ff68aab0`; final documentation head recorded in PR | Astra APPROVE; full Windows **3553 passed**; [source CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40063440798) **3553 passed** |

The branches form a linear source ancestry in that order. The final cadence source
contains the previous five corrections; this is not six independent patches to
apply to arbitrary source. Each handoff and the objective tracker retain earlier
failures, review corrections, microbenchmark limits and historical uncertainties.
GitHub's direct `main...0c01aa8` comparison confirmed the expected base,
20 commits ahead and zero behind at the source-review checkpoint. Final
documentation review and CI must be recorded with the published final head in
PR #146 before integration approval is requested.
E final documentation head is `8fa80b62d45b4e8c8bdfb0b149b28fa239f5d048`, with
Astra APPROVE and [CI success](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40033691462).
The final acceptance audit found that persistent provenance-only PARTIAL cycles
would accumulate retry backoff. The focused sixth correction is described in
`docs/research/CCI_PROVENANCE_ONLY_CADENCE.md`.
Its CI diagnosis confirmed a pre-existing unsafe immutable assumption in actual
backup acquisition. The same PR includes that narrow correction; the combined
source now has full Windows pytest, successful CI and Astra approval. Final
documentation review/CI must be recorded at the published PR head before owner
integration approval. The raw mechanism of the earlier CI quick_check failure
remains Unverified; the corrected candidate passes that regression on both hosts.

## Proposed integration, requiring owner approval

1. Recheck all six exact PR heads, required CI, independent approval, remote base
   and clean DEV state, including the combined backup correction. No branch
   deletion, reset or force push is needed. The final documentation head must
   have its own successful CI and Astra review recorded in PR #146.
2. Merge #141 into main using a merge commit that preserves its accepted commits.
3. Retarget #142 to main, verify its now-current required checks and unchanged
   approved head, then merge. Repeat in order for #143, #144, #145 and the pinned
   provenance-only cadence follow-up #146. Do not
   squash or rewrite the stacked ancestry. Stop for unexpected source changes,
   conflicts, stale approvals or failed checks.
4. Record the resulting immutable main SHA. Verify its complete source tree
   against the accepted final cadence head; investigate every difference. Obtain
   successful main/push CI and independent review of that exact integration SHA.
   A GitHub synthetic merge preview is not the release identity.
5. Keep Runtime unchanged. Integration approval does not authorize deployment,
   production queries, scanner/listener control, event repair or historical resend.

If owner approval covers only integration, stop at the verified integrated commit
and present that immutable release candidate for a separate Runtime decision.

## Runtime preflight, separate authorization and execution on Runtime

The reported host is `DESKTOP-G7M42AL`; the reported code path is
`C:\Users\aspir\Desktop\Candle Craft Inteligence`. These are supplied identities,
not a verified current connection. DEV must not mount, query, copy or inspect the
live database. No established authorized Runtime connection is assumed here.

Before proposing cutover, obtain bounded, sanitized evidence on Runtime:

- Host identity, actual scanner/listener PIDs and loaded application identity,
  launch ownership, canonical code/database/manifest/state paths, and all other
  consumers. Verify deployment from running-process evidence rather than a checkout
  SHA alone. Do not collect tokens, environment files or complete command lines
  that could contain secrets. Confirm one existing listener and no duplicate loop.
- Current application/schema compatibility. This candidate preserves **schema 27**
  and adds no database migration. The older F06 schema-26 packet is historical;
  it is not approval of a schema-27 rollout. Use the existing readiness collector's
  current supported schema and actual-source compatibility results.
- Verify source replay capture explicitly disabled for the initial cutover.
  Unknown is insufficient. Enabled capture needs separate capacity, retention
  and recovery review; this packet does not authorize its activation.
- Current physical memory availability, scanner RSS observations and fixed-local
  C: space. Independently identify the database/recovery volumes and filesystems;
  a drive letter alone is not a durability or capacity proof. Prove cooperative OS
  locking and canonical-path use on the actual filesystem without launching a
  second scanner or breaking a lease by PID/age.
- Preserve the existing reserve floor (max 10 GiB / 10% per volume), measured
  growth, restore budget and minimum ten-day horizon. Obtain at least two comparable
  byte-size points separated by 24 hours. A single above-floor point or low RSS is
  insufficient. If RAM/storage cannot meet a defensible budget, propose a capacity
  upgrade before rollout; do not reclaim protected evidence.
- Separate actual-schema copy/restore compatibility proof and a recovery plan
  accepted by the owner. Use only the existing approved procedure after its
  current identities and budgets have been fixed. Never treat a changing SQLite
  file as immutable or repair missing WAL/SHM sidecars to make a preflight pass.
  The rehearsal must verify epoch lineage and preservation, confirmation-
  consumption receipts, and external listener state as well as database content.
  Verify the final schema and exact required index definitions again before any
  consumers start. No new migration in this patch does not prove compatibility
  with an older actual Runtime schema; any required migration needs approval.

The existing `scripts/runtime_checkpoint_readiness.py collect` requires explicit
existing source and new output paths. Explicit `collect --filesystem-only`
opens no SQLite connection; plain `collect` opens bounded SQLite metadata even
without `--include-recent-runs`. Optional indexed recent-run sampling uses the established
250 ms busy budget, 2000 ms query deadline and default five-row limit, with query
plan proof. Run only on the separately authorized Runtime host/tool checkout;
no command in this packet is an instruction to use the live path on DEV.
The collector never manufactures GO_FOR_RUNTIME_DEPLOYMENT. Do not substitute
maintenance inspect, table counts, dbstat, integrity checks, checkpoint or VACUUM.

The existing SQLite backup planner still requires independently established
source quiescence; its immutable fallback does not prove concurrent safety.
Do not use it against a changing source with absent sidecars. Ordinary actual
backup read coordination may create WAL/SHM; never remove them to simulate a
read-only filesystem. The acquisition correction does not authorize any backup
or service stop. See `docs/research/CCI_MUTABLE_BACKUP_SOURCE_SAFETY.md`.

Fix the exact release SHA, all consumer paths, observation/stop budgets, recovery
steps and public-delivery settings in a concrete cutover package before asking
for rollout approval. Verify no old/new listeners overlap. A code rollback must
preserve new sidecars and evidence; never delete unknown files to unblock startup.

## Natural observation and acceptance after an approved rollout

Use a comparable 48-hour natural window where practical, with fixed universe,
cadence, configuration and maintenance context. Record UTC endpoints and the
running release identity. Do not inject production signals, run manual duplicate
loops, fabricate candles or resend historical events to create test activity.

| Objective | Evidence required before operational resolution |
| --- | --- |
| A | Reconcile real starts/finalizations, phase outcomes, cadence lag, missed slots and session boundaries with manifests. Every absence remains explicit; investigate unexpected unobserved completion/shutdown using genuine launcher/process evidence. Do not infer a crash from an absent receipt. Independent observation must notice downtime while the scanner is not running. |
| B | Account for every eligible current owner, including out-of-discovery owners, with one evaluation or attributable unavailable/error disposition. Verify the nine historical cases individually using genuine retained evidence; current evaluation does not establish historical completeness. |
| C | Retain the 73 continuity gaps and 20 missing-boundary cases as Unverified until real evidence supports another classification. Validate cursor/geometry immutability and evidence-stratified research interpretation; no fabricated outcome or reconstructed-as-prospective history. |
| D | Read-only event-key classification of the ten historical pending follow-ups, with canonical/ownership reasons and unresolved-no-send disposition where applicable. Confirm naturally created valid events keep canonical reservation/deduplication behavior. No historical mutation, adoption, synthetic HTTP receipt or automatic resend. |
| E | Compare scanner/full-cycle and inclusive phase distributions, RSS/available-memory points, current disk floor plus measured growth/restore headroom, and incremental diagnostic bytes. Include all artifact copies, not just the bounded session journal. Attribute a bottleneck only from relevant measured boundaries. Keep final observation-persistence/cleanup cost Unverified unless independently measured. |

Review the distribution and missed-slot changes against comparable source data;
do not infer causation from the synthetic DEV benchmark or unrelated medians.
Define material performance/capacity acceptance and stop budgets in the approved
Runtime package before observing results, rather than choosing them afterward.
Resource advisories alone never grant capacity approval or weaken signal policy.

Journal exhaustion means incomplete diagnostic history, not an opportunity to
delete evidence. Archive/generation management requires its own reviewed procedure.
The bounded current receipt continues until a critical integrity failure; unknown,
torn or missing established sidecars require an operator decision, not automatic
repair. An independent observer and an archive plan remain operational work.

## Honest completion boundary

Public quality 88 / grade A / RR 3.0, confirmation, liquidity/structure,
freshness/integrity, immutable economics, outbox identity/deduplication, schema 27
and disabled/non-enforcing P4 must remain unchanged. No orders, withdrawals or
transfers are introduced. A passing DEV suite or merged PR is not Runtime proof.

The historical 16h54m interval remains missing observation coverage; exact UTC
endpoints and cause are N/A / Unverified from the supplied summary. The nine
owners and ten pending events are individually Unverified in DEV. Current Runtime
capacity and actual natural improvement are N/A. Do not mark any objective
VERIFIED_RESOLVED until its required evidence exists. Historically unrecoverable
limitations may remain honestly represented without forcing their counts to zero.
