# CCI V2 repair release finalization — owner review

**DEV verification only. NOT MERGED, NOT DEPLOYED, CMC NOT ACTIVATED.**
This is the current release handoff. It supersedes the initial implementation-phase
operational proposal in CCI_V2_CONTROLLED_ROLLOUT.md. Original audit observations,
implementation detail and previous receipts remain in CCI_V2_PRODUCTION_REPAIR_REPORT.md.
Do not execute an earlier proposal to combine this code rollout with CMC activation.

## Exact releases

| Purpose | Commit | Tree |
|---|---|---|
| Current Runtime baseline | `0decc2158df59969b38a2d7441bec2441a3adc5b` | `7606f90284f3ccecc4e6c9b41610acac9e3e4d8d` |
| Tested core repair | `677181fab8a6bbff316b0d0a0c551cc82d1faab1` | `e5d41dd7f6864acf08b63b3b7f7a6c6f783c6922` |
| Compatible rollback reader | `911147cb7c0e1e5ece4a72c3ebda0a951f203d0c` | `5cab69aae6415b3a544291e3a52c5a830bb60c1b` |

Branch: `codex/cci-v2-production-repair`; rollback ref:
`codex/cci-v2-compatible-rollback`. The final release-tooling/packaging HEAD and tree,
PR URL, CI exact head and results are bound externally in the final delivery manifest
to avoid a committed self-reference. The core repair commit above remains unchanged.
Subsequent files add release controls, tests, dependency constraints, CI and evidence;
they do not modify its scanner, strategy, risk, lifecycle or delivery implementation.

Current main inspected for publication: `6ff6134` (full identity in the delivery
manifest). Its tree equals the audited baseline. Merge base with the repair is
`0decc215...`; main has one merge commit beyond that base with no tree delta.
No rebase/merge is needed to reconcile behavior. No merge into main is authorized.

## R1 — FIXED AND VERIFIED in isolated DEV

The rollback is derived from the EXACT Runtime baseline. Its entire code delta is
one enum member in `app/context/models.py`: `UNVERIFIED = "UNVERIFIED"`.
Existing usability predicates still exclude that status. It is never converted to
VERIFIED, STALE or zero, and no persisted evidence is rewritten.

`tests/test_release_rollback_roundtrip.py` runs the exact Git source of baseline,
repair, compatible rollback and rollback restart in four independent processes
against **one populated synthetic SQLite DB**. The fixture executes the real
scanner on mock candles, persists mixed inline_v1/symbol_refs_v1 scans, validates
native typed hydration and watch/UI reads, retains lifecycle history, founding
epoch, public events/attempts/parts, SENT and UNCERTAIN receipts, PENDING intents and
partially delivered messages. Real command polling logic receives a fake transport
and retains offset 700, requesting 701 and ignoring the old update.

Before/after all-table comparisons prove reads did not mutate the DB. Before each
release process, the entire preceding snapshot must match. Compatible rollback
delivers exactly three previously unsent parts through the real durable protocol
with a fake transport, retaining old sent-part rows. Already-sent and uncertain
events return their exact authority refusals with zero transport calls. Restart
reopens the resulting state with no old message or command replay. Schema remains
27 throughout. Full identities, state hashes and zero replay counts are retained in
`rollback_roundtrip.json`; the reproducible fixture and passing JUnit are included.

The unmodified baseline is intentionally NOT the code rollback target after repair
writes UNVERIFIED payloads. Use 911147c. Never restore an old DB/outbox to make an
incompatible reader work. No production DB was read, copied, hashed or modified.

## Windows CPython 3.12.10 verification

Isolated DEV interpreter: official Python 3.12.10 AMD64 NuGet runtime, MSC v.1943,
separate `.venv31210`; existing DEV and Runtime venvs unchanged. Local host OS is
Windows 11; Runtime's Windows 10 Home host was not used for tests. The interpreter
archive URL/hash, actual Python/SQLite versions, complete dependency inventory and
test times are in the packet. All 13 packages named in the Runtime preflight
inventory match, including uvicorn 0.48.0, httpx 0.28.1 and websockets 16.0.

- Exact immutable 677181f source: **4,533 passed, zero failures/errors/skips** in
  four disjoint file partitions. All collected cases executed once; before/after
  source hashes match. Actual elapsed interval and four exit codes are retained.
- Additional release-control and four-stage rollback/restart module: **21 passed**.
- Affected inventory/configuration/safety modules: **52 passed**.
- Final focused repair run: 54 passed before the PowerShell wrapper cases were
  added; the later run retained 55 passes plus three wrapper-host-policy failures.
  The complete final control module then passed all 21 cases. No failure was deleted.
