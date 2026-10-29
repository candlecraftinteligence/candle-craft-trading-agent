# Integrated signal-engine recovery evidence

Base: `1107f1c42b29c32fec2c669cdfd19152d0608a56` (remote main fetched 2026-10-29).
Branch: `codex/cci-integrated-signal-recovery`.
Worktree: `C:\CandleCraftDev\.worktrees\cci-integrated-signal-recovery`.
The original checkout and its uncommitted changes are preserved.

## Discovery source contract

`MARKET_CAP_PROVIDER=coinpaprika` preserves the existing production discovery
source until the replacement is functionally verified. `MARKET_CAP_PROVIDER=cmc`
selects CMC alone, through the existing `app/cmc` client, budgets, cache and
identity manifest. There is no automatic cross-provider fallback or mixed rank.
No runtime configuration or `.env` has been changed.

CMC numeric IDs are namespaced as `cmc:<id>` in the shared qualification engine;
membership diagnostics also carry numeric `cmc_id`. They are never interpreted
as CoinPaprika IDs. CMC v3 list-valued USD quotes and USD-keyed quote objects are
supported. Provider and acquisition timestamps remain distinct. Malformed,
incomplete, stale and future-dated evidence fails closed.

CMC bindings reuse the existing identity manifest's exact-contract review and
expiry checks. `qualification_class` must explicitly identify a genuine asset
or excluded representation. Unknown classifications consume conservative U100
slots but cannot join Binance contracts. Representation tags may exclude;
sector tags such as `liquid-staking` and `restaking` cannot exclude a protocol
token. A reviewed multiplier preserves exchange price/quantity units and is
checked against current Binance perpetual metadata. No symbol-only CMC join.

The one scanner watch loop survives startup discovery outages, monitors owned
plans on every failed iteration, retries with capped jittered backoff, and
resumes only after fresh discovery succeeds. The non-admission config placeholder
cannot enter a scanner queue. CMC clients close at command shutdown. A long
`Retry-After` records a cooldown and returns immediately to monitoring instead
of retrying early or blocking the loop for the entire cooldown.

## Bounded provider checks (2026-10-29)

Initially, three unauthenticated GETs were attempted against the documented keyless CMC
listings, cryptocurrency-map and global-metrics endpoints. All returned HTTP
429. No credentials were read and no authenticated account quotas were checked.
At that stage, fresh live ranking availability, actual account entitlements and
commercial redistribution rights remained **Unverified**. A later owner-approved
bounded private capability check was completed. Account-specific results are
kept out of this repository. Production activation still requires reviewed
identity bindings and the applicable product license/attribution prerequisites;
it is not authorized by development verification alone.

Official references inspected:

- https://coinmarketcap.com/api/documentation/pro-api-reference/keyless-public-api
- https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency/
- https://coinmarketcap.com/api/documentation/pro-api-reference/tools/
- https://coinmarketcap.com/api/pricing/

The pricing page advertises commercial use. The commercial terms permit an
integrated end-user product, including a channel/bot, subject to product scope,
personal-use end users, attribution and user-count limits. Standalone data/API
redistribution is restricted. Product registration/accepted terms remain owner
rollout prerequisites. No specific account entitlement is inferred from pricing.
See https://pro.coinmarketcap.com/user-agreement-commercial/.

## Verification environment

Use the existing `C:\CandleCraftDev\.venv\Scripts\python.exe`. On this host the
sandbox blocks Windows asyncio socket-pair creation before application code;
the affected test processes were interrupted and the mocked tests rerun outside
that restriction. Disposable databases must be under a path containing
`/pytest-of-` for the repository's synthetic epoch fixture. Neither aborted run
is a successful test result. No production scanner/listener or Telegram send
was started.

PR #152 was reviewed at `c7ff8b2305036b8382f3b30fe36f6a0a59947656`.
Its native daily source, precision and regression code can be reused selectively;
its 2H strategy and unconditional 0.786 WATCH-only policy do not meet this brief.

Group A focused verification: **165 passed in 45.93 seconds**, including
CMC normalization and U100 identity/boundary checks, three consecutive startup
outages with continuous owned-plan monitoring, automatic discovery recovery,
source cleanup/cancellation, watch scheduling, and CMC client/storage safety.
`compileall` for changed production modules and `git diff --check` passed.

