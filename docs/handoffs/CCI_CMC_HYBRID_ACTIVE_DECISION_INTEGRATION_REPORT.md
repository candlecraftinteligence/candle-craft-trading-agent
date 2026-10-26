# CCI CMC HYBRID ACTIVE DECISION INTEGRATION REPORT

Date: 2026-10-26, Europe/Bratislava. Scope: existing Scanner V1 on DEV only.

This records the original engineering phase through `2990c8f`. Its keyless-only
authentication statements describe that phase. The subsequent owner-authorized
[CMC AUTHENTICATED VALIDATION REPORT](CCI_CMC_AUTHENTICATED_VALIDATION_REPORT.md)
supersedes those limitations with real account evidence, additional fixes and
new regression results. Production activation remains disabled.

## 1. Repository branch and SHA

Branch: `feature/cmc-hybrid-active-intelligence`.

Base: `6785cf6c5a5d4c083f31a280c2ca1555bc7df88c` (refreshed GitHub main).

Implementation milestones:

- `c8e3374f0e7cf562bdced3e992d7e4d03e8da770`: complete subsystem, integration, tests and operator documentation.
- `462c00c9fcee21c16f08950a036497b8020584c9`: bounded selection provenance and source-inventory refresh. This was the original phase's final code/test revision; the later authenticated validation is recorded separately.

The pre-existing untracked `CCI_ALPHA_RECOVERY_ROOT_CAUSE.md` and linked worktrees were not modified. A two-day-old empty `.git/index.lock`, with no Git process owner, was preserved as `.git/index.lock.stale-cmc-20261026` before creating the authorized feature branch. No reset, merge, main push or Runtime deployment occurred.

## 2. Pull request

