# TP1 reference breakeven

Implemented on `fix/tp1-reference-breakeven`, based on merged Runtime release `e61a7e392db8fdbb6a9d21357ec933d5bc4ad97c`. DEV verification and draft review only; not merged or deployed.

## Authorized behavior

Adam selected the entry-zone midpoint as the setup reference, explicitly not an exchange fill. TP1 activates reference-breakeven protection; TP2 retains it. A later reference-stop exit is BREAKEVEN and retains prior TP milestones. TP1 Telegram updates instruct moving SL to breakeven and show the reference price. Existing active progress adopts the rule prospectively with a persisted boundary; completed history is unchanged.

## Implementation plan and boundaries

1. Persist versioned stop-management metadata and a distinct BREAKEVEN lifecycle outcome without overwriting original plan geometry or changing schema 26.
2. New progress uses the new policy. Existing nonterminal progress first records its adoption at the current decision cutoff; older pending candles retain the old policy. The new stop applies only to candles opening at/after the adoption cutoff and after the candle establishing TP1. Persist adoption even when there is no new candle. Invalid/unknown policy metadata fails closed.
3. Keep conservative stop-first ambiguity handling: an original stop and first TP1 in the same candle is SL; after protection is active, reference stop and further target in the same candle is BREAKEVEN. Do not infer intrabar order or retroactively reprocess cursors. No actual fill, partial-exit allocation, net PnL, costs, or guaranteed execution is implied.
4. Integrate terminal handling, reporting, policy manifests/replay compatibility, Telegram reservation/outbox/routing and active-watchlist closure. Preserve original TP and stop event identities; give BREAKEVEN its own event.
5. Test long/short, TP1/TP2, return to midpoint before original stop, same-candle ambiguity, persistence/restart, active adoption, unchanged closed history, public formatting/delivery, policy identity and research counts. Run focused checks then full pytest and compilation.
6. Commit and push the feature branch, create a draft PR with precise validation and rollout limits. No merge or Runtime operation is authorized by this implementation task.

Capture stays off, manual mode on, order execution off. Public quality 88/grade A/RR 3 and immutable economic identities are preserved. Synthetic databases only; no live Runtime access or synthetic public sends. Older code cannot be blindly rolled back onto new BREAKEVEN state; deployment needs updated-reader compatibility.

## Delivered behavior

- The canonical closed-candle evaluator stores `stop_management` evidence alongside progress: version, midpoint reference, adoption timestamp, prior policy, and the effective candle-open boundary. Original entry, targets, stop and economic identity remain unchanged.
- For new progress, TP1 enables midpoint protection from the following candle. Existing active progress first saves the current decision cutoff and preserves the original policy for older pending candles. Adoption does not rewind the cursor. TP2 retains protection.
- A protected retracement creates one `BREAKEVEN` terminal event and keeps TP1/TP2 milestones. It closes active tracking, uses the existing Telegram outbox, and counts separately from `SL_HIT` in research reports.
- Unknown or malformed stop-policy metadata prevents outcome advancement. Public breakeven delivery requires supporting policy and chronological milestone evidence. Uncertain transport is not automatically resent; retryable transport uses the existing retry policy.
- The Runtime evaluation manifest now identifies rules vocabulary v2. F04 capture fingerprints include the new stop-policy implementation; new synthetic captures reproduce offline. Historical fixed-stop reports and the separate StrategyReplay simulator are unchanged.

Example TP1 update for entry zone 100–102:

> Move SL to breakeven.
>
> Setup reference: 101 (entry-zone midpoint; not your actual fill).

TP2 repeats the move/keep instruction. A later protected exit says `BREAKEVEN`, explicitly retains earlier milestones, and makes no net-profit or actual-fill claim.

## Regression protection

Tests cover long and short setups, TP1 and TP2, reference touches before the original stop, conservative candle ambiguity, restart persistence, prospective adoption, unchanged completed history, malformed policy evidence, Telegram delivery/deduplication/retry/uncertainty, active-list closure, and owner monitoring through captured offline replay and research counts.

Existing tests that intentionally reached TP before the stop now assert BREAKEVEN. Static historical fixed-stop artifacts remain unchanged. The fake retry sender now sets a future retry time and tests explicitly make it due: the prior zero-second delay could race same-pass recovery across a wall-clock second. Retry assertions are retained and strengthened.

DEV verification on 2026-10-11:

- Full pytest: **2959 passed**, one pre-existing Starlette deprecation warning, 472.26 seconds.
- Focused breakeven and public-delivery regression: **112 passed**, including all 24 new breakeven cases.
- Related lifecycle, evaluation semantics, ownership, P5A monitoring, RR and active-watchlist regression: **169 passed**.
- Python compilation and Git diff whitespace check: passed.
- No tests skipped or marked xfail. No assertions weakened.

All full-suite test databases used the repository-recognized `pytest_tmp` path. A prior custom path bypassed synthetic test identity setup; it was corrected without altering isolation checks. Exact-head CI results belong to the PR checks and must be verified before merge.

## Runtime rollout requirements

This change is not active on Runtime until separately merged and deployed. Upgrade scanner, listener, and database readers together while the services are stopped, then restart the existing single instances using their established configuration. Existing active setups adopt the policy during subsequent canonical evaluation; there is no bulk historical rewrite or artificial catch-up message.

SQLite schema stays 26, but the new BREAKEVEN enum is a reader compatibility change. After it has been written, a code-only rollback to an older reader is not assumed safe. Preserve the live database and use a reviewed compatibility fix if rollback is needed; do not relabel outcomes or restore an older snapshot over new Runtime history.
