# CCI V2 production functionality repair and CMC activation

Historical implementation-phase report. Current release finalization, compatible
rollback R1 resolution and code-only CMC-OFF rollout are documented in
`CCI_V2_REPAIR_RELEASE_HANDOFF.md`. Preserve the historical receipts below.

Status: **DEV REPAIR VERIFIED AND COMMITTED — NOT DEPLOYED. Phase completion is not claimed.**

This report concerns the owner-supplied 1 November 2026 fast live audit. The audit
text was supplied in the conversation; its original Runtime file is inaccessible
from this DEV host. Runtime ownership/configuration observations below are the
audit's historical evidence, not new production inspection.

## Release identity and isolation

- Baseline: detached Runtime release `0decc2158df59969b38a2d7441bec2441a3adc5b`.
  Retrieved that exact object from the existing Git remote before implementation.
- Original DEV branch/HEAD: `feature/cci-signal-engine-recovery`,
  `89e4f08e20950481a4330893268aaecb9bea1ac6`. Its pre-existing untracked work was preserved.
- Repair branch: `codex/cci-v2-production-repair`.
- Isolated worktree: `C:\CandleCraftDev\.worktrees\cci-v2-production-repair`.
- Final implementation commit: **`677181fab8a6bbff316b0d0a0c551cc82d1faab1`**.
  Final report/package metadata is committed separately; the external release
  manifest at `runtime_audits/release/release_manifest.json` records the exact
  packaging HEAD and artifact hashes without a circular self-hash.
- `AGENTS.md` and `.cursor/rules/cci-core-architecture.mdc` were read from both
  the DEV checkout and the exact audited commit. Existing linked worktrees were inventoried.
- One coding environment was used for this phase. No subagents, simultaneous
  Cursor/Antigravity implementation, other-worktree edits, merge or push occurred.
- Existing Python environment: `C:\CandleCraftDev\.venv\Scripts\python.exe`, Python 3.11.9.
  The audited Runtime used Python 3.12; compatibility on that actual environment is pending.
- Current verified host: `CANDLECRAFT`; the audited Runtime host is `DESKTOP-G7M42AL`.
- The 37 implementation/test/inventory files are listed with immutable Git blob
  identities in `runtime_audits/changed_files.json`; no `.env` file is included.

## Implementation

The selective strategy now performs bounded native derivatives acquisition before
freezing its structural decision time. It retains original source/acquisition/cache
delivery facts, then evaluates those immutable observations at the resolved cutoff.
There are four prefetch workers, at most 200 symbols, a 20-second acquisition
deadline bounded by the remaining scan deadline, and the existing HTTP client has
four request slots and smaller endpoint-specific limits. Timeout/failed evidence
does not stop core analysis. Explicit historical cutoffs are never moved forward.

Funding reads of the same settled event may have different acquisition times.
Their value, event time, venue, instrument, endpoint and measurement semantics must
agree; incompatible routes, contradictory values, unsupported funding subtypes,
future observations and invalid chronology remain rejected. The causal research
admission path applies the same rule. Funding cadence comes from Binance fundingInfo
or a verified settlement-to-nextFundingTime schedule, with acquisition/source facts;
it is not inferred from a symbol or assumed universally to be eight hours.

Strategy input diagnostics separately record admission at the structural cutoff
and later supporting context. Funding/OI output values come from the same admitted
values used by the authorized numeric guards. Missing source evidence retains a
specific reason instead of an unexplained N/A.
Bounded withheld-source receipts include timestamps, source fields, exclusion
reasons and total reason counts; any truncation of individual receipts is explicit.

All three stream consumers validate literal Unicode identities against acquired
Binance USD-M metadata. Metadata must identify a TRADING, USDT-margined perpetual,
the exact base/quote/pair, and no duplicate/normalization collision. Event parsers
preserve literal identities rather than uppercasing an event into another contract.
UM/CM discriminators, malformed payload checks, exact pair checks and freshness
policies remain in force. An expired/unsupported identity is Unverified, including
on a connected liquidation stream. A quiet eligible liquidation window can still
have a verified zero event count.

Bounded service diagnostics distinguish requested/acknowledged subscriptions,
decoded events, ingestion delay, per-symbol source age, connection recovery,
assessment age, synchronization and depth-band coverage. Pending request state is
bounded to 128 and recent service history to 32 records; symbol state is pruned to
the retained universe. Existing failure/retry counters are persisted once per run.
Late acknowledgements cannot undo a newer unsubscribe. Metadata withholding does
not itself trigger reconnect churn.

