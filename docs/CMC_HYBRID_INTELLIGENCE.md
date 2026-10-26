# CCI V1 CMC Hybrid Futures Active Decision Intelligence

This subsystem adds independent market evidence to selection of already eligible CCI V1 opportunities. Default mode is `off`. No production rule activation, scanner/listener start, Runtime database operation, deployment, exchange order execution, or transfer is included in this change.

## Architecture and authoritative boundary

`app/cmc` contains contracts, REST acquisition, identity, normalization, independent features, comparisons, policy, storage and research evaluation. The provider interface is CMC REST; MCP is not imported or required.

The existing exchange-data → structure/liquidity → risk/quality/scoring → lifecycle flow remains authoritative. `ScannerRunner` starts one bounded CMC acquisition worker when configured. It requests shared pages and global snapshots in the background, including during long scans, and captures the existing exchange source facts without additional per-symbol requests. It never substitutes CMC prices or OI into the strategy.

Public delivery builds CMC candidates **after** the existing canonical eligibility checks. Recommendations use the actual CMC selection timestamp. `Candidate.original_decision_at` preserves the original CCI analysis cutoff; no CCI decision timestamp is rewritten. Evidence acquired after the selection timestamp is ineligible. This separates a new selection decision from an earlier strategy observation. Historical replays must pass their original selection cutoff to the pure evaluator; later acquisitions cannot be attached to it.

Only a typed evaluation tied to the exact current setup ID, economic plan version, direction and symbol can affect selection. Current V1 initially publishes CONFIRMED lifecycle events; the old initial WATCHLIST ranking path is not enabled by CMC. Authorized priority adjusts delivery order only between new eligible candidates with identical canonical quality, RR, technical and opportunity ranks, preserving all other occupied positions and send counts. The legacy WATCHLIST sort hook remains compatible without enabling its publication. Canonical thresholds and scores are unchanged. The final veto applies only to a newly eligible public candidate. Prior public signals, entry/fill notices, TP/SL updates, invalidations and other follow-ups retain their ownership and deduplication paths.

The veto does not mutate trade geometry, invalidation, lifecycle records or public event keys. Positive support is an evidence annotation, not a lifecycle confirmation. Missing IDs or an unavailable provider cannot promote or reject a setup.

OFF/COLLECT and failed/new invalid evaluation boundaries clear any attached in-memory CMC authority. A separate selection intent records the exact baseline/actual order and selected IDs before enforcement reaches delivery; failure to persist it restores baseline. Post-delivery status records are linked separately and never treated as fills or realized outcomes. Feature construction skips rows that no current formula consumes before repeating causal checks per candidate.

## Official endpoint contracts and account capabilities

Contracts were reviewed on 2026-10-26 against CMC's official [cryptocurrency reference](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency/), [derivatives reference](https://coinmarketcap.com/api/documentation/pro-api-reference/derivatives/), [global metrics reference](https://coinmarketcap.com/api/documentation/pro-api-reference/global-metrics/) and [keyless guide](https://coinmarketcap.com/api/documentation/pro-api-reference/keyless-public-api/). These are documentation claims, not verification of this owner's subscription.

| REST route | Support / acquisition | Credit reservation for one USD conversion |
| --- | --- | --- |
| `/v3/cryptocurrency/listings/latest` | Paginated rank and returns; background | ceil(limit/250) |
| `/v1/cryptocurrency/map` | Paginated numeric identity; background, daily cache | 0, reconciled upward if provider reports otherwise |
| `/v1/cryptocurrency/listings/historical` | Explicit date through REST client; never background/backfilled automatically | ceil(limit/100) |
| `/v5/exchange/derivatives/list` | Derivatives exchange registry and aggregate OI/turnover; background | ceil(limit/250) |
| `/v5/exchange/derivatives/market-pairs/list/latest` | Binance perpetual pages; background | ceil(limit/250) |
| `/v5/cryptocurrency/derivatives/market-pairs/list/latest` | Cross-exchange pages for explicitly configured reviewed assets | ceil(limit/250) |
| `/v5/derivatives/liquidations/quotes/latest` | Global rolling aggregates; background | 1 |
| `/v5/derivatives/liquidations/exchange/list/latest` | Exchange aggregates; bounded page | ceil(limit/250) |
| `/v5/derivatives/liquidations/cryptocurrency/list/latest` | Asset/all-exchange aggregates; bounded page | ceil(limit/250) |
| `/v1/global-metrics/quotes/latest` | Market cap, 24h cap change, BTC/ETH dominance | 1 |
| `/v3/fear-and-greed/latest` | Context only; never a confirmation rule | 1 |
| `/v1/altcoin-season-index/latest` | Rotation context | 1 |
| `/v1/cryptocurrency/categories` | Sector returns, bounded page | 1 + ceil(limit/200) |
| `/v1/cryptocurrency/category` | Causal membership for up to four configured categories | ceil(limit/250) |
| `/v1/key/info` | Explicit authenticated capability probe only; no secret/plan payload printed | conservative 1 |

