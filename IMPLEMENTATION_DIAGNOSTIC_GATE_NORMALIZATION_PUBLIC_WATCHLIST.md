# Diagnostic Gate Normalization for Public Watchlist

## 1. Summary

This phase adds a narrow diagnostic normalization layer for public WATCHLIST eligibility. Public WATCHLIST now evaluates failed gates by normalized class, not raw scanner text, and is allowed only when the normalized failed-gate class set is exactly:

`{REGIME_MARKET_CONDITION_PENDING}`

Malformed diagnostics, missing-data gate codes, unknown gate codes, target-integrity failures, RR failures, stale/terminal states, and mixed regime plus non-regime failures are blocked with explicit reasons.

Public execution signal gating was not weakened. `LIMIT_HIT` remains non-eligible as a fresh public execution signal.

## 2. Exact Allowlisted Regime/Market Codes

The public WATCHLIST normalizer maps only these failed-gate codes to `REGIME_MARKET_CONDITION_PENDING`:

- `regime_compatibility`
- `regime_blocked`
- `regime_not_confirmed`
- `market_condition_blocked`
- `market_condition_not_ready`
- `btc_eth_regime_blocked`
- `rejected_by_regime`

Scanner emission currently centers on `regime_compatibility`, with fallback handling for `regime_blocked` and `rejected_by_regime`.

## 3. Missing Data and Unknown Code Behavior

Missing-data failed-gate codes are explicitly blocked:

- `missing_regime_data`
- `regime_data_missing`
- `missing_market_data`
- `market_data_missing`

Additional blocked cases:

- Missing regime display fields block as `public_watchlist_regime_data_missing:...`
- Empty, `None`, `N/A`, or `NaN` failed-gate diagnostics do not satisfy the regime-pending rule.
- Mapping/list malformed failed-gate diagnostics block as `public_watchlist_malformed_failed_gate_diagnostics`.
- Unknown failed-gate codes block as `public_watchlist_unknown_failed_gates=...`.
- Mixed normalized classes block as `public_watchlist_conflicting_failed_gate_classes=...`.
- Target integrity remains blocked via `target_integrity_failed:*`.

## 4. Public WATCHLIST Eligibility Rule

Public WATCHLIST is allowed only when all of the following are true:

- Lifecycle state is one of WATCH/WATCHLISTED/STALKING/A_GRADE_WATCH/TRIGGERED.
- Not invalidated, expired, cooldown, rejected, archived, or stale.
- Required public fields and trade-map fields are present.
- Entry/SL/TP target integrity passes.
- Public quality and data-health gates pass.
- Planned RR is at least `2.5`.
- Normalized failed-gate class set is exactly `{REGIME_MARKET_CONDITION_PENDING}`.

Any non-regime failed gate suppresses the public WATCHLIST.

## 5. RR Verification

- Public WATCHLIST min RR: `PUBLIC_WATCHLIST_MIN_RR = Decimal("2.5")` in `app/alerts/telegram_lifecycle.py`.
- Public execution default min RR: `DEFAULT_CONFIRMED_MIN_RR = Decimal("3")` in `app/alerts/telegram_lifecycle.py`.
- `TelegramEligibilityContext.min_rr` defaults to that execution threshold.
- Active public signal display also requires `ACTIVE_SIGNAL_MIN_RR = Decimal("3")` in `app/telegram_admin/active_watchlists.py`.
- Lifecycle A-grade public threshold remains `PUBLIC_A_GRADE_MIN_RR = Decimal("3.0")` in `app/lifecycle/service.py`.

Public execution RR was not lowered.

## 6. Public/Admin Watchlist Surfaces

Public Telegram WATCHLIST copy remains clearly labeled:

- `WATCHLIST`
- `MARKET CONDITION PENDING`
- `NOT ACTIVE EXECUTION SIGNAL`

Admin/public watchlist surfaces now label regime-pending watchlist items as `market condition pending` instead of limit-zone waiting. Active signal views still use only `SIGNAL_CONFIRMED` as the active signal base and do not treat public WATCHLIST as an active execution signal.

## 7. Deduplication and Later Upgrade

Existing WATCHLIST dedupe by setup/signal id remains in place. Public WATCHLIST rows do not create active confirmed signal bases.

A later confirmed execution signal remains possible only after the full public execution gate passes, including confirmed lifecycle state, valid stored trade map, valid RR, no stale/invalidated status, target integrity, and duplicate confirmed-signal protection.

`LIMIT_HIT` still requires a prior sent `SIGNAL_CONFIRMED` for the same signal id before any public limit-hit update can be sent.

## 8. Tests Added or Updated

Added/updated focused regressions for:

- Normalizing regime-pending failed-gate codes.
- Requiring exactly the regime-pending normalized class.
- Blocking regime plus RR/non-regime failures.
- Blocking missing regime data and explicit missing-regime failed-gate codes.
- Blocking unknown and malformed failed-gate diagnostics.
- Blocking target-integrity failures.
- Blocking RR below 2.5 and allowing RR exactly 2.5.
- WATCHLIST copy and admin dashboard regime-pending wording.
- Public WATCHLIST not creating an active signal base.
- WATCHLIST dedupe and later confirmed execution upgrade.
- `LIMIT_HIT` remaining non-public-execution eligible without prior `SIGNAL_CONFIRMED`.

## 9. Test Commands and Results

Targeted:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -k "watchlist or regime or market_condition or failed_gate or diagnostic or public_signal or limit_hit or lifecycle or telegram or target_integrity or risk_reward"
```

Result: `532 passed, 656 deselected, 1 warning`

Full:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Result: `1188 passed, 1 warning`

Warning: existing `StarletteDeprecationWarning` from `fastapi.testclient` import.

## 10. Remaining Risks

- Scanner diagnostic correctness still matters. The public gate is now strict about names, but strategy code must continue emitting `regime_compatibility` for regime-only blocks.
- The allowlist includes explicit compatibility aliases for future/adjacent mode naming, but missing/unknown names are blocked by default.
- Public WATCHLIST quality depends on stored diagnostics and setup snapshots staying complete and accurate.

## 11. Recommended Next Phase

Recommended Phase 2: add scanner-mode contract tests around regime overlay emission for challenge/swing/scalp modes, including proof that non-regime failures never collapse into the regime-pending class.

## End State

- Branch name: `fix/public-signal-gate-lifecycle-hardening`
- Tests: passed, `1188 passed, 1 warning`
- Files changed in this normalization phase: `app/alerts/telegram_lifecycle.py`, `app/telegram_admin/active_watchlists.py`, `tests/test_telegram_lifecycle_delivery_phase42.py`, `tests/test_telegram_active_watchlists_phase50b.py`, `tests/test_telegram_wolf_briefing.py`, this report
- Exact normalized regime/market gate codes: `regime_compatibility`, `regime_blocked`, `regime_not_confirmed`, `market_condition_blocked`, `market_condition_not_ready`, `btc_eth_regime_blocked`, `rejected_by_regime`
- Exact blocked missing-data codes: `missing_regime_data`, `regime_data_missing`, `missing_market_data`, `market_data_missing`
- Public WATCHLIST min RR: `2.5`
- Public execution RR threshold/source: `DEFAULT_CONFIRMED_MIN_RR = Decimal("3")` and `TelegramEligibilityContext.min_rr` in `app/alerts/telegram_lifecycle.py`
- Public execution gate was not weakened.
- `LIMIT_HIT` is still not public execution eligible without prior sent `SIGNAL_CONFIRMED`.
- Public WATCHLIST does not create an active execution signal.
- No Telegram listener was run.
- No live runtime DB was used.
- No `.env` or secrets were touched.