Fresh supporting snapshots are captured at the final assessment boundary, before
sampling its observation time. Structural book evidence remains frozen separately
for the original decision. The five-second trade/book policy is unchanged.
A synthetic CPU profile measured 2.215 seconds in three strategy analyze calls
under profiling; selective strategy analysis now uses one worker slot per loop.
Cancellation does not release the slot while work is still executing. Queue/work
timings are retained. Legacy strategy execution timing was preserved.

Native CMC SHADOW context is attached to scanner results, including rejected
candidates, and is serialized for inspection. Its source and acquisition times are
checked at both the structural cutoff and assessment. Other venues' exchange-level
aggregates are not attributed to Binance candidates. Global context is explicitly
distinguished from mapped contract context. Empty identity manifests remain an
explicit limitation. CMC cannot alter eligibility, structure, geometry, RR or
lifecycle authority. A persisted 403 entitlement denial suppresses automatic
re-probing for that credential/endpoint context, including client restarts.

No main production schema/migration, order-execution code, transfer/withdrawal
functionality, signal-quality gate or Fibonacci rule was changed. Existing compact
scan payload encoding and epoch ownership remain authoritative.

## Original audit observations and dispositions

“FIXED AND VERIFIED” here identifies code/isolated verification unless a real
source capture is expressly named. It never means deployed verification.

