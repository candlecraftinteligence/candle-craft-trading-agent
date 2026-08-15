# Candle Craft Intelligence Reliability Validation

Audit date: 2026-08-15
Branch: `audit/candle-craft-reliability-validation`
Baseline: `aca528fe7a227eb43146120ffecb168cb439994f` (`origin/main`)
Validated source: `0440015` before this report-only commit

## 1. Executive verdict

**CODE_VALIDATION_PASS**

**WAITING_FOR_RUNTIME_EVIDENCE**

The tracked repository is clean, the scanner lineage defect is fixed, per-scan process RSS evidence is now persisted and reportable, the full test suite passes, and a read-only CI gate is present. Leakage-safe replay contracts pass repeatedly, but there is no tracked empirical outcome cohort and no post-change Runtime observation window. Therefore:

- the branch is suitable for review in a draft pull request;
- no merge or deployment is authorized by this report;
- no strategy or setup-gate tuning is justified;
- memory stability, live funnel behavior, Telegram delivery, and real outcome quality remain pending Runtime evidence.

## 2. Safety boundary

All Dev validation kept these controls in force:

- `LOCAL_MANUAL_MODE=true`
- `ORDER_EXECUTION_ENABLED=false`
- `TELEGRAM_DRY_RUN=true`
- `TELEGRAM_SIGNALS_ENABLED=false`
- Telegram credentials unset for the complete test run

No live order execution, withdrawal, transfer, Telegram send, deployment, exchange write, or protected database write/maintenance operation was performed. One early read-only recursive file inventory was stopped when its scope could traverse ignored `scan_runs` content; it did not deliberately open or query the database, and no write occurred. All subsequent inventory was restricted to tracked paths. The ignored live Runtime database was not migrated, copied, checkpointed, vacuumed, repaired, deleted, staged, or modified.

Runtime database inspection is outside this Dev audit. The established quiescent, immutable, query-only procedure in `docs/post_restart_funnel_audit.md` remains the only authorized evidence path after separate operational approval.

## 3. Baseline and branch evidence

The work began from a clean local `main` equal to `origin/main` at the baseline SHA. The implementation was split into reviewable commits:

| Commit | Scope |
|---|---|
| `7cbbe06` | Record cleanup evidence before deletion |
| `1355491` | Remove only approved tracked cleanup hazards |
| `e37e7fa` | Preserve scan lifecycle lineage and stabilize the time-dependent test |
| `56af4c5` | Record per-scan process memory evidence |
| `bf76aa6` | Add the read-only CI test gate |
| `0440015` | Record the evidence-based strategy no-change decision |

Before this report, the cumulative branch diff was 20 files, 889 insertions, and 18 deletions. The worktree and index were clean.

## 4. Cleanup result

The cleanup manifest was committed before the cleanup itself. Only its three approved items were applied:

1. removed one byte-for-byte duplicate, shadowed `_public_setup_quality_score_decimal` definition;
2. removed UTF-8 BOMs from two Python files without textual logic changes;
3. added `scan_runs`-scoped ignore rules for SQLite databases and sidecars.

No migration, historical compatibility value, research placeholder, dependency, untracked artifact, ignored Runtime artifact, remote branch, tag, or pull request was deleted. No tracked database, log, archive, bytecode, generated scan JSON, or JSONL artifact exists after cleanup.

## 5. Scanner trace and reliability diagnosis

The audited scanner path is:

1. load safe configuration and the symbol universe;
2. fetch public, read-only market inputs with timeout/retry handling;
3. normalize to one decision timestamp and closed candles;
4. resample higher timeframes and compute the explicit 2H structure layer;
5. run deterministic strategy analysis and strict setup gates;
6. rank candidates and apply market-regime policy;
7. create lifecycle, journal, persistence, and performance-memory artifacts;
8. route only eligible output through Telegram safety gates;
9. persist runtime diagnostics for audit and command-center display.

The main reliability defects found and addressed were:

| Finding | Resolution |
|---|---|
| Regime rejection replaced prior status history | Append `REJECTED_BY_REGIME` while preserving `IDEA_CREATED` and `JOURNAL_ENTRY_CREATED` lineage |
| No per-scan process-memory evidence | Add safe RSS sampling, persisted statistics, display fields, and bounded post-restart aggregation |
| Date-dependent X-hype safety test | Freeze the test clock at its fixture time; production behavior is unchanged |
| No repository CI workflow | Add a read-only Python 3.11 compile, dependency, and pytest gate with pinned action SHAs |
| Duplicate helper, BOMs, SQLite ignore gap | Resolve under the precommitted cleanup manifest |

## 6. Process-memory evidence design

`app/core/process_memory.py` reads current-process RSS through Windows `GetProcessMemoryInfo` or Linux `/proc/self/statm`. Unsupported or failed sampling returns an explicit error code rather than failing a scan.

Each scan attempts samples at:

- scan start;
- completion of every symbol callback;
- completion of regime finalization.

The persisted `process_memory` block records:

- measurement status and source;
- RSS start, end, observed peak, and delta bytes;
- attempted, successful, and failed sample counts;
- bounded failure codes.

Unavailable values remain `N/A`; partial or unreliable evidence remains `Unverified`. The fields flow through scanner runtime JSON, resumed/combined runs, the command center, JSONL manifests, and human diagnostics.

