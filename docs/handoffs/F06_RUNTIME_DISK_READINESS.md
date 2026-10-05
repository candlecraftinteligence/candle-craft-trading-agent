# F06 — Runtime disk and release readiness

## Phase

F06_RUNTIME_DISK_READINESS

This phase makes the existing Runtime checkpoint collector and assessor suitable for preparing the next actual Runtime release packet. It does not clean historical data, add automatic retention, prune, or `VACUUM`, and it does not claim a current Runtime capacity result.

Application schema remains 26. Public quality remains 88 / grade A / RR 3. P5A monitoring and immutable progress, F03 population isolation, F04 replay semantics with capture disabled by default, and F05 strict discovery failure handling are unchanged. `go_for_runtime_deployment` stays false on every assessment.

No merge. No deployment. Independent acceptance is not claimed. A reviewable packet is not Runtime GO.

## Problem and contract

On the pre-F06 readiness tool, a manufactured schema-26 packet could receive `PACKET_REVIEWABLE_NOT_AUTHORIZED` with an empty index inventory, no proof of `ix_lifecycle_records_epoch_locked_plan_state`, and no target migration rehearsal. Removing `restore_evidence.integrity_ok` did not make that packet incomplete. A positive `growth_budget_bytes` did not need a workload baseline or planning horizon. The runbook still described a schema-25 restore target.

Independent review of `fd749c4ba0ccf1490d0521d226481cf6ea88039f` returned REQUEST_CHANGES. Green CI on that head (run 37283279346, attempt 1, 2893 passed) did not close the findings. The probes had 13 failures and one passing published control, in three groups:

- F06-R1. `plan_state_index_matches` ignored `name` and `partial`. A fully shaped target index with another name, a missing name, `partial` false, or a missing partial flag could still be `matched`.
- F06-R2. Samples and `growth_derivation` were optional. The published baseline had no observations. `growth_budget_bytes=1` with `planning_horizon_days=365`, and a sample that only had `quality` and `comparable`, still looked reviewable.
- F06-R3. Nested type errors raised `TypeError` for `operator.evidence_origin=[]`, a source epoch `columns=1`, a target epoch column value `1`, a preservation value `{}`, and a sample `quality=[]`. CLI `assess` did not catch that error, so it wrote no assessment.

Independent review of `cdfcd12a580f6128a95e22e123e86e86dbde0e57` again returned REQUEST_CHANGES. The previous 13 failures passed. Green CI on that head (run 37292150234, attempt 1, 2897 passed) did not close the remaining source-metadata findings. A 39-case acceptance suite had 29 passes and 10 failures:

- F06-R4. A supplied `plan_state_index` of `[]`, `1`, or `"absent"` fell through the empty index inventory and was classified as absence. The canonical absence record with `columns=1` or `partial="false"` was also accepted, because those types did not fail the absence check. Each packet was reviewable.
- F06-R5. Source epoch `present` values `[]`, `{}`, `1`, and `"false"` were treated as an absent table. The packet stayed reviewable, and CLI `assess` wrote a reviewable assessment for `present="false"`.

The packet contract for this repair is `f06-runtime-release-readiness-v2`, reported as tool version `cci-runtime-checkpoint-readiness-v3`. An older or missing contract does not inherit the new prerequisites. Missing or mistyped evidence stays incomplete. A well-typed contradiction stays adverse. If any adverse fact is present, the disposition is `ADVERSE_MEASURED_RESULT` even when other fields are also missing. Declared assertions stay distinguishable from referenced results. `tool_attested` stays false. This tool is not a remote attestation system. A valid-looking SHA, a file name, `restore_tested=true`, or a reported successful deployment does not complete missing evidence and does not prove review, CI, or the code that is executing.

## Start condition

Remote `main` at the start of F06 was `011b9e2fc8a5c93578acceafe5505774537866df`, the merge of PR #129. That merge has no file differences from accepted F05 head `9b79fb4773d5c6cb73dec275336a7ed0dc62cfea`.

The corrective work stays on branch `fix/f06-runtime-disk-readiness` and draft PR #130. It starts from rejected head `fd749c4ba0ccf1490d0521d226481cf6ea88039f`. No second pull request was opened. The branch was not reset.

