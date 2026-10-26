# CCI CMC release readiness report

Date: 2026-10-26. PR: [#148](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/148).
Branch: `feature/cmc-hybrid-active-intelligence`.
Starting head independently reverified: `1122305ff6774d442729e9ed1d31cf737ea6914a`.
Main base independently reverified: `6785cf6c5a5d4c083f31a280c2ca1555bc7df88c`.
That starting head's [CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40333144808)
passed 3,667 tests. It is prior evidence; release changes require their own CI.

This is the versioned **pre-merge release packet**. The exact final head, CI and
merged SHA are authoritative in PR #148 and the final release receipt generated
after the merge. Runtime execution and production decision activation are separate
approvals; neither is granted by this packet or a merge.

## Release review and corrections

The full changed-file inventory was reviewed: existing scanner/config/CLI/public
delivery hooks; CMC contracts/client/identity/normalization/features/comparison;
policy, runtime worker, native bridge, sidecar and research; authenticated
capture/replay; fixtures/tests; operational documentation; and source-inventory
line updates. Large real-response fixtures were checked through hash-verified
replay and field-level evidence, not treated as executable policy authority.

| Review area | Finding / disposition |
| --- | --- |
| Binance priority and scanner integrity | CMC does not replace exchange candles, prices, OI, strategy score or geometry. Canonical quality/confirmation/RR gates remain authoritative |
| Default OFF and mode changes | OFF does no collection. A reused in-memory DECISION evaluation could previously survive OFF/COLLECT; release fix clears it, including invalid/new failed boundaries |
| Actual prioritization compatibility | Legacy WATCHLIST sorting is unreachable for current initial-event policy. Release adds individually authorized exact-quality-tie ordering on the existing CONFIRMED path without enabling legacy publishing or changing counts |
| Eligibility/lifecycle/dedup | Candidate builder applies canonical gates first. Prior-public follow-ups are excluded from CMC authority; setup, plan, lifecycle and Telegram identities remain owned by their existing code |
| Applied decision provenance | Policy records alone were not proof of application. New pre-application selection intent links exact evaluations, baseline/actual order and selected IDs; post-delivery observation records statuses separately from fills |
| Failure isolation | Failed policy or pre-application audit write clears CMC authority and retains baseline. Post-send audit failure is logged as missing observation; it cannot undo a send |
| Authentication and secrets | Fixed HTTPS GET origin, SecretStr excluded from repr/dumps, no credential params, no redirects, bounded codes instead of provider exception text; final staged diff credential scan required |
| Contract identity and source semantics | Exact reviewed IDs/slug/base/quote/perpetual metadata and causal timestamps. OI counting, CMC funding, index/basis and stale sector gaps remain Unverified |
| Timestamp causality | Metric and acquisition times are distinct, checked at selection; no historical backdating. OFF baseline clock consumption restored after regression caught an extra clock read |
| SQLite isolation | Dedicated cmc_ path/application ID, bounded rows/bytes/retention, idempotent evidence and UTC credit ledger. No production schema or migration diff |
| Memory, latency and credits | Bounded worker/cache/HTTP/storage settings reviewed; irrelevant rows are skipped before repeated causal checks without changing feature output. Local 100-candidate benchmark ~0.303s untraced / 200,432 bytes incremental traced allocation. Actual 4 GB Runtime capacity unverified. Default credits cannot sustain continuous full-family five-minute acquisition |
| Research completeness | Added median net R, profit factor, verified fill rate, scalp/swing cohorts and retained/displaced/introduced outcome populations. Policy batches retain original CCI decision time, native lifecycle mode and CCI regime quality/value (missing N/A, unreliable Unverified), avoiding later reconstruction from huge Runtime history. Missing/unverified outcomes remain excluded and visible |
| Autonomous execution | No order, withdrawal or transfer capability, private exchange operation, live service, production DB access or deployment added/executed |

The existing activation contract and all five rule specification hashes remain
unchanged. No quality gate or original test assertion was weakened. No unrelated
feature branch or linked worktree is part of this release. The pre-existing
untracked `CCI_ALPHA_RECOVERY_ROOT_CAUSE.md` is left untouched.

## Authenticated findings and rule readiness

The [authenticated report](CCI_CMC_AUTHENTICATED_VALIDATION_REPORT.md) establishes
successful bounded access to 15 route families, exact BTC/ETH/SOL Binance USDT
perpetual mappings, verified context/listings/global liquidations, and 20 reported
validation credits. Its immutable source/acquisition cutoff is retained.
Current Runtime freshness, plan-wide history depth and live economics are not
inferred from it. Replay uses committed selected public responses and original
raw hashes. An additional [default-profile collection](../research/cmc_release_collection.json)
ran on DEV at 2026-10-26 15:12:30–15:12:38 UTC: 16 CMC GET requests, 12 reported
credits / 15 conservative reservations under a 16-credit cap; 1,696 observations,
1,594 Verified and 102 Stale at that capture's cutoff. There were no API failures.
Three explicit two-page truncation notices retain partial listings/map/pair
coverage. The dedicated CMC sidecar was approximately 3.05 MB. Sanitized quota
usage rose from 33 to 45 that day and 35 to 47 that month, below the configured
100/day and 3,000/month. No scanner, listener or Runtime DB was involved.

All rules are reviewed individually in
[CCI_CMC_ACTIVE_DECISION_RULE_REVIEW.md](CCI_CMC_ACTIVE_DECISION_RULE_REVIEW.md).
`adverse_regime/1` is the first observational priority hypothesis. Its input
definitions are supported, its mechanics are tested, and its real economic effect
is not validated. `liquidation_stress/1` has supported aggregate inputs but needs
a stronger veto outcome study. Other rules have unresolved source requirements.
Approved/enabled production rules: **NONE**. No source gap is relabeled verified.

## Final release gates and merge procedure

Require all of: final focused/full regression, preserved canonical fixtures,
reviewed security/compatibility, no unresolved critical software finding, exact
head CI, current mergeability, current main ancestry and current repository rules.
The user explicitly authorizes the eligible merge in this assignment; no separate
production rule or Runtime rollout approval is implied.

At initial inspection, main was unprotected, effective branch rules were empty,
there were no requested/required reviews, and normal merge commits were allowed.
Recheck immediately before merging; do not use admin bypass, force merge, a stale
head or failed/pending required checks. Use GitHub's normal PR merge with the
expected head SHA. Leave the DEV checkout and all other worktrees on their
existing branches. Record the actual merge result and verify main's push CI.

The [baseline summary](CCI_CMC_BASELINE_REGRESSION_SUMMARY.md), machine-readable
release regression and verification manifest carry exact test scope/results.
The final local suite passed 3,683 distinct cases, the focused CMC suite 130 and
the affected-module suite 171. Compilation/dependency checks also passed.
[Release implementation CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40347743791)
on `2b301f27d1b56549deee68261359ec915cd6055c` passed all 3,683 cases in
365.75 seconds, with one existing deprecation warning. The final cohort-context
follow-up must also have successful exact-head CI before the merge; its result
is recorded in PR #148 and the final release receipt.
Merge remains pending until those gates are satisfied; the final receipt records
the resulting YES/NO verdict and immutable SHA rather than predicting a merge ID.

## Verdict definitions and operational blockers

| Verdict | Required evidence / current operational meaning |
| --- | --- |
| PR_RELEASE_READY | YES only after final gates above pass; pending while tests/CI are running |
| MERGED_TO_MAIN | Actual GitHub merged state and exact merge SHA in final receipt |
| CMC_COLLECT_READY | Software supports bounded collection; continuous coverage at default credits is not ready |
| CMC_SHADOW_READY | Software supports causal hypothetical evaluation and baseline delivery; Runtime observation still requires approval and resource/budget acceptance |
| CMC_DECISION_ENGINE_READY | Guarded ordering/veto/support mechanics implemented and tested; not a claim of economic readiness |
| CMC_ACTIVE_RULES_VALIDATED | NONE |
| CMC_RUNTIME_DEPLOYMENT_APPROVED | NO |
| CMC_PRODUCTION_DECISION_ACTIVATION | BLOCKED: no independent economic validation or exact owner rule approval; several rules also lack source definitions |

The exact deployment, shutdown, environment/epoch/schema checks, manual restart,
sidecar, budget and rollback procedures are in
[CCI_CMC_RUNTIME_ROLLOUT_PLAN.md](CCI_CMC_RUNTIME_ROLLOUT_PLAN.md). The staged
observation, attestation, single-rule approval, provenance, economic monitoring
and rollback contract is in
[CCI_CMC_DECISION_ACTIVATION_PLAN.md](CCI_CMC_DECISION_ACTIVATION_PLAN.md).

The next operational decision belongs to the owner: approve a concrete pinned
Runtime package after actual schema-27/epoch/capacity checks and a feasible
collection budget, then authorize observational COLLECT/SHADOW. Live rule
enforcement remains blocked until its separately reviewed evidence is sufficient.
