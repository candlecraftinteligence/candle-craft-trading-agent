# CCI Runtime deployment handoff from DEV

Date: 2026-10-24. Scope: completed DEV integration and handoff only.
Runtime authorization and execution belong to the user and Codex running directly
on the separate Runtime PC. There is no connection from this DEV session to
Runtime. Do not discover, establish, simulate or request one.
DEV verdict: **DEV_INTEGRATION_COMPLETE_RUNTIME_HANDOFF_READY**. No remaining
DEV code integration defect was found. Open documentation PR #147 and its merge
approval do not change the already integrated code target or require Runtime work
to complete this DEV task.

## Exact code target and integration evidence

- Repository: https://github.com/candlecraftinteligence/candle-craft-trading-agent.
- Final verified main and deployment target:
  **`6785cf6c5a5d4c083f31a280c2ca1555bc7df88c`**.
- Complete tree: `e98bf4dd11f20da8374fd54cf9537acf7a02f1cb`, identical to accepted
  candidate `637191f475ee834130c0ae0c88a74e7916812dc7`.
- PRs #141-146 are merged with all accepted commits preserved. They implement
  owner-monitoring accounting, honest execution-evidence strata, bounded Telegram
  provenance diagnostics, a single-watch OS lease/session record, bounded
  resource/phase observations, and provenance-only retry-cadence/backup safety.
- Exact-main [push CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40070934655):
  **3553 passed, 1 existing dependency warning in 280.94 seconds**, no failures
  or skips; dependency verification and compilation passed. Identical source
  passed full Windows pytest: **3553 passed, 1 warning in 646.33 seconds**.
  The warning is the existing Starlette/httpx TestClient deprecation.
- Final DEV safeguard audit: **108 passed in 65.96 seconds** (configuration,
  public quality, confirmation receipts, candle freshness and P4 source truth).
  Corresponding policy, identity, schema and strategy code is unchanged from
  the original integration base; no threshold or safeguard was weakened.
- Independent Astra APPROVE for the exact integrated main. Every accepted head
  is an ancestor; every merge tree matches its accepted head. See
  `CCI_RELIABILITY_INTEGRATION_RESULT.md` for the six merge SHAs.
- PR #147 contains documentation, not additional runtime code. It remains
  subject to its own review/merge approval. This handoff is usable without
  merging #147; do not deploy its documentation branch in place of the target.

## Compatibility and configuration prerequisites

1. Verify locally that the Runtime host is `DESKTOP-G7M42AL`. The reported code
   path is `C:\Users\aspir\Desktop\Candle Craft Inteligence`; independently
   confirm it and every scanner/listener/consumer's code, database and state path.
   Prove the running release from process evidence, not checkout SHA alone.
2. This release keeps **schema 27** and adds **no database migration**, required
   package change or required configuration key. Verify actual schema and exact
   required indexes before starting writable consumers. Older or incompatible
   schemas require their own approved migration procedure; do not let startup
   silently decide compatibility or treat the historical schema-26 packet as proof.
3. Keep execution disabled, public RR 3.0 / quality 88 / grade A, confirmation,
   economic/lifecycle identity, freshness/integrity and outbox deduplication.
   P4 stays disabled and non-enforcing. Source replay capture must be explicitly
   off for initial cutover. Preserve the owner's existing public-delivery policy;
   this handoff authorizes no configuration change or historical resend.
4. Watch mode automatically adds `<database>.watch.lock`,
   `<database>.watch-session.json` and `<database>.watch-observations.jsonl` beside
   the canonical database. Verify parent-directory permissions, OS locking and
   atomic replacement on the actual filesystem without starting a second scanner.
   New receipt writes are bounded to 32 KiB; journal appends have a 32 MiB budget.
   Existing larger journals are preserved with further appends suspended, never
   shrunk. Exhaustion is reported as incomplete history. Validate recognized
   lease/journal headers and any existing receipt; malformed existing files fail
   closed. A missing journal fails closed when its establishment is known.
   Missing lock markers may be created and absent receipts treated as no prior
   receipt, so arbitrary sidecar loss is not always detected. Preserve all three
   sidecars for operator review. Never break a lease by PID/age.
5. Verify current RAM/RSS and storage on C: and actual database/recovery volumes.
   Preserve the existing per-volume reserve floor (max 10 GiB / 10%), restore
   budget and ten-day growth horizon, with two comparable points at least 24 hours
   apart. Include every artifact copy in growth estimates. DEV benchmarks and
   historical Runtime capacity are not current capacity approval. No cleanup of
   databases, WAL/SHM, recovery assets, research evidence, logs or offsets.
6. Fix a separately approved recovery/restore rehearsal, epoch lineage,
   confirmation-consumption receipts and external listener-state preservation.
   The backup planner still requires independently established source quiescence;
   absent sidecars do not prove it. Actual backup read coordination can create
   WAL/SHM. Do not delete them or treat a changing database as immutable.
7. Before rollout approval, pin all consumer paths, the exact target SHA, existing
   listener ownership, recovery steps, public-delivery settings and numeric
   observation/stop budgets. A rollback must preserve new sidecars and evidence.

## Runtime Codex sequence, requiring its own authorization

First obtain the user's local Runtime preflight authorization. Start with bounded
host/process/release/path/resource and filesystem-only evidence. Existing
`scripts/runtime_checkpoint_readiness.py collect --filesystem-only` requires an
explicit verified source path and a new output path; it opens no SQLite connection.
Do not run it on DEV or substitute a guessed live path.

Plain `collect` opens SQLite metadata even without recent-run sampling. Any such
work needs its applicable Runtime authorization and safeguards. Optional indexed
sampling uses the established 250 ms busy budget, 2000 ms deadline and five-row
limit with query-plan proof. Do not substitute table counts, integrity scans,
dbstat, checkpoints or VACUUM. The collector never grants deployment approval.

After prerequisites are proven, present a concrete local cutover/recovery plan
for separate owner approval. Only an approved Runtime session may deploy the
exact target or control existing services. Verify all consumers load that release
and that scanner/listener instances never overlap. Do not merge DEV branches or
develop production fixes in the Runtime checkout.

After an approved rollout, collect a comparable natural 48-hour window with UTC
endpoints and loaded release identity. Reconcile starts/finalizations/missed slots;
account for every current owner; preserve historical uncertainty; classify the
ten old pending follow-ups read-only without resend; compare phase/resource and
artifact-growth observations. Never manufacture candles, receipts or outcomes.
The historical outage cause and individual historical cases remain Unverified.

## Copyable brief for the separate Runtime Codex session

> Work locally on the Runtime PC only under the user's separate authorization.
> The DEV task is complete; there is no DEV-to-Runtime connection to arrange.
> Deployable code target is 6785cf6c5a5d4c083f31a280c2ca1555bc7df88c,
> tree e98bf4dd11f20da8374fd54cf9537acf7a02f1cb. PRs #141-146 are integrated,
> exact-main CI passed 3553 tests and Astra approved the exact main SHA.
> Start with authorized local identity/resource/filesystem evidence, then verify
> schema 27/index/consumer compatibility and recovery safeguards before proposing
> cutover. This handoff grants no deployment, database mutation, service control,
> cleanup, event repair or resend permission. Keep execution disabled, 88/A/3,
> confirmation/identity/integrity/deduplication, disabled P4 and source capture off.
> Preserve the three watch sidecars, confirm only one scanner/listener, and obtain
> separate approval for the concrete rollout. Natural Runtime proof follows rollout.
