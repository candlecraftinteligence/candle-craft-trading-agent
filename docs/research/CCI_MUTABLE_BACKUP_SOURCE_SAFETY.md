# Mutable backup source safety

## Finding and scope

PR #146's cadence source passed 3552 Windows tests, but Linux CI twice failed
the unchanged snapshot-boundary test at quick_check (3551 passed / 1 failed;
288.28 and 340.83 seconds). The logs did not retain raw integrity rows or the
linked SQLite version. The exact cause of those CI integrity failures remains
Unverified; no check is waived and no Runtime backup is inferred to be corrupt.

Independent synthetic instrumentation did establish a pre-existing unsafe
connection setting: after the last writer closes and WAL/SHM disappear, actual
backup acquisition used the inspection reader's default immutable=1. Another
writer could resume while that reader and its apparent transaction were active.
[SQLite's immutable contract](https://www.sqlite.org/uri.html#uriimmutable)
disables locking and change detection; changing the file can cause incorrect
results or corruption errors. Absence of sidecars cannot establish quiescence.
Three instrumented DEV runs passed with that unsafe setting on SQLite 3.45.1;
passing examples do not validate the assumption.

## Narrow correction

Actual backup acquisition passes assume_immutable_when_sidecars_absent=False.
Keep mode=ro, verified query_only, the existing read transaction, online backup,
schema/count/foreign-key/quick_check verification, checksums and no-overwrite
promotion. The source cannot be written through that connection. Ordinary SQLite
coordination can create WAL/SHM; they are never deleted to simulate no changes.
Completed-snapshot verification and generic reader defaults are unchanged.

The source connection's safety proof and real later-writer snapshot behavior
are tested with both initially absent sidecars and an already-open WAL writer.
Before the correction, the no-sidecars regression observed immutable_requested
True and failed; the existing-WAL case and dry run passed (1 failed / 2 passed).
The first test draft lacked the opt-in immutable proof; instrumentation was
corrected to observe the actual setting without changing its value.
After correction the complete maintenance subset passed **61 tests in 14.99
seconds**. The combined maintenance/readiness/cadence subset passed **162 tests
in 29.21 seconds**; dependency, compilation and whitespace checks passed.
Exact combined source `0c01aa8a4213d7980fc3ccc898791230ff68aab0` received Astra
APPROVE, with **61 independent maintenance tests passed in 15.65 seconds** and
no actionable findings. [Corrected-head CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40063440798)
succeeded: **3553 passed, 1 existing dependency warning in 221.43 seconds**,
no failures or skips. Full Windows pytest on the same exact source: **3553
passed, 1 existing dependency warning in 646.33 seconds**, no failures or skips.
The warning is the existing Starlette/httpx TestClient deprecation. The earlier
CI failure is cleared on this corrected candidate; its raw mechanism remains
Unverified because the original integrity rows were not retained.

## Retained planning limitation and operational boundary

plan_backup and dry_run retain their no-artifact inspection behavior. Switching
their SQLite open blindly to non-immutable would create WAL/SHM even on a read,
as an independent synthetic experiment confirmed. The strengthened dry-run test
checks source size, mtime, hash and sidecar presence during planning and afterward.

Planning still uses an immutable fallback and therefore requires independently
established source quiescence. This is an explicit retained limitation, not a
live-consistency proof or an automatically enforced prerequisite. Do not run
planning against a changing source with absent sidecars. Planning estimates are
not deployment/capacity approval. A fully concurrent, no-artifact planner would
need a separately reviewed provisional filesystem/header contract and actual
transaction revalidation; it is outside this narrow acquisition correction.

Any Runtime backup/readiness/recovery procedure needs separate authorization,
verified identities, quiescence for planning, capacity and preservation safeguards.
This DEV patch does not authorize production access, stopping a service,
checkpoint/VACUUM, deleting sidecars or performing a backup. All fixtures are
synthetic. Schema 27, trading/public policy, lifecycle outcomes and P4 remain
unchanged. No historical receipt or outcome is reconstructed.
