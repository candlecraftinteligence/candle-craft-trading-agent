# CCI V2 controlled rollout / rollback packet

**PREPARED IN DEV. NOT APPROVED OR EXECUTED.** Implementation release:
`677181fab8a6bbff316b0d0a0c551cc82d1faab1`.

The current host is CANDLECRAFT. Runtime host DESKTOP-G7M42AL, its checkout, S:
volume and original launcher receipts are not accessible here. Exact argument
arrays, launcher override locations and graceful stop controls must be obtained
before this packet can be finalized or submitted for production approval.
The commands below deliberately refuse to substitute defaults for those inputs.

## Fixed release and budget

Use the implementation commit above; report/package commits add evidence only.
Use the artifact names and SHA-256 hashes in `release_manifest.json`.
No remote push, merge, production migration or live DB copy is part of this plan.

CMC: SHADOW, RUNTIME_SHADOW profile, 500/day and 15,000/month. Baseline projection
433/day / 12,990 per 30 days, with existing bounded retries. The proposed 20-minute
verification guardrail is <=20 additional credits including one preflight and
collector activity. Monthly/daily limits remain enforced after the observation
window. Keep CoinPaprika ranking authority and all existing safety settings.

Approve the exact four-contract identity proposal separately as part of the final
release/configuration package. Populate reviewed_at, approved_at and
owner_approval_ref using the actual owner approval, never an earlier evidence time.
Preserve evidence_ref/acquired_at and expiry. Reacquire expired identity evidence;
do not renew an expiry by editing its date without new evidence/review.

## Before the approval request

Obtain current sanitized scanner/listener launch receipts, environment allowlist,
process ownership, auto-restart inventory and tracked Git state. Confirm the full
epoch tuple from established Runtime controls. The founding epoch SHA is not this
binary release SHA. Verify Runtime Python/environment compatibility and safe key
presence, without exposing keys. Preserve exact current DB, state, manifest,
command audit, cadence, universe, risk, Telegram and ownership arguments.

The final owner-reviewed operational JSON must contain:

```json
{
  "verified_previous_release": "N/A: current Runtime evidence required",
  "scanner_arguments": null,
  "listener_arguments": null,
  "scanner_graceful_stop_control": null,
  "listener_graceful_stop_control": null,
  "auto_restart_controls": null,
  "complete_existing_epoch_identity": null,
  "launcher_file_and_exact_environment_diff": null,
  "owner_approval_ref": null
}
```

No approval request has been made for this incomplete operational JSON.

## Approved installation commands

Run on Runtime only after the complete package is approved, auto-restarts are
controlled, the original listener is gracefully stopped, the scanner finishes its
cycle and is gracefully stopped, all inventoried writers are quiescent, and their
absence is verified. Do not force-kill writers or launch a second scanner.

```powershell
$RuntimeRepo = 'C:\Users\aspir\Desktop\Candle Craft Inteligence'
$StateRoot = 'S:\CCI_RUNTIME_V2'
$ReleaseSha = '677181fab8a6bbff316b0d0a0c551cc82d1faab1'
$PlanPath = Join-Path $StateRoot 'ops\cci_v2_repair\approved_operational_plan.json'
$BundlePath = Join-Path $StateRoot 'ops\cci_v2_repair\cci_v2_repair.bundle'
$Plan = Get-Content -LiteralPath $PlanPath -Raw | ConvertFrom-Json
if (-not $Plan.owner_approval_ref -or -not $Plan.scanner_arguments -or
    -not $Plan.listener_arguments -or -not $Plan.complete_existing_epoch_identity) {
    throw 'Incomplete approved operational plan; stop'
}
if ($Plan.verified_previous_release -notmatch '^[0-9a-f]{40}$') {
    throw 'Missing verified rollback release; stop'
}
Set-Location -LiteralPath $RuntimeRepo
git status --short --branch
git worktree list --porcelain
if (git status --porcelain --untracked-files=no) { throw 'Tracked Runtime changes; stop' }
if ((git rev-parse HEAD) -ne $Plan.verified_previous_release) { throw 'Runtime release moved; stop' }
# Verify BundlePath SHA-256 against the separately approved release_manifest.json.
# Verify inventoried owners/writers are stopped through their approved controls.
git fetch --no-tags $BundlePath refs/heads/codex/cci-v2-production-repair
if ($LASTEXITCODE -ne 0) { throw 'Release import failed' }
git cat-file -e "$ReleaseSha^{commit}"
if ($LASTEXITCODE -ne 0) { throw 'Pinned implementation object missing' }
git switch --detach $ReleaseSha
if ($LASTEXITCODE -ne 0 -or (git rev-parse HEAD) -ne $ReleaseSha) { throw 'Wrong deployed release' }
$Python = Join-Path $RuntimeRepo '.venv\Scripts\python.exe'
& $Python -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Runtime dependencies incompatible; stop' }
& $Python -m compileall -q app scripts src
if ($LASTEXITCODE -ne 0) { throw 'Runtime compilation failed; stop' }
```