V3 listings use `quote[]` entries identified by USD ID 2781 and symbol, while V1 historical listings use `quote.USD`. Liquidation responses use `quotes[]`, `exchanges[]` or `cryptocurrencies[]`. The altcoin index uses `altcoin_index` and `snapshot_time`; Fear & Greed uses `value` and `update_time`. Envelope timestamps are never metric timestamps.

Current documentation advertises Free/Basic and higher tiers for many latest routes, and historical listings with plan-dependent history (Basic: one year; Builder: three years; Startup and higher: from 2013). The [2026-10-26 authenticated validation](handoffs/CCI_CMC_AUTHENTICATED_VALIDATION_REPORT.md) successfully accessed all 15 route families with bounded samples. Key-info reported 15,000 monthly credits and 50 requests/minute, without a plan name. Maximum history depth and broader entitlements remain Unverified; the dated historical sample failed its requested-day semantics despite HTTP success. No subscription is purchased. Keyless support is limited to documented public routes; authenticated derivatives are not emulated with MCP or anonymous web data.

## Evidence and instrument identity

Every normalized value has a stable hash ID, provider/endpoint, numeric CMC ID, native/raw type and textual value, Decimal normalized value, units, aggregation scope, source/acquisition timestamps, freshness policy, schema version and raw evidence reference. Successful responses remain UNVERIFIED until field-level normalization and semantic validation succeed. States are VERIFIED, STALE, UNVERIFIED, MISSING, AMBIGUOUS, UNAVAILABLE and ERROR. Missing display is N/A; unreliable display is Unverified.

Raw successful REST text is retained with its SHA-256, without request headers. Request/response failures use bounded reason codes instead of arbitrary error text that could echo credentials. Source timestamps are never filled from receipt time. Identity metadata availability is recorded separately from market source time.

Binance matching requires an explicitly approved CMC ID/slug ↔ Binance base binding, a CMC ID-map record, agreement with the independent derivatives exchange registry, exact market ID and perpetual category, base/quote IDs and exchange symbols, clean outlier/exclusion flags, and independent Binance USDT-margined TRADING/PERPETUAL metadata. Non-unit multipliers are rejected. Symbols alone never establish identity. Metadata, binding, map and exchange-registry acquisition times must all be causal; metadata expires after a day.

An owner-reviewed semantics manifest must separately establish OI units/counting, funding interval/kind and index-price currency for each market. Unknown semantics remain Unverified. Actual authenticated verification established conversion-currency USD OI but did not establish its counting convention, CMC funding interval/kind or index currency. Raw provider basis has an unresolved formula/sign convention. Computed basis requires separately reviewed USD index units. Cross-exchange funding additionally needs reviewed independent venue metadata, matching asset/USDT/perpetual identity, identical funding interval and settled/indicative semantics. At most four such assets are polled. No external venue is automatically trusted.

Existing Binance source facts are bound with the existing metadata binder. Naked legacy numbers are not comparison evidence. Comparisons require the same exact instrument, metric, units, counting/rate semantics and timestamps within 30 seconds; relative divergence tolerance is 2%. Outputs include CONSISTENT, MATERIAL_DIVERGENCE, TEMPORALLY_MISALIGNED, UNIT_MISMATCH, IDENTITY_MISMATCH, NOT_COMPARABLE and UNVERIFIED. Aggregate CMC OI is never compared as Binance-specific OI. Global liquidation totals never prove a local stop sweep.