The earlier handoff said Adam reported the Runtime rollout complete. That sentence is withdrawn. The Runtime PDF supplied after that claim records the earlier full-copy plan as `ROLLOUT_BLOCKED_BEFORE_MUTATION`, before mutation. Adam later authorized a separate code-only Runtime update despite that storage blocker. This review chat has no execution evidence for that code-only update. The exception does not turn missing F06 packet evidence into measured capacity, and this repair does not put the old full-copy reserve gate back in front of it. This DEV work did not stop, update, restart, or modify a running Runtime installation, and it did not open the live Runtime database.

## Implementation plan (executed)

1. Keep branch `fix/f06-runtime-disk-readiness` and PR #130. Do not add a second deployment framework or a remote attestation system.
2. F06-R1. Classify a supplied plan-state index as matched, incomplete, or mismatch. Require the exact name `ix_lifecycle_records_epoch_locked_plan_state`, table `setup_lifecycle_records`, column order `runtime_epoch_id`, `current_state`, `lifecycle_id`, `unique` false, `partial` true, and predicate normalized to `plan_version_id is not null`. Missing or mistyped fields are incomplete. A well-typed wrong name, table, column order, uniqueness, partial flag, or predicate is adverse, and a mismatch wins when other fields are also missing. Apply the same rules to source metadata and target rehearsal. An explicit source absence plus a valid target rehearsal stays a finding. A name-only inventory entry stays unproven. Do not install or repair an index.
3. F06-R2. Require at least two timestamped comparable representative samples spanning at least 24 hours, `planning_horizon_days` of at least 10, and `growth_derivation` in bytes. A referenced result needs `evidence_reference` and the same numbers. Project with integer ceiling: `projected_bytes = ceil(observed_delta_bytes * horizon_seconds / observation_seconds)`, then `derived_budget_bytes = projected_bytes + allowance_bytes`. A positive budget below that derived value is adverse. Well-typed arithmetic that disagrees with the samples or the horizon is adverse. A non-positive delta, a flat trace, a shrinking trace, or an idle, failed, missing, or incomparable sample stays incomplete and does not establish zero growth or infinite runway. Do not invent a minimum growth rate: one observed byte over 86400 seconds and a 10-day horizon projects to 10 bytes. A budget of 10 meets that check. A budget of 1 does not. A horizon below 10 stays incomplete and is not compared as contradictory arithmetic. Non-positive `growth_budget_bytes` stays the existing incomplete capacity rule. Per-volume accounting is unchanged.
4. F06-R3. Validate nested types before membership, `list()`, or arithmetic. A non-string origin, a non-list column value, a non-string sample quality, and a preservation value other than `preserved`, `failed`, or false are incomplete. A well-typed wrong column list stays adverse. Do not add a blanket `TypeError` handler. CLI `assess` writes the structured assessment once assess no longer raises.
5. F06-R4. Distinguish an omitted index observation, the canonical absence record, and malformed supplied evidence. Canonical absence is the expected name, `status` `absent`, null table, uniqueness, partial flag, and predicate, and `columns` `[]`. A supplied non-object does not fall back to the index inventory. A mistyped field on an absence record is incomplete. A well-typed definition that is not that sentinel stays adverse. Name-only evidence stays unproven. Genuine canonical absence with a valid target rehearsal stays a finding.
6. F06-R5. Source epoch `present` is a strict boolean. `true` requires the expected columns. `false` is the permitted table-absence finding. Missing, null, string, number, and container markers are incomplete and are not coerced to false. Other tables are still checked, so a separate well-typed column mismatch stays adverse.
7. Preserve the historical functional anchor `2bac6b4eda271bc69f2c34f42d6b7592e37fa8cc`, schema 26, capture switch `SOURCE_REPLAY_CAPTURE_ENABLED`, and `go_for_runtime_deployment` false.
8. Update the runbook for the absence shape and the boolean presence marker.
9. Run focused tests, related regressions, compilation, full pytest, and `git diff --check`.

## Final contract

- Tool version `cci-runtime-checkpoint-readiness-v3`.
- Packet contract `f06-runtime-release-readiness-v2`.
- Required index, also forced by `INDEXED BY` in `app/lifecycle/repositories.py`:

```sql
CREATE INDEX IF NOT EXISTS ix_lifecycle_records_epoch_locked_plan_state
ON setup_lifecycle_records(runtime_epoch_id, current_state, lifecycle_id)
WHERE plan_version_id IS NOT NULL;
```

- `unique` must be boolean false. `partial` must be boolean true. The normalized predicate is `plan_version_id is not null`.
- Source absence is only the canonical record above. Any other supplied `plan_state_index` value is incomplete or adverse. An omitted observation is not that record, and an inventory cannot erase a supplied type error.
- Source epoch `present` must be boolean `true` or `false`. `false` may record table absence. Any other marker is incomplete.
- Growth unit is `bytes`. `horizon_seconds` must equal `planning_horizon_days * 86400` once the horizon is an integer of at least 10. Allowance is a non-negative integer. The ceiling is `(observed_delta_bytes * horizon_seconds + observation_seconds - 1) // observation_seconds` when delta, observation, and horizon are positive.
- Evidence class is `declared_assertion` or `referenced_result`. Both keep `tool_attested` false.
- The capture-switch field remains `capture_switch`. That name avoids the secret redactor fragment `environ`. The required value remains `SOURCE_REPLAY_CAPTURE_ENABLED`.
- Evidence timestamps for restore, rehearsal, and baseline end must be at or before `operator.evidence_as_of_utc` and no older than 7 days. Future timestamps are contradictory.

`PACKET_REVIEWABLE_NOT_AUTHORIZED` means the supplied packet contains the classified facts this contract asks for and none of them are adverse. It does not mean Runtime may be deployed, and it does not mean this tool observed Runtime.

## Files

- `app/storage/runtime_checkpoint_readiness.py` — exact index classification, canonical source absence, strict epoch presence, quantitative growth derivation, and type-safe nested validation. Collect still does not migrate, initialize, install an index, create an epoch, checkpoint, vacuum, delete history, or remediate.
- `tests/test_runtime_checkpoint_readiness.py` — the positive synthetic trace includes two samples and a derivation. The old label-only baseline remains a separate incomplete fixture. Cases cover index identity, growth arithmetic, malformed nested values, malformed source absence, epoch presence, and the CLI assess path.
- `docs/operations/runtime_checkpoint_readiness.md` — exact index name, canonical absence shape, boolean epoch presence, partial true, unique false, growth field names, ceiling formula, allowance, and malformed-evidence behavior.

## Findings and synthetic results

All of these used temporary DEV databases or in-memory packets. None are Runtime measurements. A reviewable result is `PACKET_REVIEWABLE_NOT_AUTHORIZED` with `go_for_runtime_deployment` false.

| Case | Disposition |
| --- | --- |
| Complete synthetic trace, matching collector index, matching rehearsal, derived budget inside the 8 GiB term | reviewable, not authorized; `tool_attested` false |
| Canonical source index absence, verified target rehearsal | reviewable, not authorized; absence stays a finding |
| Source `plan_state_index` supplied as `[]`, `1`, or `"absent"` | incomplete `source_plan_state_index.supplied_type`; not absence |
| Canonical absence record with `columns` `1` or `partial` `"false"` | incomplete field diagnostic; not absence |
| Same malformed index together with capture enabled | adverse; the type diagnostic remains listed |
| Source epoch `present` boolean `false` on every table | reviewable, not authorized; table absence stays a finding |
| Source epoch `present` `[]`, `{}`, `1`, `"false"`, null, or missing | incomplete `source_epoch_lineage.<table>.present` |
| Mistyped `present` together with a wrong column list on another table | adverse; the presence diagnostic remains listed |
| CLI `assess` of `present` `"false"` | incomplete assessment written, exit 0, token redacted, second write refused |
| Name-only index inventory | incomplete `source_plan_state_index_definition_unproven` |
| Wrong name, `partial` false, wrong table, wrong column order, wrong predicate, or `unique` true | adverse definition mismatch |
| Missing name or missing partial flag | incomplete, not adverse |
| Wrong same-named index collected from a synthetic file | adverse; collect does not repair it |
| Old label-only baseline, including budget 1 and horizon 365 | incomplete; no samples and no derivation |
| Labels-only samples | incomplete |
| Missing derivation | incomplete |
| Referenced derivation with the numbers and `evidence_reference` | reviewable, not authorized; `tool_attested` false |
| Referenced path without the numbers | incomplete |
| Positive budget below the derived budget | adverse `growth_budget_below_derived_requirement` |
| Budget equal to the derived budget | reviewable, not authorized |
| Contradictory `projected_bytes`, or horizon 365 left on a 10-day derivation with budget 1 | adverse |
| One observed byte over 24 hours, 10-day horizon, budget 10 | reviewable, not authorized |
| Same one-byte trace with budget 9 | adverse below the derived 10 bytes |
| Flat or shrinking samples | incomplete; not zero growth |
| Invalid sample timestamp, float bytes, or non-integer derivation field | incomplete |
| Baseline shorter than 24 hours, idle with growth 0, incomparable, or horizon of 9 days | incomplete |
| Stale baseline end | incomplete `stale_evidence` |
| `evidence_origin` `[]` | incomplete `operator.evidence_origin`; CLI writes the assessment, exit 0, token redacted, second write refused |
| Origin `[]` together with capture enabled | adverse; the missing origin remains listed |
| Source epoch `columns` integer | incomplete |
| Source epoch columns a wrong list of strings | adverse definition mismatch |
| Target epoch column value integer | incomplete |
| Preservation value `{}` | incomplete |
| Sample `quality` `[]` | incomplete |
| Prior schema-26 gaps: empty inventory, omitted integrity, integrity false, capture enabled, cleanup, compression, freelist, topology | previous incomplete and adverse assertions still hold |