Apply the exact approved change to the **original launcher**, including replacement
of its effective CMC_MODE=off assignment. Settings in a different PowerShell window
or an older .env file do not override that launcher. Use
`CCI_V2_REPAIR_CONFIG_PROPOSAL.json` as the proposed allowlist, not an automatically
applied configuration. The key remains in its existing approved secret mechanism.
The separate CMC sidecar may initialize its existing generic CMC tables on first
collection; the main candle_craft.db schema remains 27.

After configuration/identity/path checks, execute the original reviewed commands
once, under their existing owner controls. These arrays must be the actual complete
arguments, including database/epoch/state bindings; never generate fresh defaults.

```powershell
# Scanner terminal/service: exactly one instance, after quiescence was established.
& $Python scripts\run_scan.py @($Plan.scanner_arguments)
# Listener terminal/service: exactly one instance, after scanner operational checks.
& $Python scripts\run_telegram_bot.py @($Plan.listener_arguments)
```

These commands belong in separate approved owner terminals/services; do not
execute a sequential script that waits for the scanner watch loop to return before
starting the listener. Existing process-parent launcher/worker pairs count as one
owner each. Inspect existing listener evidence for conflicts; do not start another
Telegram getUpdates consumer as a probe.

## Acceptance: <=3 natural scans / <=20 minutes

Record release/provenance, effective configuration (not only .env), preserved
epoch/state, one scanner/one polling listener, CMC request credits/source/acquisition
timestamps and exact batch/candidate references, admitted funding/OI and exclusion
reasons, Unicode coverage, request/ack/event distinction, source age, depth bands,
gaps/recovery, queue/work/loop delay, scan cadence, RSS and persistence/outbox status.
Query only the approved bounded scan IDs/metadata through the established read-only
SQLite helper; no full-history scan, integrity pass, checkpoint or maintenance.

Fail acceptance for incorrect owners/configuration, no actual CMC collector data,
disconnected enrichment, broken native admission, source identity faults, safety
regressions or persistence errors. Missing source data must remain N/A/Unverified
with its real limitation. A quiet eligible liquidation symbol is not a failed
connection. An absent eligible setup is not scanner failure. Do not send a public
test signal; natural trading/outcome delivery remains UNOBSERVED if none occurs.

## Rollback

First configuration mitigation, when appropriate: gracefully stop the same scanner,
restore its prior CMC launcher settings (OFF), and restart that one owner with the
unchanged DB/epoch/argument arrays. Retain the sidecar and all public/outbox evidence.
The repaired reader remains available; do not rewrite historical observations.

Full code rollback requires prior reader compatibility validation for the actual
schema, epoch and new payload fields/status values. The isolated
`rollback_reader_check.json` proves that the audited typed stream reader rejects
the new explicit UNVERIFIED status (enum validation error); its UNAVAILABLE control
passes. Prepare a compatible rollback reader for the verified current Runtime
base before requesting rollout approval. Do not substitute the unmodified audited
release. Do not coerce observations, remove fields or restore an old DB/outbox
snapshot to make an old reader work.

Only after compatible rollback is approved and the same owners are gracefully
stopped/quiescent:

```powershell
Set-Location -LiteralPath $RuntimeRepo
if (git status --porcelain --untracked-files=no) { throw 'Tracked changes; stop' }
git switch --detach $Plan.verified_previous_release
if ($LASTEXITCODE -ne 0) { throw 'Rollback checkout failed; stop' }
# Restore the exact previous launcher environment and restart the original owners.
# Preserve the current DB, journals, epoch, canonical reservations and delivery receipts.
```

Never downgrade schema/user_version, delete WAL/SHM, reset an epoch, backfill
lineage, restore stale public-send state, merge/reset main or perform production
cleanup. Recovery requiring any of those actions is outside this approval package.
