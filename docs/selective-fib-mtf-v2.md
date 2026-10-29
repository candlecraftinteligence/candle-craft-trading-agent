# Selective Fibonacci and multi-timeframe strategy v2

Development branch: `feature/cci-selective-fib-mtf-v2`.
Base: `ff85a7de744507b593ef118e322e47eba171f311` (merged PR #151).
Runtime deployment is **NOT AUTHORIZED**. This change does not migrate Runtime,
activate a CMC policy, change provider budgets, or enable delivery or orders.

## Prospective strategy contract

The scanner and CLI default to `selective-fib-mtf-v2` and use:

| Role | Native exchange interval |
| --- | --- |
| Higher-timeframe context | 1D |
| Directional bias | 12H |
| Structural analysis | 2H |
| Execution and confirmation | 15M |

All four series must contain sufficient consecutive, correctly identified,
UTC-aligned, closed candles and cover their latest required completed interval
at the decision cutoff. Open candles are excluded. Missing, stale, malformed,
mislabeled, or unverified required data fails closed. Daily data is requested
directly from the exchange and retains native source/batch provenance; 2D data
is never renamed or used as a replacement. The 2D resampler and all historical
2D diagnostic fields remain available for legacy interpretation.
Unsupported v2 hierarchies fail closed at discovery; constructing configuration
does not interrupt monitoring of an already owned legacy setup.

The only independent v2 retracement targets are Decimal `0.618`, `0.750`, and
`0.786`. The exact price is `impulse_end - (impulse_end - impulse_start) * ratio`
for either direction. Targets must intersect an existing detected OB, FVG, or
their actual intersection. Zone edges are never widened. A single tick price
inside that same original zone becomes the entry intersection.

Precision comes from the exchange's public price filter, bound to exchange,
symbol, source endpoint and acquisition timestamp, with a 300-second freshness
limit. Quantity precision is not substituted. Rounding is bounded by **both**
half a verified tick and 0.0001 of the impulse range. Both the exact target and
rounded price must lie inside the original structural zone. Instruments that
cannot fit the existing eight-decimal trade-plan/public-price contract fail
closed. Missing precision is Unverified, never a made-up default tick.

Selection ranks executable structural overlap and freshness before shallower
retracement risk; it does not rank distance from the current price. A rejected
deeper target cannot rescue an invalidated setup. No aggressive-toggle drift
can re-enable the legacy 0.382–0.618 range or independent 0.500/0.650 entries.

## Deep target protection

0.618 retains existing sweep, BOS/CHoCH, zone validity, trust, risk, freshness,
trade-plan and RR checks. 0.750 additionally requires a fresh zone, aligned
confirmed 2H structural shift, complete sweep/BOS/CHoCH and momentum evidence,
and a two-candle structural acceptance sequence after BOS. Both real closed
bodies must point in the trade direction on the accepted side; the second
close must exceed the first candle's favorable extreme. A ticker cannot
provide this evidence.

**0.786 remains observational WATCH-only, including after a strong reclaim.**
Strong reclaim requires a target touch followed by the same two-body sequence.
Weak or absent reclaim is recorded separately. Freshness and structural failures
remain explicit rejection reasons. The current lifecycle invalidates body
acceptance beyond 0.786 while the structural stop is placed beyond the sweep.
There is no approved re-entry/lifecycle/stop contract for executing that boundary.
This phase does not bypass that contract or change lifecycle ownership.

Any body close beyond 0.786, persistent body acceptance, structural breakdown,
weak wick reclaim, invalid stop/target ordering, public-price geometry failure,
or insufficient RR retains the existing rejection. Existing target construction,
hard RR floors, score and trust thresholds are preserved; no fixed target was
added to force a passing RR. Public eligibility, identity, deduplication, and
runtime epoch ownership remain canonical.

## Evidence and compatibility

Prospective setup diagnostics persist `strategy_version` and `selective_fib`:
selected ratio, impulse anchors and candle indices/timestamps, exact calculated
price, executable tick price, actual entry intersection, original zone bounds,
direction, timeframe, structural confluence, precision source/tolerance, reclaim
classification, selected rejection reasons and per-candidate rejection reasons.
The existing scan JSON persistence retains this evidence. Run provenance also
captures the strategy version in its allowlisted configuration and hash.

The low-level engine and historical replay API retain `liquidity-grab-v1` as
their backward-compatible default. New scanner calls explicitly pass v2.
Legacy replay and the legacy Fibonacci research metrics retain their original
2D/range semantics; old observations and outcomes are not rewritten. CLI legacy
requests must explicitly choose `--strategy-version liquidity-grab-v1` and their
original timeframes. All four CLI presets use the v2 hierarchy by default.

V2 replay must be explicitly versioned and supplied genuine timeframes plus an
archived `InstrumentPrecision` in its per-symbol timeframe context, verified at
each observation cutoff. Missing historical precision blocks entries; current
metadata is not backfilled into historical decisions. Replay candidates and
summaries retain their strategy version. This implementation makes no claim
about win rate or positive expectancy. Prospective observations can be compared
by strategy version without relabeling historical results.

## Verification and handoff

The regression suite includes directional target calculations and confluence,
disallowed legacy ratios, deep acceptance and reclaim, wick/body invalidation,
RR/trade-plan rejection, native closed-timeframe acquisition and source identity,
missing/stale data, formatter compatibility, public API precision failures,
and prospective/historical version separation. Final test and exact-head CI
results are recorded in the PR handoff. Development testing uses the existing
project virtual environment and isolated test databases with dry-run settings.

Next phase: owner review and an explicit Runtime release decision, followed by
prospective version-separated observation. Approving an executable 0.786 contract
requires a separate lifecycle/stop design and regression phase. No Runtime PC,
live database, scanner/listener operation, Telegram history, or deployment was
accessed in this development phase.
