# F06 — Runtime disk and release readiness

## Phase

F06_RUNTIME_DISK_READINESS

This phase makes the existing Runtime checkpoint collector and assessor suitable for preparing the next actual Runtime release packet. It does not clean historical data, add automatic retention, prune, or `VACUUM`, and it does not claim a current Runtime capacity result.

Application schema remains 26. Public quality remains 88 / grade A / RR 3. P5A monitoring and immutable progress, F03 population isolation, F04 replay semantics with capture disabled by default, and F05 strict discovery failure handling are unchanged. `go_for_runtime_deployment` stays false.

No merge. No deployment. Independent acceptance is not claimed.

## Problem and contract

On the pre-F06 readiness tool, a manufactured schema-26 packet could receive `PACKET_REVIEWABLE_NOT_AUTHORIZED` with an empty index inventory, no proof of `ix_lifecycle_records_epoch_locked_plan_state`, and no target migration rehearsal. Removing `restore_evidence.integrity_ok` did not make that packet incomplete. A positive `growth_budget_bytes` did not need a workload baseline or planning horizon. The runbook still described a schema-25 restore target.

Those are packet-completeness gaps. They are not a previously granted operational GO. The assessor already kept deployment authorization false, and this repair preserves that boundary.

The packet contract is now explicit: `f06-runtime-release-readiness-v1`, reported as tool version `cci-runtime-checkpoint-readiness-v2`. An older or missing contract does not inherit the new prerequisites. Missing evidence stays incomplete. Explicit bad evidence stays adverse. Declared assertions stay distinguishable from referenced results. This tool is not a remote attestation system. A valid-looking SHA, a file name, `restore_tested=true`, or a reported successful deployment does not complete missing evidence and does not prove review, CI, or the code that is executing.

## Start condition

Remote `main` at the start of this phase was `011b9e2fc8a5c93578acceafe5505774537866df`, the merge of PR #129. That merge has no file differences from accepted F05 head `9b79fb4773d5c6cb73dec275336a7ed0dc62cfea`. No open F06 pull request and no F06 branch existed. The local `main` checkout was clean and behind that commit; it was fast-forwarded onto it. No intervening commits were present. No unrelated work was discarded.

Adam reports the recent Runtime rollout complete. That report is user-provided status. It was not used to fill packet fields. This phase did not repeat a Runtime stop, update, or restart, and it did not modify a running Runtime installation.

## Implementation plan (executed)

1. Branch `fix/f06-runtime-disk-readiness` from `011b9e2fc8a5c93578acceafe5505774537866df`.
2. Keep one collector and one assessor. Do not add a second deployment framework.
3. Distinguish the historical functional anchor, the collector checkout, the running application identity, and the proposed release SHA. Leave the anchor at `2bac6b4eda271bc69f2c34f42d6b7592e37fa8cc`.
4. Record schema-26 epoch/lineage table metadata and the plan-state index definition from SQLite metadata. A matching index name with a different table, column order, uniqueness, or partial predicate is not compatibility.
5. Treat a missing source index as an expected preflight finding. Require an isolated-copy rehearsal that proves migration to schema 26 and the expected index before the packet is reviewable. Require final cutover re-verification before consumers start. Do not migrate, initialize, create an epoch, install an index, or backfill lineage from collect or assess.
6. Require explicit restore integrity. Missing or unknown fails incomplete. `false` is adverse. Require distinct source identity, verified snapshot identity, restored-copy path, and observation time.
7. Require preservation checks, measured restore and migration duration, peak allocation, and downtime and recovery budgets.
8. Trace `growth_budget_bytes` to a representative baseline of at least 24 hours and a planning horizon of at least 10 days. Reject idle, failed, missing, incomparable, or short samples, and reject cleanup or compression shortcuts.
9. Add durable source replay as an optional separate consumer. The first rollout requires `SOURCE_REPLAY_CAPTURE_ENABLED` disabled. Unknown is not disabled. Enabled is outside this packet.
10. Preserve bounded collection: explicit paths, filesystem-only mode, read-only consistent snapshot, no live default, short busy timeout and query deadline, no `immutable=1` fallback, indexed fixed-limit samples only, no sidecar repair, no live `COUNT` / `dbstat` / integrity scan / checkpoint / migration, no secret dump, and no output overwrite.
11. Correct the runbook for schema 26 and the accepted P5A / F03 / F04 / F05 foundation.
12. Run focused tests, related regressions, full pytest, compilation, and `git diff --check`.