- Pip check: **no broken requirements**. Compileall app/scripts/src/tests and
  `git diff --check`: pass. Full exact-head Linux/Windows CI is recorded in the
  external manifest and must be successful before the packet is accepted.

Full-suite coverage includes funding/OI causality and conflicts, Unicode stream
identities, malformed/reconnect/restoration cases, storage/reference integrity,
epoch mismatch, ownership/sharing, cancellation and worker drain, canonical
outbox/uncertainty/recovery and protected risk/quality/lifecycle routing.

Failure history is preserved: initial archived-source setup run lacked pytest's
temporary parent directory; the second complete run had 4,532 passes and one
default-settings failure from ambient DEV dotenv loading. The final run disables
ambient loading and pins Runtime's uvicorn. No assertion was suppressed. Early new
round-trip attempts corrected fixture current-row uniqueness, async worker cleanup,
and exact protocol status/continuation expectations. Three wrapper tests initially
hit Windows PowerShell 5.1's Restricted policy. They were rerun with installed
PowerShell 7 under its existing RemoteSigned policy; no policy was changed or bypassed.
The first exact-head Windows CI job stopped at pip check because the hosted global
Python included pipx with an incompatible packaging requirement. CI now uses a
fresh project venv on both platforms, matching DEV isolation; pip check is retained.
The failed job log and subsequent exact-head results are included in the packet.

## Runtime profile compatibility

Owner-supplied `C:\CCI_Architecture_Handoffs\CCI_V2_REPAIR_RUNTIME_PREFLIGHT.md`
was read in DEV. Its SHA-256 is
`954c1de3bcc38e1effc84203a865e471b8782c9fd51741c20863adcc9c103f80`.
Its observations are Runtime evidence supplied by the owner, not direct DEV access.
It reconfirms the baseline, separate worker/launcher pairs, complete epoch,
state routing, 36 overrides per role, 54 scanner arguments and 14 listener arguments.

`config/releases/runtime-profile-contract.json` preserves the exact verified
argument arrays and founding tuple, including the serialized generation binding.
It references both original profile paths and SHA-256 anchors from the preflight.
Actual JSON profiles remain Runtime-only. No unknown override value was invented.

`scripts/cci_v2_release_profiles.py prepare` reads the original profile at Runtime,
requires its exact reviewed hash, full epoch, argument vector and safety settings,
and copies **every** override unchanged into a new proposed file using exclusive
creation. It changes release/tree metadata and clears obsolete approval/CI claims.
It never edits the original profile or .env. Repair and rollback proposals are
derived the same way from the anchored original profiles. Preparation does not
grant approval. The packet carries this tool and its contract independently of
the core checkout, so it is available when that checkout selects 677181f or 911147c.

The startup function requires separate exact-release approval, correct deployed
SHA/tree, unchanged epoch and args, CMC off / CoinPaprika / manual true / orders
false, and absence of an existing role owner/launcher. It creates one hidden
dedicated console using CREATE_NEW_CONSOLE/SW_HIDE. Tests inject process inventory
and Popen; they prove refusal before launch and preservation of all overrides.
Actual Runtime startup remains unexecuted and requires owner approval.

The packet's stop wrapper uses the existing console helper only after checking its
preflight hash, current worker/parent IDs, creation times and exact script identity.
It inspects membership, then sends one graceful Ctrl+C. It waits at most 60 seconds
and fails if owners remain. It never force-kills or removes state. Use only a shell
allowed by the host's existing policy; sign/review as the owner requires, without
weakening execution policy. The primary startup tool is Python, so no PowerShell
script-policy change is needed for startup.

## Exact proposed code-only rollout and rollback

**PROCEDURE ONLY. None of these production steps has been executed or authorized.**

1. Verify the independently communicated packet SHA-256, then every manifest file
   hash, source/tree identity and Git bundle prerequisite in a non-live staging
   directory. The baseline prerequisite is reconfirmed present by the preflight.
   Review the PR and require successful CI at the manifest's exact release-tooling
   head. Inspect the package before executing tools; no .env or live state belongs
   in it. Do not extract over Runtime or use an unverified archive as a release.
2. Revalidate original profile hashes, exact current release/clean tracked state,
   owner pairs/creation times, no auto-restart competitors, existing paths and the
   complete founding tuple. Preserve all linked worktrees, untracked work and the
   legacy scan_runs junction. Assemble the proposed role profiles from original
   Runtime JSON for review. Their environment/CLI diff is EMPTY; CMC stays OFF.
3. Obtain separate owner approval for exact repair/rollback commits, packet hash,
   dependency choice, downtime, original publishing policy and current owner stop
   controls. Approval from the old release is not transferable. Do not proceed
   from this DEV handoff alone.
