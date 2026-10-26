# CCI CMC Runtime rollout plan

**PREPARED ONLY — Runtime deployment and restart are not approved.** None of
these Runtime commands was executed. The owner must approve a concrete package
with a pinned merged-main SHA, verified CI, consumer inventory, capacity evidence,
epoch/schema compatibility, maintenance window and recovery plan.

Reported Runtime code path: `C:\Users\aspir\Desktop\Candle Craft Inteligence`.
Reported DB: `S:\CandleCraftRuntime\scan_runs\main_live_runtime.sqlite`. Verify
both from actual Runtime configuration/process evidence. Do not inspect that
database from DEV. Runtime has approximately 4 GB RAM; DEV tests do not prove its
available working memory or storage capacity.

## Compatibility gates before the maintenance window

- Follow the existing [Runtime checkpoint procedure](../operations/runtime_checkpoint_readiness.md)
  for consumer, volume, quiescence, backup/restore and actual-schema-copy evidence.
  That document's schema-26 historical packet is not certification of this release.
  Current `app/storage/database.py` supports **schema 27**. PR #148 introduces
  **no production database schema or migration change** relative to its main base.
  Actual Runtime schema and accumulated code delta are unverified here. A different
  schema requires the corresponding separately reviewed copy rehearsal; no live
  migration, DB copy, full-history query, COUNT, integrity scan or VACUUM is authorized.
- Preserve the complete existing epoch tuple: `RUNTIME_EPOCH_ID`,
  `RUNTIME_EPOCH_CUTOFF_AT`, `RUNTIME_REVIEWED_RELEASE_SHA`,
  `RUNTIME_GENERATION_BINDING`, `RUNTIME_EPOCH_CONTRACT_VERSION`. The reviewed SHA
  inside that durable identity is **not** the current binary release SHA. Do not
  replace it with the CMC merge SHA, generate a new epoch, or backfill lineage.
- Account for scanner, listener, outbox workers, command processors, readers and
  any scheduled auto-restart. Use the same compatible release for every consumer.
  Preserve manifest, command-state/audit, watch-state and existing DB paths.
- Verify baseline process RSS/peak working set, scan p50/p95/max duration, cadence,
  faults, free memory and each affected physical volume. CMC needs its own local
  sidecar, not space inside or an alias to the huge production DB.
- Freeze a workload and observation plan. Suggested proposed guardrails for owner
  review: at least 512 MiB free RAM after existing workload; incremental CMC RSS
  <=128 MiB; p95 added selection latency <=1 second and <=5% of scan cadence;
  zero overlapping scanner/listener instances. These are proposed acceptance
  ceilings, not observed Runtime results or automatically approved limits.

## Prepare and verify inputs

Only after the package receives explicit Runtime approval, in an owner-controlled
PowerShell session:

```powershell
$RuntimeRepo = 'C:\Users\aspir\Desktop\Candle Craft Inteligence'
$LiveDb = 'S:\CandleCraftRuntime\scan_runs\main_live_runtime.sqlite'
$ReviewedReleaseSha = '<EXACT MERGED SHA FROM PR #148 RELEASE RECEIPT>'
$PreviousReviewedSha = '<VERIFIED CURRENT RUNNING RELEASE SHA>'
$Python = Join-Path $RuntimeRepo '.venv\Scripts\python.exe'
if ($ReviewedReleaseSha -notmatch '^[0-9a-f]{40}$') { throw 'Missing pinned release' }
if ($PreviousReviewedSha -notmatch '^[0-9a-f]{40}$') { throw 'Missing recovery release' }
Set-Location -LiteralPath $RuntimeRepo
git status --short --branch
git worktree list
if (git status --porcelain) { throw 'Runtime checkout is dirty; stop' }
if ((git branch --show-current) -ne 'main') { throw 'Review checkout ownership before changing branches' }
```

The operator must supply the previously verified launcher argument arrays and
external state paths. They are not known from DEV. An absent launcher manifest,
unknown existing consumer, incomplete epoch identity or unknown DB binding is a
STOP condition; do not substitute repository defaults.

## Graceful shutdown and exact fast-forward

1. Disable the inventoried scheduled launches/auto-restarts and administrative
   intake through their existing operator controls. Do not guess task/service names.
2. Stop the existing Telegram polling listener first, then let the scanner finish
   its transaction/current cycle and stop it with Ctrl+C in its original terminal
   or its documented graceful service control. Drain known workers and uncertain
   Telegram sends. Stop all remaining DB consumers. Do not force-kill writers.