[PR #148 — Add guarded CMC hybrid futures selection intelligence to Scanner V1](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/148).

## 3. Implemented REST endpoints

The typed endpoint registry supports all requested families: V3 listings; V1 map and historical listings; V5 derivatives exchange list, exchange market pairs and cryptocurrency market pairs; total/exchange/cryptocurrency liquidations; global metrics; Fear & Greed; altcoin season; categories/category; and an explicit key-info probe. Historical requests require an explicit date and are never fabricated or automatically backfilled. Full routes, response contracts and credit reservations are in [the architecture and operations guide](../CMC_HYBRID_INTELLIGENCE.md).

## 4. Available provider capabilities

Fixture-supported: all normalization, identity, comparison, policy and failure contracts in the new subsystem. Live keyless observations verified metric-level global metrics, Fear & Greed and altcoin-season values and fetched the numeric ID map. An initial V3 listings request succeeded, exposed the quote-array contract, and motivated a parser correction; the repeat live listings probe returned HTTP 429. The corrected parser is fixture-verified, with that final live listing-normalization check still unavailable. Saved [keyless probe results](../research/cmc_hybrid_keyless_probe.json) retain the 429 rather than replacing it with invented data.

Authenticated futures, liquidation and historical entitlements were not tested during this original phase. The later authenticated validation accessed all 15 route families, including fresh V3 listings, and verified three real Binance perpetual mappings. Field-level semantics and dated history retain the specific blockers in the authenticated report.

## 5. Decision features

Implemented versioned features: global market risk, exchange OI/turnover leverage context, funding dispersion, exact-contract OI and funding, computed basis, liquidation pressure and imbalance, BTC dominance, altcoin rotation, sector relative strength, data-confidence coverage, market-cap rank, equivalent-source consistency and research-only CCI regime compatibility. ETH dominance and all required raw context are retained separately. Financial values use Decimal. Missing, stale, ambiguous, future or semantically unknown inputs cannot become favorable evidence.

## 6. Veto rules

`data_divergence/1`: verified equivalent contract disagreement.

`liquidation_stress/1`: global hourly stress ratio ≥4 plus direction-signed liquidation imbalance ≥0.75. This is aggregate stress, never proof of an instrument stop sweep.

Both are experimental specifications, not empirically validated production gates.

## 7. Priority and positive support rules

`adverse_regime/1` recommends a priority decrease; `sector_support/1` recommends an increase among otherwise equally ranked canonical candidates. `consistent_context/1` represents positive independent support. Recommendations cannot create a setup, promote WATCH to CONFIRMED, replace structure or execution confirmation, lower RR/quality/grade thresholds, change economic-plan identity or overwrite scores.

## 8. Enabled and disabled rules

Production-enabled rules: **none**. Default mode: **OFF**. The shipped activation manifest is `[]`.

COLLECT persists evidence; SHADOW computes hypothetical decisions; DECISION is implemented and tested but enforces only independently enabled exact rule versions/formula hashes with complete source, causal/out-of-sample, test, hard-gate, baseline and explicit owner-approval attestations and valid expiry dates. Duplicate/expired/incomplete approvals do not enforce. No real approval or empirical validation was manufactured from synthetic tests.

## 9. Baseline regression

The same fixed synthetic scanner/public-decision harness was run against the isolated main export and the feature source. The resulting artifacts are byte-for-byte identical, including public economic-plan and delivery identities:

- [Main decisions](../research/cmc_hybrid_main_fixed_decisions.json)
- [Feature OFF decisions](../research/cmc_hybrid_feature_fixed_decisions.json)
- SHA-256 for both: `c8903203a8929e6a12445f2cad556a8599258e0256c1911a5a92682d57ca1c7c`.

Main baseline coverage: 3,553 distinct tests passed across the full initial run and targeted completion/reruns. The initial run had 3,494 passes and one existing Windows `os.replace` permission failure in `test_watch_session`; all 26 watch-session tests passed on rerun, and all 45 tests in the files after that stop point passed. No main-source fix was made for this transient failure.

## 10. Historical and shadow research comparison

[Synthetic policy comparison](../research/cmc_hybrid_fixture_comparison.json): three original valid fixture setups; two veto recommendations, including one loser and one winner; one retained setup. Baseline and simulated CMC expectancy are both 0.5 synthetic R, giving zero incremental expectancy in this deliberately balanced example. Fill probability and MAE remain N/A where not supplied. This tests accounting and selection mechanics, not trading performance.

Real historical/out-of-sample/prospective expectancy: **not established**. No independently verified real outcome dataset or causally collected futures history was supplied. The evaluator rejects duplicate economic-plan units, ownership mismatches and look-ahead, and excludes unknown/unverified outcomes from net-R calculations. It requires independently reconstructed outcome attribution, entry activation, fees and slippage; it does not turn candle touches into verified fills or reuse unverified CCI attribution as truth. No existing outcomes were rewritten.

## 11. Complete test results

GitHub CI on final code revision `462c00c`: **3,646 passed, one existing deprecation warning, zero failures/skips**, in 290.22 seconds. Dependency verification and compilation also passed. [Verified CI run](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40312101783), job `121535546292`.

Final local regression: **3,646 distinct tests passed, zero failures/skips**, across all 178 test files. Partition counts: 843 + 827 + 995 + 981. The reordered fourth partition initially had 980 passes and an existing safe-default settings assertion affected by an earlier CLI dotenv load of the host's research-watch flag. Repeating that partition in canonical file order with the DEV research-watch flag explicitly false passed all 981 cases. No production configuration or test assertion was changed for that process-isolation correction.

The initial feature-wide `python -m pytest` run produced 3,637 passes and one source-inventory anchor failure. Source additions shifted ten pinned line references; the catalog and its generated inventory documents were refreshed without changing or weakening the test. A subsequent focused run passed all 93 CMC tests and failed only an inventory JSON trailing-newline mismatch; preserving the generator's original no-trailing-newline format resolved it, and both exact inventory/report checks passed.

Focused CMC/configuration runs passed, including all modes, verified-rule enforcement, no promotion, causal identity, futures units and funding semantics, network/rate-limit failures, credit/cache coalescing and cancellation, sidecar bounds/idempotency, GET-only/no-order behavior, actual public delivery/deduplication, provider-outage recovery after a veto, and a 100-candidate/1,500-extra-observation policy batch within the sidecar payload budget. The final suite includes 93 new CMC tests. [Verification evidence](../research/cmc_hybrid_verification.json) records the final aggregate, JUnit hashes, initial failures and rerun outcomes. No checks were skipped to obtain a green result.

## 12. Credits and performance

The final keyless probe recorded eight request attempts and seven conservative accounting units, including retries; no authenticated subscription credits were used by this work. This is provider-reported/planning accounting, not a claim of paid billing. Defaults remain 100 credits/day and 3,000/month, persisted across restarts. These deliberately conservative limits do not promise continuous daily evidence coverage.

The scanner consumes shared snapshots from one bounded acquisition worker, including refreshes during long scans. It performs no synchronous per-symbol CMC requests. Requests, retries, cache entries, response size, page counts, observation count, polling, persistence waits, retention and file size are bounded. Ineligible/follow-up candidates do not duplicate feature provenance; confidence references only contributing feature inputs. The 100-candidate resource fixture passes without claiming a Runtime-PC latency benchmark. Runtime interval/RSS effects remain a controlled-validation task.

## 13. Security review

No real CMC credential was inspected, modified, exposed, committed or used for authenticated requests. The CMC-only probes explicitly read no credentials; existing regression tests retain their normal settings/CLI configuration paths and use mocked transports. The CMC key is a SecretStr excluded from settings dumps and repr. Authentication uses a header to a fixed HTTPS origin; redirects and arbitrary endpoint URLs are disallowed. Errors are bounded codes; request headers are not evidence. New exchange/provider traffic is read-only GET. No order execution, withdrawal, transfer, autonomous trading, Telegram listener, live scanner or production operation was added or started. Existing DEV configuration and `.env` files were not changed; safety flags were supplied only to test processes.

## 14. Persistence review

CMC research uses its own bounded SQLite sidecar, not the live Runtime database. It stores raw hashes/text, acquisition batches, typed observations, exact mappings/availability facts, features, source comparisons, errors/credits, matched immutable candidate identities and policy evaluations. UTC source/acquisition/decision indexes support causal joins. IDs are stable and writes idempotent; conflicting immutable identities are rejected.

Default main-file cap is 64 MiB with 20,000 records and 30-day retention; transient DELETE-journal space is additional. Policy evaluation batches are explicitly marked `EVALUATED_NOT_DELIVERY_PROOF`. A persisted recommendation is not proof of a public send, fill or realized result. Timeout/storage failure preserves canonical behavior, and a late write cannot be treated as an applied decision merely because a policy evaluation exists.

## 15. Known defects, limitations and external blockers

- Authenticated REST access, actual subscription limits and futures/historical entitlements remain unverified.
- OI units/counting and funding intervals/kind require independent reviewed contracts. Unknown semantics stay Unverified.
- The existing Binance funding source facts do not establish native funding cadence. That missing fact is not filled with an assumed eight-hour interval; affected funding comparisons remain Unverified or UNIT_MISMATCH, while reviewed equivalent OI comparisons are supported.
- Cross-exchange venues require explicit reviewed native metadata; no venue or ambiguous symbol mapping is automatically trusted. Page budgets may give partial coverage, which is recorded explicitly.
- A cold sidecar, exhausted credits, stale evidence or provider outage leaves canonical decisions unchanged. Default credits are not a continuous-coverage research plan.
- Real causal outcome data, out-of-sample/prospective research and profitability evidence are absent. All five production rules remain disabled.
- The repeated keyless listings probe hit 429; final live listing-parser verification is pending. Other latest public context routes were fetched and normalized.
- Runtime hardware profiling and integration with the owner's exact launcher/secret-management/release procedures require a separately approved validation/release exercise.
- Initial Windows test permission and shifted inventory issues are disclosed above; their reruns/fixes are recorded rather than hidden.

## 16. Runtime rollout

The [owner-only PowerShell rollout/rollback procedure](../CMC_HYBRID_INTELLIGENCE.md#controlled-runtime-rollout-and-rollback--owner-procedure-only) covers stopped/clean Runtime checks, exact reviewed SHA, the existing single V1 launcher, OFF baseline, separately approved COLLECT/SHADOW stages, secure owner-supplied authentication, isolated sidecar limits, and rollback via OFF without a CCI database rollback. No rollout commands were executed. Existing runtime-epoch and release safeguards remain authoritative.

## 17. Required owner approvals and inputs

Required before authenticated validation: securely supplied CMC credentials, verified account entitlements/credit limits and reviewed instrument/metric semantics. Required before any production decision rule: complete source/causal/test/baseline/out-of-sample or prospective evidence and an explicit approval naming the rule, version and formula hash. Runtime deployment and any main merge require separate release approval. No subscription purchase is authorized or attempted.

## 18. V2 portability

Provider, contracts, normalization, comparison, features, policy, sidecar and research code are separated from the V1 adapter. A future consumer supplies immutable eligible candidates and canonical ranking through that adapter contract. No V2 implementation was developed.

## 19. Exact authenticated API test status

**Original phase: NOT PERFORMED. Subsequent phase: PERFORMED.** The owner supplied a local credential and explicitly authorized real validation. All 15 route families responded successfully within the bounded credit cap. The [authenticated report](CCI_CMC_AUTHENTICATED_VALIDATION_REPORT.md) records exact coverage, source evidence and unresolved semantics; successful authentication alone does not verify futures fields or trading usefulness.

## 20. Readiness and final conditions

| Condition | Status |
| --- | --- |
| CODE_IMPLEMENTED | Yes; final code revision `462c00c` |
| TESTS_VERIFIED | Yes: full GitHub CI, 3,646 passed; local rerun details above |
| CMC_API_VERIFIED | Partial keyless context only; authenticated futures/subscription Unverified |
| DECISION_ENGINE_READY | Implemented and fixture-tested; production activation still gated |
| DECISION_RULES_VALIDATED | No empirical validation; all production rules disabled |
| PRODUCTION_ACTIVATION_APPROVED | No |
| PR_READY | Yes; PR #148 contains complete review evidence; no merge/deployment approval implied |

Runtime status: ready for owner review and preparation of controlled validation; **not approved for Runtime deployment or live decision authority**. Incremental risk-adjusted expectancy has not been demonstrated.
