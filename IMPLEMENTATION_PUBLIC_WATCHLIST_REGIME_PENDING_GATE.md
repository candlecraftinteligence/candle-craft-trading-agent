# Public Watchlist Regime-Pending Gate Implementation

## 1. Summary

Implemented a dedicated public WATCHLIST gate for high-quality setups where the only missing condition is an explicit regime or market-condition gate. This is separate from public execution signals and does not relax the Phase 1 confirmed-signal or LIMIT_HIT protections.

Public WATCHLIST output is now labeled as market-condition pending and not as an active execution signal.

## 2. Exact Allowed Condition

Public WATCHLIST is allowed only when all of the following are true:

- Lifecycle state is WATCHLISTED, STALKING, A_GRADE_WATCH, or TRIGGERED.
- Required public fields are present: signal id, symbol, direction, setup type, entry zone, stop/SL, TP1, RR, and invalidation.
- RR is at least 2.5.
- Target integrity has no diagnostic or inferred failure.
- Public quality gate passes.
- Symbol/data health is clean.
- Setup is not stale, rejected, invalidated, expired, cooldown, archived, or no-longer-tracking.
- Failed-gate set is exactly one explicit allowlisted regime/market-condition code.
- Regime state and regime compatibility label are present.

Allowed failed gate codes are narrow and exact:

- regime_compatibility
- regime_blocked
- regime_not_confirmed
- regime_not_ready
- market_condition_blocked
- market_condition_not_ready
- market_condition_not_confirmed
- btc_eth_regime_blocked
- rejected_by_regime

Missing regime data blocks the watchlist.

## 3. Files Changed

Watchlist phase changes were focused in:

- app/alerts/telegram_lifecycle.py
- app/formatters/telegram_signal_formatter.py
- tests/test_telegram_lifecycle_delivery_phase42.py
- tests/test_telegram_signal_formatter_phase42.py

The branch also still contains the prior Phase 1 hardening changes in lifecycle, scanner, active-watchlist, and related tests.

## 4. Telegram Wording Before/After

Before:

- WATCHLIST copy used general stalking/confirmation language.
- Public WATCHLIST did not explicitly say the only blocked gate was market/regime condition.
- Public WATCHLIST did not clearly separate itself from execution-signal status.

After:

- Header: WATCHLIST - MARKET CONDITION PENDING.
- Includes setup type, entry/limit zone, SL, invalidation, TP lines, and RR.
- Includes: Status: WATCHLIST - NOT ACTIVE EXECUTION SIGNAL.
- Includes: Blocked gate: market/regime condition only.
- Avoids SCALP SIGNAL, active for manual execution, confirmed, executing, and enter now.

## 5. Lifecycle Behavior Before/After

Before:

- A_GRADE_WATCH did not route to public WATCHLIST at all.
- WATCHLIST readiness was broader than the new regime-only public rule.

After:

- A_GRADE_WATCH can route to WATCHLIST only through the public watchlist gate.
- Public WATCHLIST emission does not promote lifecycle state to EXECUTING.
- WATCHLIST/STALKING/A_GRADE_WATCH/TRIGGERED remain watch/intermediate states.
- Public execution signal eligibility remains CONFIRMED only under the Phase 1 public signal gate.

## 6. Deduplication

Public WATCHLIST delivery continues to dedupe by signal_id/setup identity and alert type.

The same setup can later send one SIGNAL_CONFIRMED only if the normal public execution gate passes. A below-confirmed-min RR candidate remains blocked even if RR is above the public watchlist floor.

## 7. Tests Added/Updated

Added or updated regression coverage for:

- Public watchlist allowed when only regime_compatibility fails.
- Public watchlist requires RR >= 2.5.
- Public watchlist blocks non-regime failed gates.
- Public watchlist blocks missing regime data.
- Public watchlist blocks target-integrity failures.
- Public watchlist blocks terminal/rejected lifecycle states.
- Public watchlist copy is not execution-signal copy.
- Public watchlist emission does not promote to EXECUTING.
- Public watchlist dedupes by setup_id.
- Later confirmed execution signal is allowed only after the full execution gate passes.
- LIMIT_HIT remains blocked without prior public SIGNAL_CONFIRMED.

## 8. Test Commands And Results

Targeted:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -k "watchlist or regime or public_signal or limit_hit or lifecycle or telegram or target_integrity"
```

Result:

- 496 passed
- 684 deselected
- 1 warning

Full suite:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Result:

- 1180 passed
- 1 warning

## 9. Remaining Risks

- The watchlist gate relies on scanner diagnostics accurately reporting first_failed_gate/gates_failed. If a scanner path omits failed-gate metadata, the public watchlist is blocked.
- Admin watchlist dashboards still use broader internal/watchlist visibility rules; active public signal views remain confirmed-signal based.
- Regime-pending copy is public-safe, but message tone and exact wording may still need product review before launch.

## 10. Recommended Next Phase

Phase 2 should audit and normalize scanner diagnostic gate naming across all strategy modes so regime/market-condition failures are emitted consistently and missing diagnostics are easy to investigate without increasing alert volume.

## Final Confirmation

- Branch: fix/public-signal-gate-lifecycle-hardening
- Tests: passed
- Public execution gate was not weakened.
- LIMIT_HIT is still not public execution eligible without prior SIGNAL_CONFIRMED.
- Public WATCHLIST RR floor remains >= 2.5.
- No Telegram listener was run.
- No live runtime DB was used.
- No .env or secrets were touched.