3. Verify the inventoried PIDs are gone and DB handles are quiescent using the
   approved Runtime procedure. This sanitized process listing is only one input:

```powershell
Get-CimInstance Win32_Process |
  Where-Object { $_.CommandLine -match 'scripts[\\/]run_scan\.py|scripts[\\/]run_telegram_bot\.py|scripts[\\/]process_telegram_admin_commands\.py' } |
  Select-Object ProcessId, ParentProcessId, ExecutablePath
# No command-line dump: it could contain unrelated sensitive arguments.
# Any matching process or another inventoried DB consumer still running: STOP.
```

4. Preserve the approved recovery artifact and external state at the drained
   boundary. Never copy only the main SQLite file while WAL writers are active,
   delete WAL/SHM files, or restore old outbox state after new public sends.
5. With consumers stopped, perform only a pinned fast-forward:

```powershell
git fetch origin main
if ($LASTEXITCODE -ne 0) { throw 'Fetch failed' }
if ((git rev-parse origin/main) -ne $ReviewedReleaseSha) { throw 'Main moved or release pin differs' }
git merge-base --is-ancestor HEAD $ReviewedReleaseSha
if ($LASTEXITCODE -ne 0) { throw 'Not a fast-forward; stop without reset' }
git merge --ff-only $ReviewedReleaseSha
if ($LASTEXITCODE -ne 0) { throw 'Fast-forward failed' }
if ((git rev-parse HEAD) -ne $ReviewedReleaseSha) { throw 'Wrong deployed revision' }
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Environment incompatible' }
& $Python -m compileall -q app scripts src
if ($LASTEXITCODE -ne 0) { throw 'Compilation failed' }
```

There are no new third-party dependencies in the CMC change. If the Runtime
environment differs, repair/rehearse it separately; do not run an unreviewed
package upgrade during cutover. Do not auto-initialize or migrate the live DB.

## Credential, sidecar and effective settings

Configure only the Runtime CMC key through the owner's approved secret mechanism.
Do not copy DEV `.env`, echo a value, place it in arguments/history/transcripts,
or export it into the process environment. If the approved mechanism is the
existing Runtime `.env`, this console command accepts the value with hidden input
and updates only that key (run after verifying CWD and owner-only file access):

```powershell
& $Python -c 'from getpass import getpass; from pathlib import Path; from dotenv import set_key; p=Path(".env"); assert p.is_file(); k=getpass("CMC_API_KEY (hidden): "); assert k and "\n" not in k and "\r" not in k; _=set_key(str(p),"CMC_API_KEY",k,quote_mode="always"); print("CMC credential updated without display")'
```

Retain owner-controlled ACLs and verify them after the atomic environment-file
update. Disable transcripts/terminal recording for credential entry under the
existing secret policy. Validate only presence and effective non-secret values:

```powershell
$env:CMC_MODE = 'off'
$env:CMC_SIDECAR_PATH = Join-Path $RuntimeRepo 'research_runs\cmc_runtime.sqlite'
$env:CMC_IDENTITY_MANIFEST = Join-Path $RuntimeRepo 'config\cmc\identity.empty.json'
$env:CMC_ACTIVATION_MANIFEST = Join-Path $RuntimeRepo 'config\cmc\activations.disabled.json'
$env:ORDER_EXECUTION_ENABLED = 'false'
$env:LOCAL_MANUAL_MODE = 'true'
& $Python -c 'from app.core.config import Settings; s=Settings(); assert not s.order_execution_enabled and s.local_manual_mode and s.cmc_mode=="off"; print({"cmc_key_present":bool(s.cmc_api_key),"cmc_mode":s.cmc_mode,"sidecar":str(s.cmc_sidecar_path),"daily_credits":s.cmc_daily_credits,"monthly_credits":s.cmc_monthly_credits})'
```

