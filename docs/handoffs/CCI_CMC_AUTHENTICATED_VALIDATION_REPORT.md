# CMC AUTHENTICATED VALIDATION REPORT

Date: 2026-10-26. Branch: `feature/cmc-hybrid-active-intelligence`.
PR: [#148](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/148).
Starting revision: `2990c8fbb3a5565859e1d1f722ea7fe734be5b53`.

## Finding

The owner's CMC credential successfully accessed all 15 implemented route
families with the bounded parameters tested. BTC, ETH and SOL Binance USDT
perpetual identities are verified. Fresh global metrics, current listings and
global liquidation aggregates pass their field contracts. Exact-contract CMC OI,
funding, index price and basis remain **Unverified** because the reviewed provider
definitions do not establish all required semantics. This capture establishes
usable contextual data, not incremental trading expectancy or authority to
activate a decision rule.

The [machine-readable evidence](../research/cmc_authenticated_validation.json)
contains source hashes, acquisition times, coverage, observations, comparisons,
credit use and quota observations. The [portable real-response fixture](../../tests/fixtures/cmc_authenticated/README.md)
supports credential-free regression replay. Raw bodies are retained locally in
`research_runs/cmc_authenticated_validation/20261026T123736Z`; they are ignored by
Git. Selected committed rows have their own hashes and retain original full-body
hashes separately; they are not presented as byte-identical wire responses.

## Credential and operational boundaries

The key was read from the owner's local `.env` with interpolation disabled,
wrapped in `SecretStr`, and supplied only in the fixed CMC HTTPS authentication
header. It was not printed, exported to the process environment, logged, changed,
or committed. Key-info responses were reduced to whitelisted limits/credit usage;
account identifiers and raw key-info bodies were not persisted. Capture rejects
any response containing the credential before writing evidence. Replay never
loads credentials or opens a network client.

Only read-only CMC GETs and Binance public USD-M market-data GETs were performed.
No scanner, Telegram listener, exchange private endpoint, production database,
order, transfer, deployment, merge or decision-rule activation was used. Existing
production identity and activation manifests are unchanged. The three reviewed
bindings are explicitly validation-only, approved at the actual engineering
review time `2026-10-26T12:57:52.652985Z`; market evidence used in the final
comparison was acquired after that review. No approval was backdated.

## Source coverage and budget

The pre-existing owner probe `research_runs/cmc_authenticated.json` was inspected:
`authenticated_api_verified=true`, eight successful route responses, seven
conservative reservations and six reported credits. That proved transport access,
not all field semantics. The additional validation ran from approximately
12:37:36 to **13:01:39.784751 UTC** on 2026-10-26.

| CMC route | Requests | Reported credits | Result for tested parameters |
| --- | ---: | ---: | --- |
| `/v3/cryptocurrency/listings/latest` | 2 | 2 | Quote-array shape and fresh rank/return fields validated |
| `/v1/cryptocurrency/map` | 1 | 0 | Numeric identity and slug matches; symbol collisions present |
| `/v1/cryptocurrency/listings/historical` | 1 | 1 | HTTP success; requested-day contract failed |
| `/v5/exchange/derivatives/list` | 1 | 1 | Binance registry verified; aggregate metrics aged by final cutoff |
| `/v5/exchange/derivatives/market-pairs/list/latest` | 3 | 3 | Exact three contracts and quote structure validated |
| `/v5/cryptocurrency/derivatives/market-pairs/list/latest` | 3 | 3 | Independently returned the same Binance market IDs |
| `/v5/derivatives/liquidations/quotes/latest` | 2 | 2 | Fresh global 1h/4h/24h USD totals and sides validated |
| `/v5/derivatives/liquidations/exchange/list/latest` | 1 | 1 | Binance aggregate shape validated; stale at final cutoff |
| `/v5/derivatives/liquidations/cryptocurrency/list/latest` | 1 | 1 | Three asset aggregates validated; stale at final cutoff |
| `/v1/global-metrics/quotes/latest` | 2 | 2 | Fresh cap, signed cap change and dominance validated |
| `/v3/fear-and-greed/latest` | 1 | 1 | Timestamped index validated within its daily freshness contract |
| `/v1/altcoin-season-index/latest` | 1 | 1 | Timestamped index validated within its daily freshness contract |
| `/v1/cryptocurrency/categories` | 1 | 1 | Returned sector timestamps from 2021/2024; stale |
| `/v1/cryptocurrency/category` | 1 | 1 | Membership timestamp from 2021; stale |
| `/v1/key/info` | 3 | 0 | Quotas/usage observed without saving raw account response |
| **Total** | **24** | **20** | **15 distinct routes, no CMC API errors** |

Configured local limits were **100 credits/day and 3,000/month**. A dedicated
sidecar enforced a **25-credit conservative validation cap**; 24 reservations
were consumed. The owner probe's seven reservations were also accounted for,
giving 31 conservative reservations versus the configured daily 100. Actual
reported cost including that probe was 26 credits. No endpoint access purchase or
plan upgrade was attempted.

Key-info reported **15,000 credits/month** and **50 requests/minute**. Its first
validation observation showed 13 credits used that day / 15 that month; the last
showed 33 / 35 and 14,965 remaining that month, consistent with the additional
20 credits. Account usage can include unrelated owner calls; the dedicated ledger
is the validation's usage authority. No plan name or provider daily credit limit
was returned, so those remain N/A. Successful small samples do not prove maximum
history depth, all symbols, full pagination or every subscription entitlement.

There were also **24 successful public Binance requests** across exchangeInfo,
fundingInfo, premiumIndex, openInterest, ticker price and funding history. Each
request had bounded response size, timeout and retry limits. No Binance key was
loaded or sent. The latest seven metric requests were launched concurrently
(one CMC pair page and six native OI/price requests); concurrency does not imply
matching provider source times.

## Verified instrument mapping

| Binance contract | CMC asset ID / slug | CMC market ID | Independent checks |
| --- | --- | ---: | --- |
| BTCUSDT | 1 / bitcoin | 47150 | Exchange and asset pair APIs; Binance metadata |
| ETHUSDT | 1027 / ethereum | 47205 | Same independent checks |
| SOLUSDT | 5426 / solana | 67944 | Same independent checks |

All three use CMC exchange ID **270**, slug `binance`, quote ID **825**, exact
base/quote exchange symbols, category perpetual, clean exclusions/outlier flags,
and Binance `TRADING`, `PERPETUAL`, quote/margin `USDT` metadata. The map response
contained 13 BTC, 6 ETH and 8 SOL symbol matches, including aliases/inactive assets.
Only exact numeric ID plus slug established the intended asset. The map returned
29 entries despite limit 20; no completeness claim was inferred from that limit.

## Field-level evidence and classifications

The primary definition reference is CMC's official
[derivatives reference](https://coinmarketcap.com/api/documentation/pro-api-reference/derivatives/)
and [schema collection](https://coinmarketcap.com/api/documentation/pro-api-reference/~schemas).
The downloaded schema was inspected independently of HTTP response success.

| Observation | Classification and reason |
| --- | --- |
| CMC reported contract last price | Three VERIFIED USD observations at the final cutoff: explicit conversion quote, exact identity, finite positive value and causal/fresh provider timestamp |
| CMC open interest | USD notional unit is documented for the conversion quote; **UNVERIFIED** because counting convention is not defined. No native base quantity was relabeled USD or compared numerically |
| CMC current funding | **UNVERIFIED**: interval and settled-versus-indicative meaning are absent from reviewed provider evidence. Binance cadence is not copied into CMC |
| CMC index price | **UNVERIFIED**: schema describes a contract index but does not independently establish its currency; the enclosing conversion quote alone is insufficient |
| CMC provider basis | **UNVERIFIED**: formula, sign and scale are not established. Raw value retained separately |
| Computed basis | Numeric diagnostic retained; **UNVERIFIED** without independently established index currency. No annualization |
| CMC global liquidation totals/sides | Nine VERIFIED USD rolling-window observations at cutoff, with explicit metric timestamps, nonnegative values and long+short consistency. Scope remains global/all contracts |
| CMC exchange/asset liquidations | Shape/windows validated at acquisition; STALE at final cutoff. They are aggregates, not contract liquidation events or local stop-sweep evidence |
| Global context/current listings | Four global metrics and twenty rank/return observations VERIFIED in latest captures. Signed negative cap return is valid |
| Fear & Greed / altcoin season | VERIFIED daily context; no confirmation authority |
| Sectors/category membership | STALE; 2021/2024 source times retained instead of replacing them with acquisition/envelope time |
| Dated historical listings | UNVERIFIED requested-day semantics: request was `2026-10-25`, but source timestamps were `2026-10-26T12:46:04...Z`. Three return fields additionally age to STALE at final cutoff |

The final contract page yields **3 VERIFIED and 15 UNVERIFIED** CMC derivative
observations. These statuses apply to the recorded validation cutoff. They do
not make the saved data fresh at a later replay wall clock.

Binance [USD-M market-data documentation](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
and the actual exchange responses support native contract metadata, base OI and
funding payment timestamps. All three fundingInfo rows explicitly reported
**8 hours**; `updateTime` was null. Their acquisition times are recorded separately;
no provider metadata timestamp was invented. The funding-history facts describe
settled payments, whose latest source time was 08:00 UTC, not CMC's current rate.
The existing native bridge does not infer an interval from exchangeInfo.

## Actual CMC-versus-Binance comparison

The existing comparator was executed against nine metric pairs with verified
instrument identities. No units, timestamps, tolerances or semantics were changed
to force agreement.

| Metric | BTCUSDT | ETHUSDT | SOLUSDT |
| --- | --- | --- | --- |
| Open interest | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| Funding | UNVERIFIED | UNVERIFIED | UNVERIFIED |
| Last price | UNVERIFIED | UNIT_MISMATCH | UNIT_MISMATCH |

Native OI was verified as base quantity: 94,218.408 BTC, 2,327,294.170 ETH and
8,759,152.35 SOL. CMC OI is USD notional with unspecified counting, so it cannot
be compared to those quantities. Native funding history was stale and CMC rate
semantics were incomplete. Native price is USDT while CMC reported price is USD;
USDT/USD parity was not assumed.

The BTC ticker provider timestamp exceeded local acquisition by **11.862 ms**,
so its causal check correctly returned Unverified; no clock correction or grace
period was invented. Price source skew was approximately **81 seconds**, also
exceeding the unchanged 30-second comparison limit. Unit mismatch takes precedence
over skew for ETH/SOL. No CONSISTENT or MATERIAL_DIVERGENCE result was produced.

The normalized-price-minus-index diagnostic had the opposite sign from raw
provider basis in this sample. Reported and normalized prices also differed.
This is evidence of an unresolved basis/methodology convention, not sufficient
evidence of a provider defect or permission to flip signs.

## Implementation defects corrected

1. Signed `market_cap_change_24h` was incorrectly caught by the nonnegative
   market-cap check. Negative returns now remain valid; negative capitalization
   still fails. The final real response's -0.01246099% is VERIFIED.
2. Index price and computed basis previously inherited USD without a separate
   index-currency review. Both now require explicit reviewed index units; raw
   provider basis remains Unverified.
3. Historical HTTP success could mask a source timestamp after the requested day.
   The client now preserves request parameters and normalization rejects missing
   request dates or source times later than the requested UTC day.
4. The native bridge dropped valid source facts because it treated the documented
   request-context category as an identity conflict, even after independent exact
   contract binding. That one known provenance annotation is accepted; missing or
   mismatched response symbols and all other identity reason codes still fail.
   Quote notional retains the instrument's quote currency rather than becoming USD.

The new capture/replay CLI is isolated from scanner startup and policy evaluation.
No scanner decision algorithm, score, gate, ranking formula or trading setup was
changed. Existing tests retain their assertions.

## Verification and handoff

The existing project `.venv` passed **114 targeted CMC tests** and the full
**3,667 distinct tests across all 179 test files**, with zero failures, errors or
skips. The full local run used four disjoint file partitions in canonical order,
independent temporary/cache directories, and explicit safe DEV flags (including
research-watch false). Counts were 864 + 827 + 995 + 981; unique testcase IDs were
checked to prevent duplicate counting. Dependency consistency and compilation of
`app`, `scripts`, `src` and `tests` also passed. No test assertions were weakened.
The [verification manifest](../research/cmc_authenticated_verification.json)
records JUnit SHA-256 hashes and exact environment details. Serial GitHub CI on
the pushed implementation revision is checked separately before completion.

Unresolved provider evidence: CMC OI counting; current funding kind/interval;
index currency and basis convention; normalized-versus-reported price method;
historical requested-day behavior; stale sector timestamps; broader plan/history
coverage; and source-time skew suitable for strict cross-provider comparisons.
Provider clarification or an independently reviewed versioned contract is needed
to remove those blockers. None was converted into a production semantics manifest.

Next phase: obtain those specific definitions and collect a contiguous prospective
shadow dataset under separate runtime authorization. Evaluate canonical candidates,
including rejected winners and retained losers, with verified economic outcomes,
fees/slippage and out-of-sample evidence. Real alpha/expectancy remains N/A.
Keep CMC mode OFF and rules disabled until explicit owner release/activation
approval. This phase performs no merge, deployment or activation.
