# CCI V2 combined rollout prerequisite repair

Development only, from `0086e9e508e9b4b75f1a6c0c0c8210be5260c326` on
`codex/cci-v2-combined-prerequisites`. This is not a production activation packet.
No Runtime filesystem or database was accessed; no service, Telegram polling,
external authenticated acquisition, trading or production configuration was run.
The existing main checkout and other worktrees were preserved.

## Repair scope and root causes

The historical launcher accepted only 36 overrides and CMC OFF. The repaired
controller accepts a complete hash-bound reviewed source environment, including
future reviewed profiles with more than 36 overrides, and derives only eight
explicit feature settings. Historical 36-override profiles retain their original
validation. The allowlist covers Precision/legacy presentation, CMC OFF/SHADOW,
dedicated sidecar, identity file, CUSTOM profile/file and credit caps. Decision
mode, ranking-provider changes, arbitrary Python/environment settings, altered
arguments/epoch/generation, changed content/path hashes and unsupported releases
are rejected. Unchanged settings are copied exactly.

A new staged contract must specify exact `release_trees`, source profile hashes,
complete source environments and per-role `activation_reviews` packet hashes.
The activation packet binds role, SHA/tree, full epoch, source hash, contract
digest, exact before/after changes and content-hashed evidence files. Its contract
digest excludes only `activation_reviews` to avoid a hash cycle. Separate owner
approval binds the full contract, proposal, packet, role, identity and differences
and expires within at most 24 hours. All operational CLI starts require this
fresh approval, including CMC-off recovery. There is no owner approval in this PR.
The existing historical release contract has deliberately not been activated or
replaced. PowerShell wrappers accept the new contract/packet arguments and retain
their DEV refusal. Preparation never starts a process or opens SQLite.

The Unicode failure came from constructing one raw-symbol map filter. Collection
now uses bounded unfiltered ID-map pagination sorted by `cmc_rank`, requesting
only the supplemental `is_active` field. The CUSTOM bound is four pages of at
most 5,000 map rows, with the existing 1 MiB body cap. No prefix stripping or
symbol equivalence is used. Exact numeric IDs, slugs, native Binance perpetual
metadata and separate owner authorization still own the join. A response/body
failure or missing/ambiguous requested numeric ID withholds contract enrichment.
When every manifest ID is present, exact requested-ID coverage can be complete
inside an explicitly partial global catalog. The batch records these separately;
global pagination is never relabeled complete. This supports the eligible
universe where complete source evidence fits the reviewed bounds; it cannot
guarantee every future provider listing fits them.

