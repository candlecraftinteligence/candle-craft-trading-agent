# CCI Evidence Contract

Canonical structured definition: `app/analytics/evidence_contract.py`  
Contract version: `cci-evidence-contract-v2`

This document explains current repository behavior. It does not change scanner,
lifecycle, delivery, outcome, or identity generation. Audit-reported live-database
measurements are not reproduced here.

**CURRENTLY UNSAFE / AMBIGUOUS** marks concepts that exist in code but cannot
support research statistics without a later identity/outcome phase.

A missing table or metric is **unavailable**, not zero.

## Counting units that must not be mixed

| Unit | Current persistence | Research use |
| --- | --- | --- |
| Observation | `symbol_results` row `(run_id, symbol)` | Safe as scan-time instrument count |
| Candidate | `setup_candidates` row (0..1 per symbol per run by convention) | Safe as candidate-row count |
| Setup identity string | `setup_lifecycle_records.setup_identity` | **CURRENTLY UNSAFE / AMBIGUOUS** |
| Lifecycle generation | `lifecycle_id` | Safe as lifecycle-row/event count, not a plan |
| Economic plan | `plan_identity` includes `lifecycle_id` | **CURRENTLY UNSAFE / AMBIGUOUS** |
| Outcome row | progress/analytics rows | Not a completed trade |
| Public event | `event_key` / `(signal_id, alert_type)` | Delivery event, not a trade |
| Unique trade | **unavailable** | Requires frozen plan + fill/exit policy + authoritative outcome |

Do not manufacture a unique-trade count with `DISTINCT lifecycle_id`.

## Entities

See `build_evidence_contract()["entities"]` for producers, consumers, mutability,
timestamps, cardinality, and research-safety for:

`OBSERVATION`, `CANDIDATE`, `SETUP`, `ECONOMIC PLAN`, `PLAN VERSION`, `LIFECYCLE`,
`READINESS`, `QUALITY`, `CONFIRMATION`, `ACTIVATION`, `SIMULATED FILL`,
`MANUAL FILL`, `PUBLIC SIGNAL`, `OUTCOME`, `REPLAY RECORD`.

Summary of current vs intended:

- **Observation** is one stored symbol result in one run. It is not a unique setup.
- **Candidate** is an optional geometry snapshot for that observation. It is not an activated plan.
- **Setup** is closest to `setup_identity` (`symbol|mode|direction|entry_low|entry_high|stop_loss|invalidation_reason`). Targets are omitted. **CURRENTLY UNSAFE / AMBIGUOUS**.
- **Economic plan** is closest to stored lifecycle geometry plus `canonical_plan_identity`. That hash includes `lifecycle_id`, so generation identity is mixed into economics. Public `canonical_plan_id` is a different, mode-neutral namespace. **CURRENTLY UNSAFE / AMBIGUOUS**.
- **Plan version** is P1 parallel `setup_lifecycle_records.plan_version_id` (SHA-256 of plan economics without `lifecycle_id`). Current outcome/public consumers still use `plan_identity`. Historical rows remain NULL. **CURRENTLY UNSAFE / AMBIGUOUS** for research counting until consumer migration.
- **Lifecycle** is `lifecycle_id` / `setup_generation_id`. Geometry freezes only in `PLAN_LOCK_STATES` (`TRIGGERED` is excluded). Current-row filters are not as-of reconstruction.
- **Readiness** and **quality** are overwritten snapshot scores/labels. They are not economic identity.
- **Confirmation** is `confirmation_count` / `confirmed_at` / state `CONFIRMED`. It is not entry.
- **Activation** is split and no longer treated as one economic funnel:
  - Legacy `scan_runs.valid_activations` counts watch-loop `WatchActivation` alerts and is `DEFAULT 0` on non-watch runs. The physical field and operational consumers are unchanged. **Deprecated as an economic research metric.** Mixed-window sums that include non-watch zeros are **CURRENTLY UNSAFE / AMBIGUOUS**.
  - Watch-scoped research projection: `activation_accounting.watch_alert_activations` (unavailable when no watch rows or the watch flag/column is missing; a complete 0 requires watch iterations in the window).
  - Closed-candle entry-activation evidence: `setup_lifecycle_events.reason = ENTRY_ACTIVATED`, counted only as **event records** (`entry_activated_event_records`). Not a unique fill occurrence.
  - `ENTRY_FILL_SIMULATED` is a different event-record unit; the reason text cannot distinguish simulated vs verified fills. Manual fill and fill-occurrence counts are **unavailable**.
- **Simulated fill** exists as a transition reason and replay `filled` flags. **Manual fill** is **NOT FOUND**.
- **Public signal** is a delivery event. Message hash is SHA-256 of formatted text, not JSON.
- **Outcome** has two tables and can fan out on `lifecycle_id`. TP progress is not a completed trade. **CURRENTLY UNSAFE / AMBIGUOUS**.
- **Replay record** uses a different `setup_fingerprint`. An empty `replay_results` table is unavailable replay evidence, not a 0% win rate.

## Metric dictionary

Producers live in `app/storage/repositories.py` (`_scan_run_record`, `_bucket_counts`,
`_lifecycle_state_counts`, `_a_grade_actionability_counts`, `_scan_summary_metadata`)
and `app/watch_mode.py` for watch-only fields. Persistence is `scan_runs` insert-once.