## Strategy integration (group B)

Prospective scanner defaults use `selective-fib-mtf-v2` with native closed
1D / 12H / 4H / 15M candles. The execution and confirmation roles share the
same 15M source. The legacy strategy engine and historical replay retain
their 2D / 2H defaults; daily evidence is never relabelled as synthetic 2D.

PR #152's precision, native-daily context, provenance, formatter and regression
changes were applied selectively onto the current base. Its deep-level policy
was adapted: all three levels are independently evaluated, and all require an
actual post-BOS contact followed by a closed directional reaction, confirmed
structure and the existing canonical sweep/momentum confirmation. There is no
extra two-body requirement. A wick touch alone is not confirmation or a fill.
Body acceptance beyond 0.786 and structural breakdown remain rejected.

Zone freshness is checked before the first contact, using the existing OB/FVG
freshness calculations. The evidence also retains freshness at decision time,
so the confirming touch cannot retroactively disqualify its own previously
fresh zone. Prior consumed zones remain blocked. Ticker prices are not inserted
as synthetic closed bodies in the prospective wick/body assessment.

Focused strategy, legacy, candle-freshness, RR and fake-delivery regressions:
**327 passed in 214.06 seconds**. Final Fibonacci/CLI/command-center checks:
**153 passed in 21.52 seconds**. These runs overlap and must not be summed.
Full `compileall`, `pip check` and `git diff --check` passed. Final full-suite,
end-to-end product verification and CI evidence is recorded below.

## Final economics and entry semantics (group C)

The prospective stop is beyond the sweep/OB invalidation and nearby confirmed
liquidity proxies within one observed ATR. The buffer is the largest of the
existing 0.10 ATR buffer, observed spread when supplied, and one verified tick.
Stops round outward. Distant liquidity is diagnosed rather than blindly widening
the stop. Two closed neighbours on each side confirm liquidity swing proxies;
already swept levels cannot serve as opposing-liquidity targets.

The final public contract is `validate_public_trade_plan`, in the existing
trade-plan validator. It uses the worst entry-zone edge, final protected stop,
TP2 and execution costs. The public floor is always 3.0 and higher configured
floors remain binding. The prospective target selection is independent of RR:
TP2 must be an opposing structural level, targets round inward, and weak final
economics are rejected without target expansion. Telegram and lifecycle use
the same validator instead of trusting an asserted RR or falling back to TP1.

`execution_friction_bps=12` is an explicit conservative modelling allowance,
not a verified exchange fee schedule. Observed spread is added when available;
otherwise two ticks are reserved. Actual fees/spread/slippage remain Unverified
when no verified observation exists. Risk sizing includes this allowance and
rounds quantity down, so a wider stop cannot increase permitted monetary risk.

Prospective strategy output says `Entry confirmed`, never `Filled` from a wick.
Historical outcome tracking keeps its existing simulated state progression and
same-candle stop precedence. New entry evidence explicitly records simulated
price contact, false user/exchange execution verification, source timestamps
and economic plan version. A simulated touch cannot supply missing structural
confirmation for a new public signal. Existing TP milestones and epoch ownership
are retained; no historical database rewrite or schema migration was performed.

Explicit primary rejection categories distinguish setup absence, missing
confirmation, invalid economics, unavailable/stale data, provider failures and
internal errors. No category provides directional market evidence.

Verification: **231 passed in 31.97 seconds** for focused risk/strategy/public
delivery regressions; **208 passed in 28.32 seconds** for historical lifecycle,
geometry, economic identity and new anti-forgery/contact guards; final economic
contract run **151 passed in 15.69 seconds**. Runs overlap. Old positive public
fixtures that asserted 3R/4R with weaker TP2 geometry were corrected to truthful
synthetic plans. Their deduplication, delivery and milestone assertions remain;
negative tests explicitly retain the old failing geometry. Historical outcome
fixtures retain their default economics; only prospective transport fixtures
opt into a qualifying plan before persistence/ownership is established.

## Acquisition, supporting evidence and persistence (group D)