## Commands and counts

DEV environment: `LOCAL_MANUAL_MODE=true`, `ORDER_EXECUTION_ENABLED=false`, `TELEGRAM_DRY_RUN=true`, `TELEGRAM_SIGNALS_ENABLED=false`, `SOURCE_REPLAY_CAPTURE_ENABLED=false`.

```text
python -m pytest tests/test_runtime_checkpoint_readiness.py --override-ini="addopts=" -q --tb=line
```

56 passed in 8.11s.

```text
python -m compileall -q app scripts src tests
python -m pytest --override-ini="addopts=" -q --tb=line tests/test_p5a_active_owner_monitoring.py tests/test_f03_prospective_research_population.py tests/test_f04_durable_source_replay.py tests/test_f05_discovery_fail_closed.py tests/test_f05_source_cleanup.py tests/test_storage_database.py tests/test_runtime_checkpoint_readiness.py
```

Compilation exited 0. Related collection was 225 passed in 54.13s: readiness 56, P5A 25, F03 18, F04 59, F05 discovery 9, F05 source cleanup 22, storage database 36.

```text
python -m pytest --override-ini="addopts=" -q --tb=line
git diff --check
```

Full local suite: 2900 passed, 1 warning, 478.15s (0:07:58), exit 0. The warning is the existing Starlette/`httpx` deprecation. `git diff --check` exited 0.

## Unchanged gates and schema

- `SCHEMA_VERSION` remains 26.
- Public quality remains 88, grade A, minimum RR 3. No setup-quality, structural-confirmation, or RR gate was edited.
- Lifecycle semantics were not edited.
- F05 owned source-client cleanup, primary error diagnostics, and cancellation propagation were not edited.
- F04 bounded replay and default capture remain disabled. This phase did not enable capture.
- P5A owner monitoring and immutable progress were not edited.
- No order execution, withdrawal, transfer, live Telegram send, scanner watch loop, or listener was added or started.
- Shared and distinct volume accounting and the reserve floor `max(10 GiB, 10% of volume)` are unchanged. The 8 GiB growth term in the synthetic capacity fixture is still an accounting input; the new check is that it must be at least the derived byte budget.

## Delivery

- Branch: `fix/f06-runtime-disk-readiness`
- Base SHA: `011b9e2fc8a5c93578acceafe5505774537866df`
- Rejected prior heads: `fd749c4ba0ccf1490d0521d226481cf6ea88039f`, then `cdfcd12a580f6128a95e22e123e86e86dbde0e57`
- Earlier index and growth repair: `b72330b75efae7c268b466f6bed7c9365ab85c88`
- Source-metadata repair SHA: `12eb0017f4924a582ef43975856977a6440f8185`
- Handoff SHA: `2a1df3189a33cd274ed28373779dd64061277397`
- The documentation commit that records the verified handoff CI below is the branch tip after that commit. Its pull-request check is the CI result for the exact final head.
- Draft PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/130
- Schema version: remains 26
- Runtime database: not opened, copied, or modified