4. At a quiet boundary, stop the scanner once via the reviewed helper/wrapper,
   verify FINALIZED, worker drain/join and both processes absent, then stop the
   listener and prove its pair exited. If a wait times out, inspect the same owners;
   never start a competing replacement. Preserve offsets and uncertain sends.
5. Only under that approval, import the verified bundle if necessary, select the
   pinned repair commit, confirm its exact tree and empty tracked diff, and run
   approved dependency/import readiness checks. No production pytest, schema
   change, fresh generation, DB initialization, checkpoint, cleanup or state copy.
   Existing Runtime packages match the observed inventory; do not upgrade the
   active venv as an implicit step. An optional offline wheel set supports a
   separately approved staged environment if actually needed.
6. Start the scanner ONCE from its approved profile using the packet's Python
   startup tool, verify owner receipt, then start the listener ONCE in its separate
   hidden console after exclusive ownership checks. Keep the exact original epoch,
   paths, canonical delivery settings, 3R/88/A quality, two confirmation cycles and
   manual/no-execution settings. Do not send a public test or notice.
7. Under later approved Runtime acceptance, inspect at most three natural scans
   within 20 minutes (the preflight proposes two). Record provenance, storage,
   lifecycle/outbox, Unicode/native evidence, freshness and cadence. No eligible
   setup is a valid outcome. Natural signal/outcome delivery stays UNOBSERVED if
   none occurs. A scan receipt alone does not prove a delivered signal.
8. If acceptance fails, stop the same owners gracefully. Select the tested
   compatible rollback 911147c and its unchanged environment/argument profiles,
   verify tree/dependencies/epoch, then restart exactly one owner per role. Never
   select unmodified 0decc21 after new-status writes, reset offsets/epochs, rewrite
   payloads, restore stale DB/outbox state, downgrade schema or delete WAL/SHM/leases.

Guarded invocation examples (paths are operator-bound packet/proposal/approval files):

```powershell
# PROFILE PREPARATION ONLY on Runtime after packet verification; no owner starts.
& $RuntimePython "$PacketRoot\scripts\cci_v2_release_profiles.py" prepare scanner --release repair --output $ProposedScanner
& $RuntimePython "$PacketRoot\scripts\cci_v2_release_profiles.py" prepare listener --release repair --output $ProposedListener
# After approved stop/install/readiness checks; each call starts only its one role.
& $RuntimePython "$PacketRoot\scripts\cci_v2_release_profiles.py" start scanner --release repair --output $ProposedScanner --approval $ApprovedPlan
& $RuntimePython "$PacketRoot\scripts\cci_v2_release_profiles.py" start listener --release repair --output $ProposedListener --approval $ApprovedPlan
# Rollback preparation/start uses --release rollback and separately reviewed files.
```

## CMC separate activation boundary

This code release retains **CMC_MODE=off / MARKET_CAP_PROVIDER=coinpaprika**.
The prior 500/day / 15,000-month cap proposal and RUNTIME_SHADOW profile (433/day
before retries) are for a future separately approved activation, not current settings.
Dormant Runtime .env limits remain 400/day / 12,000/month until explicitly approved
activation replaces the effective overrides. No .env or launcher was changed.

The existing proposed BTC/ETH/SOL/XRP manifest remains owner-unapproved; it records
real DEV capture times, exact provider IDs, Binance identities and source hashes.
Reacquire stale mapping/registry/market evidence, review exact IDs/units/categories,
and obtain real owner approval with correct timestamps/expiry before asset joins.
Do not invent current IDs or derivatives units. Verify actual Runtime credential,
plan/credits and endpoint limits within an approved budget, use the separate sidecar,
remove the effective OFF override only in that later stage, and prove real native
API acquisition plus scanner/candidate persistence then. No extra CMC request was
made during this finalization. CMC cannot generate or approve setups, change market
structure/geometry/RR, promote lifecycle, weaken gates or switch ranking authority.

## Remaining limits and approval boundary

DEV R1 and Python 3.12 blockers are closed by the recorded tests. CI/remote objects
and artifact hashes are evaluated against the external final delivery manifest.
Current raw Runtime profiles were not copied to DEV; their hash-bound derivation
must succeed on Runtime before startup. Execution-policy/signing acceptance, current
owners, live persistence/feeds/resource performance and natural deliveries require
later operator verification. No physical RAM was added, paging cause was not proved,
and no Runtime performance improvement or deployed success is claimed.

Stop here for owner review. No automatic merge, deployment, CMC activation, public
message, order execution, transfer or withdrawal is part of this finalization.