All listed counters are **per-run snapshots**. Summing them across runs recounts repeated
observations. None of them count unique economic plans.

| Metric | Counting unit | Trust |
| --- | --- | --- |
| `symbols_requested` / `queued` / `completed` / `scanned` | run workload | Safe as workload; not activation |
| `total_valid_setups` | observation with `display_bucket=valid` | Safe as observation count |
| `near_misses` | observation `near_miss` | Safe as observation count |
| `rejected` | observation `no_setup` (name is ambiguous vs `rejected_no_edge`) | **CURRENTLY UNSAFE / AMBIGUOUS** |
| `data_issues` | watch metadata or display `data_issue`; may disagree with `data_issues_json` | **CURRENTLY UNSAFE / AMBIGUOUS** |
| `valid_activations` | watch alert count; else default 0 | **CURRENTLY UNSAFE / AMBIGUOUS**; deprecated as economic metric. Use `activation_accounting`. |
| `still_watching` | watch eligible symbols not activated | Safe as watch snapshot |
| `actionable_setups` | **alias of** `actionable_a_grade_setups` | **CURRENTLY UNSAFE / AMBIGUOUS** |
| `confirmed_setups` | observations with lifecycle state `CONFIRMED` | **CURRENTLY UNSAFE / AMBIGUOUS** |
| `actionable_a_grade_setups` | A-grade actionable observations, including target caution | **CURRENTLY UNSAFE / AMBIGUOUS** |
| `candidate_a_grade_setups` | broader A-grade candidate observations | Safe as observation count; not disjoint |
| `blocked_a_grade_by_*` | actionability blocker states | Safe as observation blocker counts; not unique plans |
| `fatal_target_blocks` / `soft_target_warnings` | overlap actionable/blocked counters | **CURRENTLY UNSAFE / AMBIGUOUS** |

These columns are **not** a disjoint conversion funnel. Reconciled requested/queued/completed/scanned
counts do not imply successful activation.

`display_status == valid_setup` maps to `display_bucket == valid`. Watch activation uses
`display_status == valid_setup`, which is a different predicate from lifecycle fill.

## Timestamp contract

| Name | Existing field | As-of suitable? |
| --- | --- | --- |
| `source_event_time` | NOT FOUND on scan rows; candle/`event_time` elsewhere | Partial for candles only |
| `source_receipt_time` | NOT FOUND; `received_at` on microstructure | No on scan path |
| `feature_ready_time` | NOT FOUND | No |
| `scan_time` | `scan_runs.timestamp` (persist UTC ISO) | Weak persist window |
| `decision_time` | `ScannerRunConfig.decision_timestamp` | Best current as-of clock; not persisted as a `scan_runs` column |
| `lifecycle_event_time` | `setup_lifecycle_events.timestamp` | Event log, not record as-of |
| `public_available_time` | NOT FOUND; `reserved_at` / `sent_at` | Delivery wall-clock |
| `outcome_evaluation_time` | `last_evaluated_at` plus candle milestone times | Mixed event/eval clocks |

Naive CLI bounds are rejected. Timezone-aware inputs are normalized to UTC.
`now()` is never substituted for a missing historical event time.
SQLite `CURRENT_TIMESTAMP` columns (`created_at` / `updated_at`) are documented as
UTC naive `YYYY-MM-DD HH:MM:SS` and accepted only for those columns.

A persist-time or event-time filter is a **retrospective summary**, not a causal
as-of replay. Information-availability timestamps are absent.

## Provenance (prospective)

Future stored runs may include `scan_runs.runtime_stats_json.research_provenance`
(`cci-run-provenance-v1`). The key is added after `ScannerRuntimeStats.model_dump`.
It is not part of setup fingerprints, lifecycle identity, public event keys,
message hashes, or outcome identity.

The safe configuration hash uses an explicit allowlist in
`app/storage/run_provenance.py`. Full configuration dumps are never hashed.
Dirty git trees are not represented as reproducible from commit SHA alone.
Historical rows are not backfilled.

## Read-only audit

```powershell
.\.venv\Scripts\python.exe scripts\audit_evidence_baseline.py --database-path <explicit-test-db> --start 2026-09-01T00:00:00Z --cutoff 2026-09-07T00:00:00Z
```

Required arguments: `--database-path`, `--start`, `--cutoff`. Window is `[start, cutoff)`.
The live path `S:\CandleCraftRuntime\scan_runs\main_live_runtime.sqlite` and any
`main_live_runtime.sqlite` basename are refused. Connections use SQLite URI
`mode=ro` and `PRAGMA query_only=ON` without `immutable=1`.

## Unique trade evidence (unavailable)

A unique-trade count would require associating an entry occurrence with:

1. An immutable `plan_version_id` (frozen instrument, direction, mode/horizon, entry bounds, stop, targets, exit-policy version).
2. An explicit fill policy and a recorded fill event (simulated vs manual distinguished).
3. An explicit exit policy and one authoritative outcome owner for that fill.

Those requirements are not jointly satisfied. Unique-trade count status: **unavailable**.