## Final changes

- `app/storage/runtime_checkpoint_readiness.py`
  - Tool version `cci-runtime-checkpoint-readiness-v2`.
  - Packet contract `f06-runtime-release-readiness-v1`.
  - Collector reports `plan_state_index`, `epoch_lineage`, and a static `release_contract`. It still does not open a live path by default and does not write the database.
  - Assessor requires the new prerequisites and still returns `go_for_runtime_deployment: false`.
  - Consumer inventory includes `durable_source_replay`. Maintenance notes state that the historical opener and backup helper are not automatically safe for a changing WAL database with absent sidecars.
- `tests/test_runtime_checkpoint_readiness.py`
  - Positive synthetic packets now carry classified synthetic facts.
  - The previous incomplete schema-26 shape is no longer reviewable.
- `docs/operations/runtime_checkpoint_readiness.md`
  - Schema-26 target, index DDL, source-versus-target rehearsal, F04 capture switch, restore identity, 24-hour baseline, 10-day runway, and quiescent backup limits.
  - The historical anchor is not rewritten to imply approval of later `main`.

The capture-switch field on the packet is `capture_switch`. The key name avoids the existing secret redactor fragment `environ`, which would otherwise drop `environment_switch` and hide the operator's statement. The required value remains `SOURCE_REPLAY_CAPTURE_ENABLED`.

## Compatibility and report changes

| Item | Previous report | This report |
| --- | --- | --- |
| Tool version | `cci-runtime-checkpoint-readiness-v1` | `cci-runtime-checkpoint-readiness-v2` |
| Packet contract | implicit | `contract_version` must be `f06-runtime-release-readiness-v1` |
| Schema target | code already used 26; runbook still said v25 | schema 26 in code and runbook |
| Plan-state index | name inventory only | metadata definition, or an explicit absence finding |
| Restore integrity | only `false` was adverse | missing or unknown is incomplete; `false` is adverse |
| Growth budget | any positive integer | positive integer plus representative baseline and horizon >= 10 days |
| Source replay | not in the inventory | optional consumer; initial cutover requires the real switch disabled |
| Authorization | always false | always false |

Evidence timestamps used for restore, rehearsal, and baseline end must be at or before `operator.evidence_as_of_utc` and no older than 7 days. That window is this packet's staleness rule. It is not a Runtime measurement. Future timestamps are contradictory. Observations exactly 7 days old remain fresh; older observations are `stale_evidence`.

`PACKET_REVIEWABLE_NOT_AUTHORIZED` means the supplied packet contains the classified facts this contract asks for and none of them are adverse. It does not mean Runtime may be deployed, and it does not mean this tool observed Runtime.

## Bounded safety properties

- Collect and assess do not deploy, migrate, checkpoint, vacuum, create an epoch, install an index, backfill lineage, or authorize cutover.
- An explicit existing source file is required. There is no live-database default.
- Read-only mode uses a consistent snapshot, `assume_immutable_when_sidecars_absent=false`, and refuses `immutable=1`.
- WAL without SHM, SHM without WAL, and a WAL header without sidecars stay unavailable. Sidecars are not created or deleted.
- Busy timeout default remains 250 ms. Query deadline default remains 2000 ms.
- Recent-run sampling still requires `ix_scan_runs_timestamp` and a proven plan.
- Output creation still refuses an existing path.
- Secret-like keys are still removed from written reports.
- Shared and distinct volume accounting, the reserve floor `max(10 GiB, 10% of volume)`, and contradictory topology behavior are unchanged.
- Existing occupancy is not charged twice. Freelist bytes are not subtracted. Unperformed cleanup and compression ratios are adverse if supplied as capacity.
- Schema remains 26. Quality gates, RR, lifecycle semantics, and F05 cleanup were not edited.

