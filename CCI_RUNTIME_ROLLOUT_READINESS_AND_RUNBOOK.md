# CCI P0B3B2B0A Runtime rollout readiness and runbook

Assessment date: 2026-10-28 (Europe/Bratislava). DEV source audit only.
Verified base: `a5ffec2e7446ed6ec274fa8fb6482b4bcb9c24ce` (fresh origin/main, PR #159).
Candidate branch: `codex/p0b3b2b0a-atomic-outbox-safety`;
[PR #160](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/160).
Implementation commit: `f3de472e62d54e93826f2fd87af4acf2c44d2aac`.
The immutable candidate SHA, PR and exact-head CI are recorded in the companion
[implementation handoff](CCI_P0B3B2B0A_IMPLEMENTATION_HANDOFF.md) and PR release record.
No moving branch name is an executable release identity.

## Decision and limits

**Runtime rollout: NO-GO today. New Telegram production integration: NO-GO.**
This is a complete assessment with unmet operational gates, not deployment approval.
No Runtime host, file, process, secret, database or configuration was inspected.
Reported database size (~150 GB), RAM (4 GB), Runtime path, running SHA, schema,
free space, active epoch, listener count and recoverability are **Unverified**.
Current measurements and actual Runtime compatibility evidence are **N/A**.

The repository reserves `scan_runs\main_live_runtime.sqlite` for Runtime. The
older checkpoint document reports `S:\CandleCraftRuntime\scan_runs\main_live_runtime.sqlite`
and code under `C:\Users\aspir\Desktop\Candle Craft Inteligence`; both absolute
paths remain **Unverified** here and were not accessed. The code's default
`scan_runs\candle_craft.db` is not evidence of the active Runtime database. Require
independent scanner/listener launch and physical-file/volume identity evidence;
do not silently substitute a default or create a missing database.

No additional source fix is established as a prerequisite to deploying the
already integrated P0A/P0B1/P0B2 changes with the existing delivery implementation.
Their worker ownership is supported by source and synthetic regression evidence.
This is a source-scoped conclusion, not a claim about the unknown deployed
baseline or the entire accumulated delta from it. A schema/path/reader mismatch,
unproved recovery resources, or an undrainable worker blocks that rollout.
Resolve those exact prerequisites; do not expand this phase to migrate or repair.

The B0A terminalizer is called by the existing delivery implementation and will
be active when that implementation is used. B1/B2A remain dormant. Their remaining
outer-owner extraction, hot authority/destination drift, postclaim abort,
completion capacity and reconciliation decisions block **future integration**;
they do not activate themselves merely because their modules are deployed.
Existing raw sender/direct reconciliation risks still apply to the old path.
If the actual Runtime baseline requires stronger delivery guarantees than that
path supplies, a coordinated release preserving it is also NO-GO pending a
separate delivery phase. Tests passing cannot waive this decision.

## Source-backed inventory

| Phase | Production wiring at this candidate | Evidence and limits |
| --- | --- | --- |
| P0A / PR #153 | Active in `_run_watch_mode`: `SerializedDiskWorker` handles watch state, manifest/nightly history and watch output | [scanner](scripts/run_scan.py), [disk worker](app/runtime/disk_isolation.py), [handoff](docs/operations/CCI_P0A_EVENT_LOOP_ISOLATION_HANDOFF.md). One worker, bounded admission, settlement before follow-on writes. File I/O still takes wall time; JSON/GIL and synchronous scan storage remain |
| P0B1 / PR #154 | Active: `_apply_lifecycle_isolated` dispatches the whole lifecycle boundary to `LifecycleWorker` | [worker](app/runtime/lifecycle_isolation.py), [repository](app/lifecycle/repositories.py), [handoff](docs/operations/CCI_P0B1_LIFECYCLE_SQLITE_ISOLATION_HANDOFF.md). Worker opens/uses/closes SQLite; cancellation fences the receipt; shutdown joins before session lease release |
| P0B2 / PR #155 | Active: `monitor_obligations_with_market_data` awaits read/fetch/write stages on the lifecycle worker | [monitor](app/lifecycle/owner_monitoring.py), [handoff](docs/operations/CCI_P0B2_CONTINUATION_SQLITE_ISOLATION_HANDOFF.md). No continuation connection across candle HTTP; stage 3 revalidates obligations/epoch. Missing scan run is not fabricated |
| P0B3B1 / PR #157 | Dormant: `TelegramOutboxWorker`, stage functions and `deliver_single_reserved_part` have no scanner/production caller imports | [protocol](app/alerts/telegram_outbox_protocol.py). Owns fresh connections and fences completion, but does not extract existing outer orchestrator ownership |
| P0B3B2A / PR #158 | Dormant: `TelegramSenderTransport` is defined in its module and used by tests, not the production orchestrator | [adapter](app/alerts/telegram_sender_adapter.py). Strict acknowledgment/retry evidence; must not be inserted into the old helper in this release |
| P0B3B2B0 / PR #159 | Documentation and characterization tests | [contract](docs/operations/CCI_P0B3B2B0_TELEGRAM_DELIVERY_OWNERSHIP_CONTRACT.md). Architecture prerequisites are still open except the bounded terminalizer defect fixed here |
| P0B3B2B0A | Active shared primitive: five terminalizer calls in `telegram_lifecycle.py` receive observed attempt authority | [outbox](app/alerts/telegram_outbox.py), [callers](app/alerts/telegram_lifecycle.py). Atomic, conditional no-send update; no active-claim abort, no reopening terminals, no policy retention redesign |

The source inventory was checked with searches across `app` and `scripts`, not
inferred from handoff titles. Integration suites include P0A/P0B1/P0B2, worker
shutdown, continuation epoch change, transport and lifecycle tests in full pytest.
DEV latency fixtures are synthetic; no Runtime latency improvement is claimed.

## Remaining risks and their release effect

| Risk | Existing integrated release | Future Telegram integration |
| --- | --- | --- |
| Outer alert/lifecycle repositories remain open on loop across Telegram awaits; synchronous SQL/scan storage can still stall it | Residual known architecture; observe phase timings and contention. No claim of complete loop isolation | Blocker: extract whole preparation/projection units; do not move live connections or wrap arbitrary calls in threads |
| Cancellation after claim can leave IN_FLIGHT even with zero HTTP; expired recovery becomes UNCERTAIN | Preserve evidence; loss of unsent delivery is possible. Never replay on lease expiry | Exact postclaim no-start evidence/disposition remains a separate protocol decision |
| B1/adapter authority and destination can drift after stage 1; strict persistence can refuse after provider acceptance | Dormant boundary; B0A does not solve cross-stage revocation in any path | Blocker: immutable dispatch binding plus coordinated quiescence; no invented recheck guarantee |
| Raw sender 5xx/408 classifications differ from adapter; direct reconciliation branches are outside B1 | Existing classifications remain. Lost acknowledgments, reconciliation duplicates and retry outcomes require observation/review | Blocker: explicitly settle semantics and prove each migrated family, including direct branches |
| Repeated cancellation and worker teardown can wait indefinitely on a stuck synchronous job | Active lifecycle/disk joins are intentional ownership fences. Failure to prove drain is an operational FAIL; no forced overlap/restart | Finite operation/admission/completion capacity and failure-readback contract still required |
| CMC sidecar `to_thread` cancellation can outlive its waiter | Preexisting, unchanged. Quiesce process/sidecar writers before code/state switch; unresolved writer is FAIL | Separate from this outbox fix; do not silently repair CMC here |
| Missing/contradictory parts or receipts prevent B0A terminalization | Deliberate fail-closed refusal; retain rows. Local caller skipped/failed response is not proof of a durable terminal mutation | Needs explicit evidence/reconciliation if such records exist; never backfill authority |
| Other completion primitives are not generally state-fenced/idempotent | `record_part_result` can mark the event UNCERTAIN on a part-result conflict; `mark_uncertain_after_persistence_failure` has no expected-attempt argument. Their existing callers require exclusive completion ownership; B0A does not make these arbitrary stale calls safe | Blocker to broader completion/reconciliation authority. Stage-3 ID checks and fallback readback do not prove that every duplicate/stale completion preserves a prior terminal state |
| Legacy/quarantined or UNCERTAIN public history | Retain as read-only historical evidence, no reset/replay | Never becomes automatically eligible through integration |
| DEV Windows receipt replacement failure during first full suite | One unchanged watch-session test hit WinError 5; 26 module tests then passed in fresh system temp. Cause Unverified, no retry patch added. Runtime filesystem/drain evidence remains mandatory; recurrence is FAIL | Does not justify broadening Telegram integration or suppressing receipt errors |

The current client has no provider idempotency/reconciliation key establishing
external exactly-once delivery. Do not infer it from exclusive SQLite authority.
The completion concern above is a source-level limitation, not a newly observed
Runtime incident. See `record_part_result`, `_mark_event_locked` and
`mark_uncertain_after_persistence_failure` in the linked outbox source, and
`stage_3_outbox_persistence` in the protocol. No extra completion actor is added
here. If the actual deployment requires competing completion owners or proves a
stale fallback reachable, keep NO-GO and scope a separate exact-state completion
fix; do not expand this terminalizer phase or claim global mutator safety.

## Release packet and PASS/FAIL gates

Every gate must PASS before a separately authorized cutover. Missing evidence
is FAIL, with unavailable facts marked N/A and uncorroborated reports Unverified.

| Gate | PASS | FAIL / action |
| --- | --- | --- |
| G1 release identity | Owner-approved immutable 40-hex merged release SHA; actual release CI success; candidate PR exact-head CI success; accumulated diff from observed Runtime SHA reviewed | Moving main, branch-only pin, old CI, or unknown Runtime baseline: STOP |
| G2 consumer compatibility | Every scanner, listener, command/research reader, dashboard, scheduled job and maintenance consumer listed and compatible with schema 27, `inline_v1`/`symbol_refs_v1`, indexes and epoch contracts | Unknown consumer or schema mismatch: NO-GO; separate compatibility phase |
| G3 database/epoch | Scanner and listener absolute resolved DB paths refer to the approved same physical file/volume; schema 27 and required index definitions verified; full expected epoch identity matches durable control/lineage | Default path, epoch ID alone, mismatched cutoff/contract/reviewed SHA/generation binding or absent metadata: STOP; never create/rebind/backfill an epoch to pass |
| G4 capacity/recovery | Measured main/WAL/SHM and 24-hour growth peaks; per-physical-volume allocation budget; verified consistent backup and restore exercise; recovery time fits approved outage; memory budget demonstrated | Assumed backup feasibility, insufficient reserve, unknown topology or no restore proof: NO-GO |
| G5 exclusive ownership | Scheduled starts disabled; old command intake stopped; all writers/readers drained; workers joined/handles closed; no old listener; no active transport owner | Surviving process/transaction/sidecar writer, hung teardown or duplicate poller: STOP, do not start replacement |
| G6 configuration | `LOCAL_MANUAL_MODE=true`, `ORDER_EXECUTION_ENABLED=false`; existing authorized routes/quality/cadence/CMC policy retained; source replay capture off for initial release unless separately approved | Strategy/RR/Fibonacci/CMC/public eligibility changes or unknown effective safety settings: FAIL |
| G7 observation plan | Named operator, baseline, absolute latency/resource ceilings, review windows, stop mechanism and compatible rollback SHA/commands frozen before cutover | Undefined limits or unsupported rollback reader: NO-GO |
| G8 authorization | Owner approves the exact completed release packet and operational window separately | This document, tests or PR approval alone do not authorize Runtime operations |

Current gate disposition: G1 candidate validation is recorded in the handoff/PR;
merged release approval is pending. G2-G7 Runtime evidence is N/A/Unverified.
G8 is absent. **Overall NO-GO.**

The current [database module](app/storage/database.py) declares `SCHEMA_VERSION=27`.
[Operational startup](app/runtime_epoch/startup.py) rejects every different
schema and never migrates it implicitly. The historical
[checkpoint runbook](docs/operations/runtime_checkpoint_readiness.md) describes
schema 26 and is not a schema-27 cutover recipe. Its bounded collector uses the
current `SCHEMA_VERSION` dynamically and can supply metadata; its assessment
never grants deployment. B0A introduces no schema change or migration. If Runtime
is older, stop this release procedure and commission the smallest actual-schema
rehearsal/migration plan under separate approval. No universal rollback SHA can
be selected until the running baseline and all consumers are known.

## Capacity, WAL and recovery prerequisites

Treat ~150 GB / 4 GB as planning constraints, not present measurements. Do not
load history into memory, materialize a second scanner result, run unrestricted
counts/JSON scans, `dbstat`, VACUUM, REINDEX, full integrity checks, live hashes,
or an ad-hoc backup against the live database. Keep reads indexed and bounded.
SQLite connection cache/worker memory and paging must fit the measured host.

Collect filesystem allocation, WAL/SHM, physical volume identity/free/total bytes,
growth and WAL peaks over at least 24 hours of comparable workload. Aggregate all
concurrent obligations on each physical volume: new backup, restored candidate,
WAL/log/growth increments and any separately approved migration temporary space.
Require a reserve of at least max(10 GiB, 10% volume capacity), or the owner's
stronger established reserve. Existing allocated DB bytes must not be counted
twice as new demand; sparse/logical size is not actual free capacity.

A full copy and separate restore of a ~150 GB database can require roughly two
additional database-sized allocations, before WAL, external state and reserves.
Do not assume compression or a second destination creates capacity. Prove both
capacity and elapsed restore time with a valid recovery method on the approved
host/storage. With 4 GB RAM use a bounded streaming/snapshot method under an
approved procedure; no in-memory backup. A storage snapshot is acceptable only
with independently established SQLite consistency, retention and restore proof.
If no feasible consistent recovery exists, remain NO-GO.

Never delete WAL/SHM, truncate WAL, change journal/synchronous settings or run a
checkpoint as a convenient cleanup. The expected writable profile is WAL,
`synchronous=FULL`, busy timeout 5000 ms and autocheckpoint 1000 pages; B0A changes
none of these. A live WAL database without safe sidecar evidence must not be
opened with `immutable=1`. Preserve consistent DB/sidecar relationships and
external watch/session/manifests/command state in the recovery packet.

## Separately executable operator procedure

Execute NONE of this from DEV. The following is a future Runtime procedure with
explicit prerequisites. Unresolved packet values are blockers, not defaults.

1. **Prepare off the running code path.** Record old running code/interpreter,
   sanitized launch arguments, all consumer identities, scheduled starts, exact
   DB/state/manifest paths and full nonsecret epoch identity. Do not print `.env`
   or command arguments containing credentials. Stage the approved immutable
   release and its tested environment in a separate directory. Review the entire
   delta from the observed old release, not just this PR. Preserve old code/env
   securely. No automatic main update and no PR #152 inclusion by assumption.
2. **Collect bounded evidence under separate read-only authorization.** Use the
   pinned diagnostic checkout and the commands below. Retain filesystem results
   even if SQLite metadata is unavailable. Stop on WAL-without-SHM or
   WAL-header-without-sidecars; never repair sidecars to obtain a green report.
3. **Close G1-G8.** Approve measured volume/recovery and 4 GB resource budgets,
   actual schema/index/decoder and full epoch compatibility, exact release and
   rollback SHA, operator and maintenance window. Rehearse on the separately
   authorized recovered copy. All mocks/dry-run scans stay on an isolated copy;
   dry-run delivery against the active DB terminalizes real PENDING intents.
4. **Stop new work.** Disable scheduled launches/automatic restarts/admin jobs.
   Stop the one Telegram command listener first so commands cannot enqueue work.
   Request graceful scanner/watch stop through its existing controlling console
   or service. Stop any independent writers/readers. Allow admitted SQL, HTTP
   and completion persistence to settle. Verify lifecycle/disk worker joins,
   session lease release and absence of old DB handles/processes. Do not use a
   repeated Ctrl+C or force-kill as evidence of a completed transaction.
5. **Freeze and preserve the boundary.** Record existing IN_FLIGHT/UNCERTAIN and
   SENT part receipts read-only, using bounded indexed observations (no bulk
   history scan). Acceptance of unresolved HTTP remains Unverified. Take the
   approved final consistent recovery snapshot including external state. Verify
   its restore and quiescent boundary before switching code. No history deletion,
   event replay, outbox reset or canonical pointer repair.
6. **Activate code only after compatibility proof.** Select the pinned release
   for all consumers without editing files underneath an old process. Preserve
   the active database and epoch; no migration is authorized by this plan.
   Recheck absolute paths, schema 27, index definitions and full expected identity
   on the exact file that will be opened. Failure means keep stopped.
7. **Restart one owner at a time.** Start one scanner using the exact reviewed
   launch arguments/paths and already authorized Runtime configuration. Public
   delivery remains governed by its existing approved policy; do not toggle
   signals off/dry-run on against the live DB as a smoke test, because that can
   consume terminal evaluations. If observation without public effects is
   required, approve a separate isolation method before this step. After the
   scanner's first valid run/provenance/storage checks pass, start exactly one
   listener with explicitly reviewed DB/manifest/state/audit paths. Restore only
   compatible readers and scheduled tasks, with no duplicate listener or scanner.
8. **Observe and decide.** Verify actual process revision/interpreter/paths,
   effective safe flags and single poll ownership. Run the observation gates
   below; start no new B1/B2A dispatcher. If a gate fails, stop admission and use
   the coordinated rollback boundary below. Do not make strategy adjustments to
   hide performance or delivery failures.

Bounded read-only command template for step 2 (future Runtime only):

```powershell
$ReleaseRoot = '<owner-approved isolated pinned checkout>'
$ReleaseSha = '<approved immutable 40-hex release SHA>'
$SourceDb = '<verified absolute Runtime DB path>'
$EvidenceDir = '<approved existing evidence directory with capacity>'
foreach ($value in @($ReleaseRoot, $SourceDb, $EvidenceDir)) {
    if ($value.Contains('<') -or -not (Test-Path -LiteralPath $value)) { throw 'Unresolved packet path' }
}
if ($ReleaseSha -notmatch '^[0-9a-f]{40}$') { throw 'Unresolved release SHA' }
Set-Location -LiteralPath $ReleaseRoot
if ((git rev-parse HEAD) -ne $ReleaseSha) { throw 'Release SHA mismatch' }
if (git status --porcelain --untracked-files=no) { throw 'Tracked release files changed' }
$Python = Join-Path $ReleaseRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw 'Approved interpreter missing' }
$Stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$FsReport = Join-Path $EvidenceDir "b0a-fs-$Stamp.json"
$SqlReport = Join-Path $EvidenceDir "b0a-metadata-$Stamp.json"
& $Python scripts/runtime_checkpoint_readiness.py collect --source-path $SourceDb --output-path $FsReport --filesystem-only
if ($LASTEXITCODE -ne 0) { throw 'Filesystem collection failed' }
& $Python scripts/runtime_checkpoint_readiness.py collect --source-path $SourceDb --output-path $SqlReport --busy-timeout-ms 250 --query-deadline-ms 2000
if ($LASTEXITCODE -ne 0) { throw 'Metadata collection failed' }
# An exit-0 collector can report unavailable/adverse evidence. Inspect the report;
# exit 0 alone does not close G2/G3, prove full epoch identity or authorize startup.
```

Full identity evidence must include epoch_id, cutoff_at, contract_version,
reviewed_release_sha and generation_binding from independently verified durable
authority. The epoch's reviewed release is its immutable creation binding, not a
field to replace with today's candidate SHA. Record today's deployment SHA in
the external release manifest. Commands above are not a process inventory,
capacity proof, full epoch comparison or deployment command. The collector
refuses output overwrite. Use the separately reviewed read-only identity
procedure in G3; operational startup then enforces that same full identity again.
Do not substitute the startup inspection opener for the bounded live collector:
its mode=ro alone does not establish sidecar noncreation on a changing WAL file.
Freeze the exact service/console stop/start commands in the approved packet;
unknown local service/task names must never be guessed here.

## Observation, abort and rollback

Collect a comparable 24-hour baseline before cutover; then immediate first-run
checks, first 3 completed cycles, ~1 hour, 24 hours and seven continuous days.
Include a planned graceful restart after early gates pass under the separately
authorized observation procedure. Low event activity is N/A for unexercised
delivery/multipart paths, not proof they work. Do not generate production alerts
to satisfy observation.

| Area | PASS criteria | Abort / hold criterion |
| --- | --- | --- |
| Identity/ownership | Approved actual process SHA/path/interpreter; one scanner and one listener; unchanged schema/epoch and provenance | Any mismatch, duplicate poller/HTTP 409, unregistered writer, lost lease or lineage violation: stop immediately |
| Cadence/loop | Comparable scan-duration p95 below approved cadence and <=110% of baseline p95; sampled lag and synchronous-block maxima within predeclared absolute ceilings and no >10% regression over comparable baseline | Missing baseline/ceiling: hold. Three consecutive exceeded cycle/lag limits: stop. Worker isolation does not promise shorter scan wall time |
| SQLite/storage | Every admitted transaction has a settled disposition; no new unexplained busy/lock errors; WAL and per-volume free space remain inside approved growth/reserve budgets | Failed commit, unresolved ownership, corruption evidence or reserve breach: stop immediately; no emergency history cleanup |
| Memory | Peak working set/commit, available RAM, paging and disk response inside the measured 4 GB host budgets frozen in G7 | Sustained paging thrash, allocation failure or predeclared low-memory breach: stop; no unbounded cache/workers |
| Telegram | Canonical event/reservation/part states agree, SENT receipts preserved, expected route, no duplicate or reordered public effect | Any lost receipt, event/part contradiction, duplicate, unexpected attempt, or new unexplained UNCERTAIN/unresolved persistence: freeze sending/admission and inspect; never replay |
| Reconciliation | New and recovered records retain economic identity, ordering and dedupe; known quarantined/uncertain population stays isolated | Eligibility resurrection, historical mutation or incompatible direct branch outcome: stop and preserve evidence |
| WebSockets | Valid frames/health progress, reconnect/staleness/gap counters within approved baseline budget; no inference of market confirmation from a healthy socket | Repeated stale feeds/reconnects or no receive progress beyond approved threshold: stop/hold dependent evaluation; never fabricate data |
| Cancellation/restart | Workers joined, no old handles/processes, session lease safely released; next start respects SENT/UNCERTAIN and attempt limits | Hung drain exceeds observation budget: remain stopped, preserve diagnostics; no automatic replacement while owner may still write |

The 0.5-second loop monitor stores maxima, not a true lag percentile; compare
per-cycle maxima over matched windows and retain sample counts. Source telemetry
is in `phase_profile.event_loop`, `disk_isolation`, `lifecycle_isolation`,
`continuation_isolation` and inclusive phase elapsed times. Do not sum overlapping
phase times. Busy/HTTP/receipt monitoring must use bounded existing logs/queries;
if a required signal is unavailable, G7 fails instead of claiming an observation.

**Coordinated rollback:** stop listener/intake and schedulers first, gracefully
drain scanner/worker owners, stop readers, preserve failed-release logs plus the
current DB/WAL/external state, and verify zero active owners. Never start an old
binary while the new owner can still persist. A compatible code-only rollback
may reopen the same current DB only after the exact fallback SHA's schema-27,
decoder, epoch, outbox and external-state compatibility has been proved. Record
the reintroduced terminalizer defect if falling back before B0A; keep delivery
stopped if that would be unsafe. Prefer a separately reviewed forward fix.

Before any new operational writes or public side effects, an approved consistent
pre-cutover code/data/external-state bundle can be restored through its rehearsed
procedure. **After new writes or possible public sends, never replace the active
outbox with an old snapshot.** Preserve all newer events/receipts and uncertainty;
use compatible code rollback or separately reviewed forward recovery. If
compatibility is unproved, staying stopped is the rollback disposition. Never
reset, replay, delete, down-migrate, or selectively merge historical public events.

No deployment, migration, production send, scanner/listener operation, merge,
order, withdrawal or transfer is performed or authorized by this deliverable.
