# Reliability integration result

Date: 2026-10-24. Implementation CODE_COMPLETE and integrated. Overall mission:
PARTIALLY_RESOLVED. No objective is VERIFIED_RESOLVED from DEV evidence alone.

The owner explicitly approved integration of PRs #141-146 in order, preserving
their accepted commits and candidate `637191f475ee834130c0ae0c88a74e7916812dc7`,
followed by CI and independent Astra review of the resulting main commit.
That approval excluded Runtime preflight and deployment.

## Immutable integrated candidate

- Main: `6785cf6c5a5d4c083f31a280c2ca1555bc7df88c`.
- Complete tree: `e98bf4dd11f20da8374fd54cf9537acf7a02f1cb`.
- Approved candidate: `637191f475ee834130c0ae0c88a74e7916812dc7`, same tree.
- Original main base: `a88ed55d8923c1af7078e8c612de4cec106c623b`.

| PR | Preserved accepted head | Resulting merge commit |
| --- | --- | --- |
| [#141](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/141) | `bdcc90e649fdcd4d5b542f867ba27264a6d038d9` | `53382866938ceb90cf61c2bacee6e213f2a1a63e` |
| [#142](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/142) | `e4a5fb9693ce78c9194d32dcac0231b109671c74` | `c331192658fdaf3dc0fbce4ff09442c5562c0096` |
| [#143](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/143) | `2cfd608d5744b6f2dfcce53cfdc568dcd6471223` | `619ce492ef0b1a2b4d676b1d0635535f53cc125b` |
| [#144](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/144) | `1830aa87b2c47ca5b2e51df1cc5a12c453c464fc` | `7a3350fa2fb11ae33a571184f2de337195c6b22a` |
| [#145](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/145) | `8fa80b62d45b4e8c8bdfb0b149b28fa239f5d048` | `9fedfeb970bf4f3c273bff5d684447b167162efa` |
| [#146](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/146) | `637191f475ee834130c0ae0c88a74e7916812dc7` | `6785cf6c5a5d4c083f31a280c2ca1555bc7df88c` |

All six exact heads and their successful CI were rechecked before integration.
PRs #142-146 were retargeted to main in order; their recalculated merge states
were CLEAN/MERGEABLE and current checks successful. Merge API calls pinned the
expected head and used merge commits. No squash, rebase, force push or branch
deletion was used. Intermediate main CI could be superseded by later pushes;
the final exact-main run below is the integration evidence.

## Verification

- Local Git objects and remote main metadata confirmed the final SHA/tree.
  Full-tree diff against candidate `637191f` was empty, exit code zero.
- Independent Astra APPROVE for exact main `6785cf6`: all six accepted heads
  remain ancestors, every merge tree matches its accepted head, final tree equals
  the approved candidate, no source differences or new safeguard changes.
- [Exact-main push CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40070934655)
  completed successfully for `6785cf6`: **3553 passed, 1 existing dependency
  warning in 280.94 seconds**, no failures or skips. Dependency verification and
  compilation also passed. The warning is the existing Starlette/httpx TestClient
  deprecation. The PR-filtered workflow connector is insufficient to find this
  push run; GitHub CLI identified the exact event/SHA and connector job logs
  supplied the actual test result.
- The unchanged implementation already passed full Windows pytest: **3553
  passed, 1 existing dependency warning in 646.33 seconds**. It was not rerun
  solely for identical merge contents. Final candidate CI and both source and
  documentation Astra reviews are retained in PR #146.
- DEV checkout and linked worktrees remained unchanged during integration.
  Afterwards, branch `codex/reliability-integration-handoff` was created from
  exact main solely for this receipt/tracker update. It changes no runtime code
  and is not permission to merge an additional documentation PR.

## Remaining work and authorization

Runtime was not accessed or changed. The actual running release, schema/index
compatibility, processes/consumer paths, resource headroom, growth/restore budget,
filesystem locking, source capture configuration and external listener state
remain N/A / Unverified. No verified authorized Runtime connection is assumed.

The next phase is separately authorized bounded read-only preflight on the
reported Runtime host `DESKTOP-G7M42AL`, using the evidence requirements in
`CCI_RELIABILITY_INTEGRATION_RUNTIME_PACKET.md`. Explicit filesystem-only
collection precedes any separately bounded SQLite work. No service control,
deployment, maintenance, event repair or resend follows from integration approval.

Backup planning still requires independently established source quiescence;
absent WAL/SHM do not establish it. Actual backup coordination may create
sidecars. No backup, checkpoint, cleanup or sidecar deletion was performed.
See `../research/CCI_MUTABLE_BACKUP_SOURCE_SAFETY.md`.

Only after preflight may a concrete cutover package fix consumer identities,
recovery safeguards and observation/stop budgets for separate rollout approval.
Natural Runtime measurements remain necessary before operational resolution.
The historical outage cause, nine owners and ten pending event cases remain
individually Unverified. Historical missing evidence is not reconstructed.

Quality 88/A/3, confirmation, economics, freshness/integrity, outbox deduplication,
schema 27 and disabled/non-enforcing P4 are preserved. No order, withdrawal or
transfer capability was introduced.