CMC sidecar defaults: 64 MiB main-file page cap, 20,000 records, 30-day bounded
retention; record cap <=2 MiB, short DELETE-journal transactions and 100 ms busy
wait. Reserve additional journal/index/disk headroom (at least 128 MiB total
allocation plus the owner's volume reserve). It must have a `cmc_` filename,
be on suitable local storage, and not alias the production DB. An existing
non-CMC database is refused. No production table/index is created by CMC.

## OFF, COLLECT, SHADOW and manual restart

First validate the release offline on DEV or an approved isolated rehearsal copy,
with Telegram dry-run and signals disabled. Do not run a purported dry-run scanner
against the production DB as a smoke test: it may still write runtime state.
The release fixture command is offline and starts no live service:

```powershell
& $Python tests\cmc_release_fixture.py research_runs\cmc_runtime_offline_check.json
if ($LASTEXITCODE -ne 0) { throw 'Offline compatibility failed' }
```

Manual operational restart must use the owner-verified original argument arrays,
including the exact DB and external-state paths, cadence, universe, strategy,
quality/risk limits and complete epoch identity. Preserve approved public Telegram
settings, not the isolated test flags. Do not use fresh default CLI arguments.

```powershell
# Terminal A, only after approval and quiescence; one scanner owner.
if (-not $ApprovedScannerArguments) { throw 'Missing verified scanner launcher arguments' }
& $Python scripts\run_scan.py @ApprovedScannerArguments
# Terminal B, after the scanner's first operational checks pass; one listener.
if (-not $ApprovedListenerArguments) { throw 'Missing verified listener arguments' }
& $Python scripts\run_telegram_bot.py @ApprovedListenerArguments
```

Start in OFF and verify loaded release, process ownership, unchanged epoch/DB,
normal canonical acceptance/event identity/follow-ups, and no CMC storage/network
activity. Capture the approved baseline. Each later mode change uses the same
graceful single-process restart, never a second parallel watch loop:

```powershell
$env:CMC_MODE = 'collect'  # After approval for this observation stage.
# Keep the activation manifest empty; restart only the existing scanner owner.
$env:CMC_MODE = 'shadow'   # After collect stability/budget/coverage acceptance.
# Hypothetical recommendations and actual baseline delivery are recorded separately.
```

Do not execute DECISION activation until the
[individual rule activation plan](CCI_CMC_DECISION_ACTIVATION_PLAN.md) passes.
Mode alone cannot authorize a rule. No OI, funding, basis or sector policy is
eligible with the present source gaps.

## Credit and observation budget

The authenticated account reported 15,000 credits/month and 50 requests/minute;
no account plan name or provider daily quota was returned. Local defaults remain
100/day, 3,000/month and 300-second polling. Full collection requests up to two
pages for listings/map/exchange pairs plus global, indices, exchange and asset
liquidations and sectors. A first full cycle reserves approximately 13 credits
without optional assets/categories; a repeated five-minute full profile can
require roughly 2,760 credits/day under the documented cache/page bounds.
Actual usage depends on page completeness and provider cost reconciliation.

**Neither the local defaults nor the observed monthly account quota support
continuous full-family five-minute collection.** Budget exhaustion returns
UNAVAILABLE and baseline behavior; it is not a reason to raise limits silently.
Approve a finite observational period, endpoint/page profile, effective cadence,
coverage requirement and total cost before Runtime collection. A future focused
global-plus-liquidation profile would still need 576 requests/day at five-minute
cadence (17,856 in a 31-day month), before other calls; that profile is a capacity
scenario, not a shipped configuration or an inferred entitlement. Provider lag
can further reduce the usable five-minute evidence window.

Measure actual ledger credits and freshness under the approved workload. Missing
or stale intervals must be included in SHADOW coverage; do not interpret a
budget-starved sample as continuous prospective validation. Export research
evidence before the bounded retention window expires through an approved
sidecar-only archival plan with its own disk budget. Do not add history queries
to the huge production DB to reconstruct missing evidence.

## Rollback

First remove CMC authority: gracefully stop the existing scanner, set that same
launcher's `CMC_MODE=off`, keep activation entries disabled, and resume only its
approved single process. OFF clears attached in-memory CMC evaluations. Confirm
canonical behavior and preserved follow-ups. Keep sidecar/economic evidence.

If code rollback is necessary, stop all affected consumers again. Verify that
the previous release can read the current schema, epoch and payload encoding;
otherwise STOP for a compatible forward fix. Only under the approved recovery
package, `git switch --detach $PreviousReviewedSha` pins the former code without
resetting branch history. Use the unchanged DB and external-state bindings.
Do not downgrade schema/user_version, restore stale outbox state, delete journals,
wipe epochs, or reset/delete Runtime data. A DB restore is a separate recovery
operation requiring its own verified, post-send-safe plan and owner approval.