| ID | Original observation | Disposition | Evidence / practical limit |
|---|---|---|---|
| A1 | One scanner and one polling listener, detached audited release | UNRESOLVED | Historical ownership is established by supplied audit. Current ownership/release/launcher arguments require Runtime access. No process was replaced. |
| A2 | Epoch founding SHA differs from deployed release SHA | VERIFIED EXPECTED BEHAVIOR | Preserve epoch `cci-v2-20261030T055135Z-2be0d3abe7f8`, founding SHA `5c72caf5b7baa4b421708d9f4550887c637c7e14`; do not rewrite the durable identity for this release. |
| A3 | Three complete scans, no timeouts/storage errors, 300-second cadence | VERIFIED EXPECTED BEHAVIOR | Audit iterations 50–52 only; new-release cadence is not yet measured in production. |
| A4 | 19.188-second session loop lag / 2.661-second synchronous block | UNRESOLVED | Measured CPU work moved off loop and bounded; Runtime lag improvement and the remaining blocking contributors require post-rollout measurements. No paging cause is claimed. |
| B1 | Effective launcher CMC_MODE=off overrides .env shadow | UNRESOLVED | Proposed launcher diff below; production activation has not been authorized or performed. |
| B2 | CMC client exists, account/endpoint access untested | FIXED AND VERIFIED | Native authenticated DEV probe succeeded on six routes, 4 reported/reserved credits; public source capture index below. This is not Runtime activation. |
| B3 | Empty identity manifest | UNRESOLVED | Four source-backed proposed exact bindings prepared. Owner approval/current Runtime credential and manifest activation remain pending. Empty-manifest collection is explicitly partial. |
| B4 | CoinPaprika ranking authority | VERIFIED EXPECTED BEHAVIOR | Retained; CMC context activation is separate from ranking selection. No ranking switch. |
| B5 | Requested 100, 66 qualified, 34 unknown identities, 54/55 exchange matches | VERIFIED EXPECTED BEHAVIOR | Fresh bounded DEV source review independently reproduced 2,000 ranked / 100 boundary / 66 qualified / 34 unclassified / 55 exchange eligible. All 34 IDs and source/consumer/freshness/withholding reasons are recorded in withheld_identity_review.json. Their genuine-asset classifications remain Unverified, not proven exclusions; no blanket admission or padding. |
| B6 | Transient XRP ambiguity; CC ambiguous | FIXED AND VERIFIED | Exact `xrp-xrp`→CMC 52→Binance XRPUSDT binding validated against public provider/native metadata and collision regression. CC remains fail-closed; no CC identity evidence establishes a safe replacement. |
| B7 | Eligible 币安人生USDT rejected by shared ASCII predicate | FIXED AND VERIFIED | Fresh Binance exchangeInfo confirms exact trading USDT perpetual; synthetic replay passes all three parsers/consumers. Natural production event coverage is still pending. |
| B8 | Liquidation connection continuity overstates unsupported-symbol coverage | FIXED AND VERIFIED | Unsupported identity returns UNVERIFIED; quiet eligible zero-window test remains verified. Receipt/window time is distinguished from per-symbol source-event time. |
| B9 | Connected/subscribed/decoded/per-symbol evidence conflated | FIXED AND VERIFIED | Bounded request/ack, decoded, source age and assessment diagnostics added; wrong-contract and collision regressions retained. |
| B10 | Reconnect/restoration history unavailable in saved scan evidence | FIXED AND VERIFIED | Existing reconnect policy retained; diagnostics persisted and restoration/late-ack tests run. A natural production reconnect remains unobserved. |
| B11 | Trade-flow snapshots expire before assessment | FIXED AND VERIFIED | Final boundary refresh regression, original cutoff preserved, five-second policy unchanged. Host-level coverage after deployment remains Unverified. |
| B12 | Seven synchronized books lack full requested band coverage | VERIFIED EXPECTED BEHAVIOR | Synchronization and complete depth-band coverage are separate fields. Finite visible depth is not comprehensive liquidity. |
| B13 | Acquired OI missing from strategy inputs/output due to late acquisition | FIXED AND VERIFIED | Bounded pre-cutoff acquisition and actual scanner regression admit funding/OI; late/incompatible data remain withheld. Production admission proportions pending. |
| B14 | Funding conflicting_provenance for settled/regular funding | FIXED AND VERIFIED | Independent settled-event receipts admitted; contradictory values/subtypes/routes and causal violations rejected in both strategy and research admission. |
| B15 | Funding cadence/category/timestamps conflict or absent | FIXED AND VERIFIED | Explicit Binance schedule evidence and provenance; current/predicted/special data cannot masquerade as settled regular funding. Missing schedules remain N/A with a reason. |
| B16 | 114 verified / 50 stale account ratios | VERIFIED EXPECTED BEHAVIOR | Stale source observations stay withheld at the existing cadence-derived freshness boundary; a new cache receipt cannot refresh source time. |
| B17 | CMC context absent from scanner and rejected candidates | FIXED AND VERIFIED | Acquisition→sidecar→scanner result injection regression; rejected technical outcome unchanged. Real Python API acquisition proven separately. Runtime injection is UNRESOLVED until approved rollout. |
| B18 | CMC OI units/funding semantics not independently established | EXTERNAL LIMITATION | Provider pair fields were returned; denomination/counting convention and settled-vs-indicative interval semantics are not invented. Global liquidations and explicitly USD exchange aggregates remain available context. |
| C1 | All 164 symbol / 328 mode observations rejected; no valid setup shown | VERIFIED EXPECTED BEHAVIOR | Audit first-failure totals reconcile. Native timeframe provenance, structural confirmation, allowed Fibonacci levels, tick tolerance and required reaction are unchanged. |
| C2 | HYPE/ETC/XRP near misses lack a valid OB/FVG or other required gates | VERIFIED EXPECTED BEHAVIOR | A display near miss is not setup or lifecycle acceptance. No gate was loosened to change these historical rejections. |
| C3 | Generic row failed_gate N/A while per-mode first failure exists | VERIFIED EXPECTED BEHAVIOR | Accepted-plan fields can be N/A for rejected candidates; per-mode detailed diagnostics remain the evidence for their actual blockers. No zero-N/A promise. |
| C4 | Tick tolerance rejects HBAR/ENA/ICP/APT with additional blockers | VERIFIED EXPECTED BEHAVIOR | Public-price quantization and Fibonacci/tick intersection tests retained. No tolerance relaxation. |
| C5 | Protected stops, TP geometry and net TP2 RR unobserved naturally | UNOBSERVED PRODUCTION EVENT | Isolated real-engine fixtures cover long/short, Fibonacci ratios, protected stops and net RR. No naturally accepted production plan has been observed for this release. |
| C6 | Two independent confirmations / successful lifecycle progression unobserved | UNOBSERVED PRODUCTION EVENT | Isolated lifecycle/observation-consumption regressions cover the required semantics; natural progression remains unobserved. |
| C7 | Spread-driven risk change unobserved | UNOBSERVED PRODUCTION EVENT | Causal structural spread is separate from fresh supporting book context; no claim of a natural live spread-driven plan. |
| D1 | Canonical route enabled while legacy live-alert switch false | VERIFIED EXPECTED BEHAVIOR | Existing canonical outbox authority remains. No public Telegram test or fabricated signal was sent. |
| D2 | Customer notice sent; no natural trading delivery receipt | UNOBSERVED PRODUCTION EVENT | The notice is not trading delivery evidence. Natural trading delivery stays UNOBSERVED. |
| D3 | TP1/TP2/TP3/SL/BE follow-ups and real duplicate suppression unobserved | UNOBSERVED PRODUCTION EVENT | Isolated canonical routing/retry/uncertain/prior-public/epoch tests cover capability; live outcomes remain UNOBSERVED. |
| D4 | Empty outbox, no V2 active published obligations | VERIFIED EXPECTED BEHAVIOR | Historical audit state only; recheck current bounded metadata during approved acceptance. No cleanup/backfill. |
| E1 | 3.88 GiB RAM, 87–90% load / 368–493 MiB available | EXTERNAL LIMITATION | Software did not add RAM. Paging attribution is Unverified. Measure actual free RAM, RSS and paging on Runtime; do not infer a memory fix from DEV results. |
| E2 | SQLite schema 27, successful writes, compact symbol_refs_v1 | VERIFIED EXPECTED BEHAVIOR | No production schema or compact-format change. Isolated persistence tests required; no live DB access/copy/maintenance occurred. |
| E3 | ~9.28 MiB/scan / ~2.61 GiB/day extrapolated DB growth, ample S: capacity | VERIFIED EXPECTED BEHAVIOR | Historical extrapolation only, not a capacity guarantee or reason for production cleanup. New context payload growth must be observed after deployment. |
| R1 | Previous-release compatibility with new explicit UNVERIFIED stream status | UNRESOLVED | Exact audited typed reader rejects this status in an isolated synthetic check. A compatible rollback reader for the verified current Runtime release is required before full code rollback can be approved; CMC OFF on the repaired reader is a configuration mitigation. |

