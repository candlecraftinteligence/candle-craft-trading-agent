# CMC V1 shadow recovery operations

## Boundaries and timestamp ownership

`ScannerRunner.run` resolves `ScannerRunConfig.decision_timestamp` from its clock
before provider acquisition and candle analysis. An explicit historical cutoff
is preserved. Watch orchestration formerly combined the completed symbols with
the unresolved iteration template, losing that cutoff. Both watch and single
scan aggregation now copy the resolved cutoff from the runner's returned config.
The shared watch template remains unresolved so each new run gets its own cutoff.
Partial checkpoints without a resolved producer timestamp remain unavailable;
CMC never reconstructs a cutoff from file time or the current wall clock.

The later public-selection boundary records `cmc_selection_at` separately from
`cci_original_decision_at` and `evidence_knowledge_cutoff`. Source, acquisition,
identity-review and independent metadata timestamps must be causal at analysis.
Market evidence must also remain fresh at selection. Evidence acquired during a
long scan cannot be attributed retrospectively to its original analysis. Historical
replay uses the same rule. Resumed payloads need their original per-symbol cutoff;
without one CMC evaluation remains unavailable. Reprocessing changes neither the
analysis timestamp nor setup/plan identity. Sidecar policy batches are observations
of a selection boundary, not revisions of the economic plan.
An earlier resumed analysis also cannot inherit the later run's CCI regime context;
that context is marked Unverified and regime compatibility is omitted.

The existing lifecycle, Runtime epoch and Telegram ledgers retain authority.
`policy_batch`, `selection_intent` and `selection_application` record different
stages. The last records observed send/block status, never fills or net outcomes.
The added leverage counters measure eligible exact-rank ties and differing
hypothetical adjustments. They do not imply a changed public delivery.

## Identity review and expansion

`config/cmc/identity.empty.json` remains the disabled starting point.
`config/cmc/identity.proposed.json` is an offline engineering proposal backed by
hash-checked authenticated captures in `tests/fixtures/cmc_authenticated`.
Its BTC/ETH/SOL numeric asset IDs, slugs, Binance USDT contract symbols, CMC market
IDs and exchange ID are explicit. No owner approval is present. The old
`bindings.reviewed.json` is validation-only historical engineering evidence and
must never be substituted for production authorization.

Historical evidence status `EVIDENCE_VERIFIED` is separate from authorization:
`IDENTITY_REVIEW_PENDING`, `OWNER_APPROVED`, `EXPIRED`, or `UNVERIFIED`.
`approved=false` and `approved_at=null` in proposals are intentional. To approve
one binding, the owner must review independent current CMC ID-map, exchange
registry, market-pair and Binance exchangeInfo captures, retain their provenance
references, exact IDs and acquisition/review times, specify an expiry, and supply
`owner_approval_ref`, `approved_at` and `approved=true`. The chronological contract
is acquisition <= review <= approval <= decision < expiry. Production collection
always requires this contract. Missing exact IDs or approval remains unsupported.

The historical `match_instrument` default is retained for offline engineering
capture analysis. The production collector explicitly sets
`require_owner_approval=True`. A historical `approved` boolean alone cannot enable
production mapping. Duplicate numeric IDs, bases, contract symbols or market IDs
are rejected by the manifest model, including programmatic construction. Numeric
IDs and slugs resolve ticker collisions; a ticker filter is only a bounded fetch,
never the authority for a join. Wrong market, venue, quote, margin, contract type,
trading status, outlier/exclusion metadata or non-unit multipliers fail closed.
Independent metadata, ID map and exchange registry expire after 24 hours.

Expand gradually by adding explicitly reviewed assets to an equivalent captured
review set, then generating a new proposed manifest with `propose_identities`.
That workflow does not enumerate the futures universe or infer IDs from tickers.
Review pending and expired assets stay unsupported. A verified identity observation
has a separate 24-hour metadata validity; it never refreshes price, OI, funding,
basis or five-minute market evidence.

## Collection configuration

