# CCI reliability objective tracker

Checkpoint: 2026-10-24, owner accounting, research stratification, Telegram
provenance and watch-session implementation CODE_COMPLETE; resource observations
under final DEV verification.
DEV investigation based on the user-supplied October 23
audit and source at `a88ed55d8923c1af7078e8c612de4cec106c623b`. Audit counts
are historical observations, not current Runtime measurements. Production was
not accessed. Current Runtime identity, resource headroom, coverage and delivery
state are N/A / Unverified. No rollout is authorized by this tracker.

## A — Scanner continuity

- Status: TESTED.
- Weakness: historical 16h54m absence of normal scanning; four missed start slots.
- Evidence: `schedule_after_iteration` uses monotonic time, accounts for overruns
  and has deterministic tests. The watch loop persists completion/failure
  summaries, but starts are not a durable supervisor lifecycle contract.
- Correction: a shared OS lease before reset/discovery through service cleanup,
  bounded atomic current receipt and separate session observation journal. Actual
  starts/finalization and unobserved completion/shutdown are explicit. Final phase
  status/schedule includes the reporting tail; old duration scope is labelled.
  No automatic restart, process killing or age/PID-based lease breaking.
- Branch: `codex/reliability-watch-session`, based on D final head `2cfd608`.
- Implementation PR: [#144](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/144). Contract:
  `docs/research/CCI_WATCH_SESSION_OBSERVATIONS.md`.
- Tests: 232 broader watch/command/owner/provenance/inventory tests passed.
  Corrected focused session suite: 25 passed; repeated tail-failure case added.
  Final corrected session/cadence/inventory subset: 76 passed in 27.73 seconds.
  Initial oversized fixture ID caused Windows setup errors and was shortened.
- Independent preflight: Astra requested preservation of original exceptions
  across descriptor/temporary cleanup failure and final status recomputation after
  watch-output failure. Both corrected with failure-injection regressions. Final
  full pytest on `c8d2a2fdc78f7b31c5d93152c8ed4b4c0b3ed65b`:
  **3455 passed, 1 existing dependency warning in 1641.43 seconds**, no failures
  or skips. Astra APPROVE for that exact implementation head, independently
  44 session/cadence/inventory tests passed. [Required CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/39994803059)
  succeeded. Dependency, compile, whitespace and invariant checks passed.
- Handoff: `docs/handoffs/CCI_RELIABILITY_WATCH_SESSION.md`. The final
  documentation-only head needs its own CI/review. A subsequent Astra design
  review could not run because the reviewer account reached its usage limit;
  no approval for that review is inferred.
- Final documentation head `1830aa87b2c47ca5b2e51df1cc5a12c453c464fc` subsequently
  received Astra APPROVE after the usage window reset, and [CI success](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40028951292).
  PR #144 is ready for owner review, unmerged and undeployed.
- Deployment / Runtime verification: not deployed / N/A.
- Uncertainty: outage cause cannot be established from DEV source. P4 causation
  is Unverified. Requires genuine launcher/process/manifest evidence.
  The supplied historical 16h54m interval is missing observation coverage; exact
  start/end UTC are N/A in the supplied summary. No outage receipt is fabricated.

## B — Existing-owner monitoring accounting

- Status: TESTED.
- Weakness: nine historically uncovered owners; monitoring success can obscure
  missing execution evidence.
- Root cause verified in DEV: P5A already selects economic obligations independent
  of discovery. `OwnerMonitoringResult.as_dict` counted only lag rows; evaluator
  exceptions and acquisition subsystem failures could disappear from the count.
  `run_scan` discarded continuation diagnostics after deriving a phase status,
  leaving discovery-pass omissions in saved scan evidence.
- Correction: per-owner dispositions, a complete denominator including failures,
  explicit verified-evaluation counts, and retained discovery/continuation
  diagnostics in scan JSON, SQLite payloads and manifests. Failed discovery watch
  iterations also retain the monitoring report. No eligibility change.
- Branch: `codex/reliability-owner-coverage`.
- Implementation PR: [#141](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/141).
- Implementation SHA: `735d0b4dbe85e79f082e7cb6b6560c7890262f10`.
- Tests: existing P5A/watch subset plus initial regressions: 98 passed. Expanded
  accounting/failure-visibility/binding subset after the first review correction:
  41 passed. Full runs were interrupted at 44% and 10% without failures to
  revalidate review corrections. Final full applicable pytest on `735d0b4`:
  **3390 passed, 1 dependency deprecation warning in 612.93 seconds**. No skips
  or failures. The warning concerns FastAPI/Starlette TestClient's httpx adapter.
  Dependency, compile and whitespace checks passed.
  Corrected accounting/failure/binding plus command/watch suites: 167 passed.
- Independent review: Astra returned CHANGES_REQUIRED for `fc170b0` because
  one-shot error exits preceded durable report persistence, and raised watch
  continuations left incompleteness implicit. The correction writes a diagnostic
  manifest before one-shot failure, marks continuation errors explicitly, and
  preserves discovery-failure startup reports. These records explicitly are not
  scan-completion records. A second review of `b2d8251` found that simultaneous
  discovery and monitoring exceptions still lacked an unknown-coverage report;
  that path now shares the explicit N/A population error report. Synthetic scanner
  artifact defaults are isolated under pytest temp directories. Corrected
  exact-head review of `735d0b4`: **Astra APPROVE**, independently 41 targeted
  tests passed, no remaining actionable findings. Code-review approval only.
- [Required CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/39923605763):
  success for `735d0b4`. Dependency, compile and whitespace checks passed.
- Final documentation head `bdcc90e649fdcd4d5b542f867ba27264a6d038d9` independently
  Astra APPROVED; [final-head CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/39925121114)
  succeeded. PR #141 is ready for owner review, unmerged and undeployed.
- Deployment / Runtime verification: not deployed / N/A.
- Uncertainty: DEV counterexamples do not identify the nine historical owners'
  actual cause. Current collection can expose but cannot repair historical gaps.

## C — Historical lifecycle integrity

- Status: TESTED (supplied research-evidence stratification only).
- Weakness: 73 historical continuity gaps and 20 missing confirmation boundaries.
- Evidence: outcome evaluator rejects absent boundaries and continuity holes;
  `record_closed_candle_evidence_gap` preserves cursor positions and emits no
  synthetic terminal event. B makes current evaluation distinct from lifetime
  completeness; `historical_completeness` stays Unverified.
- Correction: preserve these guards; classify supplied execution-integrity claims
  separately from plan attribution, preserve source timestamps/lineage, and add
  evidence strata to evaluation and plan-interpretation counts. Missing status is
  unavailable; conflicting snapshots are mixed; a reported Verified status with
  a diagnostic has a separate conservative stratum. No original outcome is changed.
- New DEV counterexample: `project_outcome_ownership` accepts a synthetic progress
  row with `integrity_status=Unverified` and a candle-gap diagnostic as an
  interpretable `tp3_terminal` when plan attribution and supplied context match.
  The report already keeps expectancy/P&L unavailable and warns that these are
  snapshot interpretations. It does not stratify that interpretation by supplied
  execution-evidence integrity. This is a reporting-classification issue, not
  evidence that the lifecycle engine fabricated a terminal state.
- Branch: `codex/reliability-research-evidence`, based on B's final head. The
  focused PR will target `codex/reliability-owner-coverage` until integration.
- Implementation PR: [#142](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/142),
  stacked on PR #141; report contract is described in
  `docs/research/CCI_EXECUTION_EVIDENCE_STRATIFICATION.md`.
- Tests: 107 targeted report, attribution, context and cutoff tests passed.
- Astra review of `0220a0f`: CHANGES_REQUIRED for the retained tracking-boundary
  field name. The correction retains actual `tracking_start_at`, lifecycle id,
  namespace and each original source reference. The conflicting-boundary regression
  passes; corrected exact-head review approved the implementation.
- Astra APPROVE for corrected `25cc560`; 89 independently verified targeted
  tests. [CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/39946206142)
  passed. Full local run: 3404 passed, 1 failed, 1 dependency warning in 863.03 s.
  The failure was an existing scanner test that assumed real strategy CPU work
  would finish inside 0.2 s on Windows; it passed alone in 2.30 s. Its timeout
  contract now uses a blocked synthetic symbol and already computed real results,
  with a bounded outer test timeout. Production timeouts and scanner code are
  unchanged. Corrected timeout/report/context/cutoff subset: 108 passed.
  Final full applicable pytest on `30f8d0f38d13949f869c1c9dbd7d4b647192a335`:
  **3405 passed, 1 dependency warning in 633.88 seconds**, no failures or skips.
  Astra APPROVE for that exact head; corrected timeout regression independently
  passed. [Required CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/39948528496)
  succeeded for the same head. Dependency, compile and whitespace checks passed.
- Handoff: `docs/handoffs/CCI_RELIABILITY_RESEARCH_EVIDENCE.md`.
- Final documentation head `e4a5fb9693ce78c9194d32dcac0231b109671c74`: Astra
  APPROVE and [CI success](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/39950075163).
  PR #142 is ready for owner review, unmerged and undeployed.
- Deployment / Runtime verification: not deployed / N/A.
- Uncertainty: no historical candles recovered; none fabricated or relabeled.

## D — Telegram provenance

- Status: TESTED.
- Weakness: ten historical PENDING follow-ups lack canonical reservations.
- Evidence: ownership checks require the canonical pointer and matching event /
  reservation identity; outbox recovery fails closed without that pointer.
- Prospective diagnostic finding: `recover_public_lifecycle_alerts` logs a
  missing-attempt event on every pass and continues without a delivery diagnostic
  for that event. The ten historical events' current state remains Unverified.
- Correction: a bounded read-only inspection of known public event families in
  PENDING/RETRYABLE/IN_FLIGHT, separate from recovery; event-key/canonical/ownership
  reasons, retained run evidence, and PARTIAL status for unresolved or incomplete
  coverage. No delivery attempt is invented. The process warning cache is bounded
  at 1024 fingerprints and ten warnings per inspection. Default inspection is
  256 rows, 64 per SQL page, at most 50 detail rows; incomplete coverage is explicit.
  Existing recovery selection is unchanged and cannot be starved by diagnostic
  limits. Recovery checks canonical association before using any attempt fallback.
- Branch: `codex/reliability-telegram-provenance`, based on C final `e4a5fb9`.
- Implementation PR / independent review: pending; Astra read-only design review
  supported separate diagnostics, canonical guard reuse and explicit coverage.
- Tests: initial targeted run exposed a synthetic fixture mixing SENT attempts
  with a PENDING event; the fixture now represents pending state consistently.
  Expanded provenance/outbox/recovery/quality/watch/command subset: 309 passed.
  Corrected final targeted subset including failure persistence: 41 passed.
  Report contract: `docs/research/CCI_TELEGRAM_PROVENANCE_DIAGNOSTICS.md`.
  Astra APPROVE on `82750b5`, 59 independent tests passed. Full Windows run:
  3428 passed, one failure, one dependency warning in 784.67 s; CI also 3428 passed
  and the same inventory-anchor failure. Two source line references were stale
  after added imports. Catalog and its deterministic artifacts corrected without
  changing the existing checks. Corrected inventory/provenance subset: 58 passed.
  Final full existing-environment Windows pytest on `29056e76c315385fdb5cbb379ea0155434fd1e95`:
  **3429 passed, 1 dependency warning in 1624.99 seconds**, no failures/skips.
  Astra APPROVE for the same head; both existing inventory checks independently
  passed. [Required CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/39986875741)
  succeeded. Longer Windows test runtime cause remains Unverified.
- Implementation PR: [#143](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/143),
  stacked on #142; unmerged and undeployed. Handoff:
  `docs/handoffs/CCI_RELIABILITY_TELEGRAM_PROVENANCE.md`. Final documentation-only
  head receives its own CI/Astra review and is recorded in the PR after publication.
- Final documentation head `2cfd608d5744b6f2dfcce53cfdc568dcd6471223`: Astra APPROVE;
  [CI success](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/39990861792).
  PR #143 is ready for owner review, unmerged and undeployed.
- Deployment / Runtime verification: not deployed / N/A.
- Uncertainty: historical delivery receipts are unavailable in DEV. No event
  mutation or resend has been attempted.

## E — Resource reliability

- Status: IMPLEMENTING.
- Weakness: historically low Runtime RAM/disk and cadence overruns.
- Evidence: scanner already samples RSS through platform APIs; supervisor records
  processing duration before the manifest/admin/presentation tail. It does not
  yet measure the full finalization boundary or all downstream phase latency.
  DEV free C: capacity was approximately 115 GB, not a Runtime measurement.
- Later DEV point observations: 98.8 GiB free C:, 7.86 GiB total physical RAM /
  1.91 GiB available. These sequential observations are not Runtime evidence or a
  resource stability result. Existing readiness policy reserves the greater of
  10 GiB or 10% per volume, plus the documented measured growth/restore terms.
  Preserve this policy; historical F06 schema-26 runbooks are not a current
  schema-27 release authorization or current Runtime capacity evidence.
- Root cause verified in DEV: current telemetry does not attribute downstream
  phase work or current system resources. The new A journal bounds rows but has
  no total append budget. No SQLite bottleneck or historical disk-growth cause
  is established.
- Correction: 19 fixed inclusive elapsed-call profiles with QPC clock metadata;
  one advisory resource point (RSS, Windows physical RAM and proven fixed-local
  system disk), reused reserve floor, and a 32 MiB journal budget. A durable
  terminal marker plus receipt latch prevents post-gap append across abrupt exit
  or restart. Current receipt continues without auto-cleanup or backoff changes.
- Branch: `codex/reliability-resource-observations`, based on A final `1830aa8`.
- Contract: `docs/research/CCI_RESOURCE_PHASE_OBSERVATIONS.md`.
- Implementation PR: [#145](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/145),
  stacked on #144. Astra exact-head review of `8ca919a`: CHANGES_REQUIRED because
  a missing established normal journal could be recreated. Corrected established
  ownership is independent of exhaustion and callback configuration, with
  active-session/restart/legacy/configuration regressions. The first full pytest
  was stopped at 10% without observed failures to verify this correction.
  Corrected broader subset: 242 passed in 46.05 seconds; final corrected
  profile/resource/session/inventory subset: 111 passed in 16.64 seconds,
  including conservative legacy/contradictory-ownership cases.
- Tests: corrected profile/resource/session subset 65 passed; expanded resource,
  watch/command/owner/provenance/stream/cadence/inventory subset 238 passed in
  45.97 seconds. Full pytest, exact-head review and CI pending. Astra preflight
  findings corrected with regressions for restart latching, failure/startup
  warnings and resource timestamp failure. Durable marker also covers abrupt
  helper exit before receipt persistence, exact admission and torn markers.
- Alternating 100-pair DEV filesystem probe: A median 10.816 ms / p95 18.362 ms,
  E including actual local resource point and synthetic profile median 11.499 ms /
  p95 18.059 ms. Receipts 1,128 / 6,979 bytes; 100-finalization journals
  89,294 / 628,430 bytes. No scanner, SQLite or network. Other artifact writes
  and matched Runtime overhead remain Unverified.
- B diagnostic microbenchmark (synthetic DEV structures, 200 repetitions,
  summary plus JSON serialization, excludes disk/SQLite): 41 owners produced
  28,455 bytes versus 15,198 legacy bytes, median 0.416 ms / p95 0.553 ms;
  118 owners produced 80,548 versus 43,476 bytes, median 1.188 ms / p95 2.484 ms.
  These are per report, not total artifact growth; reports can appear in more
  than one artifact. Runtime cadence and storage impact remain Unverified.
- A filesystem-only DEV probe, 100 repetitions of start/finish receipt writes and
  journal append with flush/fsync: median 22.210 ms, p95 37.617 ms using the
  high-resolution QPC timer. Receipt 1,183 bytes; 100 synthetic finalizations plus
  header/start records 94,704 bytes. No scanner, SQLite or network work. The
  scheduling monotonic clock has 15.625 ms resolution on this DEV environment;
  do not imply sub-tick precision or matched Runtime overhead from these points.
- Deployment / Runtime verification: not deployed / N/A.
- Uncertainty: Runtime capacity may require a hardware/storage upgrade. No
  database bottleneck has been established.

## Continuation and release boundaries

Owner-accounting implementation is CODE_COMPLETE with final-head CI and Astra
approval; PR #141 stays unmerged and undeployed. Handoff:
`docs/handoffs/CCI_RELIABILITY_OWNER_COVERAGE.md`.
Research stratification is CODE_COMPLETE at implementation/test head `30f8d0f`;
the final documentation head `e4a5fb9` has CI and Astra approval.
PRs #141 through #144 remain unmerged and undeployed. D's final documentation
head `2cfd608` and A's final `1830aa8` have CI and exact-head Astra approval.
Continue E's corrected full pytest, exact-head review and CI, then prepare the
concrete integration and Runtime verification packet. No historical outage cause was found in
repository notes or the scoped GitHub audit search; it remains Unverified.
Preserve quality 88, grade A, RR 3.0,
confirmation, immutable economics,
freshness and deduplication. P4 shadow remains disabled and non-enforcing.
No merge, deployment, production database access, scanner, listener or external
market-data acquisition was authorized as part of implementation.

Overall state: PARTIALLY_RESOLVED. No objective is VERIFIED_RESOLVED. A passing
DEV suite is not release approval or evidence of natural Runtime improvement.