One watch owner now retains one public HTTP pool and market cache across its
sequential iterations. Shared identical acquisitions are cancellation-safe;
shutdown cancels and joins pending fetches. Closed 1D/12H/4H/15M data expires at
the next actual candle boundary. A delayed latest close gets at most a one-second
retry window, never a full-day stale cache. Original acquisition facts survive
cache delivery. Bulk exchange metadata is reused for five minutes, preserving
its original receipt time. Explicit historical cutoffs reject later metadata.

HTTP concurrency is four, with smaller endpoint budgets for metadata and
derivatives histories. Shared 418/429 cooldowns honor numeric or dated Retry-After;
retries have bounded exponential jitter. Response bodies, retry history, cache
entries, disk-cache bytes and in-flight requests have explicit caps. Existing
WebSocket resynchronization and untrusted-state clearing remain authoritative.

The existing derivatives source-truth evaluator supplies compact optional
supporting context. Its research enforcement flags remain unchanged. Only fresh,
unit-compatible, identity-verified values available at the structural cutoff
reach existing numeric guards. Later supporting observations retain a separate
cutoff and cannot supply structural confirmation. Missing values are N/A;
disconnected, stale and unverified streams are never promoted. A synchronized
causal book spread may increase modeled execution costs, never reduce them.

The actual SQLite path already uses merged lossless symbol references to avoid
duplicating full symbol payloads in scan rows. Its equality/hash checks and epoch,
transaction, ownership and cancellation boundaries are preserved. The new
context is bounded and stored once per symbol, without raw streams or duplicate
copies for each strategy mode. No new database or persistence framework is added.

Verification: 158 passed in 32.22 seconds (source truth, candles, acquisition and
WebSocket recovery); 112 passed in 33.22 seconds (watch, cache, HTTP and disposable
SQLite storage); final ownership/freshness/spread checks: 27 passed in 1.27 seconds.
These runs overlap. Compileall and diff checks passed. Final full-suite and
real-scanner positive product evidence is recorded below.

## Customer delivery and real product path (group E)

Customer cards identify TP2 RR, preserve stored exchange price precision, state
hard-stop price contact and conditional risk-based execution, and show optional
derivatives availability explicitly. Zone and TP updates say observed/reached;
simulated tracking never claims user fills, realized profit or exchange execution.
Existing TP/BE history, receipt identity, outbox fencing and deduplication remain.
The generic legacy trade-alert renderer now delegates to the same public economic
validator, carries modeled execution cost and cannot relabel an asserted best RR
as TP2 RR or confirm an incomplete/sub-3R plan.

Real orchestration found and corrected three integration gaps: native Binance
candles use the existing `binance_futures` venue label; optional derivatives
absence was double-penalized as invalid mandatory data; independent target
suggestions could contradict the final structural TP2. Exact futures aliases
now normalize consistently, mandatory health uses the existing required/optional
classifier for V2, and target assessment revalidates the final plan rather than
selecting a competing TP2. Legacy target-policy behavior and all numerical public
score/quality/RR floors remain unchanged. No confidence score is invented.

A structural zone awaiting contact/reaction retains its protected conditional
plan and its explicit failed entry gate. The existing near-miss/WATCH policy
recognizes these pending-entry reasons. Execution eligibility remains false,
confirmation count is zero, and a new closed reaction plus separate qualifying
observation is required before public confirmation.

`tests/test_integrated_signal_product.py` uses real CMC U100 qualification and
identity intersection, native Binance HTTP normalization/evidence capture, real
ScannerRunner, LiquidityGrabEngine, risk and quality assessment, lifecycle,
disposable SQLite and fake Telegram. Automatic epoch/origin test shortcuts are
disabled. It proves LONG/SHORT construction at 0.618/0.750/0.786, native closed
timeframes, protected stops, net TP2 RR >=3, optional-derivatives outages,
TRIGGERED-to-CONFIRMED progression, one send across repeated delivery, and
complete public output. A separate natural WATCH-to-trigger-to-confirmation
case keeps the same setup identity. Invalidated, weak-volume, wick-only,
unconfirmed-4H, low-RR, stale-daily and structural-provider-failure cases reject.

