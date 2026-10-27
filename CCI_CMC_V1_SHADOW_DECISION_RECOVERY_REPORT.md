# CCI CMC V1 shadow decision recovery report

## Repository

- BRANCH: `feature/cmc-v1-shadow-decision-recovery`
- BASE_SHA: `81bbfc6ee806aa4928c287cd685314ea3fff8acd` — fetched origin/main; PR #150 merge.
- IMPLEMENTATION_SHA: `a7f86ef5f94a1e6283234d9af4109890ebfa6f75` (includes initial recovery `e8d9f855f84ac465f9d3ececb6815972f0ed3998` and the saved-snapshot guard).
- PR_URL: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/151
- CI_STATUS: PASS on initial recovery `e8d9f85` (CI run 40486973371, 3,831 tests).
  The final restart guard/report head is verified through PR #151 checks and its
  exact-head result is recorded in the PR description/final handoff.
  No merge or deployment is authorized by this phase.
- Worktree: `C:\CandleCraftDev\.worktrees\cmc-v1-shadow-decision-recovery`.
  The original checkout, its branch/untracked files and other worktrees are preserved.

## Root causes

DECISION_TIMESTAMP_DEFECT: independently reproduced through bounded watch
orchestration, real ScannerRunner.run and the real public-delivery service with
mocked market acquisition and Telegram transport. Before repair, the delivery
boundary receives `decision_timestamp=None`, even though ScannerRunner resolved it.

ROOT_CAUSE: `_run_watch_scan_iteration` and the single-scan orchestration aggregate
completed symbols with their unresolved template config. The runner's original
pre-acquisition cutoff is lost. The CMC boundary correctly refuses to invent it.

REPAIR: propagate the runner's resolved cutoff into completed aggregate configs.
Keep the original watch template unresolved for subsequent iterations. Record
analysis and selection cutoffs separately. Filter both CMC and native evidence
against the original analysis cutoff and recheck freshness at selection. Resumed
payloads without an authoritative per-symbol cutoff remain unavailable.
An earlier resumed analysis cannot inherit the later run's CCI regime; that context
is Unverified and compatibility is omitted. This regression was also reproduced
before repair.

TEST_EVIDENCE: `tests/test_cmc_watch_recovery.py` covers the actual bounded watch
runner/public boundary, policy persistence, hypothetical stress/adverse-regime
recommendations, recorded authenticated global context, repeated processing/dedup,
future acquisitions/sources, stale data and missing cutoffs. The original failing
assertion was `None != 2026-01-01T00:00:00+00:00` at the delivery boundary.
Existing CMC release/policy tests retain replay, activation, rejected-candidate,
follow-up, public ownership and baseline protections. No synthetic trigger is
reported as a real observed market trigger or trading improvement.

IDENTITY_MAPPING_DEFECT: the committed default manifest is empty. Historical
validation-only asset approvals lacked a current owner-approval/exact-contract
expiry contract. The old matcher accepted any integer market ID consistent with
its other fields; it did not pin it to the reviewed contract.

ROOT_CAUSE: engineering identity verification, owner authorization and current
source semantics were not represented as separate operational facts.

REPAIR: exact contract/exchange/market IDs, chronological evidence/review/approval,
explicit owner reference and expiry are required by production collection. Duplicate
bindings fail even when constructed programmatically. Binding expiry propagates
into observations. Reviewed metadata cannot be renewed by a failed refresh; this
additional cache regression was reproduced before repair. Identity metadata has
its own validity and cannot refresh market-price evidence.
Restoring a saved snapshot now rechecks the current owner-approved binding and
fingerprint. Empty, expired, revoked or changed manifests remove older contract
observations while retaining independent global context. Four failing restart
scenarios were reproduced before repair; the valid-approval control still passes.

`config/cmc/identity.proposed.json` represents BTC (ID 1/bitcoin/market 47150), ETH
(1027/ethereum/47205) and SOL (5426/solana/67944), Binance exchange ID 270, USDT
perpetual contracts. `docs/research/cmc_recovery_evidence.json` preserves hash-based
provenance. Historical status is EVIDENCE_VERIFIED; authorization is
IDENTITY_REVIEW_PENDING, with `reviewed_at`, `approved_at` and owner reference null.
No review/owner timestamp was fabricated. Proposed evidence expiry is seven days
after acquisition. EXPIRED and UNVERIFIED remain explicit states.