## Tests and evidence

- `runtime_audits/native_regression.xml`: **117 passed**.
- `runtime_audits/repair_focused2.xml`: **65 passed** for the then-current repairs.
- `runtime_audits/synthetic_cpu_profile.json`: labeled synthetic DEV CPU profile;
  not Runtime latency or RSS proof.
- **Complete required-suite coverage: 4,533 unique tests have passing results; 0
  errors, 0 skips, no missing or unexpected collected tests.** Four disjoint file
  partitions executed every collected test exactly once against frozen application,
  test and configuration sources; before/after source hashes match.
- The final complete-suite invocation reported **4,532 passed / 1 failed**. The
  failure was a generated inventory-document anchor mismatch. After regenerating
  the two affected documents, the entire **34-test** validation module passed.
  The original failure is preserved; it is not described as a clean single run.
  `verification_summary.json`, `final_verified_coverage.xml`, the original
  `full_pytest.xml`, four partition receipts and `final_docs_regression.xml` make
  this distinction inspectable.
- Final focused repairs: **71 passed** (`final_focused.xml`). Final additions,
  including native preflight failure/budget handling and withheld-source receipts:
  **47 passed** (`final_additions.xml`). Dependency check: **no broken requirements**.
- Initial sandbox attempts stalled in Windows `asyncio` socketpair initialization.
  Those identified DEV test processes were stopped; isolated tests were rerun with
  working local IPC. No production process was involved.
- The first full-suite attempt was interrupted: a custom basetemp name did not
  satisfy the existing synthetic epoch fixture. The suite was restarted with the
  repository's standard `.pytest_tmp` root. Its CLI failures were not labeled passes.
- A historical regression treated different receipt times for the same settled
  event as a provenance conflict. It is updated to distinguish valid independent
  receipts from incompatible source routes, with order-independent coverage checks.
- A completed intermediate full run recorded **4,505 passed / 14 failed** before
  those expectation/anchor fixtures were corrected. Its original receipts remain
  in `full_attempt2.xml` / `full_attempt2.log`. The first custom-temp attempt was
  interrupted and is not counted as a complete suite.
- Precision-cache regression fixtures now explicitly simulate an independent
  post-cutoff metadata refresh. Pre-cutoff acquisition no longer needs the former
  redundant post-cutoff optional lookup; frozen precision must still survive a
  real later refresh, through lifecycle and isolated fake delivery.