Focused verification includes 210 passing tests (native product construction,
target assessment, candle integrity and acquisition provenance), 245 passing
strategy/lifecycle/quality/fake-delivery tests, and 104 passing ownership,
foreign-plan attack and active-dashboard tests. Runs overlap. A previous full
run reported 54 failures and 4,340 passes; those failures were repaired with
truthful positive fixture economics and updated observed-event assertions.
The subsequent full-suite and CI results are recorded below; earlier failed
runs are retained here as debugging evidence, not counted as passing checks.

Old positive public fixtures asserting stronger RR than their actual TP2 were
corrected before persistence/ownership. Existing negative cases and assertions
remain. Disk-cache lineage tests now observe a hit inside the deliberate
one-second retry window for an incomplete latest candle; expired entries still
refetch. Cache value-integrity probes run under asyncio because acquisition
deduplication now creates tasks. The optional-timeout test has an explicit
250ms delayed optional source against a 100ms request budget and additionally
asserts the actual timeout diagnostic; mandatory setup work is not assigned an
unrealistic 10ms budget. Source-inventory anchors/artifacts were regenerated.

## Ranking refresh budget follow-up

The existing CMC service retains one successful, bounded bulk discovery response
for 30 minutes. Every delivery re-normalizes it against the current identity
manifest, including approval and expiry; original source/acquisition timestamps
are preserved. A failed refresh stops new discovery rather than relabelling an
old snapshot. No second ranking source or durable raw-stream store is introduced.

A deterministic 24-hour simulation of 288 five-minute scans makes 48 successful
500-row acquisitions. At two credits each, this healthy refresh workload is 96
credits/day or 2,976 in 31 days, within the repository's existing 100/3,000
guards. Retries, restarts and other CMC consumers still share the existing credit
guard; exhaustion fails closed and owned-plan monitoring continues. These are
configuration/load calculations, not private account usage or a quota guarantee.
Focused client/U100/storage/product checks: **58 passed in 12.29 seconds**;
final U100/supervisor checks: **19 passed in 1.93 seconds**. Runs overlap.
Final combined CMC/client/real-product/discovery/supervisor regression run:
**103 passed in 44.90 seconds**. Full compileall, pip check and diff check passed.

## Final DEV verification and acceptance coverage

At implementation commit `037b5015904d956907bc66eec3a33e735a310d84`:

- Full local Windows suite: **4,397 passed, 1 warning in 868.81 seconds**.
- Exact-commit GitHub CI: **4,395 passed, 2 skipped, 1 warning in 368.15 seconds**.
  The two skipped cases require real Windows CreateFile sharing semantics and
  passed in the local Windows suite. The warning is the installed Starlette
  test client's deprecation of its httpx integration.
- CI dependency checks, full compileall and full pytest passed:
  https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40918361646.
- The later ranking-budget and direction-normalization follow-ups add four tests;
  their final exact-head CI
  result is maintained in the review record for the same PR:
  https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/165.

Final review reproduced a public-wrapper edge-selection defect for a normalized
input such as `" LONG "`: the underlying validator accepted the trimmed direction
but the wrapper had selected the favourable entry edge. The wrapper now
normalizes direction once before both edge selection and validation. Expanded
LONG/SHORT cases retain the same sub-3R, friction and stricter-floor assertions;
the new LONG case failed before the fix. No public threshold was relaxed.
Final protected-plan, real-product, CMC U100 and lifecycle-eligibility regression:
**82 passed in 38.59 seconds**. Compileall, pip check and diff check passed again.

The following are deterministic DEV acceptance results, with mocked upstream
responses, fake Telegram and disposable SQLite. They are not live Runtime
benchmarks, real fills or profitability evidence.