Source-metadata repair CI for `12eb0017f4924a582ef43975856977a6440f8185`:

- Run: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/37295646815
- Attempt: 1
- Result: success
- Job: Python 3.11 tests, ID `111716092244`
- Log result: `2900 passed, 1 warning in 203.90s (0:03:23)`

Earlier green runs stay historical and are not this candidate. Index and growth repair `b72330b75efae7c268b466f6bed7c9365ab85c88` passed run 37291252581. Its handoff `cabfbb011c302f366709bfe60bbfeecca1bc656d` passed run 37291636794. The reviewed tip `cdfcd12a580f6128a95e22e123e86e86dbde0e57` passed run 37292150234 and remains REQUEST_CHANGES.

Handoff CI for `2a1df3189a33cd274ed28373779dd64061277397`:

- Run: https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/37296126114
- Attempt: 1
- Result: success
- Job: Python 3.11 tests, ID `111717647420`
- Log result: `2900 passed, 1 warning in 196.77s (0:03:16)`

The documentation commit that records this handoff CI is the branch tip. The successful pull-request check on that tip SHA is the CI for the exact final head. Do not treat `cdfcd12a580f6128a95e22e123e86e86dbde0e57` or an earlier green run as the candidate.

Acceptance is not claimed. Adam owns merge and any later Runtime procedure.

## Limits

- The assessor classifies supplied JSON. It does not attest review, CI, process identity, restore success, or disk capacity.
- Synthetic fixtures are labeled `synthetic`. They are not Runtime measurements.
- Historical ~81 GiB figures remain historical. They were not reused as current capacity.
- The code-only Runtime exception has no execution evidence in this review chat. It is not a measured capacity result.
- The 7-day evidence age rule is a packet freshness bound, not an observed Runtime clock.
- Quiescent backup is still the historical maintenance helper. This phase does not add a new snapshot implementation.
- `CREATE INDEX IF NOT EXISTS` cannot repair a same-named index with the wrong definition. That case is adverse.
- One observed byte is not rejected for being small. It is projected with the ceiling formula. A budget below that projection is adverse.
- A malformed source index or epoch presence marker is not evidence that the object is absent. Canonical absence remains a preflight finding when the target rehearsal is valid.
- Final cutover re-verification is required by the packet and is not marked complete by a reviewable preparation packet.
- Repository handoffs for P5A, F03, F04, and F05 may still contain historical deferred wording from their implementation dates. Their merges are the accepted foundation. This document does not reopen those phases.

## Operational actions

None on Runtime. No merge, no deployment, no cleanup, no retention job, no listener, no watch loop, no network scan, no Telegram send, no order, and no live database access.

DEV actions were this repair on the existing branch, local tests, the existing draft pull request, and CI.

## Runtime facts still needed

A later Runtime procedure, not this DEV packet, still has to supply measured evidence for the machine that will actually run:

- Running process identity, interpreter, and code revision, separate from the collector checkout and from the proposed release SHA.
- Reviewed release SHA and its successful CI, as referenced evidence rather than a bare SHA.
- Verified source path, snapshot identity, and isolated restored-copy path.
- Source schema and whether `ix_lifecycle_records_epoch_locked_plan_state` is absent, matched, or a wrong definition, including name, partial true, and unique false.
- Rehearsal on that actual source schema proving schema 26, the expected index, epoch/lineage tables, and the preservation checks.
- Measured restore duration, migration duration, peak allocation, downtime budget, and recovery budget.
- At least two representative samples spanning at least 24 hours, a derivation in bytes, and a growth budget at least the ceiling projection over at least 10 days.
- Per-volume free and total bytes, with backup and restore topology that is not inferred from drive letters or role names.
- `SOURCE_REPLAY_CAPTURE_ENABLED` observed disabled for the initial cutover.
- Final verification of schema 26 and the index again before consumers start.

Until those facts exist and Adam authorizes that concrete package, operational status remains `RUNTIME_CHECKPOINT_NOT_YET_AUTHORIZED`.