- Final validation covered the existing integrated signal product, selective
  Fibonacci/risk rules, confirmation observation consumption, public eligibility,
  canonical outbox/recovery, TP/SL/BE routing, epoch boundaries and compact storage.
  Every test uses isolated state. No production DB or public destination is used.
- `rollback_reader_check.json`: isolated direct loading of the exact audited Git
  context/stream models rejects UNVERIFIED at the typed `status` field; the
  baseline UNAVAILABLE control passes. This exposes a rollback limitation rather
  than concealing it as a successful compatibility test. It does not inspect or
  modify any production payload.

## CMC identities, entitlement and credits

`runtime_audits/source_capture/index.json` records actual native acquisitions from
13:06:52–13:06:56 UTC on 1 November 2026, hashes of retained public responses, and
sanitized account limits. All six CMC requests were authenticated. key/info and map
reported zero credits; exchange registry, pairs, global metrics and liquidations
reported one each. Total: **4 credits**, with an **8-credit hard DEV preflight cap**.
Credential values/headers were neither displayed nor retained in these artifacts.
The capture is historical evidence, not an assertion that its market metrics are
fresh at the time this report is read.

Account response: 15,000 credits/month, 50 requests/minute. No account plan name or
daily provider quota was returned. Owner authorized **500/day and 15,000/month**.

| Binance contract | CMC ID | CMC slug | CMC exchange ID | CMC market ID | Approval |
|---|---:|---|---:|---:|---|
| BTCUSDT | 1 | bitcoin | 270 | 47150 | Proposed, not owner approved |
| ETHUSDT | 1027 | ethereum | 270 | 47205 | Proposed, not owner approved |
| SOLUSDT | 5426 | solana | 270 | 67944 | Proposed, not owner approved |
| XRPUSDT | 52 | xrp | 270 | 47207 | Proposed, not owner approved |

Reviewable manifest: `config/cmc/identity.runtime-repair.proposed.json`. Bindings
expire seven days after the actual captured evidence cutoff. Owner approval must
use its real approval/review time and reference; do not backdate it. Before rollout,
reacquire current identity metadata if necessary. The quote asset is CMC ID 825 / USDT.
The manifest deliberately does not assert unverified derivative semantics.

`runtime_audits/universe_capture/index.json`, `universe_review.json` and
`withheld_identity_review.json` contain a separate bounded current DEV qualification
sample acquired at 13:50:30 UTC. It performs no scanner analysis or stream
subscription and does not change Runtime membership. The same 66/34 split is
reproduced; CC remains ambiguous and XRP is explicitly disambiguated. All 34
withheld IDs lack a reviewed positive genuine-asset classification under the
existing policy. Ticker rank/name/price alone do not establish that classification;
the review does not declare them non-genuine or silently admit them.

Proposed `RUNTIME_SHADOW` profile:

| Family | Cadence | Maximum credits/day before retries |
|---|---:|---:|
| Global market metrics | 300 s | 288 |
| Global liquidations | 900 s | 96 |
| Binance derivative pairs, one page of <=250 | 1800 s | 48 |
| Exchange registry, <=100 | 86400 s | 1 |
| ID map, <=1000, exact reviewed IDs checked | 86400 s | 0 reported/estimated |
| Total | | **433** |

Thirty-day projection: **12,990**, leaving **2,010 monthly / 67 daily** for retries.
The monthly hard cap also applies in a 31-day month. Collection cadence does not
extend source freshness: pair/liquidation metrics can be stale between acquisitions.
No claim of continuous fresh coverage for every family is made. Bounded pages do
not establish complete provider market coverage. Unsupported endpoints must be
excluded or expose a persisted limitation; do not buy credits or upgrade plans.