## Synthetic adversarial results

All of these used temporary DEV databases or in-memory packets. None are Runtime measurements.

| Case | Disposition |
| --- | --- |
| Prior schema-26 packet, empty index inventory, no rehearsal, integrity omitted, positive growth without a baseline | `INCOMPLETE_PREREQUISITES` |
| Integrity omitted or unknown on an otherwise complete packet | incomplete |
| Integrity `false` | `ADVERSE_MEASURED_RESULT` |
| Index name only, definition not inspected | incomplete `source_plan_state_index_definition_unproven` |
| Wrong source or target index definition | adverse definition mismatch |
| Schema 26 without the index, rehearsal not migrated to 26 | incomplete |
| Schema 26 without the source index, verified target rehearsal | reviewable, not authorized; absence stays a finding |
| Schema 24 source with verified schema-26 rehearsal | reviewable, not authorized |
| Schema 24 source whose rehearsal stops at schema 25 | incomplete |
| Positive growth without a baseline | incomplete |
| Baseline shorter than 24 hours, idle, incomparable, or horizon of 9 days | incomplete |
| Capture enabled | adverse, outside the initial-cutover contract |
| Capture unknown or a different switch name | incomplete |
| Fresh classified synthetic packet | reviewable, not authorized; origin remains `synthetic` |
| Stale restore timestamp | incomplete |
| Future rehearsal timestamp, mismatched source identity, or rewritten anchor | adverse |
| Cleanup reclaim, compression ratio, or freelist subtraction | adverse |
| Reported successful deployment with missing integrity | still incomplete |
| Shared volume, distinct volumes, mixed topology, reserve floor, missing topology | previous failure and success assertions still hold |
| Missing or unsafe WAL/SHM, read-only deadline, writer coexistence, secret redaction, exclusive output | previous refusals still hold |
| Collector on a schema-26 file after the index is dropped or redefined | does not recreate or repair the index |

A reviewable result in this table is `PACKET_REVIEWABLE_NOT_AUTHORIZED` with `go_for_runtime_deployment` false.

## Commands and counts

DEV environment for these commands: `LOCAL_MANUAL_MODE=true`, `ORDER_EXECUTION_ENABLED=false`, `TELEGRAM_DRY_RUN=true`, `TELEGRAM_SIGNALS_ENABLED=false`, `SOURCE_REPLAY_CAPTURE_ENABLED=false`.

```text
python -m pytest tests/test_runtime_checkpoint_readiness.py -q --tb=line
```

49 passed.

```text
python -m compileall -q app scripts src tests
python -m pytest tests/test_p5a_active_owner_monitoring.py tests/test_f03_prospective_research_population.py tests/test_f04_durable_source_replay.py tests/test_f05_discovery_fail_closed.py tests/test_f05_source_cleanup.py tests/test_storage_database.py tests/test_runtime_checkpoint_readiness.py -q --tb=line
```

Compilation exited 0. Related collection was 218 tests: readiness 49, P5A 25, F03 18, F04 59, F05 discovery 9, F05 source cleanup 22, storage database 36. The run exited 0.

```text
python -m pytest -q --tb=line
git diff --check
```

Full local suite: 2893 passed, exit 0, one existing Starlette/`httpx` deprecation warning, about 630 seconds. `git diff --check` exited 0.

## Unchanged gates and schema