REMAINING_MAPPING_LIMITATIONS: current independent metadata and owner reviews are
BLOCKED_EXTERNAL. Other unsupported contracts fail closed. Funding intervals,
OI counting/units, price currency equivalence and index/basis semantics are still
separate unverified contracts, even for a verified identity.

## CMC collection

- SUPPORTED_PROFILES: FULL_RESEARCH, DECISION_FOCUSED, LOW_CREDIT, CUSTOM.
- RECOMMENDED_RUNTIME_PROFILE: DECISION_FOCUSED, subject to owner rollout and quota review.
- EXPECTED_REQUESTS_PER_DAY: 341 under configured upper page bounds and no optional category/external-market IDs.
- EXPECTED_CREDITS_PER_DAY: 339.
- EXPECTED_CREDITS_PER_30_DAYS: 10,170.
- EXPECTED_CREDITS_PER_31_DAYS: 10,509.
- MONTHLY_ALLOWANCE: historically verified 15,000; current entitlement/remaining quota BLOCKED_EXTERNAL.
- Proposed local cap: 400/day, 12,000/month. Existing defaults remain 100/day, 3,000/month.
- EXPECTED_FRESHNESS_COVERAGE: conditional 40% joint coverage for the two global
  rule inputs at 60-second source lag/no selection delay; 30% with one-minute
  selection delay and zero when selection latency exceeds 300 seconds. Actual
  Runtime decision coverage is N/A pending a prospective cohort.
- CREDIT_EXHAUSTION_BEHAVIOR: deny further paid reservations, preserve canonical
  decisions, let prior evidence expire. Zero-estimate ID-map requests may still run
  under the request-rate cap. Failures keep conservative reservations; valid actual
  provider costs reconcile UTC day/month totals across restarts.

Evidence: `docs/research/cmc_recovery_budget.json`, generated by
`python tests/cmc_recovery_fixture.py` from deterministic 24-hour/30/31-day scenarios.

| Schedule | Requests/day | Credits/day | Credits/30d | Credits/31d | Conditional fresh global coverage |
| --- | ---: | ---: | ---: | ---: | ---: |
| Existing broad 4h | 74 | 78 | 2,340 | 2,418 | 1.67% |
| Two sources 15m | 192 | 192 | 5,760 | 5,952 | 26.67% |
| LOW_CREDIT 15m | 205 | 203 | 6,090 | 6,293 | 26.67% |
| DECISION_FOCUSED 10m | 341 | 339 | 10,170 | 10,509 | 40% |

Two five-minute paid sources alone need 17,856 credits/31 days unconstrained and
offer only 80% modeled coverage with 60-second source lag. The constrained scenario
exhausts its local budget. Simulations also include doubled costs, six-hour outage,
default low application caps and already-stale sources. Forecasts are conditional
estimates, not provider guarantees or measured CCI decision frequencies. They account
for endpoint row costs, original cache TTLs and configured pagination bounds.

Accounting distinguishes PROVIDER_REPORTED_CREDITS, LOCAL_RESERVED_CREDITS,
FAILED_ATTEMPT_RESERVATIONS, UNKNOWN_USAGE and ACCOUNT_USAGE_OUTSIDE_CCI. Outside
account use stays N/A without an account receipt. Legacy unclassified totals remain
charged as unknown. Retries, probes and other use require real quota headroom.

## Decision intelligence

- SHADOW_EVALUATION_FUNCTIONAL: YES in the offline actual watch/public-selection
  path; prospective Runtime operation remains BLOCKED_EXTERNAL until approved rollout.
- POLICY_BATCH_PERSISTENCE: verified in temporary dedicated CMC sidecars.
- HYPOTHETICAL_RECOMMENDATIONS: synthetic adverse-regime priority decrease and
  liquidation-stress veto persist; recorded authenticated context is evaluated
  without manufacturing a trigger or economic result.
- PUBLIC_DELIVERY_UNCHANGED_IN_SHADOW: verified with real delivery logic and fake transport,
  including repeated cycles, stale/absent evidence and sidecar/activation failures.
- DECISION_ENGINE_FUNCTIONAL: existing independently authorized mechanics remain covered.
- PRODUCTION_RULES_ENABLED: NO changes/enabling in this branch; Runtime was not inspected.
- FIRST_RULE_RESEARCH_PRIORITY: adverse_regime/1; liquidation_stress/1 is a separate
  higher-risk veto study. Neither has demonstrated prospective economic efficacy.