The native bridge accepts the documented request-context category only after independent contract binding, and rejects other identity conflicts. Quote notional retains USDT where that is the instrument's quote; USD parity is not assumed. Historical normalization preserves the requested date and rejects source timestamps later than that UTC day. Signed market-cap percentage changes are valid even when negative.

## Feature definitions, version 1

All features record input evidence IDs, source/acquisition timestamps, units, version, scope and missing-data reason. Calculations require fresh causal verified observations and compatible timestamps. Missing/ambiguous inputs return N/A rather than a favorable zero.

| Feature | Formula / relevance |
| --- | --- |
| CmcGlobalMarketRisk | Negative reported 24h total market-cap change, in percent |
| CmcDerivativesLeverage | Exchange aggregate USD OI / 24h USD derivatives turnover; not actual account leverage |
| CmcFundingDispersion | max minus min equivalent funding across at least two exchanges |
| CmcOpenInterestContext | Exact perpetual OI, retaining verified units |
| CmcFundingContext | Exact verified per-interval funding, retaining rate semantics |
| CmcBasisContext | (Exchange-reported contract price − index price) / index price; no annualization |
| CmcLiquidationPressure | 24 × rolling global 1h liquidations / rolling global 24h liquidations |
| CmcLiquidationImbalance | (Long − short rolling 1h liquidations) / total |
| CmcBtcDominanceContext | Observed BTC market-cap share; ETH dominance retained separately |
| CmcAltcoinRotation | CMC altcoin-season index / 100 |
| CmcSectorRelativeStrength | One unambiguously observed sector's 24h return minus total market-cap change; causal membership required |
| CmcDataConfidence | Fraction of core contextual features with verified inputs; not a probability or ranking score |
| CmcMarketCapRank | Observed positive integer CMC rank |
| CmcDataConsistency | 1 for equivalent consistent observations; −1 for a material equivalent-source divergence; N/A otherwise |
| CmcRegimeCompatibility | Research classification: direction agrees/opposes CCI RISK_ON/OFF and an independent >3% matching cap movement; other known regimes are neutral |

The sector formula uses a broad capitalization-return benchmark, not an assertion that sector average price change and total capitalization change have identical constituent weighting. This distinction must be considered in calibration. Multiple simultaneous sector memberships are deliberately ambiguous in version 1.

## Decision rules and activation

| Rule ID / version | Recommendation and deterministic condition | Production default |
| --- | --- | --- |
| `data_divergence` / 1 | VETO_CANDIDATE when verified equivalent consistency classification is −1 | Disabled |
| `liquidation_stress` / 1 | VETO_CANDIDATE when hourly stress ratio ≥4 and direction-signed imbalance ≥0.75 | Disabled |
| `adverse_regime` / 1 | PRIORITY_DECREASE when direction-signed negative cap change >3% and stress ≥2 | Disabled |
| `sector_support` / 1 | PRIORITY_INCREASE when direction-signed sector relative return ≥2 percentage points, cap return supports direction and absolute basis ≤0.005 | Disabled |
| `consistent_context` / 1 | CONFIRMED_SUPPORT when equivalent sources agree and absolute basis ≤0.005; eligibility unchanged | Disabled |

Each candidate/rule emits NO_EFFECT, CONFIRMED_SUPPORT, PRIORITY_INCREASE, PRIORITY_DECREASE, VETO_CANDIDATE or INSUFFICIENT_EVIDENCE, with original setup and economic-plan IDs, direction, canonical eligibility, references/timestamps, rule ID/version/formula hash, reason, evidence status, hypothetical/enforced state and resulting policy eligibility. Unknown feature versions, stale/future evidence, duplicate feature definitions and duplicate approvals cannot enforce a rule.

DECISION is implemented, but mode alone grants no authority. Each enabled activation must match the exact rule version and specification hash and include source-contract, causal evaluation, out-of-sample/prospective, tests, hard-gate compatibility, baseline-comparison and owner-approval references, an affirmative no-material-negative-regression attestation, and a valid approval/expiry interval. These are owner-reviewed evidence attestations, not cryptographic verification of linked reports. An empty manifest activates nothing. No production approvals or favorable performance claims are shipped.