Primary semantic references reviewed:
[Binance fundingInfo and market data](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data),
[CMC derivative contracts](https://coinmarketcap.com/api/documentation/pro-api-reference/derivatives/),
[CMC global metrics](https://coinmarketcap.com/api/documentation/pro-api-reference/global-metrics/).

## Effective configuration proposal — not applied

| Setting | Audited effective value | Proposed scanner value |
|---|---|---|
| CMC_MODE | launcher override `off` | `shadow`, removing/replacing effective OFF override |
| MARKET_CAP_PROVIDER | coinpaprika | coinpaprika |
| CMC_COLLECTION_PROFILE | unverified | RUNTIME_SHADOW |
| CMC_DAILY_CREDITS / CMC_MONTHLY_CREDITS | Runtime values unverified | 500 / 15000 |
| CMC_IDENTITY_MANIFEST | identity.empty.json in older .env | Owner-approved version of the proposed four-contract manifest |
| CMC_ACTIVATION_MANIFEST | unverified | config/cmc/activations.disabled.json |
| CMC_SIDECAR_PATH | no active acquisition | S:\CCI_RUNTIME_V2\cmc_shadow.sqlite, distinct from candle_craft.db |
| Safety/epoch/universe/risk/Telegram arguments | supplied audit baseline | Preserve verified current values exactly |

Runtime credential availability is not established by the DEV key check. Verify
presence through the existing secret mechanism, without exporting/displaying it.
Constructor arguments > process environment > `.env` > defaults under Settings;
the actual launcher's explicit environment assignment must be included in review.

## Rollout, rollback and bounded acceptance

**No production deployment, configuration activation, process replacement or schema
change has been approved or performed. No release approval is requested until the
package is complete.** Current Runtime connectivity and launcher receipts are the
remaining prerequisites for an exact operational command package.

The Runtime path `C:\Users\aspir\Desktop\Candle Craft Inteligence` is absent on this
host and S: is not mounted. The owner has been asked for an approved connection or
accessible evidence path. Do not guess current PIDs, launcher arguments, task names,
epoch bindings or graceful process controls from historical values.

The release package must pin a tested feature commit/bundle and its hash, the exact
current Runtime release, approved identity manifest, launcher diff, credit budget,
original scanner/listener argument arrays and graceful stop/start controls. Preserve
all paths in S:\CCI_RUNTIME_V2 and the entire immutable epoch tuple. Do not merge or
push to main as an implicit deployment step.

After separate owner approval: verify clean tracked Runtime state and inventoried
consumers/auto-restarts; stop the listener and gracefully finish/stop the single
scanner; verify no competing owner; install only the pinned release and approved
config; start exactly one scanner and then one polling listener using the preserved
argument arrays. No synthetic/public signal, live DB migration, cleanup or copying.

See `CCI_V2_CONTROLLED_ROLLOUT.md` for the guarded commands and rollback limits.
CMC can be disabled by restoring its prior launcher settings while retaining the
repaired reader. Full code rollback must use a compatibility-verified reader for
the actual previous Runtime release: the unmodified audited typed stream reader
rejects the new explicit UNVERIFIED status. Its compatible rollback release must
be prepared and reviewed once the current Runtime base is known, before rollout
approval. Preserve the current DB/epoch/outbox and original single-owner commands.
Never restore a stale outbox, downgrade schema, delete journals, coerce evidence,
reset epochs or run maintenance for this phase. CMC's own bounded sidecar is
separate from the schema-27 production database.

Observe at most three naturally completed scans in at most 20 minutes. Record exact
deployed release/effective settings, scanner/poller ownership, native CMC batch and
candidate evidence references, admitted funding/OI and withholding reasons, Unicode
coverage, per-source/per-symbol freshness, acknowledgements/recovery, cadence, RSS,
actual persistence and canonical routing. Natural signals/outcomes are UNOBSERVED
if none occur. A successful scan receipt alone is not signal-chain acceptance.

Latest new-release production acceptance: **UNVERIFIED — not deployed / Runtime
connection unavailable**. Natural trading delivery: **UNOBSERVED**. Natural outcome
delivery: **UNOBSERVED**. Remaining known in-scope verification work and identity
activation are explicitly open; this report does not declare phase completion.

## Local release artifacts

`runtime_audits/release/` contains the incremental Git bundle, exact implementation
source ZIP, evidence ZIP and external `release_manifest.json` with SHA-256 hashes.
The evidence ZIP retains the original failed/interrupted receipts, final passing
coverage, public source captures, reviewed withholding list, configuration proposal
and guarded rollout/rollback packet. It contains no live database, credential file
or operational secret. The incremental bundle requires the audited baseline object;
its packaging HEAD adds evidence only. Deploy only the explicitly pinned tested
implementation, after the missing Runtime inputs and rollback compatibility are
resolved and the owner approves the final controlled rollout.
