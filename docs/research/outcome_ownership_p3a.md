# CCI P3A — Outcome Ownership Semantic Contract

**Status:** Diagnostic contract + read-only reconciliation projection.
**Does not** persist a canonical plan-outcome, migrate consumers, or mint occurrence ids.
**Audited base commit:** `da43358f552626c6ef48235a691fb3a53228c36b` (`main` / P2B #108).
**Schema:** v21 unchanged.

This report records repository-grounded ownership as of the audited commit. Synthetic
fixtures in `tests/test_outcome_ownership_contract.py` and
`docs/research/outcome_ownership_p3a_synthetic_report.json` are labeled synthetic.
No live or existing scan database was read.

## Units (keep separate)

| Unit | Meaning in P3A |
| --- | --- |
| Source evidence row | One supplied record from progress, events, analytics, or lifecycle tables |
| Verified immutable plan identity | `plan_version_id` that remints from a supplied lifecycle snapshot |
| Plan-level outcome | Diagnostic interpretation of **one** coherent evaluation context |
| Fill / trade occurrence | **Unavailable** (not invented) |

## Actual owners found

- **Authoritative stored outcome projection:** `setup_lifecycle_outcome_progress` keyed by `UNIQUE(lifecycle_id, plan_identity)`.
- **`plan_identity`** hashes `lifecycle_id` plus geometry (`app.lifecycle.outcome_policy.canonical_plan_identity`). Compatibility aliases (`compatible_plan_identities`) are lookup variants, not proof of equal immutable economics.
- **P1 `plan_version_id`** is latched on `setup_lifecycle_records` only. `evaluate_closed_candle_outcomes` does not receive or persist it.
- **Analytics** `setup_outcome_analytics` is keyed by `UNIQUE(lifecycle_id, final_outcome)` and has no `plan_identity` column. Nested `raw_payload_json.outcome_progress.plan_identity` may exist when the producer passed a progress snapshot. That nested identity is the only proven plan binding. Unbound analytics remains lifecycle-level evidence and must not decide a plan-specific economic conflict. P3A does not change analytics persistence.
- **Events** are append-only; no event idempotency key. Repeated scans can append additional `ENTRY_FILL_SIMULATED` event records while a single progress row remains.
- **Public delivery** uses `event_key` / `(signal_id, alert_type)` and is not an outcome owner.
- **Replay** `replay_results` uses a separate fingerprint and is not joined to lifecycle outcomes.

## Permissible diagnostic bindings

Verified attribution requires all of:

1. A supplied lifecycle snapshot with `plan_version_id` that remints from that snapshot's economics.
2. Progress `plan_identity` in `compatible_plan_identities` of **that same snapshot**.
3. No `plan_version_invariant_violation`.

Otherwise the helper reports missing/legacy identity, conflicting identity/economics, incomplete evidence, or ambiguous evaluation context. A current lifecycle row must not lend its id to an older unbound `plan_identity`.

## Evaluation context

Progress stores `tracking_start_at`, cursors, and `execution_timeframe`. As-of cutoff and replay/run identity are **not** columns on progress. One `plan_version_id` across lifecycle generations is one inventory item, not automatically one outcome. Replay and live namespaces must not be merged. Missing anchors are not invented.

**Producer invariant — terminal-before-cursor path.** `evaluate_closed_candle_outcomes` copies an already-terminal lifecycle `current_state` onto progress via `_terminal_progress_for_record`. That path sets `terminal_outcome` (and `invalidated_at` when INVALIDATED) and may mark `integrity_status=Unverified` with `diagnostic=terminal_state_preceded_canonical_outcome_cursor`. It does **not** set `tracking_start_at`. `first_evaluated_at` is the evaluation-pass clock, not a proven tracking window. A retained terminal is raw/source evidence; it does not by itself prove a coherent evaluation context or authorize a plan-level interpretation.

**Producer invariant — closed-candle path.** When the evaluator tracks a live window, `_with_tracking_start` durably binds `tracking_start_at`. That field is the P3A evaluation-window anchor.

**Canonical one-outcome-per-plan-version is unproven** as a persisted research unit.

## Event-record identity

Diagnostic P2A event-record counts use `(source_namespace, event_id)` when `event_id` is present. The same `event_id` in `live-monitoring` and `replay-run-a` is two supplied event records. Duplicate copies in one namespace do not inflate the count. These counts are not fill occurrences or unique trades. P3A does not mint occurrence ids.

## Authority and idempotency

- Do not use `updated_at`, latest-row, first-terminal, `MAX`, or `DISTINCT` as ownership proof.
- Same physical progress key may be presented more than once: identical payloads collapse; contradictory payloads conflict; monotonic non-null milestone enrichment on that key is producer-defined.
- Once `terminal_outcome` is set, the evaluator does not advance that progress row. Later lifecycle `COOLDOWN` / `ARCHIVED` is successor state, not economic replacement.
- No correction/supersession protocol is invented. Conflicting economic terminals remain conflicts.

## Terminal semantics (diagnostic)

| Evidence | Treatment |
| --- | --- |
| TP1 / TP2 timestamps | Milestones; not wins |
| TP3 + `terminal_outcome=TP_HIT` | Take-profit terminal under current exit policy |
| Generic `TP_HIT` without `tp3_at` | Not promoted to TP3 |
| TP1/TP2 then `SL_HIT` | Keep milestones and the stop; no invented P&L |
| Invalidation with no `entry_at`, complete coverage, no entry evidence | Pre-entry invalidation, not a stop |
| Expiry with no `entry_at`, complete coverage, no entry evidence | Pre-entry expiry, not a loss |
| INVALIDATED / EXPIRED with no `entry_at` and incomplete coverage | Entry relationship uncertain; raw terminal preserved |
| SL_HIT with no `entry_at` | Never "before entry"; entry relationship uncertain |
| Missing `entry_at` without completeness | Uncertainty, not a negative finding |

## Synthetic producer observations (not live history)

Reproduced with `evaluate_closed_candle_outcomes` on temporary SQLite:

1. Repeated scans of one latched plan: **one** progress row; additional event records; `plan_version_id` remains absent from progress columns.
2. Geometry change after latch: **two** progress `plan_identity` rows; old entry preserved; latched `plan_version_id` **preserved** with `plan_version_invariant_violation` (P1), so the new geometry must not inherit that id.
3. TP1 then stop: `tp1_at` retained with `terminal_outcome=SL_HIT`.
4. Two current generations of identical economics require superseding the first `is_current` row; `plan_version_id` can match while `plan_identity` differs.

## Unresolved dependencies (next phase, not this PR)

- Prospective `plan_version_id` on the outcome write boundary (producer/identity propagation).
- Durable evaluation-anchor / run-namespace binding if one plan-outcome per version is required.
- Occurrence identity only if fill/trade statistics are required (separate from plan-level simulation).
- Consumer query/denominator migration (research `tp_hit_rate` is lifecycle reachability; public follow-ups use compatible `plan_identity`).

P3A does not implement those.