The post-restart audit selects only a compact allowlist from persisted runtime JSON. It aggregates record coverage, verified/unverified counts, RSS ranges and deltas, positive-delta cycle count, and sampling failures. It deliberately reports `OBSERVATIONAL_ONLY` after enough records to aggregate and never declares a memory leak or stability automatically.

## 7. Bounded Dev memory observations

The native Windows probe returned one valid point:

| Field | Value |
|---|---|
| RSS | 16,429,056 bytes |
| Source | `windows:GetProcessMemoryInfo` |
| Error | None |

This proves the sampler operates in the Dev environment; one point says nothing about stability.

A separate mocked, offline repeated-scan probe used `tracemalloc`, two warmups, and eight measured cycles. Measured current allocations were 26,502; 31,310; 32,861; 34,283; 36,817; 36,897; 38,697; and 40,239 bytes, for 40,239 bytes net growth from the pre-measurement baseline and a 13,737-byte measured range.

That bounded result is not an RSS measurement, not a Runtime soak, and not a leak verdict. It only shows that the deterministic mocked path completes repeatedly with small bounded Python allocation growth over the sampled window.

## 8. Strategy and replay result

The tracked replay matrix passed twice, 184 tests per run. It verifies closed-candle decision boundaries, higher-timeframe close availability, resistance to future-candle mutation, deterministic event ordering, conservative ambiguous-candle handling, and no proportional outcome fallback.

The built-in dataset analyzers found zero tracked empirical rows, zero terminal-outcome rows, zero `result_r` coverage, and zero validation candidates. A two-case synthetic contract benchmark produced one TP1 outcome and one stop, but its `low_sample_size` and `mixed` labels make it unsuitable for any performance claim.

Accordingly, empirical win rate, expectancy, profit factor, drawdown, target-inside-chop counterfactuals, and threshold calibration remain **N/A**. No strategy, target, scoring, stop, entry, grade, or minimum-RR behavior changed. The detailed decision is in `docs/audits/CCI_STRATEGY_EVIDENCE_REVIEW_2026-08-15.md`.

## 9. Verification evidence

| Gate | Result |
|---|---|
| Baseline full suite | 1 failed, 1,862 passed; failure traced to a test clock aging beyond its fixture window |
| Cleanup-focused matrix | 421 passed |
| Reliability-focused matrix | 232 passed |
| Additional targeted reliability checks | 29 passed |
| Replay matrix, run 1 | 184 passed in 3.33 s |
| Replay matrix, run 2 | 184 passed in 3.15 s |
| Final complete suite | 1,869 passed, 1 warning in 113.27 s |
| Dependency consistency | `pip check`: no broken requirements |
| Compilation | `compileall` across `app`, `scripts`, `src`, and `tests`: pass |
| CI syntax and safety assertions | pass |
| Tracked high-confidence secret patterns | none found |
| Tracked database/log/archive/bytecode artifacts | none found |
| Whitespace and Git state | clean |

The sole final warning is the existing Starlette `TestClient` deprecation notice about its httpx integration. It is not a test failure, but dependency modernization should address it separately.

## 10. Remaining findings

These open findings were not expanded into unrelated changes:

| Priority | Finding | Status |
|---|---|---|
| P2 | Universe selection lacks persisted exchange contract metadata | Open |
| P2 | Some cross-phase operations are not one atomic transaction | Open |
| P2 | Stable lifecycle identity is not universal across every artifact | Open |
| P2 | Live terminal outcomes and replay performance memory remain separated | Open |
| P2 | Producer/version/cohort metadata is incomplete for empirical comparison | Open |
| P3 | Dependencies are not locked by a constraints or lock artifact | Open |
| P3 | Strategy analysis recomputes modes inside the requested-mode loop | Open optimization |
| P3 | Starlette/httpx test-client deprecation | Open maintenance |

None of these justifies weakening qualification or safety gates. The P2 items should be handled in narrowly scoped follow-up work with their own tests and migration review where applicable.

## 11. Runtime evidence handoff

After review and only after separate authorization to adopt the code on Runtime, collect evidence as follows:

1. Record the exact restart timestamp, code SHA, unchanged watch interval, and approved command.
2. Preserve normal scanner output; do not reset performance memory or mutate historical records for the audit.
3. Take Snapshot B after at least 72 hours and Snapshot C after seven days, keeping the same start boundary, source, interval, and audit options except end time and label.
4. On the Runtime PC, stop the scanner and verify its process is absent before audit access. If WAL/SHM sidecars exist or quiescence cannot be proven, stop and report NO-GO.
5. Run only `scripts/audit_post_restart_funnel.py --source-mode quiescent-immutable` under the documented procedure. It must verify immutable `mode=ro`, `query_only=1`, absent sidecars, and unchanged source metadata.
6. Review scan-cycle coverage, errors, gate concentration, lifecycle progression, explicit Telegram sent evidence, terminal outcomes, and the new process-memory block.
7. Treat fewer than two verified RSS records as `WAITING_FOR_RUNTIME_EVIDENCE`. Treat two or more only as `OBSERVATIONAL_ONLY`; evaluate the 72-hour and seven-day patterns manually, including coverage, failures, workload changes, starts, ends, peaks, and deltas.
8. Do not infer that rejected `target_inside_chop` setups would have won. Any tuning still requires attributable, time-ordered terminal outcomes and an out-of-sample cohort.

Until those artifacts are returned and reviewed, the terminal project state is:

**WAITING_FOR_RUNTIME_EVIDENCE**