| Case | Result | Evidence |
| --- | --- | --- |
| 1. CMC U100 verified perpetual intersection | Passed | `test_cmc_u100_provider.py`, `_cmc_membership` in `test_integrated_signal_product.py`; unknowns, exclusions, collisions, multipliers and no backfill |
| 2. Qualified LONG | Passed | Real ScannerRunner/native Binance normalization/risk/lifecycle/fake delivery |
| 3. Qualified SHORT | Passed | Same path with independently evaluated SHORT fixtures |
| 4. All three Fibonacci levels | Passed | LONG and SHORT parametrization for 0.618, 0.750 and 0.786 |
| 5. Protected structural SL | Passed | `test_liquidity_protected_plan.py`, real product stop assertions |
| 6. Invalid/weak/unconfirmed/sub-3R rejection | Passed | Real product negative markets, no TP3 rescue, stricter configured floor retained |
| 7. WATCH to trigger to confirmation | Passed | Real product progression retains owner identity; separate confirming observation |
| 8. Wick contact versus confirmation | Passed | Wick-only market rejection, simulated-contact guard, outcome contact tests |
| 9. TP1/TP2/TP3/SL/BE history | Passed | `test_lifecycle_outcomes.py`, `test_public_tp_milestone_delivery.py`, `test_public_lifecycle_followup_delivery_incident.py` |
| 10. Repeated scan/delivery deduplication | Passed | Real product single receipt/send plus existing outbox/ownership regressions |
| 11. Startup CMC/discovery outage | Passed | `test_f05_source_cleanup.py`: repeated outages, monitoring every iteration, fresh recovery |
| 12. REST and WebSocket recovery | Passed | `test_acquisition_recovery.py`, `test_websocket_recovery.py`, health/transport suites |
| 13. Optional derivatives missing | Passed | Real product 503 fixtures and `test_native_supporting_context.py`; no invented confirmation |
| 14. Stale mandatory source | Passed | Real product stale daily rejection, closed-candle/source-causality suites |
| 15. SQLite/epoch integrity | Passed | Real product exact row/receipt counts and hydration; single-copy, worker, epoch and ownership suites |
| 16. Worker shutdown/no overlap | Passed | Shared-client/cache cancellation and close; watch session/supervisor/WebSocket ownership suites |
| 17. Bounded acquisition/resources/timing | Passed in fixtures | Concurrency/response/cache bounds, request coalescing, cadence overrun and 24-hour CMC load simulation |
| 18. Complete truthful public plan | Passed | Six LONG/SHORT/ratio fake-delivery cases assert prices, TP2 RR, bias, confirmation, invalidation, missing context and risk warning |

Real limited-hardware Runtime scan duration, peak RSS, provider outage behavior
under production load, current production strategy/configuration and commercial
product registration are **Unverified**. They require the separately approved
rollout; no production operation was performed to infer them.

## Deployment and rollback requirements (owner action only)

1. Review PR #165 and its exact-head CI, then explicitly approve merge. Record
   the resulting reviewed release SHA. No automatic merge is authorized.
2. Separately approve a Runtime rollout. Before it, inspect the operator's actual
   launch arguments and effective configuration, confirm exclusive scanner and
   listener ownership, and preserve existing epoch/generation and database-path
   safeguards. A development default does not prove the running Runtime version.
3. Use the existing Python environment and normal reviewed release procedure.
   The prospective default is `selective-fib-mtf-v2` with native 1D/12H/4H/15M;
   explicit legacy historical configurations retain their interpretation.
4. CMC activation additionally requires owner-reviewed current numeric-ID to
   exact-perpetual bindings, genuine/excluded classifications, 1000-contract
   multipliers, approval/expiry and license/product-attribution prerequisites.
   Confirm shared quota headroom without printing secrets. Then explicitly
   select `MARKET_CAP_PROVIDER=cmc`; CMC is the sole ranking authority in that
   mode. The unchanged default remains CoinPaprika until this controlled switch.
5. Validate the selected source and deterministic dry-run in DEV/staging first,
   with Telegram dry-run and disposable storage. On the approved Runtime release,
   observe one owned scan/watch service, one separately owned listener, fresh
   required candles, bounded requests/cadence and delivery receipts. Do not enable
   order execution. This document does not authorize live starts or sends.
6. Rollback requires owner approval: stop only the affected release through the
   normal service procedure, restore the previously reviewed release (baseline
   `1107f1c42b29c32fec2c669cdfd19152d0608a56` for this PR) and its exact prior
   source/strategy arguments. Preserve the authoritative SQLite database, epoch,
   setup ownership, economic plans, outbox and receipts. Do not purge, rewrite,
   copy, VACUUM or roll back the database. Verify release/epoch compatibility
   before a controlled restart; if it cannot be established, keep discovery
   stopped and require operator review.

There is no schema migration in this PR. No Runtime deployment, live database
access, `.env` edit, actual Telegram delivery or order execution occurred.
