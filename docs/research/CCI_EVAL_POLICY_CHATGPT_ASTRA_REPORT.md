# CCI CURSOR → CHATGPT / ASTRA RETURN REPORT

**Phase:** EVAL_POLICY — Evaluation Policy Manifest (unbound)

**How to use:** Copy the ASTRA report block below into ChatGPT. Do not paraphrase. Do not omit the STOP / unavailable claims.

---

BEGIN ASTRA REPORT

# CCI EVAL_POLICY — Cursor Return Report for ASTRA

**Date:** 2026-09-09
**Operator:** Cursor Cloud Agent (`bc-8e47a059-258a-4683-8a47-064492e5f8b3`)
**Repo:** `candlecraftinteligence/candle-craft-trading-agent`
**PR:** https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/117
**Branch:** `cursor/evaluation-policy-manifest-f8b3`
**Base / audited `origin/main`:** `afbc4ae75cbc886daea09cb536ae577661b96ada` (merge of PR #116 STORAGE_SINGLE_COPY)
**First implementation head:** `4838495000919aca265dd87ac770b218ab89a41c`
**Schema:** application `SCHEMA_VERSION` / SQLite `PRAGMA user_version` = **25** unchanged
**Manifest version:** `cci-evaluation-policy-manifest-v1`

Prompt received by Cursor was the Windows path
`c:\Users\aspir\Downloads\CCI_EVALUATION_POLICY_MANIFEST_Cursor_Master_Prompt.md`.
The file body was **not** present in the cloud workspace. This phase was implemented from
EVAL_SEMANTICS remaining work (PR #115) plus STORAGE_SINGLE_COPY as the current main base.
If ASTRA's original prompt required additional obligations beyond the unbound nine-dimension
manifest, those obligations were not executed.

**CORRECTNESS > COMPLETION**

## 1. Mission result

Finite question answered:

**What is the current implemented evaluation-policy surface, which tokens are forbidden
surrogates for a binding, and is that surface complete and persisted?**

Answers:

1. The nine EVAL_SEMANTICS dimensions are now an inspectable catalog.
2. None of the listed surrogates is an `evaluation_policy_id` / `policy_hash`.
3. The surface is **not** complete and **not** persisted.

This is **not** an immutable evaluation-policy binding.

## 2. Files changed

| Path | Role |
| --- | --- |
| `app/analytics/evaluation_policy.py` | Diagnostic catalog only. Does not evaluate candles, open a DB, or persist IDs. |
| `docs/research/evaluation_policy_manifest.md` | Human research contract. |
| `docs/research/CCI_EVAL_POLICY_CHATGPT_ASTRA_REPORT.md` | This copy-paste return report. |
| `tests/test_evaluation_policy_manifest.py` | Synthetic/source proofs. |

No evaluator, schema, migration, settings, Telegram, scanner, RR/quality gate, or public-interface file was modified.

## 3. What was proven

Machine-readable catalog: `evaluation_policy_manifest()` is deterministic JSON.
`complete_immutable_evaluation_policy_binding` is **false** for runtime and replay.
Every `evaluation_policy_id` / `policy_hash` / `admission_id` / `evaluation_episode_id`
key in the payload is `unavailable`.

Required dimensions (all unbound):

| Dimension | Runtime | Replay |
| --- | --- | --- |
| entry_model | zone overlap `entry_touched` | point touch `_price_touched`; `entry_low`/`entry_high` unused by `_simulate_trade` |
| boundary_alignment | confirmation or material-plan `first_evaluated_at`; fully-post-boundary | detection index + 1 |
| execution_timeframe | progress row must match | `ReplayConfig.execution_timeframe` default `15m` |
| price_basis | supplied OHLC; receipt time unavailable | supplied OHLC; no fetch inside `_simulate_trade` |
| same_candle_precedence | conservative stop-wins; local `ambiguity_policy` metadata | `ReplayConfig.same_candle_policy`; `_evaluate_exit_candle` unused |
| fill_rules | first fully post-boundary closed bar; no fill window | detection+1 inside fill window |
| termination | evaluator SL/TP3; lifecycle shortcut may copy EXPIRED/INVALIDATED | `ReplayOutcome` + modeled R |
| expiry_horizon | no hold window; end of input ≠ economic terminal | fill/hold windows; `NOT_FILLED` / hold `EXPIRED` |
| target_semantics | entry candle cannot credit targets; TP1/TP2 milestones retained through later `SL_HIT`; `TP_HIT` only after TP3 | fill candle can credit targets; prior TP then stop can return that TP with `sl_hit=False` |

Re-executed here: zone overlap without point touch remains a named policy difference.
Other predicate claims are source-inspected and pointed at existing EVAL_SEMANTICS tests.
The full paired candle matrix was **not** re-run in this phase.

Forbidden surrogates (all refused; digest is never copied into an id):

`source_sha256`, `pinned_git_sha`, `schema_version`, `pragma_user_version`, `scan_run_id`,
`database_filename`, `script_name`, `caller_name`, `safe_configuration_hash`,
`algorithm_marker`, `document_revision`, `fixture_label`, `raw_payload_format`,
`ambiguity_policy_metadata`, `entry_causality_contract`, `replay_candidate_key`,
`plan_version_id`, `plan_identity`, `lifecycle_id`.

Unknown kinds are also refused.
Local markers that are **not** complete bindings:

- `metadata_json.ambiguity_policy = conservative_stop_wins`
- `ENTRY_CAUSALITY_CONTRACT = fully_post_boundary_closed_candle_v1`
- progress `source = canonical_lifecycle_closed_execution_candles`
- `scan_runs.raw_payload_format` (`inline_v1` / `symbol_refs_v1`) — STORAGE_SINGLE_COPY representation only

`ReplayConfig` differences are a declared experiment envelope, not a persisted policy.
Unset `max_fill_candles` / `max_hold_candles` stay `unset_uses_mode_timeframe_default`.
Current code defaults are not historical defaults for past rows.

Absent / externally controlled remain **N/A**, not zero: fees, slippage, queue position,
partial fills, stop movement, quantity fractions, live order-book fills, original
market-data possession at decision time, admission cause/time, continuation vs re-entry,
dependence groups.

Schema proof: fresh synthetic DB is `user_version` 25. No `evaluation_policy_id`,
`policy_hash`, `admission_id`, `evaluation_episode_id`, `source_namespace` columns.
No `admitted_evaluation_episodes` or `evaluation_policies` tables.

Production walk: `evaluation_policy_id` / `policy_hash` do not appear under `app/` or
`scripts/` except this diagnostic module. The only normal `app/`/`scripts/` caller of
`evaluate_closed_candle_outcomes(` remains `app/lifecycle/service.py`.

## 4. What was explicitly NOT done

- No production evaluator change.
- No schema / migration / backfill.
- No `evaluation_policy_id` or `policy_hash` minting.
- No hashing of source or git SHA into a policy.
- No source-context binding.
- No prospective admission persistence. **Admission STOP remains.**
- No tracker / scheduler / pass ledger.
- No replay-as-runtime-outcome-authority.
- No expectancy, win rate, unique-trade count, or P&L.
- No RR / setup-quality / confirmation / public `event_key` / Telegram change.
- No CMC / regime / target / directional-veto / champion-challenger work.
- No live runtime database access.
- No merge.

P3A `canonical_outcome_per_plan_version.established` remains false.
`evidence_contract_payload()["unique_trade_count"]["status"]` remains `unavailable`.
`TRIGGERED` remains outcome-eligible and unlocked. Plan lock is not admission.

## 5. Tests

Focused: `tests/test_evaluation_policy_manifest.py`

Local full suite: `python -m pytest` → **2430 passed** at `4838495`, 2 existing
Starlette/httpx deprecation warnings.

GitHub Actions on `4838495`: run 34316100057, job 102352525701, **passed**.

A later commit that only adds this return report will have a new head SHA; prefer that
head's Actions run over 34316100057 if they disagree.

No live or existing scan database was read. Fixtures are synthetic.

## 6. Safety invariants preserved

- `ORDER_EXECUTION_ENABLED=false` default; no withdrawals; no transfers.
- Dev-PC Telegram dry-run / no live Telegram enablement in this PR.
- Public RR and setup-quality gates unchanged.
- P2B confirmation ownership unchanged.
- Live `scan_runs\main_live_runtime.sqlite` not accessed.

## 7. Original-audit A–W delta

RESOLVED always has stated scope. The catalog existing does not solve a finding.

| ID | Status after this PR | Note |
| --- | --- | --- |
| H | PARTIALLY RESOLVED | Policy **surface** is now inspectable and unbound. Source-context, admission, full coverage still missing. |
| O | FOUNDATION PRESENT, SEMANTIC NON-EQUIVALENCE PROVED | Unchanged. This phase names the unbound dimensions. Replay is still not runtime outcome authority. |
| R | PARTIALLY RESOLVED | STORAGE_SINGLE_COPY (v25) already on main. Historical duplicate `results[]` remains. Not a policy binding. |
| P | STILL OPEN | Admission unit unresolved. |
| N | STILL OPEN | Global `UNIQUE(plan_version_id)` still unjustified. |
| M | STILL OPEN / UNAVAILABLE | Unique trade/fill identity. |
| A–G, I–L, Q, S–W | Unchanged from EVAL_SEMANTICS / STORAGE_SINGLE_COPY | |

## 8. Remaining blockers (unchanged in kind)

1. Explicit prospective pre-outcome admission and complete population accounting.
2. Immutable economics **plus** authoritative source-context binding **plus** complete evaluation-policy binding.
3. Continuation / retry / restart versus new admission / re-entry.
4. Episode-owned tracking attempts or explicit gaps, plus interval coverage.
5. Legal terminal, unfilled, censored, unresolved, and conflicting-evidence handling without outcome-based attrition.
6. Episode-scoped outcome reconciliation and analytics/consumer migration.
7. Dependence-aware validation; episode identity ≠ independent samples.

## 9. Next candidate — NOT authorized here

If ASTRA later authorizes work, the named next candidate remains:

**Prospective Evaluation Context Binding** — authoritative source context **and** an
immutable evaluation-policy binding that names all nine dimensions, refuses the
forbidden surrogates, leaves absent external controls unavailable, and still does not
treat replay as runtime outcome authority.

This report names that object. It does not create it. Do not implement it from this
return report.

## 10. Honesty limits

- The original master-prompt file body was not available to Cursor.
- Same-opportunity producer parity between scanner discovery and replay discovery is **not** established.
- Live DB size/runway was **not** measured.
- Current runtime/replay code is **not** a historical default for past rows.
- Characterized engine disagreement is evidence, not a defect to “fix” in this phase.

END ASTRA REPORT
