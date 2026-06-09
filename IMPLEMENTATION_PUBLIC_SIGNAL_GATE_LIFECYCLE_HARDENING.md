# Public Signal Gate and Lifecycle Hardening

## 1. Summary

Implemented Phase 1 safety hardening for public Telegram signal quality. Entry-zone and limit-zone touches are now treated as internal touch/intermediate events unless separate public confirmation and prior-emission rules are satisfied.

The patch makes the system stricter:
- Entry-zone touch no longer promotes WATCHLISTED, STALKING, or A_GRADE_WATCH directly to EXECUTING.
- LIMIT_HIT is no longer a public active state or a public signal base.
- Public LIMIT_HIT updates require an existing sent SIGNAL_CONFIRMED alert for the same stable signal id.
- LIMIT_HIT Telegram copy no longer says SCALP SIGNAL, active for manual execution, confirmed, or executing.
- Active signal views require a stored SIGNAL_CONFIRMED base row and use stored public signal plan data.
- Target-integrity blocks no longer synthesize passed gates in diagnostics.

## 2. Files Changed

- app/alerts/telegram_lifecycle.py
- app/formatters/telegram_signal_formatter.py
- app/lifecycle/eligibility.py
- app/lifecycle/state_machine.py
- app/pipeline/scanner_runner.py
- app/telegram_admin/active_watchlists.py
- tests/test_lifecycle.py
- tests/test_lifecycle_eligibility.py
- tests/test_scanner_runner.py
- tests/test_telegram_active_watchlists_phase50b.py
- tests/test_telegram_lifecycle_delivery_phase42.py
- tests/test_telegram_signal_formatter_phase42.py
- tests/test_telegram_wolf_briefing.py
- IMPLEMENTATION_PUBLIC_SIGNAL_GATE_LIFECYCLE_HARDENING.md

## 3. Lifecycle Before and After

Before:
- WATCHLISTED/STALKING/A_GRADE_WATCH entry-zone touch could promote directly to EXECUTING.
- LIMIT_HIT and limit_zone_hit were treated as active public signal states in eligibility/view logic.
- A_GRADE_WATCH was not plan-locked.

After:
- WATCHLISTED/STALKING/A_GRADE_WATCH entry-zone touch promotes to TRIGGERED with ENTRY_ZONE_TOUCHED reason.
- A_GRADE_WATCH can move to CONFIRMED only when confirmation gates are satisfied.
- LIMIT_HIT and limit_zone_hit are internal touch states, not public active states.
- A_GRADE_WATCH and CONFIRMED are plan-locked so entry/SL/TP/RR do not mutate casually.

## 4. Telegram Before and After

Before:
- A-grade entry touch could create a public LIMIT_HIT signal without a prior public signal.
- LIMIT_HIT Telegram copy used SCALP SIGNAL and active manual execution language.
- Watchlist outcome tracking could send LIMIT_HIT from a public watchlist alone.

After:
- Direct A-grade limit-touch public bypass is removed.
- LIMIT_HIT public updates require a prior sent SIGNAL_CONFIRMED row for the same signal id.
- LIMIT_HIT messages are labeled ENTRY ZONE TOUCHED and AWAITING FOLLOW-THROUGH.
- LIMIT_HIT updates use the stored public signal plan snapshot when available.

## 5. Public Gate Behavior

Added a structured PublicSignalGateResult with:
- allowed
- reason_codes
- blocking_reasons
- state
- setup_id
- symbol

The public gate blocks:
- SIGNAL_CONFIRMED unless lifecycle state is confirmed and required fields are present.
- LIMIT_HIT unless a prior public SIGNAL_CONFIRMED emission exists.
- LIMIT_HIT when lifecycle state is internal touch, terminal, rejected, or not public active.
- Missing entry, stop, TP1, TP2, TP3, RR, invalidation, symbol, direction, or signal id.

## 6. Tests Added or Updated

Added/updated regression coverage for:
- Entry-zone touch does not promote to EXECUTING.
- LIMIT_HIT and limit_zone_hit are not public active states.
- LIMIT_HIT requires prior public SIGNAL_CONFIRMED emission.
- LIMIT_HIT formatter copy avoids execution/signal language.
- Active signal detail/listing hides standalone LIMIT_HIT rows.
- Public signal gate blocks non-confirmed state and missing fields.
- Target-integrity block diagnostics do not synthesize gates_passed.
- A_GRADE_WATCH and CONFIRMED plan locking preserves entry/SL/TP/RR.
- Runtime DB auto-discovery skips inferred scan_runs/main_live_runtime.sqlite.
- Wolf briefing no longer counts LIMIT_HIT as active.

## 7. Verification

Targeted command:
`.\\.venv\\Scripts\\python.exe -m pytest tests -k "limit_hit or entry_zone or lifecycle or telegram or public_signal or active_signal or target_integrity"`

Result:
460 passed, 712 deselected, 1 warning.

Full command:
`.\\.venv\\Scripts\\python.exe -m pytest`

Result:
1172 passed, 1 warning.

## 8. Known Remaining Issues

- Runtime DB explicit path behavior was not changed. This phase only prevents inferred selection of scan_runs/main_live_runtime.sqlite.
- Deeper audit of all non-Telegram public surfaces remains recommended.
- Historical sent LIMIT_HIT rows may still exist in old databases; active views now require SIGNAL_CONFIRMED as the base row.
- No live/manual order execution behavior was added or reviewed beyond confirming this patch did not add execution.

## 9. Recommended Phase 2

Phase 2 should focus on:
- Runtime DB selection policy and warnings for explicit runtime-looking DB paths.
- Historical database repair/migration guidance for legacy LIMIT_HIT-only public attempts.
- Broader public-surface snapshot consistency audit outside Telegram active views.
- Additional identity matching hardening for multi-signal same-symbol cases.

## Final Confirmation

- Branch: fix/public-signal-gate-lifecycle-hardening
- Tests: passed
- Files changed by this phase: 14
- Top behavior changes: entry touch is TRIGGERED/internal; LIMIT_HIT is not a fresh public signal; public updates require prior SIGNAL_CONFIRMED; active views require confirmed base rows.
- Remaining risks: explicit runtime DB path policy and legacy historical LIMIT_HIT rows need Phase 2 handling.
- Exact next recommended phase: Phase 2 runtime DB selection policy plus legacy public-alert repair guidance.
- Telegram listener was not run.
- scan_runs/main_live_runtime.sqlite was not used.
- .env and secrets were not touched.