Modes:

- `off`: no provider/storage initialization; original serialized scanner config/results and canonical decisions preserved.
- `collect`: acquire and persist evidence only.
- `shadow`: evaluate complete recommendations and hypothetical selection; public decisions unchanged.
- `decision`: apply only individually approved, current, version-matched rules; canonical gates still run.

## Storage, resources and failure behavior

The dedicated `research_runs/cmc_intelligence.sqlite` sidecar uses a CMC application ID and rejects foreign database schemas/names. It does not migrate or attach the CCI database. Stable IDs and INSERT OR IGNORE make evidence writes idempotent. UTC-normalized indexed source/acquisition/decision timestamps support causal lookup. Instrument identities exclude changing acquisition metadata from their economic identity hash.

It stores raw references, snapshots/observations, exchange metadata, mappings, collection/credit/error summaries, features, native comparison facts and matched policy/candidate batches. `policy_batch` is marked `EVALUATED_NOT_DELIVERY_PROOF`: a policy recommendation is not proof of a Telegram send, fill, or realized outcome. Existing canonical delivery records remain the authority for actual effects. A cancelled/timed-out write may leave an unapplied evaluation; research must not treat it as an enforced delivery. Persistence errors remove CMC influence from that selection pass.

Defaults: two concurrent HTTP requests; three attempts; three-second request timeout; one-MiB response bodies; 16 cache/in-flight keys; two pages per configured paginated source; 2,500 normalized observations; 30-second background collection deadline. CMC HTTP never runs synchronously per symbol. Worker cancellation closes its HTTP client. Sidecar initialization has a 250-ms wait budget, rehydration 100 ms, activation-file read 100 ms, policy persistence 250 ms, and SQLite busy timeout 100 ms. CPU, scheduling and OS disk latency are not hard real-time guarantees; timeouts preserve canonical behavior.

The sidecar's main-file page cap defaults to 64 MiB, with 20,000 records and 30-day retention. Logical payload eviction starts at half the page budget to allow indexes/slack. Indexed oldest-record batches bound maintenance; idle files are not eagerly purged. DELETE journaling avoids a persistent WAL; transient rollback journal space is additional to the main-file cap. No Runtime-PC benchmark is claimed.

Credit reservations persist across restarts, by UTC day/month, including retry attempts. Reported credits reconcile conservative reservations upward, never downward. Defaults are 100/day and 3,000/month; they intentionally stop collection before unknown subscription limits and do not guarantee continuous 24h coverage. Account limits/cadence must be reviewed before enabling collection. Keyless reported accounting units are not a claim of paid credit consumption. A three-failure circuit opens for 60 seconds; auth/entitlement failures remain explicit UNAVAILABLE states. Cache TTL does not establish source freshness.

## Configuration and safe verification

Settings: `CMC_MODE`, `CMC_API_KEY` (SecretStr, omitted from dumps/repr), `CMC_KEYLESS` (false), `CMC_SIDECAR_PATH`, `CMC_IDENTITY_MANIFEST`, `CMC_ACTIVATION_MANIFEST`, `CMC_POLL_SECONDS` (300), `CMC_DAILY_CREDITS`, `CMC_MONTHLY_CREDITS`, `CMC_STORAGE_MAX_BYTES`, `CMC_STORAGE_MAX_RECORDS`, `CMC_RETENTION_DAYS`. Settings changes require a process restart; a running loop does not accumulate collectors for changed configurations.

Use `config/cmc/identity.empty.json` and `config/cmc/activations.disabled.json` as disabled starting files. Build identity/semantic reviews using the typed `IdentityManifest` schema, preserving evidence references and actual review/acquisition dates. Never put API keys in either manifest or source control. Supply the key through the owner's approved process secret mechanism; do not assume it exists. Existing `.env` and live Runtime variables were not changed by this implementation.

DEV-only offline commands, using the existing environment:

```powershell
Set-Location C:\CandleCraftDev
$env:LOCAL_MANUAL_MODE = 'true'
$env:ORDER_EXECUTION_ENABLED = 'false'
$env:TELEGRAM_DRY_RUN = 'true'
$env:TELEGRAM_SIGNALS_ENABLED = 'false'
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m scripts.cmc_intelligence --demo --output research_runs/cmc_demo.json
.\.venv\Scripts\python.exe -m scripts.cmc_intelligence --evaluate research_runs/verified_research_rows.json --output research_runs/cmc_comparison.json
```

An explicitly requested CMC-only probe starts no scanner/listener and opens only the named sidecar:

```powershell
# Keyless: no credentials are read. Authenticated-only routes report UNAVAILABLE.
.\.venv\Scripts\python.exe -m scripts.cmc_intelligence --probe --output research_runs/cmc_keyless.json
# Owner runs this only after securely supplying CMC_API_KEY in this process:
.\.venv\Scripts\python.exe -m scripts.cmc_intelligence --probe --authenticated --output research_runs/cmc_authenticated.json
# Optional bounded collection, also with no scanner or Telegram listener:
.\.venv\Scripts\python.exe -m scripts.cmc_intelligence --collect-once --authenticated --identity-manifest config/cmc/identity.reviewed.json --output research_runs/cmc_collection.json
```

## Research method and limits

For bounded authenticated validation without exporting the local credential, use
the separate validation CLI. It reads `.env` in memory with interpolation
disabled, enforces the local daily/monthly budgets plus a conservative 25-credit
cap, and never starts a scanner/listener. `--capture` collects bounded source
families for one to three explicitly reviewed validation-only bindings;
`--refresh` adds fresh matched metrics to an existing capture. Use a dedicated
CMC sidecar ledger and retain it across invocations. The full capture fits the
default cap; prior owner-probe reservations are deducted from configured limits.
No production identity or activation manifest is created by this command.

```powershell
# Requires explicit owner authorization for authenticated acquisition.
.\.venv\Scripts\python.exe -m scripts.cmc_authenticated_validation --capture --source research_runs/cmc_validation --bindings research_runs/validation_bindings.reviewed.json --output research_runs/cmc_validation_report.json
# Fully offline replay of selected real public responses; no credential access.
.\.venv\Scripts\python.exe -m scripts.cmc_authenticated_validation --replay --source tests/fixtures/cmc_authenticated --bindings tests/fixtures/cmc_authenticated/bindings.reviewed.json --output research_runs/cmc_validation_replay.json
```

See the [authenticated report](handoffs/CCI_CMC_AUTHENTICATED_VALIDATION_REPORT.md)
for actual costs, route coverage, raw source hashes, nine comparison outcomes and
the distinction between field verification and production authority. Saved
captures are evaluated at their original final acquisition cutoff.

`ResearchRow` accepts a policy evaluation and an independently attributed economic-plan outcome. One original decision per economic plan is required; duplicate plans or ownership mismatches are rejected. Unknown/unverified outcomes are excluded from expectancy. Verified net R additionally requires known entry activation, close time, attribution reference, fees and slippage. The evaluator does not reconstruct trustworthy fills from candle touches or fix existing CCI attribution. Owner-provided independently reconstructed outcomes remain an external research input; no live Runtime database was accessed to obtain them.

The comparison reports original valid setups, hypothetical veto/priority/support counts, baseline versus shadow versus individually enabled decision selection, lost opportunities, prevented losers and rejected winners, long/short/regime/validation cohorts, entry activation, fill-probability coverage, net R, MAE, additional fee/slippage sensitivity, evidence coverage and incremental expectancy. Unknown values stay N/A. Original outcome records are never rewritten. Selection uses deterministic canonical ranking, then CMC tie-breaks. Include all vetoed winners as well as losers, all candidates competing for the same delivery limit, and an unfiltered contiguous evaluation period.

`cmc_hybrid_fixture_comparison.json` is deliberately synthetic and includes a vetoed winner and loser. It verifies mechanics, not profitability. `improvement_proven` remains false. Real out-of-sample/prospective data, verified execution attribution, and owner-reviewed analysis are required before any claim of incremental expectancy or rule activation. Historical REST data acquired today cannot be joined to yesterday's original decision as if it was known then.

## Controlled Runtime rollout and rollback — owner procedure only

