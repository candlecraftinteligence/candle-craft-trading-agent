# CCI V2 telemetry persistence and coverage review

DEV implementation only. No merge, deployment, Runtime restart, Runtime database
access, environment edit, CMC activation or Telegram delivery is part of this work.

Branch: `codex/cci-v2-telemetry-coverage`.
Base: `965f3cef1eb112e61e6a1213fae3bff0c7c21c6e` (merged repair PR #169).
Deployed implementation identified by the owner:
`677181fab8a6bbff316b0d0a0c551cc82d1faab1`.
The PR handoff records the final commit and exact-head verification receipts.

## Correction

`scripts/run_scan.py::_combined_runtime_stats` previously reconstructed runtime
statistics without `derivatives_prefetch`, `stream_health`, `analysis_worker` or
`cmc_acquisition`. It now forwards each mapping from the authoritative runner.
Without runner statistics, those mappings remain empty. Existing totals, rounding,
symbol outcomes, timeouts and memory accounting retain their existing behavior.

`app/command_center.py::build_command_center_payload` exposes the same mappings
under `runtime_metrics`. This is an additive structured reporting change; delivery
and message formatting are unchanged. Storage and native hydration already retain
the full model and require no schema change.

The offline regression executes a real `ScannerRunner` against the existing
synthetic HTTP market, combines its results, writes a synthetic epoch-bound SQLite
database, checks `runtime_stats_json`, hydrates both `inline_v1` and `symbol_refs_v1`,
validates the native `ScannerRunResult`, and reads operational artifacts and the
command-center report. All four nonempty mappings must match the runner at every
boundary, including false/null CMC-off values and missing stream observations.
Separate assertions preserve mixed symbol accounting and unavailable memory data.

## Historical coverage evidence and disposition

The owner's latest restart observations supplied with this task are historical
evidence: aggregate flow **0 VERIFIED / 55** per early iteration, liquidations
**0 available windows**, frozen books **42 VERIFIED / 55**, and **30 aggregate-ID-gap
windows in iteration 2**. These are not new DEV measurements. The only local Runtime
handoff found in `C:\CCI_Architecture_Handoffs` was the earlier
`CCI_V2_REPAIR_RUNTIME_PREFLIGHT.md`; it predates cutover and cannot establish the
later per-symbol timings or reasons. No Runtime database or feed was inspected.

Inspection and deterministic replays establish the current contracts:

- Aggregate flow uses the complete closed **15m** window for summary status.
  Partial warm-up cannot be VERIFIED. A gap in aggregate ID `a` resets coverage;
  underlying trade IDs `f`/`l` do not define aggregate continuity. Restoring fresh,
  contiguous observations must cover a new full closed window after the last gap.
  Duplicates and older IDs are discarded; stale last events remain STALE.
- Watch mode owns long-lived services and reconciles symbol changes in place.
  Reconciliation of an unchanged set preserves its aggregator, acknowledgements
  and coverage; it does not restart the warm-up or send another subscription.
- Liquidations subscribe to `!forceOrder` with all-market scope. Only the matching
  successful acknowledgement starts observation coverage. Each 1m/5m/15m window
  needs its own full closed interval; the summary uses 15m. Unobserved windows
  retain missing counts/notionals. A quiet, continuously covered window can report
  observed zero, without inventing an event. Disconnect invalidates coverage.
- Book verification requires a valid snapshot/diff bridge, continuous sequence and
  freshness. It has no 15-minute warm-up. Frozen snapshots retain their original
  time and values when later depth arrives; a fresh update cannot retroactively
  refresh a stored decision. Synchronization and full depth-band coverage remain
  separate facts.

The aggregate/liquidation summary counts are compatible with early 15-minute
warm-up, and genuine ID gaps extend aggregate warm-up. The supplied counts alone
do not establish why 13 books were withheld or why the ID discontinuities occurred.
If "0 available windows" includes every short liquidation window after its full
acknowledged interval, warm-up alone is insufficient to explain that observation.
The necessary per-symbol timing/acknowledgement/reason evidence is unavailable here.
No concrete parser, subscription, continuity or snapshot-lifecycle defect was
demonstrated. **Stream coverage remains awaiting longer natural Runtime coverage
observations.** No production stream code or freshness/coverage guard was changed.

## Verification and safety

Focused telemetry/stream tests: **22 passed** on the existing DEV CPython 3.12.10
environment. `compileall -q app scripts src tests`, `pip check` and `git diff --check`
passed. The PR handoff records the complete suite and Linux/Windows exact-head CI
outcomes, including any failures or skips, before completion is claimed.

Initial sandboxed asyncio runs stalled creating Windows' local socket pair and
were interrupted; offline tests then ran with loopback available. Initial test
fixture failures were corrected by passing the same ranks to CLI serialization and
storage and comparing serialized symbol fields (transient model fields are excluded
by the existing storage contract). No production assertion or guard was removed.

Only the combiner, structured reporting, tests and this handoff change. The
1D/12H/4H/15M structure, Fibonacci 0.618/0.750/0.786, protected SL, TP2 net RR >= 3,
Grade A/public gates, canonical Telegram route, schema 27, epoch and CoinPaprika
ranking are unchanged. Verification uses CMC_MODE=off, LOCAL_MANUAL_MODE=true,
ORDER_EXECUTION_ENABLED=false and Telegram dry-run/disabled settings, without
loading dotenv files or changing any environment file.

After separately authorized adoption, observe natural per-symbol source/receive
times, identity freshness, requested/acknowledged subscriptions, assessment reasons,
gap/resync counts and connection history. Judge each full closed window from the
most recent acknowledged connection or coverage reset. Correlate persistent gaps
or unavailable books with those facts before proposing another correction. No
scanner strategy change or broader signal eligibility is recommended.
