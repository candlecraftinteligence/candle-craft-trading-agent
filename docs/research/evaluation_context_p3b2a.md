# CCI P3B2A — Evaluation Context Contract / Boundary Proof

**Status:** Read-only contract + diagnostic hardening. No schema change.
**Does not** persist a durable as-of cutoff, mint evaluation/occurrence ids, or
advance P3C canonical reconciliation.
**Audited base commit:** `3219e16dc95a53839c1cbda1a6626120d4b54086` (`main` / P3B1 #110).
**Schema:** v22 unchanged.

This report records what existing outcome-evaluation evidence can prove, and
where P3A previously overstated that proof. Fixtures in
`tests/test_evaluation_context_p3b2a.py` are labeled synthetic. No live or
existing scan database was read.

## Research unit (existing storage grouping)

The physical progress history is `(lifecycle_id, plan_identity)`, with an
independently verified optional `plan_version_id`.

This is an existing uniqueness key, **not** a newly established evaluation
occurrence identity. The same `plan_version_id` may appear on several progress
rows and lifecycle generations.

Keep these dimensions separate. Do not collapse them into one Boolean:

| Dimension | What current persistence can support | What it cannot prove |
| --- | --- | --- |
| Physical progress grouping | `UNIQUE(lifecycle_id, plan_identity)` | Unique evaluation/trade/fill occurrence |
| Immutable-plan attribution | P3B1 first-INSERT `plan_version_id` when proven | Canonical one-outcome-per-plan |
| Start-boundary provenance | Producer metadata `tracking_boundary_source` when present | Original beginning after v19 cursor backfill |
| Timeframe / candle eligibility | `execution_timeframe` plus closed-candle predicate `close <= decision_timestamp` | Historical point-in-time data availability |
| Cursor / evaluated prefix | Mutable `evaluation_cursor_*` and `processed_candle_count` | That every candle from start to a claimed horizon was evaluated |
| As-of / decision cutoff | In-memory argument to this pass only | Reconstruction of the exact cutoff from durable progress |
| Completeness of supplied records | Caller `coverage_complete` assertion | Row-level coverage of the producer history |
| Namespace | Fixture/diagnostic `source_namespace` when supplied | Authoritative live/replay evaluation namespace |
| Terminal / milestone evidence | Raw timestamps and `terminal_outcome` | Coverage up to `outcome_at` on the terminal-shortcut path |

## Causal time map (runtime unchanged)

| Value | Producer / clock | Role | Persisted? | Constrains eligibility? | Cannot prove |
| --- | --- | --- | --- | --- | --- |
| `decision_timestamp` | Scanner `ScannerRunConfig.decision_timestamp` (clock fallback at run start) → `_StrategyExecution` → `ScannerSymbolResult.lifecycle_decision_timestamp` (excluded from dumps) → service `or now` | Upper closed-candle cutoff | **Lost after the pass** | Yes: `close_timestamp <= decision_timestamp` | Reconstructable cutoff; that processing time was the cutoff |
| `evaluated_at` / service `now` | Lifecycle apply clock (`now` or `now_utc_iso()`) | Processing-pass stamp | `first_evaluated_at` (INSERT-only) and `last_evaluated_at` (updated) | No | Decision cutoff; evaluated coverage |
| `tracking_start_at` | Aligned first fully-post-boundary open, or legacy cursor / v19 backfill | Lower entry-tracking boundary (aligned) | Column + metadata copy | Yes, after start is established | Origin if metadata marker absent; that candles before the cursor were evaluated |
| `tracking_boundary_timestamp` | Confirmation or material-plan `first_evaluated_at` | Unaligned causal boundary used to compute start | Producer metadata when the closed-candle path writes it | Indirectly | Universal decision timestamp |
| `evaluation_cursor_open_at` / `close_at` | Last consumed eligible candle | Mutable prefix cursor | Columns | Next-candle expected open | Full interval coverage; failed/no-op passes |
| Candle open/close | Exchange candle timestamps, UTC | Data interval | Last consumed pair in metadata | Eligibility uses close | Future/unclosed candles |
| `confirmed_at` / confirmed event | Lifecycle confirmation | Default entry-tracking boundary | Lifecycle record / events | Start alignment | Outcome coverage |
| `outcome_at` | Terminal candle close or copied lifecycle `last_transition_at` | Milestone/terminal stamp | Column | No | That the shortcut evaluated candles to that time |
| `created_at` / `updated_at` | SQLite `CURRENT_TIMESTAMP` | Row bookkeeping | Columns | No | Causal cutoff |

Material-plan policy (unchanged): when other `plan_identity` rows exist for the
lifecycle, the start boundary may be `first_evaluated_at` if that stamp is after
confirmation (`tracking_boundary_source=material_plan_first_evaluated_at_after_confirmation`).
That is first-processing time of the new progress row, not a universal decision
timestamp.

An aligned `tracking_start_at` may legitimately lie after the last eligible
closed candle while the evaluator waits for the first fully post-boundary close.
Recording a start with zero processed candles is not proof those candles were
evaluated.

## Service fallback (characterized, not changed)

`SetupLifecycleService` passes:

```text
decision_timestamp = symbol_result.lifecycle_decision_timestamp or now
evaluated_at = now
```

When the scanner supplies `lifecycle_decision_timestamp`, cutoff and processing
time can differ. When that field is absent (empty `_StrategyExecution()`, or a
direct evaluator call), both arguments can coincide. Coincidence is not proof
that the two clocks are interchangeable for every producer.

`lifecycle_decision_timestamp` is `exclude=True` on `ScannerSymbolResult`. It is
not recovered from ordinary model dumps.

## Persistence / reload

- Ordinary producer continuation reloads the same `(lifecycle_id, plan_identity)`
  row. A later `scan_run_id` does not split the window.
- `ON CONFLICT` **does** replace `tracking_start_at`. SQL does not enforce
  write-once immutability. Ordinary closed-candle continuation does not drift
  the producer-established start; raw upsert can.
- `ON CONFLICT` **does not** replace `plan_version_id` or `first_evaluated_at`.
- v19 initializer `_migrate_outcome_tracking_start_v19` copies
  `evaluation_cursor_open_at` into null `tracking_start_at` and is **not**
  guarded by `existing_version < 19`. Reopening a database can backfill that
  way without a per-row provenance marker. Do not infer prospective origin from
  a v22 `user_version` or timestamp equality.

## Source / namespace / scan_run_id

| Token | Classification |
| --- | --- |
| `source=canonical_lifecycle_closed_execution_candles` | Algorithm / producer marker |
| P3A `"unspecified"` | Missing-source handling, not a minted runtime namespace |
| Explicit synthetic fixture namespace | Fixture provenance only |
| `scan_runs.runtime_stats_json` research provenance | Scan-invocation provenance; no durable link to a continuing progress history |
| `event.scan_run_id` | Optional processing-trigger association on bound events |

`setup_lifecycle_events.scan_run_id` is nullable TEXT with **no foreign key** to
`scan_runs`. Production `scripts/run_scan.py` applies lifecycle **before**
`store_scan_result`, so events can carry a scan id before the `scan_runs` row
exists. `scan_run_id=None` remains supported. `scan_run_id` is not an evaluation
owner, durable namespace, or occurrence id.

Replay `replay_results` is not joined to lifecycle outcomes.

## P3A overclaim that this phase corrects

Before P3B2A, `project_outcome_ownership` set:

```text
evaluation_context.complete = bool(nonempty tracking_start_at)
evaluation_context_complete = True  # on an interpretable plan
```

A legacy cursor-derived stamp, an invalid string, or a valid start with no
durable as-of could therefore look like complete causal context.

After P3B2A:

- `complete` / `evaluation_context_complete` stay **false**.
- A normalizable `tracking_start_at` without timestamp/cursor conflicts may
  still authorize a **supplied-snapshot interpretation**.
- `start_boundary_provenance` is `absent`, `unproven`, or `producer_metadata`.
- Caller `coverage_complete` and report `as_of` do not become row-level
  provenance.
- Valid P3B1 attribution is preserved when context is ambiguous.
- Raw terminals, source refs, and milestones remain available.

## Stronger claims that remain unproven

Exact durable decision/as-of reconstruction; full causal coverage; authoritative
live/replay binding; database-enforced start immutability; canonical
one-outcome-per-plan; unique evaluation/trade/fill occurrence; expectancy/alpha.

## Persistence is deferred

An extra as-of column would preserve one currently lost input. It would not by
itself prove immutable start, continuous historical coverage, terminal-shortcut
provenance, or a unique evaluation occurrence. Write semantics would still need
to distinguish first evaluation, successful advancement, failed attempts, no-op
passes, and terminal completion. That work is out of scope for P3B2A.

## Acceptance matrix (requirements 1–35)

| # | Requirement | Evidence |
| --- | --- | --- |
| 1 | P3B1 first-INSERT attribution | existing `test_eligible_progress_stores_exact_p1_id_for_evaluated_plan`; `test_projection_is_read_only_deterministic_and_does_not_open_a_database` |
| 2 | Fresh window persists producer start | `test_nonempty_tracking_start_is_anchor_not_complete` |
| 3 | Ordinary pass does not drift start; raw upsert is not write-once | `test_raw_upsert_can_replace_start_but_ordinary_pass_does_not_drift` |
| 4 | Reload preserves window and attribution | `test_reload_and_later_scan_id_continue_one_window` |
| 5 | Rediscovery does not mint a new context | same reload test; one SQL row |
| 6 | Later/absent `scan_run_id` does not split the window | `test_reload_and_later_scan_id_continue_one_window`; `test_scan_run_id_is_optional_event_association_not_evaluation_owner` |
| 7 | Explicit cutoff not replaced by later processing | `test_later_processing_clock_does_not_admit_future_candles`; material-plan characterized separately |
| 8–9 | Closed-candle predicate, exact close, future candles | `test_exact_close_is_eligible_and_unclosed_next_candle_is_not`; existing `test_open_future_candle_cannot_activate_entry` |
| 10 | Timeframe stability / mismatch | existing `tests/test_lifecycle_outcomes.py` integrity path |
| 11–12 | Terminal shortcut / no invented start; v19 characterized | `test_terminal_shortcut_without_start_keeps_attribution_and_raw_terminal`; `test_v19_cursor_backfill_is_unproven_origin_not_prospective_proof` |
| 13–14 | Same plan across generations; distinct plans | `test_same_plan_across_generations_and_distinct_plans_stay_separate` |
| 15 | Cursor, stale, no-op, wait, gap | `test_noop_and_failed_pass_do_not_add_evaluated_coverage`; existing lifecycle outcome tests |
| 16–20 | TP ladder, TP1→SL, entry/same-candle, invalidation, expiry | existing P3A/lifecycle suites; partial-terminal test retains EXPIRED |
| 21–22 | P2B / P2A unchanged | existing suites; this PR does not edit those producers |
| 23–28 | Public delivery, event_key, message_hash, analytics, health, memory | N/A to this PR (no producer/consumer edits); existing suites remain the proof |
| 29–30 | Canonical plan-outcome false; unique trade unavailable | `test_projection_is_read_only_deterministic_and_does_not_open_a_database` |
| 31–32 | Context dimensions vs string presence; legacy not prospective | `test_nonempty_tracking_start_is_anchor_not_complete`; v19 test |
| 33 | Schema/uniqueness/migrations unchanged | `test_schema_remains_v22_with_no_new_context_column`; existing `tests/test_storage_database.py` |
| 34–35 | Full pytest / CI | post-implementation run |

