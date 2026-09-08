# CCI R0_OBSERVATION_UNIT — Economic Observation Admission Contract and Producer Reachability Proof

**Status:** Research contract + synthetic producer proofs. No production Python change.
**Does not** persist admissions, mint episode/trade IDs, migrate consumers, add a tracker, or compute expectancy.
**Audited base commit:** `39fe9b908de6d362c5c26b52f8eddb2caaa35fcf` (`main`, merge of PR #113 / P3_PREFIX).
**Schema:** application `SCHEMA_VERSION` / SQLite `PRAGMA user_version` = **24** unchanged. Fresh synthetic `PRAGMA schema_version` = 46 (SQLite catalog revision, not the application version). No admission/episode columns exist.

Fixtures in `tests/test_observation_unit_contract_r0.py` are labeled synthetic. No live or existing scan database was read.

This phase is a finite prerequisite to original-audit independent outcome reconciliation and causal replay. It specifies the **admitted plan-evaluation episode** as a future accounting unit and proves which relationships current producers actually establish.

## 1. Purpose and original P5 / P6 dependency

Original P5 required that an admitted plan continue to receive outcome evaluation after it leaves scanner discovery, or have an explicit gap reason. Mature, covered, unambiguous observations must reconcile without join fan-out.

Original P6 required frozen economics, causal source availability, explicit entry/exit assumptions, reproducible decisions/outcomes, and separation of exact-as-run evidence from reconstructed simulations.

Those requirements cannot be implemented as ownership or denominators until the unit is defined:

**One explicitly admitted plan-evaluation episode: one frozen economic plan evaluated from an evidenced admission boundary under a declared source context and fixed evaluation-policy semantics.**

This is a future semantic contract. Current CCI does not prove a complete population of those episodes. An admission is an accepted decision to begin one evaluation obligation, made before its outcome is known. It is not created retrospectively because a progress row, fill event, TP, or analytics record exists.

## 2. Reviewed / current base, schema, and evidence scope

| Fact | Evidence |
| --- | --- |
| Fresh `origin/main` | `39fe9b908de6d362c5c26b52f8eddb2caaa35fcf` |
| PR #113 | Merge commit is that SHA; implementation head `ccbd656c33850ee3380c25705b6d8ab7a1c789be` is an ancestor |
| Schema | v24; progress `UNIQUE(lifecycle_id, plan_identity)`; analytics `UNIQUE(lifecycle_id, final_outcome)` |
| P3_PREFIX columns | `last_eligibility_decision_at`, `last_eligibility_prefix_evidence_json` present and nullable |
| Absent | `admission_id`, `evaluation_episode_id`; analytics has no `plan_identity` / `plan_version_id` |
| Local baseline | `C:\CandleCraftDev\.venv\Scripts\python.exe -m pytest -q` → 2367 passed, 1 Starlette/httpx warning, ~176s |
| CI at reviewed SHA | Actions run 34228620249, 2367 passed, 2 warnings (platform-dependent warning count) |

Evidence labels used in tests (not application enums):

- **normal production path reached with synthetic inputs**
- **direct lower-level writer/import-supported state**
- **malformed/adversarial supplied state**

A supplied fixture is not a live-market reproduction. Identity, generation, lifecycle ownership, and outcome decisions were not mocked.

## 3. Unit distinctions and admission / continuation rules

Keep these separate. Do not collapse them into one Boolean:

| Unit | Meaning | Current CCI |
| --- | --- | --- |
| Scan observation | One observed decision/input record. Repeated scans can observe one opportunity many times. | `ScannerSymbolResult` inside `SetupLifecycleService.apply_to_run_result` |
| Structural lineage | P1 `setup_id` (venue, symbol, direction, mode, structural_anchor) | Latched when evidence exists; missing anchor → unavailable |
| Immutable economic version | P1 `plan_version_id` (setup_id + entry/stop/targets/invalidation) | Latched only in `PLAN_LOCK_STATES`; `TRIGGERED` stays unlocked |
| Lifecycle generation | `lifecycle_id` / `setup_generation_id` | Deterministic SHA when the structural anchor is known; UUID when missing |
| Physical progress owner | `UNIQUE(lifecycle_id, plan_identity)` | Mutable projection, not an evaluation ledger |
| Evaluation pass | One `evaluate_closed_candle_outcomes` application | Last-pass cutoff + P3_PREFIX envelope |
| Admitted evaluation episode | Proposed accounting unit | **Not persisted. Not recoverable from current keys.** |
| Simulated entry | Evidenced event under a declared model | Progress `entry_at` / P2A simulated-fill events; not a user fill |
| Real trade/fill | Authorized external/manual process | Unavailable; do not add such a process |
| Statistical dependence group | Related exposures that must not be assumed independent | Unresolved; no clustering heuristic in this phase |

Rules (specification only; no admission table):

1. Prospective admission must reference verified immutable economics, an evidenced admission cause/time, and the entry/tracking boundary actually applicable. Plan locking is not automatically admission. A progress INSERT is not automatically admission.
2. Source/scope must distinguish runtime observation, replay experiment, reconstructed history, and synthetic fixtures. A script name, DB filename, `scan_run_id`, algorithm marker, or caller string is not an authoritative namespace.
3. Evaluation-policy semantics must cover the actual entry model, boundary alignment, price basis/timeframe, same-candle ambiguity, target/stop/termination, and expiry/horizon. Existing local markers do not prove a complete immutable policy binding. Do not hash a fictional historical policy version.
4. Same admission, unchanged economics/context, repeated scan, retry, no-op pass, and process restart are continuation. They must not enlarge the observation population.
5. The same `plan_version_id` in different physical rows is insufficient to declare either one episode or multiple episodes. A common timestamp, matching prices, equal outcomes, or a shared structural anchor does not supply missing admission provenance.
6. A changed known structural anchor can establish a new lineage under current identity rules. It does not prove statistical independence of prior exposure.
7. A material economic change requires separate frozen-plan treatment in the eventual admission design. It must not rewrite an admitted episode. If producers preserve a latched ID while flagging `plan_version_invariant_violation`, retain that fact; do not fabricate a valid revised ID.
8. A terminal episode must never be reopened by relabeling a rescan. Future re-entry requires distinct contemporaneous trigger/admission evidence under an explicit re-entry policy. Current cooldown/archive rotation or elapsed time alone does not establish that evidence. Do not add a re-entry route.
9. Scalp/swing and public mode-neutral deduplication are not sufficient economic-episode equivalence rules. Preserve distinct source records and mark unresolved overlap.
10. Missing, conflicting, malformed, or legacy evidence remains unresolved. No automatic merge, split, or preferred-latest replacement.
11. Admission-population membership cannot depend on whether a setup later fills, wins, has complete coverage, or produces a convenient analytics row.
12. Every future admitted open episode needs an ongoing tracking obligation or an explicit gap record. Full interval history and liveness cannot be inferred from the last P3_PREFIX envelope.
13. One authoritative outcome must eventually be scoped to a supported episode and evaluation policy. Do not impose a global `UNIQUE(plan_version_id)` outcome rule.

An episode is an accounting unit, **not** proof of statistical independence.

## 4. Existing-fact versus missing-prospective-fact map

| Future episode fact | Reusable current fact | Missing prospective fact |
| --- | --- | --- |
| Frozen economics | P1 `plan_version_id` when reminted; latch after `PLAN_LOCK_STATES` | Fill/exit-policy version; tick-quantized lifecycle minting |
| Lineage | P1 `setup_id` when anchor known | Lineage for missing-anchor UUID generations |
| Physical row | `UNIQUE(lifecycle_id, plan_identity)` | Episode identity distinct from that row |
| First-INSERT plan attribution | P3B1 `plan_version_id` on new progress only | Legacy recovery; overwrite/backfill (intentionally forbidden) |
| Tracking start | `tracking_start_at` + `tracking_boundary_source` metadata | Original decision clock; complete origin after v19 cursor backfill |
| Last pass | P3B2B cutoff + P3_PREFIX W/P/C envelope | Whole-interval acquired/evaluated history; pass ledger |
| Scan association | Optional event `scan_run_id` | Authoritative source namespace; FK to `scan_runs` |
| Continuation of same generation | Same-known-anchor `generation_rotation_reason` returns None, including after cooldown/archive | Explicit continuation-vs-admission record |
| Changed-anchor new lineage | Service supersession + new `setup_id` / `plan_version_id` | Independence / re-entry policy |
| Simulated entry | Progress `entry_at` under closed-candle contract | Binding to an admitted episode |
| Real fill | None | Entire occurrence identity |
| Policy binding | Local timeframe, closed-candle predicate, terminal shortcuts | Immutable policy version covering fill window, same-candle, horizon |
| Source binding | Caller-supplied P3A `source_namespace` (diagnostic only) | Durable runtime vs replay vs reconstruction namespace |
| Tracking obligation | Service evaluates only supplied symbols with non-`None` candles | Independent tracker; gap records for omitted symbols |
| Denominator | Physical row counts only | Admission population, coverage/attrition, dependence |

## 5. Reachability table

Producer → admission/rotation/continuation decision → durable facts → current consumer → proved → missing.

| Producer | Decision | Durable facts | Consumer | Proved | Missing |
| --- | --- | --- | --- | --- | --- |
| `generation_rotation_reason` + `new_setup_generation_id` | Same known anchors never rotate; changed known anchors rotate; missing-anchor uses UUID; cooldown/archive rotation only on missing-anchor paths | `lifecycle_id`, `structural_anchor` | Service lookup `get_record(symbol, mode, direction)` where `is_current=1` | Same-anchor continuation; changed-anchor supersession | Admission; independence |
| `SetupLifecycleService._apply_to_symbol_result_with_meta` | Rotation then `evaluate_lifecycle_transition`; evaluate outcomes only if `execution_candles is not None` | Lifecycle row, optional progress, optional analytics | Scanner `run_scan` (lifecycle **before** `store_scan_result`); tests | Ordinary path continuation/rotation | Tracking when symbol or candles omitted |
| `evaluate_closed_candle_outcomes` | Lookup/INSERT by `plan_identity`; eligible states; terminal shortcut | Progress row; P3B1 INSERT-only `plan_version_id`; cutoff; prefix envelope | P3A projection; P2A event counts | Pass-local evaluation | Episode membership; whole history |
| Direct `upsert_record` / `supersede_record` / evaluator | Can place two generations under one reminted `plan_version_id` | Multiple progress rows | P3A `STATUS_AMBIGUOUS_CONTEXT` | Storage-supported fan-out | Ordinary-path duplicate defect (**not demonstrated**) |
| Public `_public_economic_setup_plan` | Mode-neutral hash | `canonical_plan_id` / `event_key` | Telegram delivery | Cross-mode collapse for alerts | Research episode key |
| `StrategyReplayEngine._candidate_key` | `symbol:mode:direction:pullback_tf:sweep_index:entry` | Replay in-memory / `replay_results` | Research replay expectancy | Isolated simulation key | Runtime admission / P1 binding |
| `store_scan_result` | Writes run `raw_payload_json` plus symbol/setup rows | Scan payload duplication | Research queries | Provenance of stored runs | Admission; lossless reconstruction of as-run evaluator inputs |
| `app.research.queries` | Lifecycle-level records/events; replay rows | Reports | Humans / memory | Physical counts | Episode denominator |

**Production callers of `evaluate_closed_candle_outcomes(`:** only `app/lifecycle/service.py` besides the definition in `outcomes.py`. Mentions in `evidence_contract.py` are documentation. `apply_to_run_result` skips `status == "not_run"`. Watch-iteration missing symbols are filled as `not_run` in `scripts/run_scan.py` and therefore also skip evaluation. This is a demonstrated service-path limitation, not a measurement of the live runtime scheduler.

## 6. Same-anchor, changed-anchor, missing-anchor, revision, restart, mode-overlap, and re-entry

### Same known anchor (ordinary producer)

Repeated scans and repository reopen keep one `lifecycle_id`, `setup_id`, `plan_version_id`, and one progress owner. `last_evaluated_at` may advance; that is another **pass**, not another episode.

Existing coverage reused: `test_same_known_structural_anchor_never_rotates_generation`, `test_restart_reuses_active_generation_and_confirmation_progress`, `test_repeated_scan_observation_does_not_churn_persisted_identities`.

New: `test_same_known_anchor_repeated_scan_and_reopen_keep_one_progress_owner`.

### Changed known anchor (ordinary producer)

A later observation with a different known `setup_generation_anchor` rotates. New `lifecycle_id`, new `setup_id`, new `plan_version_id` (plan version includes setup_id). Prior row `is_current=0`. Prior progress is **not** further evaluated on that pass. Supersession does not economically resolve the prior plan.

New: `test_changed_anchor_through_service_supersedes_without_resolving_prior_progress`.

Existing generation tests that plant `COOLDOWN` then observe a new anchor mix a direct write with a producer observation (`test_completed_same_geometry_new_sweep_creates_isolated_generation`, `test_lifecycle_rotation_on_new_anchor_changes_setup_lineage`). Rotation reason in those cases is still `new_structural_anchor` because both anchors are known.

### Same known anchor after cooldown (re-entry is not established)

`generation_rotation_reason` returns `None` for the same known stored/observed anchor in every tested state, including `COOLDOWN` and `ARCHIVED`, because the known-anchor branch precedes cooldown/archive rotation.

Planting `COOLDOWN` (direct writer) then observing the same known anchor through the service **reuses** the generation. Elapsed cooldown is not contemporaneous re-entry admission.

New: `test_same_known_anchor_after_planted_cooldown_reuses_generation` (mixed evidence; cooldown planted).

### Missing anchor

Ordinary missing-anchor path: UUID `lifecycle_id`, `setup_id`/`plan_version_id` unavailable (`missing_structural_anchor`). A second missing-anchor scan continues the same UUID. A later known anchor **while still current** does not rotate (legacy reuse) and may latch P1 IDs onto that UUID.

Missing-anchor + planted expired `COOLDOWN` + another missing-anchor observation rotates with `completed_cooldown_new_setup` and a new UUID. That UUID change is bookkeeping, not proof of a distinct economic opportunity.

New: `test_missing_anchor_uuid_is_bookkeeping_not_economic_opportunity`, `test_missing_anchor_cooldown_rotation_mints_new_uuid_without_setup_id`.

Existing: `test_legacy_active_generation_is_reused_and_backfilled_conservatively`, `test_missing_anchor_leaves_setup_id_unavailable_until_known`.

### Revisions and material boundaries

`TRIGGERED` is not in `PLAN_LOCK_STATES`. Observed geometry can be adopted before lock. After `CONFIRMED`, `_plan_or_observed_value` keeps stored non-`N/A` economics; a later observation with a different TP does not rewrite the latched `plan_version_id`.

Direct mutation after latch preserves the latched id and records `plan_version_invariant_violation`. `proven_progress_plan_version_id` then returns `None` (P3B1 will not attribute a new row to the old owner). ON CONFLICT does not update progress `plan_version_id`.

Material-plan `first_evaluated_at` (P3B2A `test_material_plan_boundary_uses_first_evaluated_at_without_changing_policy`) is the current boundary source when another `plan_identity` already exists on the lifecycle. Ordinary locked observation does not create that second identity. Do not replace that boundary with `confirmed_at` or a fabricated decision clock.

New: `test_triggered_may_adopt_geometry_then_lock_preserves_latched_plan`, `test_p3b1_insert_only_attribution_and_progress_conflict_sql`.

Producer limit (characterized, not repaired): an unlocked `TRIGGERED` observation that adopts an invalid TP ladder cannot transition to `REJECTED` (`TRIGGERED` ↛ `REJECTED`). Confidence decay can then expire a `B+` setup. Invalid ladders were not used as the revision proof above.

### Restart

Existing `test_restart_reuses_active_generation_and_confirmation_progress` plus the reopen in the new continuation test. Restart is continuation.

### Mode overlap

Lifecycle lookup is `(symbol, mode, direction)`. P1 `setup_id` includes mode. Ordinary producer therefore creates two current records and two progress owners for swing vs scalp with the same prices/anchor. Public `_public_economic_setup_plan` is mode-neutral and can assign the same public plan id. Those public ids are delivery identity, not research episodes.

Replay `_candidate_key` is `symbol:mode:direction:pullback_calculation_timeframe:sweep_candle_index:entry`. It is not `plan_version_id`. Changing sweep index changes the replay key without changing P1 economics; scalp vs swing use different fill-window defaults (`_fill_window`). Matching closed-candle cutoffs would still not bind admission, fill, expiry, or exit semantics across runtime and replay.

New: `test_mode_and_replay_keys_are_not_research_episode_keys`. Existing public-mode tests in `tests/test_triggered_confirmed_telegram_delivery.py` remain valid.

### Stored fan-out versus reachable fan-out

Direct writer: two lifecycle ids, same reminted `plan_version_id`, two progress rows, P3A `STATUS_AMBIGUOUS_CONTEXT`. Label: **direct lower-level writer**.

Ordinary same-anchor producer: one generation, one progress row. Label: **normal production path**. Do not treat the storage counterexample as a current-path duplicate defect. An automatic generation merge could destroy legitimate historical contexts while repairing no demonstrated ordinary-path defect.

New: `test_ordinary_same_anchor_producer_does_not_fan_out_same_plan_version`. Existing: `test_same_plan_across_generations_and_distinct_plans_stay_separate`.

## 7. Prospective admission boundary / smallest candidate seam

No existing seam supplies all episode facts.

The smallest **future** seam consistent with current producers is `SetupLifecycleService._apply_to_symbol_result_with_meta`, after `generation_rotation_reason` and plan latch, at the first eligible outcome INSERT for a newly locked `plan_version_id`.

Required missing inputs (not invented here):

- explicit admission decision and time, distinct from plan lock and from progress INSERT
- declared source context with authority (not `scan_run_id` alone)
- immutable evaluation-policy binding covering entry/fill/horizon/same-candle/termination
- continuation vs new-episode rule that matches the producer proofs above
- tracking obligation or explicit gap when the symbol/candles are omitted
- unresolved handling for missing-anchor UUIDs and direct-writer fan-out

Naming this seam does **not** authorize implementing it.

## 8. Why current keys and latest-pass envelopes cannot establish the denominator

Required populations, kept separate (no performance metrics computed):

| Population | Current physical unit | Trustworthiness |
| --- | --- | --- |
| Sampled candidate/decision observations | Scan symbol rows / lifecycle last-seen updates | Observational only |
| Admitted plan-evaluation episodes | **None** | Missing contract |
| Episodes with evidenced simulated entry | Progress rows with `entry_at` | Not scoped to admissions |
| Episodes with supported resolved outcomes | Progress `terminal_outcome` / analytics `(lifecycle_id, final_outcome)` | Not episode-scoped; analytics may be unbound |
| Unresolved / unfilled / expired / invalidated / censored / conflicting | Raw terminals, NULL attribution, P3A statuses | Must remain visible |

**How far are we from a trustworthy denominator?** Missing contracts, not a percentage:

- prospective admission and continuation/re-entry relationship
- immutable economics **and** policy/source-context binding
- complete observation-population accounting
- independent tracking and required causal price-path evidence
- explicit terminal, gap, and censoring treatment
- episode-scoped outcome reconciliation and consumer migration
- dependence-aware sampling and validation

Unique executed-trade/fill identity is an additional blocker for **real** execution statistics. It does not block all controlled setup simulations, and those simulations must not silently claim it.

TP1/TP2 are milestones. TP1 then SL retains both. `TP_HIT` without proved TP3 is not TP3. A missing entry timestamp is not proof of no entry. Completion of W or P is not a win, loss, or whole-history completion.

Why existing artifacts fail as a denominator:

- `lifecycle_id` is a generation, not an admission.
- `plan_version_id` can be shared across imported generations or absent on missing-anchor rows.
- `plan_identity` includes `lifecycle_id`; it is a physical progress key.
- Analytics uniqueness is lifecycle/final-outcome.
- `scan_run_id` is optional event association and is not stored on progress (`test_scan_run_id_is_optional_event_association_not_evaluation_owner`, reused; new underdetermination test).
- Last P3_PREFIX envelope is last accepted pass, not coverage and not admission.
- Research queries read lifecycle rows and replay rows; replay expectancy is not an admitted-episode rate.

## 9. Replay, storage, tracking-liveness, and analytics readiness

| Topic | Ready now | Not ready |
| --- | --- | --- |
| Bounded synthetic causal reproduction for explicitly supplied cases | Yes (existing replay causality tests; this phase does not add a runner) | Promoting replay rows into authoritative production outcomes |
| Frozen economics for a bounded fixture | Can be supplied and checked | Complete historical economic reconstruction |
| Runtime vs replay | Both have closed-candle foundations | Identical admission, price-path availability, fill, expiry, exit; namespace binding |
| Outcome comparison | Fixture/context level without counting lifecycle rows as trades | Production observation/outcome reproduction |
| Policy terminal | Legal early terminal ends the required path | Treating missing later candles as a gap |
| Historical download | Labeled reconstruction possible | Proof of what the runtime had at decision time |
| Storage duplication | `store_scan_result` still writes run payload plus symbol payloads; original ~81.21 GiB / ~3.65 GB/day figures are historical audit estimates, not current measurements | Dedup/migration in this phase (not authorized) |
| Tracking liveness | Characterized: omitted symbol or `lifecycle_execution_candles is None` does not evaluate | Scheduler/independent tracker (not authorized) |
| Analytics | P3A remains a read-only supplied-record projection | Canonical ownership; consumer migration |

No replay redesign, bulk history acquisition, parameter sweep, or expectancy output is authorized by this phase.

Strategy destination (hypothesis only): poor counter-market shorts in strong bullish conditions remain a research question. No short veto, CMC, regime heuristic, or 75%+ accuracy gate.

## 10. Updated original-audit A–W matrix

RESOLVED always has the stated scope; it never means historical runtime repair.

| ID | Finding | Status | Scope, source, tests, remaining limit |
| --- | --- | --- | --- |
| A | Evidence/reproducibility | PARTIALLY RESOLVED | P0 contracts + run provenance exist (`app/storage/run_provenance.py`, `tests/test_run_provenance.py`). Exact-as-run outcome replay and historical datasets are not established. |
| B | Economic identity | FOUNDATION PRESENT BUT NOT CONSUMED | P1 mint/latch (`app/lifecycle/economic_identity.py`, `tests/test_economic_identity.py`); P3B1 INSERT-only (`tests/test_outcome_plan_attribution_p3b1.py`). Consumers not migrated. |
| C | Activation accounting | RESOLVED | P2A scoped watch/event semantics (`tests/test_activation_accounting_truth.py`). Not unique fills/trades. |
| D | Lifecycle ownership | RESOLVED | P2B ownership boundary (`tests/test_lifecycle_ownership_repair_p2b.py`). Not an independent tracker. |
| E | CONFIRMED/ACTIONABLE oscillation | RESOLVED | Prospective protected transitions. No historical rewrite. |
| F | Outcome ownership | PARTIALLY RESOLVED | Physical owner + P3A diagnostics (`app/analytics/outcome_ownership.py`). Authoritative research ownership is not. |
| G | plan_version_id outcome attribution | PARTIALLY RESOLVED | First-INSERT only; ON CONFLICT does not update (`tests/test_p3b1_insert_only_attribution_and_progress_conflict_sql`). No legacy recovery. |
| H | Causal evaluation context | PARTIALLY RESOLVED | P3B2A/B + P3_PREFIX. Admission/source/policy binding and full interval history remain incomplete. |
| I | Exact eligibility cutoff durability | RESOLVED | Last accepted committed qualifying application only (`tests/test_durable_eligibility_cutoff_p3b2b.py`). |
| J | Supplied-prefix disposition | PARTIALLY RESOLVED | Truthful last-pass W/P/C (`tests/test_prefix_disposition_evidence_p3_prefix.py`). Not whole-interval coverage. |
| K | Lifecycle-generation fan-out | PARTIALLY RESOLVED | Same-known-anchor guarded (`tests/test_lifecycle.py`, new continuation/rotation tests). Storage/import fan-out remains possible and is **not** a demonstrated ordinary-path duplicate defect. |
| L | Analytics ownership | STILL OPEN | `UNIQUE(lifecycle_id, final_outcome)`; no plan column. |
| M | Unique trade/fill occurrence | STILL OPEN | `unique_trade_count` UNAVAILABLE (`evidence_contract_payload`). Simulated research episode ≠ real user fill. |
| N | Canonical plan-level outcome | STILL OPEN | Requires a declared evaluation context. Global one-result-per-`plan_version_id` is not justified (P3A ambiguity on direct-writer fan-out). |
| O | Replay | FOUNDATION PRESENT BUT NOT CONSUMED | Causal candle filtering exists (`tests/test_replay_causality_phase2.py`, `tests/test_strategy_replay.py`). Replay is not lifecycle outcome authority. Candidate key ≠ episode key. |
| P | Trustworthy expectancy denominator | STILL OPEN | Admission unit unresolved (this contract). Research queries still use lifecycle/replay rows (`app/research/queries.py`). |
| Q | TARGET_INSIDE_CHOP / target research | DEFERRED INTENTIONALLY | No target-policy change. |
| R | Raw payload duplication / DB growth | STILL OPEN | `store_scan_result` retains run results plus symbol payloads. Current runtime size unmeasured here. |
| S | Regime/context intelligence | DEFERRED INTENTIONALLY | Existing components ≠ validated directional compatibility. |
| T | CMC integration | DEFERRED INTENTIONALLY | No eligibility effect. |
| U | Setup-family / target / strategy redesign | DEFERRED INTENTIONALLY | Needs trustworthy comparison population. |
| V | Performance memory | DEFERRED INTENTIONALLY | Untrusted labels. |
| W | Champion/challenger research | DEFERRED INTENTIONALLY | Unit unresolved. |

## 11. Acceptance table

| Claim | Executed test or explicit limit |
| --- | --- |
| Schema remains v24; no admission columns | `test_fresh_synthetic_schema_is_v24_without_admission_columns` |
| Same-anchor continuation is one progress owner | `test_same_known_anchor_repeated_scan_and_reopen_keep_one_progress_owner`; existing same-anchor/restart tests |
| Changed-anchor service rotation does not resolve prior progress | `test_changed_anchor_through_service_supersedes_without_resolving_prior_progress` |
| Same-anchor after cooldown is not re-entry admission | `test_same_known_anchor_after_planted_cooldown_reuses_generation` (cooldown planted) |
| Missing-anchor UUID ≠ distinct opportunity | `test_missing_anchor_uuid_is_bookkeeping_not_economic_opportunity` |
| Missing-anchor cooldown rotation is UUID bookkeeping | `test_missing_anchor_cooldown_rotation_mints_new_uuid_without_setup_id` |
| Ordinary producer does not fan out same `plan_version_id` | `test_ordinary_same_anchor_producer_does_not_fan_out_same_plan_version` |
| Direct-writer fan-out stays ambiguous | same test + existing P3B2A/P3B1 generation tests |
| Unlock then lock preserves latched plan; invariant retained | `test_triggered_may_adopt_geometry_then_lock_preserves_latched_plan` |
| P3B1 INSERT-only SQL | `test_p3b1_insert_only_attribution_and_progress_conflict_sql` |
| Omitted symbol / omitted candles do not evaluate | `test_omitted_symbol_and_missing_candles_do_not_advance_progress` |
| Sole production evaluator caller is the lifecycle service | same test (repository walk of `app/` and `scripts/`) |
| Keys cannot recover admission/source | `test_existing_keys_cannot_recover_admission_or_source_context`; existing P3A/P3B2A namespace/completeness tests |
| Mode-neutral public id and replay key ≠ episode | `test_mode_and_replay_keys_are_not_research_episode_keys` |
| Canonical outcome, unique trades, complete context remain unproven | `test_claim_preservation_markers_remain_unproven`; existing P2A–P3_PREFIX suites |
| Live deployment tracking completeness | **Unproven.** Service-path limitation only. |
| Ordinary-path duplicate generations under one plan version | **Not demonstrated.** |
| Production admission population | **Unavailable.** Not invented. |

## 12. Exact boundaries for a later Prospective Research Admission Evidence phase

Authorized later only by a fresh ASTRA decision. This contract constrains it:

- Prospective only. No historical backfill. No rewriting NULL legacy attribution.
- Do not substitute a trade/fill UUID for an admission.
- Do not impose global `UNIQUE(plan_version_id)` outcomes.
- Do not reconstruct history from latest-pass envelopes.
- Do not merge unsupported generation relationships (especially not to “fix” direct-writer fan-out).
- Do not drop unresolved observations.
- Do not invent policy/source provenance or a fictional historical policy hash.
- Do not add re-entry because cooldown expired or the same anchor reappeared.
- Do not treat public mode-neutral ids or replay candidate keys as episode keys.
- Do not enable a tracker/scheduler unless admission membership is already explicit.
- Do not change strategy, RR/quality gates, confirmation ownership, public `event_key`/`message_hash`, or execution safety.
- Schema/production Python remain unchanged in R0; a later phase that persists admission evidence is a separate, bounded implementation.

## Invariants preserved

RR and setup-quality gates, P2B confirmation ownership, public delivery identities, `ORDER_EXECUTION_ENABLED=false` default, no withdrawals, Dev-PC Telegram dry-run, no live DB access, SCHEMA_VERSION 24.

**CORRECTNESS > COMPLETION**
