# TP1 reference breakeven

Implemented on `fix/tp1-reference-breakeven`, based on merged Runtime release `e61a7e392db8fdbb6a9d21357ec933d5bc4ad97c`. DEV verification and draft review only; not merged or deployed.

## Authorized behavior

The first head, `e89750a29e929e6b026a484a447367fa7bb0aab3`, was rejected on review: a missing outcome-progress row did not establish new-policy provenance. The correction binds every unbound plan at its first decision cutoff, preserves original-stop behavior for older pending candles, versions the binding to v2, and tests missing-progress owners through production owner monitoring. No schema, gate, Runtime data, or order-execution change is included.

Adam selected the entry-zone midpoint as the setup reference, explicitly not an exchange fill. TP1 activates reference-breakeven protection; TP2 retains it. A later reference-stop exit is BREAKEVEN and retains prior TP milestones. TP1 Telegram updates instruct moving SL to breakeven and show the reference price. Existing active progress adopts the rule prospectively with a persisted boundary; completed history is unchanged.

## Implementation plan and boundaries

1. Persist versioned stop-management metadata and a distinct BREAKEVEN lifecycle outcome without overwriting original plan geometry or changing schema 26.
2. Every unbound plan records adoption at the current decision cutoff, including a previously owned plan with no progress row. Clamp the cutoff to at least the prior cursor close when present. Older pending candles retain the original stop. Newly tracked setups establish the same durable boundary on their first evaluation. The new stop applies only to candles opening at/after that boundary and after the candle establishing TP1. Persist adoption even when there is no new candle. Invalid/unknown policy metadata fails closed.
3. Keep conservative stop-first ambiguity handling: an original stop and first TP1 in the same candle is SL; after protection is active, reference stop and further target in the same candle is BREAKEVEN. Do not infer intrabar order or retroactively reprocess cursors. No actual fill, partial-exit allocation, net PnL, costs, or guaranteed execution is implied.
4. Integrate terminal handling, reporting, policy manifests/replay compatibility, Telegram reservation/outbox/routing and active-watchlist closure. Preserve original TP and stop event identities; give BREAKEVEN its own event.
5. Test long/short, TP1/TP2, return to midpoint before original stop, same-candle ambiguity, persistence/restart, active adoption, unchanged closed history, public formatting/delivery, policy identity and research counts. Run focused checks then full pytest and compilation.
6. Commit and push the feature branch and update existing draft PR #132 with precise validation and rollout limits. No merge or Runtime operation is authorized by this implementation task.

Capture stays off, manual mode on, order execution off. Public quality 88/grade A/RR 3 and immutable economic identities are preserved. Synthetic databases only; no live Runtime access or synthetic public sends. Older code cannot be blindly rolled back onto new BREAKEVEN state; deployment needs updated-reader compatibility.

## Delivered behavior

- The canonical closed-candle evaluator stores `stop_management` evidence alongside progress: version, midpoint reference, adoption timestamp, prior policy, and the effective candle-open boundary. Original entry, targets, stop and economic identity remain unchanged.
- All unbound progress saves the current decision cutoff before processing evidence. Missing progress is never treated as proof of new-policy provenance. TP1 enables midpoint protection from the following candle at/after that boundary, and TP2 retains it. Older pending candles keep the original stop; adoption does not rewind the cursor.
- A protected retracement creates one `BREAKEVEN` terminal event and keeps TP1/TP2 milestones. It closes active tracking, uses the existing Telegram outbox, and counts separately from `SL_HIT` in research reports.
- Binding policy `tp1_entry_zone_midpoint_breakeven_v2` requires a non-null cutoff and the original-stop predecessor. Unknown/malformed bindings, including the rejected v1 policy, prevent outcome advancement. Public breakeven delivery requires supporting policy and chronological milestone evidence. Uncertain transport is not automatically resent; retryable transport uses the existing retry policy.
- The Runtime evaluation manifest now identifies rules vocabulary v2. F04 capture fingerprints include the new stop-policy implementation; new synthetic captures reproduce offline. Historical fixed-stop reports and the separate StrategyReplay simulator are unchanged.

Example TP1 update for entry zone 100–102:

> Move SL to breakeven.
>
> Setup reference: 101 (entry-zone midpoint; not your actual fill).

TP2 repeats the move/keep instruction. A later protected exit says `BREAKEVEN`, explicitly retains earlier milestones, and makes no net-profit or actual-fill claim.

## Regression protection