- `SCHEMA_VERSION` remains 26.
- Public quality remains 88, grade A, minimum RR 3. No setup-quality, structural-confirmation, or RR gate was edited.
- Lifecycle semantics were not edited.
- F05 owned source-client cleanup, primary error diagnostics, and cancellation propagation were not edited.
- F04 capture remains off unless `SOURCE_REPLAY_CAPTURE_ENABLED=true` and `SOURCE_REPLAY_EVIDENCE_PATH` are both set. This phase did not enable capture.
- No order execution, withdrawal, transfer, live Telegram send, scanner watch loop, or listener was added or started.

## Delivery

- Branch: `fix/f06-runtime-disk-readiness`
- Base SHA: `011b9e2fc8a5c93578acceafe5505774537866df`
- Code and runbook SHA: `73d06c293979a59637bc3214ef78ab929d0aa4ce`
- Handoff SHA: `8d90af69da289212882295d29e2e2a3777946748`
- The documentation commit that records the verified handoff CI below is the branch tip after that commit. Its pull-request check is the CI result for the exact final head.
- Draft PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/130
- Schema version: remains 26
- Runtime database: not opened, copied, or modified

Code and runbook CI:

- Run: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/37282100327
- Attempt: 1
- Result: success
- Head SHA: `73d06c293979a59637bc3214ef78ab929d0aa4ce`
- Job: Python 3.11 tests, ID `111672306409`
- Log result: `2893 passed, 1 warning in 228.52s (0:03:48)`

Handoff CI:

- Run: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/37282720488
- Attempt: 1
- Result: success
- Head SHA: `8d90af69da289212882295d29e2e2a3777946748`
- Job: Python 3.11 tests, ID `111674296811`
- Log result: `2893 passed, 1 warning in 208.55s (0:03:28)`

Acceptance is not claimed. Adam owns merge and any later Runtime procedure.

## Limits

- The assessor classifies supplied JSON. It does not attest review, CI, process identity, restore success, or disk capacity.
- Synthetic fixtures are labeled `synthetic`. They are not Runtime measurements.
- Historical ~81 GiB figures remain historical. They were not reused as current capacity.
- The 7-day evidence age rule is a packet freshness bound, not an observed Runtime clock.
- Quiescent backup is still the historical maintenance helper. This phase does not add a new snapshot implementation.
- `CREATE INDEX IF NOT EXISTS` cannot repair a same-named index with the wrong definition. That case is adverse.
- Final cutover re-verification is required by the packet and is not marked complete by a reviewable preparation packet.
- Repository handoffs for P5A, F03, F04, and F05 may still contain historical deferred wording from their implementation dates. Their merges above are the accepted foundation. This document does not reopen those phases.

## Operational actions

None on Runtime. No merge, no deployment, no cleanup, no retention job, no listener, no watch loop, no network scan, no Telegram send, no order, and no live database access.

DEV actions were a fast-forward of local `main` to the verified F05 merge, this feature branch, local tests, a draft pull request, and CI.

## Runtime facts still needed

A later Runtime procedure, not this DEV packet, still has to supply measured evidence for the machine that will actually run:

- Running process identity, interpreter, and code revision, separate from the collector checkout and from the proposed release SHA.
- Reviewed release SHA and its successful CI, as referenced evidence rather than a bare SHA.
- Verified source path, snapshot identity, and isolated restored-copy path.
- Source schema and whether `ix_lifecycle_records_epoch_locked_plan_state` is absent, matched, or a wrong definition.
- Rehearsal on that actual source schema proving schema 26, the expected index, epoch/lineage tables, and the preservation checks.
- Measured restore duration, migration duration, peak allocation, downtime budget, and recovery budget.
- At least 24 hours of representative Runtime workload baseline and at least ten days of growth runway.
- Per-volume free and total bytes, with backup and restore topology that is not inferred from drive letters or role names.
- `SOURCE_REPLAY_CAPTURE_ENABLED` observed disabled for the initial cutover.
- Final verification of schema 26 and the index again before consumers start.

Until those facts exist and Adam authorizes that concrete package, operational status remains `RUNTIME_CHECKPOINT_NOT_YET_AUTHORIZED`.