Leverage counters now persist eligible exact-rank tie groups and differing
hypothetical priorities. Current CONFIRMED ranking changes order within exact ties,
not membership. Actual Runtime tie frequency and economic leverage are
BLOCKED_EXTERNAL. `docs/research/CMC_DECISION_READINESS_AND_RANKING.md` supplies a
bounded sanitized export contract, rule-readiness criteria and a separate mathematical
ranking research specification with independent deployment/activation requirements.
No ranking authority, capacity, public gate or economic plan was changed here.

## Verification

- TARGETED_TESTS: 186 selected checks passed (CMC plus inventory/report anchors),
  3,650 unrelated tests deselected; one existing Starlette/httpx deprecation warning.
  Command: project Python `-m pytest tests -k 'cmc or test_inventory_anchors_match_source
  or test_reports_name_every_required_section'`; JUnit evidence in the DEV-only
  `research_runs/cmc_recovery/targeted-final.xml`.
- FULL_PYTEST: 3,830 passed, zero failed/skipped, one existing deprecation warning,
  672.83 seconds on DEV. Project Python: `-m pytest
  --junitxml=research_runs/cmc_recovery/full-final.xml`. The subsequently added
  resumed-regime and five restart-authorization regressions also pass in the 186 selected checks;
  the final PR CI must run the complete current suite against the exact head.
- GITHUB_CI: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40486973371
  passed all 3,831 tests on `e8d9f855f84ac465f9d3ececb6815972f0ed3998`.
  The final guard/report head is rechecked through
  https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/151/checks
  after this report is finalized; the final handoff pins that run and head.
- BASELINE_REGRESSION: five baseline/CMC modes match the protected canonical
  fixture; six policy cases pass. Evidence: `docs/research/cmc_recovery_canonical_regression.json`.
- SECURITY_REVIEW: no credential-pattern hits in changed/new files; CMC transport
  remains GET-only; no .env/database added; no order execution/transfer/withdrawal;
  no canonical database migration or Runtime access. Compilation, dependency
  consistency and all 48 active source-inventory anchors pass.
- RUNTIME_RESOURCE_MODEL: bounded single worker, profile-specific caches/page caps,
  two concurrent requests, local 45/min cap, default 1 MiB response and 2,500 normalized
  observations; 64 MiB/20,000-record sidecar and 2 MiB write limit. Selection caps
  200 candidates/5,000 observations and caches shared causal features per boundary.
  Local 100-candidate/2,413-observation benchmark including persistence: approximately
  0.27 s without tracing, 0.61 s with tracing, 7.65 MB peak Python allocations.
  These concurrent-DEV measurements do not certify Runtime-host latency or total RSS.
- KNOWN_BLOCKERS: current owner instrument approvals, current entitlement/quota,
  prospective Runtime cohort, measured Runtime-host resources and economic validation
  are BLOCKED_EXTERNAL. No live CMC requests or credential reads were needed.

The first sandbox verification sessions were stopped because Windows asyncio hung
creating its local socketpair. Offline verification was rerun outside that sandbox.
An isolated test-directory run initially failed the existing synthetic Runtime
identity guard; the watch regression now supplies the complete explicit synthetic
identity. No production identity safeguard was relaxed.
The first completed full run had 3,822 passes and one generated-report anchor
mismatch. The final settings placement preserves the original source anchors and
historical report artifacts; both affected checks pass without overwriting prior
research evidence. A final complete suite rerun supplies the status above.

## Deployment

RUNTIME_DEPLOYMENT_APPROVED: NO.

RUNTIME_MODIFIED: NO.

NEXT_RUNTIME_ROLLOUT_REQUIREMENTS: owner reviews the PR/CI and rollback, verifies
current quota and resource headroom, reviews exact instrument bindings against fresh
independent metadata, and authorizes a controlled SHADOW rollout. Exact proposed
environment changes, monitoring, retention/eviction caveats, failure behavior and
rollback are in `docs/operations/cmc_shadow_recovery.md`. The running Runtime remains
on its existing release. No scanner/listener process was started outside bounded tests.

NEXT_DECISION_ACTIVATION_REQUIREMENTS: verified source contracts, prospective causal
evaluation, measured actual selection leverage, mature outcome and cost attribution,
adequate uncertainty/power analysis, untouched OOS/prospective comparison, no material
negative regression and exact rule/version/hash owner approval. No force-decision
shortcut exists. V2 reuse is limited to these contracts/components; V2 is not implemented.