The existing lazy per-event-loop service remains the only worker. Configuration
changes require a process restart. OFF creates no CMC service or files. There are
no per-symbol or per-candidate REST requests. All selection uses one frozen snapshot.

| Profile | Fast families | Slower families |
| --- | --- | --- |
| FULL_RESEARCH | Existing families at `CMC_POLL_SECONDS` | Existing cache TTLs still apply; optional category/external-market IDs require manifests |
| DECISION_FOCUSED | Global metrics and global liquidations every 600 s | Listings/altcoin index hourly; ID map/exchange registry/exact pairs daily |
| LOW_CREDIT | Global metrics and global liquidations every 900 s | Listings/altcoin index every 6 h; identity families daily |
| CUSTOM | Explicit typed family list | Owner-selected family schedules, page caps and resource bounds |

`CMC_COLLECTION_PROFILE_PATH` is required for CUSTOM; the file is limited to 32 KiB.
The typed `CollectionProfile` schema in `app/cmc/profiles.py` defines every field.
For example:

```json
{"name":"CUSTOM","families":[
  {"endpoint":"/v1/global-metrics/quotes/latest","seconds":900},
  {"endpoint":"/v5/derivatives/liquidations/quotes/latest","seconds":900}
],"concurrency":2,"requests_per_minute":45,"cache_entries":32,
"max_body_bytes":1048576,"max_observations":2500,"retention_days":30}
```

Profiles cap pagination, request concurrency, requests/minute, response bytes,
cache entries, normalized observations and retention. Existing sidecar byte and
record caps still apply. Dynamic category/external-venue requests remain bounded
by the identity manifest. The scheduler checks every 60 seconds; family cadences
are minimum intervals, rounded by worker timing. Slow provider calls, polling
drift, partial pages and source publication lag reduce effective coverage.
If selection occurs more than 300 seconds after analysis, evidence known at analysis
cannot remain fresh for the five-minute global rules. More polling cannot repair
that latency gap. Measure actual analysis-to-selection delays before rollout; the
separate event-loop isolation phase retains ownership of scanner latency changes.

Completed families are cached with original acquisitions and restored from the
sidecar across restarts. Client cache and inflight coalescing remain shared. Failed
families retry no faster than every min(cadence, 300) seconds; the client additionally
has bounded retries, 429 handling, a circuit breaker and a local request-rate cap.
Outages retain prior evidence with its original timestamps, so it becomes stale.
Partial pages and oversized restart-cache payloads remain explicit coverage errors.
A large family that cannot fit one sidecar record is not restart cached; it is
reacquired subject to the persisted budget. Provider/account use by other processes
cannot be rate-limited by this process.

## Credits and forecasts

Default application caps remain 100/day and 3,000/month. Captures dated 2026-10-26
record 15,000/month and 50 requests/minute for the account. These historical facts
are not a current entitlement check. The account-wide allowance and local application
budget are separate. No credential was read or new authenticated request made here.

Each attempt reserves estimated endpoint/page credits before HTTP. Actual valid
provider receipts reconcile both higher and lower costs to the reservation's UTC
day/month, even if the response arrives after midnight. Missing receipts keep the
estimate; failed/cancelled attempts retain conservative reservations. Higher actual
charges may exceed a local cap for the completed request; later reservations fail
closed. Accounting survives restarts independently of evidence retention.

`credit_summary` distinguishes PROVIDER_REPORTED_CREDITS, LOCAL_RESERVED_CREDITS,
FAILED_ATTEMPT_RESERVATIONS, UNKNOWN_USAGE and ACCOUNT_USAGE_OUTSIDE_CCI. The last
remains N/A without an account-wide receipt. Legacy totals remain charged; they
are not evidence of provider-confirmed usage. Zero-reservation routes such as the
ID map can still be requested at an exhausted credit cap, under the request-rate
limit. A zero estimate is not a promise that future provider costs stay zero.

`docs/research/cmc_recovery_budget.json` compares 24 hours and 30/31 days, upper
pagination bounds, original endpoint TTLs, cost changes, outages, exhausted budgets
and selection delay. Baseline assumes no optional category/external-venue IDs,
uniformly distributed decisions, 60-second source lag, no scan delay, and estimated
endpoint costs. Actual reported costs and observed source ages must replace these
assumptions in monitoring. The model deliberately does not widen freshness gates.