Tests cover long and short setups, TP1 and TP2, reference touches before the original stop, conservative candle ambiguity, restart persistence, prospective adoption, unchanged completed history, malformed policy evidence, Telegram delivery/deduplication/retry/uncertainty, active-list closure, and owner monitoring through captured offline replay and research counts.

Existing tests that intentionally reached TP before the stop now assert BREAKEVEN. Static historical fixed-stop artifacts remain unchanged. The fake retry sender now sets a future retry time and tests explicitly make it due: the prior zero-second delay could race same-pass recovery across a wall-clock second. Retry assertions are retained and strengthened.

Historical DEV verification on 2026-10-11 for the rejected first head:

- Full pytest: **2959 passed**, one pre-existing Starlette deprecation warning, 472.26 seconds.
- Focused breakeven and public-delivery regression: **112 passed**, including all 24 new breakeven cases.
- Related lifecycle, evaluation semantics, ownership, P5A monitoring, RR and active-watchlist regression: **169 passed**.
- Python compilation and Git diff whitespace check: passed.
- No tests skipped or marked xfail. No assertions weakened.

Those passing checks did not cover an existing owner without progress and are not acceptance of that head. All full-suite test databases use the repository-recognized `pytest_tmp` path. A prior custom path bypassed synthetic test identity setup; it was corrected without altering isolation checks.

## Missing-progress correction and review

The review reproduction used a valid epoch-owned locked plan confirmed at 00:00, entry and TP1 evidence, an original-stop candle ending at 00:20, and policy adoption at 00:25. The first head returned BREAKEVEN when no progress row existed, but SL_HIT when a baseline progress row existed. The corrected code records cutoff 00:25 and SL_HIT in both cases. These are synthetic times and outcomes, not a live Runtime audit.

The correction removes the existing/new-progress switch entirely. This avoids guessing when an owner first became subject to the policy. A new setup adopts at initial tracking; later TP1 and retracement still produce BREAKEVEN. An older setup can retain earlier TP1 evidence and gain protection only for candles opening at/after its saved adoption boundary. The boundary survives reopening and repeated evaluations.

Eleven new regression cases in `tests/test_tp1_breakeven_adoption.py` cover long/short missing-progress owners with TP1/TP2 before an original stop, old midpoint touches followed by protected touches after restart, newly tracked owners, rejected v1/null bindings, and the manifest's declared semantics. Existing breakeven delivery fixtures now establish the policy at an entry-time snapshot before adding later outcomes. Paired evaluator fixtures explicitly adopt before simulating the subsequent TP/stop sequence. The evaluation-context comparison uses one common adoption point before varying later evaluation cutoffs; all original assertions remain, with an additional assertion proving that adoption is distinct from those later cutoffs.

Correction verification on 2026-10-12:

- Exact review reproduction: both missing-progress and baseline-progress owners now retain SL_HIT for the pre-adoption stop, with cutoff 00:25.
- Full local run: **2969 passed, 1 failed**, one existing Starlette warning, 985.47 seconds. The sole failure was the evaluation-context fixture comparing different first-adoption cutoffs; that run had already collected the earlier fixture.
- After correcting that fixture, the focused breakeven/adoption/evaluation-context run passed **57 tests** in 24.44 seconds, retaining all original context assertions. No production code changed after the full run started.
- Compilation and Git diff whitespace checks passed. No tests skipped/xfail and no assertions weakened.
- Final full-suite acceptance must use successful CI on the corrected exact commit. The result and run URL are recorded in PR #132 and the accompanying correction review after publication; the rejected head's CI is not acceptance evidence.

## Runtime rollout requirements

This change is not active on Runtime until separately merged and deployed. Upgrade scanner, listener, and database readers together while the services are stopped, then restart the existing single instances using their established configuration. Existing active setups adopt the policy during subsequent canonical evaluation; there is no bulk historical rewrite or artificial catch-up message.

The supplied Runtime handoff `TP1_BREAKEVEN_RUNTIME_BLOCKED_2026-10-11.md` records BLOCKED_BEFORE_MUTATION: no downtime, no new release/data writes, both services remained on `e61a7e3`. That is evidence about the rejected rollout attempt, not authorization to deploy a replacement. The current live DB and the `scan_runs` junction both resolve to S: paths and must be rediscovered from current process/configuration evidence; preserve the junction and state paths. Keep the authorized code-only storage exception.

SQLite schema stays 26, but the new BREAKEVEN enum is a reader compatibility change. After it has been written, a code-only rollback to an older reader is not assumed safe. Preserve the live database and use a reviewed compatibility fix if rollback is needed; do not relabel outcomes or restore an older snapshot over new Runtime history.
