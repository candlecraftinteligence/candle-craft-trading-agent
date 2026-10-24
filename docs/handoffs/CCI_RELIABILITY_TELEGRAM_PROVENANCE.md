# CCI reliability: Telegram provenance handoff

Date: 2026-10-23. State: **CODE_COMPLETE**, pending owner integration/release and
natural Runtime verification.
No merge, deployment, historical resend or Runtime access authorization.

- Branch: `codex/reliability-telegram-provenance`.
- PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/143.
- Stacked base: `e4a5fb9693ce78c9194d32dcac0231b109671c74` (PR #142).
- Implementation/correction head: `29056e76c315385fdb5cbb379ea0155434fd1e95`.
- Commits: `82750b5` (implementation), `29056e7` (source-reference repair).
- The final documentation-only head is recorded in the PR after publication and
  receives its own exact-head CI and Astra review.

## Problem and correction

Recovery omitted unresolved reservation provenance from run evidence and could
repeat the same missing-attempt warning every cycle. The attempt loader has an
event-key fallback; it is not canonical reservation authority. Existing outbox
guards already fail closed.

A small read-only collector classifies known public event families in PENDING,
RETRYABLE and IN_FLIGHT states. It retains event keys, source update timestamps,
canonical pointers and separate ownership/reservation reasons. Diagnostics stay
separate from delivery attempts; unresolved events do not inflate attempt/receipt
counts. Recovery checks canonical association before using the existing loader.

Default inspection is 256 rows in pages of 64, under an initial upper event id;
at most 50 issue details are retained. Incomplete coverage is explicit and counts
are lower bounds. The diagnostic prefix cannot limit the independent send recovery
query. A persistent large backlog can leave newer diagnostic rows uninspected.
Warnings are capped at ten per inspection, with a 1024-fingerprint process cache.
Eviction/restart can permit repeats within that budget; current evidence remains.

Reports persist in scan JSON, SQLite scan payloads, manifests and watch summaries.
Unresolved/incomplete provenance makes the Telegram phase PARTIAL. Disabled is
NOT_REQUESTED, unavailable counts are N/A, and failed collection is not healthy
zero. One-shot exceptions write diagnostic-only partial manifests before failing;
failed persistence preserves both errors. Fatal watch summaries retain unavailable
coverage. UNRESOLVED_NO_SEND is a diagnostic disposition, not a database state.

## Verification

- Expanded canonical/recovery/quality/watch/command subset: 309 passed.
- Targeted provenance/status/failure subset: 41 passed. Corrected inventory and
  provenance subset: 58 passed.
- Independent Astra APPROVE on exact `29056e7`; existing anchor and exact-artifact
  tests independently passed. Implementation `82750b5` was independently approved
  with 59 passing targeted tests.
- Required CI on `29056e7`: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/39986875741
  succeeded. Full existing-environment Windows pytest on the same head:
  **3429 passed, 1 existing dependency warning in 1624.99 s**, no failures/skips.
  The warning is FastAPI/Starlette TestClient's deprecated httpx adapter.
  This was slower than the prior full run; its cause is Unverified.
- Earlier Windows full run on `82750b5`: 3428 passed, one failure, one dependency
  warning in 784.67 s. CI had the same single stale inventory-anchor failure.
  Added imports moved two referenced declarations. Catalog references and their
  two deterministic artifacts were updated; existing checks were not weakened.
- Dependency, compile, whitespace and invariant-diff checks passed. No strategy,
  lifecycle, schema, public quality 88/A/3, atomic outbox/deduplication or P4 policy
  changes. No dependency changes or live transport calls.
- Bounded DEV CPU/size observations are documented in
  `docs/research/CCI_TELEGRAM_PROVENANCE_DIAGNOSTICS.md`; they exclude artifact
  writes and do not establish matched Runtime overhead.

## Risks and continuation

The ten historical pending events are individually Unverified; DEV has no current
Runtime rows or proven historical HTTP receipts. This correction diagnoses future
source observations without repairing, adopting or automatically resending history.
SENT/UNCERTAIN/terminal rows are outside its declared inspection scope. A passing
suite does not prove historical delivery or natural Runtime improvement.

Next: scanner continuity (A), using a shared OS lease and bounded durable session
observations before discovery and through cleanup. Detect missing completion and
shutdown observations without inferring crash causes or adding a restart service.
Then add low-overhead phase/resource observations (E). Reuse the existing capacity
policy; do not weaken its reserve or growth evidence requirements. The source
runbook still requires separate current Runtime measurements and release approval.

The historical 16h54m interval, nine uncovered owners, 73 continuity gaps and
20 missing confirmation boundaries remain limitations recorded in the objective
tracker. No historical timestamp, candle, outcome or receipt is reconstructed here.