The decision-focused proposal uses 339 estimated credits/day, 10,170/30 days and
10,509/31 days, leaving 4,491 of a 15,000 allowance before outside usage. Its two
global rule inputs have conditional joint freshness coverage of 40%, falling to
30% with a one-minute selection delay and zero for already-stale provider data.
Two paid sources every five minutes alone require 17,856 credits in 31 days at
one credit each, before other families/retries. Continuous freshness is not feasible
under that assumption. Global dominance, ranking and altcoin context are available
when fresh; slower research inputs remain N/A or Unverified when unsuitable.

## Proposed Runtime changes — owner rollout still required

```dotenv
CMC_MODE=shadow
CMC_COLLECTION_PROFILE=DECISION_FOCUSED
CMC_POLL_SECONDS=300
CMC_DAILY_CREDITS=400
CMC_MONTHLY_CREDITS=12000
CMC_IDENTITY_MANIFEST=config/cmc/identity.proposed.json
CMC_ACTIVATION_MANIFEST=config/cmc/activations.disabled.json
CMC_STORAGE_MAX_BYTES=67108864
CMC_STORAGE_MAX_RECORDS=20000
CMC_RETENTION_DAYS=30
```

These are a review proposal, not applied configuration. The proposed identity file
still enables no mappings until reviewed and approved individually. Alternatively
keep the empty identity manifest for global-only SHADOW. A 12,000 application cap
reserves 3,000 account credits as nominal headroom for retries, probes, other usage
and recovery. Verify current remaining monthly quota and unrelated usage before
raising caps. Keep the existing sidecar path; never point it at the Runtime DB.
The named profile owns cadence; `CMC_POLL_SECONDS` applies to FULL_RESEARCH only.

## Policies, monitoring, resources and rollback

`adverse_regime/1` and `liquidation_stress/1` can consume verified fresh global
metrics/liquidations without any instrument binding. They remain hypotheses.
`data_divergence/1`, `consistent_context/1` and `sector_support/1` remain blocked
by missing equivalent units, funding/OI/index semantics, source timing or sector
membership. Identity approval does not verify those semantics.

Monitor collection errors, retained source age, per-family acquisitions, partial
pages, reservation/receipt deltas, denied requests, policy-batch counts, verified
features, triggered hypotheses, exact-rank tie counts, selection intents and
application records separately. A collector success is not a candidate evaluation;
a trigger is not enforcement; selection intent is not a send; a send is not a fill.
Export a small prospective cohort before sidecar eviction. Retention is a maximum,
not a guarantee: the 64 MiB/20,000-record limits can evict earlier. Missing raw
provenance after eviction disqualifies unsupported economic claims.

The default client holds 32 response-cache entries, at most two concurrent requests
and 1 MiB per response. Profile/manifest page limits bound additional family caches;
normalization is capped at 2,500 observations. Selection caps 200 candidates and
5,000 observations and reuses causal/feature calculations within a boundary.
Policy records are capped at 2 MiB, writes have a 250 ms selection timeout and the
sidecar uses a 100 ms SQLite busy timeout. Timed-out thread writes may finish later;
their authority has already been discarded. Benchmark data in the recovery evidence
includes a 100-candidate, 2,413-observation local evaluation and sidecar write.
It does not certify total RSS or latency on the separate 4 GB Runtime PC.

Rollback: set CMC_MODE=off and restart the owner-controlled Runtime service using
the established rollout procedure. Preserve the sidecar as research evidence.
No migration of the canonical Runtime database is introduced. An older release
would not honor the new approval/expiry schema, so use the empty identity manifest
when rolling code back. Never restart or deploy Runtime from this DEV phase.

Scanner V2 may reuse the typed evidence, strict identity review, family scheduler,
accounting, pure feature functions and immutable policy/selection records. No V2
pipeline, agentic technical-analysis call, live order, transfer or withdrawal is added.