The later [release readiness packet](handoffs/CCI_CMC_RELEASE_READINESS_REPORT.md),
[Runtime rollout plan](handoffs/CCI_CMC_RUNTIME_ROLLOUT_PLAN.md) and
[individual activation plan](handoffs/CCI_CMC_DECISION_ACTIVATION_PLAN.md)
supersede the generic procedure below where more specific. They require the
current schema-27 compatibility and full existing epoch identity, quantify credit
coverage constraints, and distinguish merge approval from Runtime approval.

No commands in this section were executed on Runtime. Deployment and rule activation require separate owner approval. Use the existing single Scanner V1 service/launcher and established Runtime safeguards.

1. Review the feature PR, source contracts, identity/semantic reviews, test evidence, storage/resource budgets and disabled activation manifest. Record the exact reviewed release SHA and approved collection credit limits. Do not merge automatically.
2. On DEV, run the authenticated CMC-only probes above after the owner supplies credentials. Verify endpoint-specific entitlements, timestamps, OI units/counting, funding kind/interval and instrument metadata. Keep unsupported fields Unverified. Confirm collection/sidecar recovery without scanner or Telegram operation.
3. With release approval, stop the existing Runtime scanner through its established launcher/service. Confirm no scanner remains before updating. Do not launch a parallel scanner or listener. Do not copy, migrate, inspect, or attach the live Runtime database for this feature.
4. In the existing Runtime repository, execute these reviewed Git checks/update steps. Stop if there are local changes or unexpected worktrees; do not reset/merge them:

```powershell
$RuntimeRepo = Read-Host 'Existing Runtime V1 repository path'
$ReviewedSha = Read-Host 'Owner-approved immutable release SHA'
Set-Location -LiteralPath $RuntimeRepo
git status --short --branch
git worktree list
# Continue only after the owner confirms a clean, stopped Runtime installation.
git fetch origin feature/cmc-hybrid-active-intelligence
git show --no-patch --format=fuller $ReviewedSha
git switch --detach $ReviewedSha
.\.venv\Scripts\python.exe -m compileall -q app/cmc
```

5. In the **existing launcher environment**, initially keep `CMC_MODE=off`, `ORDER_EXECUTION_ENABLED=false`, the existing Telegram policy, existing database path and existing runtime epoch/release safeguards. Configure the isolated CMC sidecar path and disabled activation manifest. Resume the existing single scanner with its established operator command. Verify unchanged baseline behavior.
6. After approval for observational collection, configure the same launcher's environment and restart that single process:

```powershell
# These values must be persisted through the existing launcher/service's approved
# environment mechanism, not by printing or rewriting its secret configuration.
$env:CMC_MODE = 'collect'
$env:CMC_SIDECAR_PATH = Join-Path $RuntimeRepo 'research_runs\cmc_intelligence.sqlite'
$env:CMC_ACTIVATION_MANIFEST = Join-Path $RuntimeRepo 'config\cmc\activations.disabled.json'
# Supply reviewed CMC_DAILY_CREDITS / CMC_MONTHLY_CREDITS and secure CMC_API_KEY
# through that same owner-controlled mechanism. Then resume only the existing service.
```

7. Observe credits, freshness/identity coverage, collection deadlines, scan interval/resource effects and sidecar size. Enable `shadow` only after those checks. Accumulate prospective evidence with canonical public decisions unchanged. Inspect both vetoed winners/losers and missed/retained opportunities.
8. `decision` requires a separate approval identifying each rule/version/formula hash plus all activation evidence. No such approval is included. Keep rules without enough evidence in shadow. Do not lower any CCI gate to improve coverage.
9. Roll back by setting that same launcher's `CMC_MODE=off` and restarting its single scanner. This immediately removes CMC selection authority without a CCI database rollback. If code rollback is needed, stop the service, switch to the previously recorded approved SHA, and use the existing launcher safeguards. Preserve the CMC sidecar for research; do not delete or rewrite CCI outcomes.

## V2 portability

The contracts, client, normalization, comparison, features, policy and research evaluator are independent of Scanner V1. A future consumer supplies immutable eligible candidates and canonical rank/IDs through a small adapter. The V1-only adapter lives in `integration.py` plus the three focused existing entry-point hooks. No Scanner V2 code was developed.
