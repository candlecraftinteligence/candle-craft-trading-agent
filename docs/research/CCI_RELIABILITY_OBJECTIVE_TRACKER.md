# CCI reliability objective tracker

Checkpoint: 2026-10-23, owner accounting CODE_COMPLETE; research stratification
implemented and under review. DEV investigation based on the user-supplied October 23
audit and source at `a88ed55d8923c1af7078e8c612de4cec106c623b`. Audit counts
are historical observations, not current Runtime measurements. Production was
not accessed. Current Runtime identity, resource headroom, coverage and delivery
state are N/A / Unverified. No rollout is authorized by this tracker.

## A — Scanner continuity

- Status: INVESTIGATING.
- Weakness: historical 16h54m absence of normal scanning; four missed start slots.
- Evidence: `schedule_after_iteration` uses monotonic time, accounts for overruns
  and has deterministic tests. The watch loop persists completion/failure
  summaries, but starts are not a durable supervisor lifecycle contract.
- Correction: investigate durable start/exit coverage and separate scanner timing
  from full-cycle timing. No restart supervisor is proposed.
- Implementation PR / independent review: N/A.
- Tests: existing `test_watch_supervisor_reliability.py`; new acceptance pending.
- Deployment / Runtime verification: not deployed / N/A.
- Uncertainty: outage cause cannot be established from DEV source. P4 causation
  is Unverified. Requires genuine launcher/process/manifest evidence.

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

- Status: REVIEW_PENDING (supplied research-evidence stratification only).
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
- Implementation PR: pending; report contract is described in
  `docs/research/CCI_EXECUTION_EVIDENCE_STRATIFICATION.md`.
- Tests: 106 targeted report, attribution, context and cutoff tests passed.
  Full suite and independent Astra exact-head review pending.
- Deployment / Runtime verification: not deployed / N/A.
- Uncertainty: no historical candles recovered; none fabricated or relabeled.

## D — Telegram provenance

- Status: INVESTIGATING.
- Weakness: ten historical PENDING follow-ups lack canonical reservations.
- Evidence: ownership checks require the canonical pointer and matching event /
  reservation identity; outbox recovery fails closed without that pointer.
- Prospective diagnostic finding: `recover_public_lifecycle_alerts` logs a
  missing-attempt event on every pass and continues without a delivery diagnostic
  for that event. The ten historical events' current state remains Unverified.
- Correction: investigate bounded event-key diagnostics and warning suppression
  while preserving unresolved status and preventing any automatic resend.
- Implementation PR / tests / independent review: N/A / existing outbox tests /
  pending new acceptance investigation.
- Deployment / Runtime verification: not deployed / N/A.
- Uncertainty: historical delivery receipts are unavailable in DEV. No event
  mutation or resend has been attempted.

## E — Resource reliability

- Status: INVESTIGATING.
- Weakness: historically low Runtime RAM/disk and cadence overruns.
- Evidence: scanner already samples RSS through platform APIs; supervisor records
  full-cycle duration. These do not yet attribute all downstream phase latency.
  DEV free C: capacity was approximately 115 GB, not a Runtime measurement.
- Correction: assess low-overhead phase/resource instrumentation and bounded
  diagnostic sizes. No cleanup or production access.
- Implementation PR / independent review: N/A.
- Tests: existing process-memory and cadence tests; matched Runtime evidence N/A.
- B diagnostic microbenchmark (synthetic DEV structures, 200 repetitions,
  summary plus JSON serialization, excludes disk/SQLite): 41 owners produced
  28,455 bytes versus 15,198 legacy bytes, median 0.416 ms / p95 0.553 ms;
  118 owners produced 80,548 versus 43,476 bytes, median 1.188 ms / p95 2.484 ms.
  These are per report, not total artifact growth; reports can appear in more
  than one artifact. Runtime cadence and storage impact remain Unverified.
- Deployment / Runtime verification: not deployed / N/A.
- Uncertainty: Runtime capacity may require a hardware/storage upgrade. No
  database bottleneck has been established.

## Continuation and release boundaries

Owner-accounting implementation is CODE_COMPLETE with final-head CI and Astra
approval; PR #141 stays unmerged and undeployed. Handoff:
`docs/handoffs/CCI_RELIABILITY_OWNER_COVERAGE.md`.
Finish C's full verification, PR/CI and exact-head independent review, then
continue A, D and E. Preserve quality 88, grade A, RR 3.0,
confirmation, immutable economics,
freshness and deduplication. P4 shadow remains disabled and non-enforcing.
No merge, deployment, production database access, scanner, listener or external
market-data acquisition was authorized as part of implementation.

Overall state: PARTIALLY_RESOLVED. No objective is VERIFIED_RESOLVED. A passing
DEV suite is not release approval or evidence of natural Runtime improvement.