Pair acquisition uses the existing four-page capability with 250 rows per page.
It now rejects duplicate/invalid IDs across pages, repeated pages, contradictory
short-page totals, changed declared totals and inconsistent exchange identity.
The entire incomplete/ambiguous pair family is withheld from contract enrichment;
raw evidence remains research evidence. No first-page result is called complete.
The provider's original pair IDs, exclusion fields, timestamps and raw provenance
remain intact. Both the [official ID-map reference](https://coinmarketcap.com/api/documentation/pro-api-reference/cryptocurrency)
and [derivatives reference](https://coinmarketcap.com/api/documentation/pro-api-reference/derivatives/)
were checked for these bounded query contracts; no account API was called.

The old restart record duplicated four pages into a single oversized family.
The new cache fragments each immutable page under the unchanged record cap and
publishes a last-written family manifest. Recovery checks each fragment, index,
hash, original acquisition timestamp, endpoint, page size and family size. Missing,
evicted, future, retained-too-long, duplicate or corrupt fragments cause a cache
miss. Existing byte/record/retention limits, the payload half-budget and SQLite
resource policy remain unchanged. Legacy small family records remain readable.
The resource probe also exposed a timezone-dependent Pydantic representation in
observation hashing; canonical instrument fields now keep identical observations
stable after JSON recovery. Decimal values are reconstructed from the original
hash-checked provider response rather than from a lossy JSON representation of
normalized fields. This changes CMC research IDs only, not strategy logic.

Collector construction previously returned None and could leave no result-level
context. Every SHADOW analyzed result now has explicit research-only status,
including error/rejected results before callbacks and persistence. Acquisition
pending, collector failure, acquisition failure, exhausted budget, no approved
mapping, global-only, verified contract context and stale/unavailable evidence are
distinct fields. Available global and unavailable contract status can coexist.
The original strategy cutoff is retained; later evidence never moves backwards
onto it. Research status is not a trading confirmation and has no promotion,
veto, priority or public admission authority.

## Identity review and actual capability

The deterministic fixture proves acquisition/review of 55 exact candidate IDs
and slugs, including the literal Unicode native contract, without approving any
identity. The collector probe exercises 55 synthetic approved contract bindings
against a shared 820/1,000-pair response with independent synthetic metadata.
These fixtures are explicitly fictional and are not market evidence.

[Owner review inventory](evidence/combined_prerequisites/owner-review.json) contains
55 symbols from the committed 2026-11-01 universe evidence and incorporates the
owner-supplied 2026-11-02 NO-GO narrative as research input. The exact expanded
55-row Runtime capture/manifest was not supplied on DEV. Roster equality with the
latest scan is therefore Unverified; missing exact ID/slug values are N/A. The
four historical BTC/ETH/SOL/XRP candidates retain their original IDs/provenance,
and all entries remain unapproved. Nothing was converted into a live manifest.

All 55 still require fresh exact binding review and owner approval. Preserve these
additional withholds: 1000LUNCUSDT, 1000PEPEUSDT and 1000SHIBUSDT need independent
multiplier qualification and are explicitly withheld by instrument admission even
if an identity manifest claims owner approval; non-unit binding multipliers are
also withheld. ARBUSDT remains excluded (staging reports market
1238049, `exclusions=["volume"]`); 币安人生USDT needs exact native identity review.
OI units/counting, funding interval/semantics and index currency remain Unverified
for every symbol until separately established. Identity approval cannot establish
those semantics. No gate or universe membership was relaxed.

## Credit economics and freshness

The reviewed workload proposal is
`config/cmc/combined-shadow.collection.json`: global metrics and global
liquidations every 1,800 s; four shared pair pages every 14,400 s; daily map and
registry. Concurrency 1, rate cap 20/minute, cache 16, body 1 MiB, observations
1,000, retention 30 days. Sidecar limits stay 64 MiB and 20,000 records with a
2 MiB individual cap and a payload half-budget.

| Planning case | Daily credits | 31 days |
|---|---:|---:|
| Normal scheduled workload | 121 | 3,751 |
| Three attempts per scheduled request | 363 | 11,253 |
| Effective application hard caps | 388 | 11,988 |
| Remaining headroom at three attempts | 25 | 735 |

These are reserved-cost planning figures, not current balances or a worst-case
outage guarantee. Failed-family backoff can allow retries every 300 s, so extended
outages may exhaust caps earlier. Reservations remain charged across failures,
cancellation and restart. Cache replay does not create provider requests or
reconcile an old receipt again. Provider costs exceeding estimates stay charged
and block the next reservation. UTC day/month buckets and reconciliation retain
the original reservation time. In a 31-day month, 388/day totals 12,028; the
11,988 monthly cap binds independently and is never bypassed.

The staging account entitlement was 15,000/month, used 4 before validation and 16
after validation, leaving 14,984 at that observation. The separate validation
ledger's 12 provider-reported credits explain that difference once. The reduced
388/11,988 allowances leave that headroom within the prior 400/12,000 application
allowances; they are not an additional account debit. Current account balance,
outside usage and fresh Runtime reservation reconciliation are N/A here.
Retain the existing `research_runs/cmc_runtime.sqlite` ledger and October
reservations. Changing/creating a sidecar cannot establish a new credit allowance;
the exact future packet must reconcile ledgers before approval.

Five-minute freshness gates stay unchanged. At four-hour pair cadence, ideal
contract time coverage is at most 300/14,400 = 2.083%, before publication lag,
missing mappings, outages or scan alignment. Global cadence gives at most 16.667%
at zero lag, or 13.333% with a hypothetical 60-second lag. These are conditional
timing bounds, not measured decision coverage. Continuously polling four pages
every five minutes alone costs 1,152/day. This budget does not provide it.
Measure verified global/contract results divided by analyzed results, mapped
symbols divided by eligible symbols, complete batches divided by attempted
batches, source-age p50/p95/max, latency p95, unavailable/failure/budget counts,
and actual reserved versus provider-reported usage over natural scan cutoffs.

A future bounded authenticated validation plan requires separate owner approval:
at most one key-info, four map pages, one registry, four pair pages, one global
and one liquidation request per attempt (12 requests); at most three attempts,
36 HTTP requests and a conservative 24-credit reservation envelope using current
endpoint estimates. Recheck entitlement and existing reservations first, retain
all receipts, withhold incomplete/stale data and reconcile separate validation
charges into the effective allowance. This plan is documented, not authorized
or executed by this development phase.

## Memory evidence and Runtime admission

Fresh-process Windows DEV receipts:
[820 pairs](evidence/combined_prerequisites/synthetic-memory-820.json),
[1,000 pairs](evidence/combined_prerequisites/synthetic-memory-1000.json).

| Synthetic workload | Peak process RSS | Peak traced Python allocation | SQLite file |
|---|---:|---:|---:|
| 820 pairs, 55 instruments, four pages | 88.96 MiB | 18.52 MiB | 5.95 MiB |
| 1,000 pairs, 55 instruments, four pages | 98.61 MiB | 23.95 MiB | 9.01 MiB |

The largest live traced block in the larger workload was 1,038,142 bytes. Every
HTTP body stayed under 1 MiB and every evidence record under 2 MiB; both restarts
made zero additional mock HTTP requests. Python allocator tracing excludes some
native allocations; OS peak RSS includes interpreter/import overhead. A largest
live block is not a proof about every short-lived allocation. These are bounded
observations on DEV, not an enforced RSS ceiling or Runtime incremental estimate.

The staging host report had about 3.88 GiB RAM and 570.7 MiB available; historical
scanner/listener peaks were 622.6/73.6 MiB. Those role peaks alone exceed the
reported available RAM, before CMC or the owner-reviewed 512 MiB retained floor.
The prior 128 MiB CMC allowance was unmeasured and is not used as proof here.
Runtime CMC increment, simultaneous process peaks, commit headroom and paging
pressure remain unmeasured. Resource readiness is BLOCKED on that evidence.

Before an approved prestart, with role ownership proven quiescent, obtain fresh
(within five minutes) total/available physical RAM, committed bytes/commit limit,
actual process private bytes/current and peak RSS, pagefile configuration/usage,
and paging counters. Physical available RAM is not commit headroom; a larger
pagefile does not create physical RAM. Require measured scanner + listener + CMC
incremental peaks + retained floor below available physical RAM, adequate commit
headroom, and an approved simultaneous workload proving stable headroom, scan
deadlines, SQLite bounds and paging behavior. `assess_host` withholds verification
when any measurement or simultaneous-peak evidence is missing. Stop before launch
on a shortfall; do not substitute DEV synthetic RSS for Runtime measurements.

Recommended remediation is more physical RAM or a larger isolated Runtime host,
followed by remeasurement. Safe follow-up reductions include fewer transient
copies of raw/data/text, streaming fragment recovery and indexed retention/cache
reads. Retain source evidence, timestamps and quality gates. No application was
closed and no Windows memory/pagefile setting was changed.

## Rollback and Telegram integrity

The exact `1d163143` transport is tested with a reserved rich payload: it chooses
plain `sendMessage`. The exact `0086e9e` transport chooses `sendRichMessage` and
preserves the HTML. Thus `1d163143` is not an unconditional rollback target.
The retained-intent assessor checks payload hashes, known formats, complete
inventory and all pending/retryable/in-flight/uncertain/sent states without
opening a database, mutating messages or replaying them. Operational code
rollback now requires a separately reviewed exact rich-compatible transport/state
proof; the historical rollback manifest does not qualify.

Use separate controlled, owner-approved fallbacks:

1. Set CMC OFF while retaining this compatible code, sidecar, generation, offsets
   and canonical outbox. Regenerate exact reviewed differences and fresh approval.
2. Select legacy presentation for newly reserved messages while retaining rich
   transport for saved intents. Existing rich reservations stay byte-for-byte
   unchanged. Their hashes, canonical identity and retry ownership remain intact.
3. Use code rollback only after exact target/tree transport and retained-state
   compatibility proof, complete quiescent inventory, canonical dedup/offset
   checks and fresh approval. `0086e9e` is the minimum existing combined baseline
   with offline rich transport evidence, not an automatically authorized target.
   Without a current compatible recovery proof, remain stopped. Never restore an
   old database, offset or sidecar; never rewrite or replay saved public messages.

Synthetic state tests preserve rich payloads through pending/retryable delivery,
block unknown-outcome/in-flight replay, and prevent duplicate canonical delivery.
Existing Precision tests prove persistence and recovery after a legacy setting
change. Existing exact-source roundtrip tests cover typed state and listener
offset continuity; they cover legacy transport only and do not authorize rich
rollback. The Runtime narrative's latest-200 empty outbox sample is not a complete
drain or inventory proof. Live Telegram appearance and delivery remain Unverified.

## Verification, decision and remaining work

The implementation plan was written before code edits. Baseline regression:
84 passed on the exact baseline archive, Windows Python 3.12.10. Focused and full
candidate receipts are recorded with the PR and final handoff; exact-head CI is
authoritative. One existing canonical-equality test was extended to assert the
new required SHADOW unavailable status, while still comparing every canonical
field across modes. Existing profile fixtures were strengthened to supply fresh
exact approvals and synthetic rich-compatibility proof for rollback; the direct
launch API has the same strict approval requirement as the CLI. No quality,
transport, epoch or argument assertion was removed.
No strategy geometry, RR threshold, signal gate, lifecycle rule or schema changed.

The initial sandbox test attempts blocked in Windows asyncio local socketpair
creation before provider work; mocked regressions were rerun with that local
execution permission. An initial focused roundtrip invocation used a temporary
directory name rejected by its existing safety assertion; it was corrected to a
child of `.pytest_tmp`. The guard was preserved.

Final pre-PR focused regression: **424 passed**. The local full Windows run
finished with **4,735 passed, two failed**, and one existing Starlette warning.
The failures were stale source inventory line references and an original
assertion requiring the identical frozen snapshot after collector timeout.
Source anchors and their generated inventory artifacts were updated; failure
status now uses a separate research view without replacing the acquired snapshot.
The unchanged original snapshot/deadline assertions and affected CMC/inventory
suites then passed: **87 passed**. Compileall, pip check and git diff --check pass.
Full exact-candidate Windows/Linux CI must pass before the development repair
verdict is final; the PR checks and final owner handoff record those results.
One earlier concurrent focused run hit the existing bounded policy-persistence
timeout; the serial focused run passed without any deadline change. Interrupted
development test attempts are not counted as successful full runs.

Required Runtime prerequisites remain: fresh 55-row source evidence and exact
identity approvals, independent metric semantic verification, measured host
capacity/remediation, current provider/ledger reconciliation, complete outbox and
offset/owner continuity checks, and a new exact release/tree/CI/profile/approval
staging packet. Natural Binance renewal and public Telegram acceptance windows
require separate rollout approval. This PR is not production-ready authorization.

Expected development decision after exact-head checks: DEVELOPMENT REPAIR PASS;
CMC ACQUISITION PARTIAL (fixture capability verified; no fresh authenticated DEV
validation); CMC IDENTITY UNAPPROVED; RELEASE GUARDS PASS; ROLLBACK SAFETY PASS
for fail-closed controls, with Runtime code rollback still unapproved; RUNTIME
RESOURCE READINESS BLOCKED.
