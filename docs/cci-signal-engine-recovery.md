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

Three unauthenticated GETs were attempted against the documented keyless CMC
listings, cryptocurrency-map and global-metrics endpoints. All returned HTTP
429. No credentials were read and no authenticated account quotas were checked.
Fresh live ranking availability, actual account entitlements and commercial
redistribution rights therefore remain **Unverified**. This blocks retiring
CoinPaprika or asserting that CMC production activation is ready.

Official references inspected:

- https://coinmarketcap.com/api/documentation/pro-api-reference/keyless-public-api
- https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency/
- https://coinmarketcap.com/api/documentation/pro-api-reference/tools/
- https://coinmarketcap.com/api/pricing/

The pricing page advertises commercial use, but it does not establish this
account's plan or subscriber redistribution rights. `/v1/key/info` requires
authentication. No inference about a specific account is made from public docs.

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
Full `compileall`, `pip check` and `git diff --check` passed. Exact-head full-suite,
end-to-end product verification and CI are still pending.
