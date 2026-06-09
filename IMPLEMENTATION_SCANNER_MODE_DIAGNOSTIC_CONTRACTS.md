# Scanner-Mode Diagnostic Contracts

## 1. Summary

This phase adds regression protection for scanner-mode diagnostics feeding the public WATCHLIST regime-pending gate.

The public execution gate was not weakened. The public WATCHLIST gate remains separate and only allows watchlist output when the normalized failed-gate class is exactly `REGIME_MARKET_CONDITION_PENDING`, all required setup fields are present, target integrity passes, and public WATCHLIST RR is at least `2.5`.

One safety tightening was added: public WATCHLIST failed-gate extraction no longer synthesizes a regime-pending code from generic `regime_blocked` or `rejected_by_regime` status alone. Scanner diagnostics must explicitly provide the failed gate. Generic `rejection_stage="regime"` is ignored as a failed-gate code so it cannot pollute a valid regime-only diagnostic set, while non-regime rejection stages still block mixed failures.

## 2. Actual Scanner / Strategy Modes Found

The scanner strategy modes are `LiquidityGrabMode` values from `app/strategies/liquidity_grab_pullback.py`:

- `challenge`
- `swing`
- `scalp`

`app/pipeline/scanner_runner.py` uses these as `DEFAULT_STRATEGY_MODES`.

## 3. Diagnostic Emission Path

Relevant scanner/regime functions and classes:

- `ScannerRunner.run`
- `_market_regime_for_scan`
- `_apply_market_regime_to_results`
- `_apply_market_regime_to_symbol`
- `_strategy_diagnostics_with_regime_block`
- `_strategy_diagnostics_with_regime_overlay`
- `_symbol_mode_for_regime`
- `_compatibility_for_mode`
- `MarketRegimeResult`
- `RegimeCompatibility`
- `RegimeAdjustment`

When regime compatibility blocks an otherwise actionable setup, `_strategy_diagnostics_with_regime_block` now remains under contract to emit:

- `first_failed_gate = "regime_compatibility"`
- `gates_failed = ("regime_compatibility",)` for regime-only blocks

## 4. Allowlisted Regime / Market Codes

The public WATCHLIST allowlist remains:

- `regime_compatibility`
- `regime_blocked`
- `regime_not_confirmed`
- `market_condition_blocked`
- `market_condition_not_ready`
- `btc_eth_regime_blocked`
- `rejected_by_regime`

Scanner-mode contract tests cover the actual emitted scanner raw code: `regime_compatibility`.

The existing normalization tests continue to cover the full allowlist.

## 5. Non-Regime Failures Verified

The scanner-mode contract tests verify these failures do not normalize as regime pending:

- Liquidity failure: `missing_confirmed_sweep`
- Reclaim failure: `wick_sweep_reclaim`
- Structure failure: `missing_confirmation_structure_shift`
- Pullback / OB-FVG failure: `no_ob_or_fvg_zone`
- Target-integrity failure: `target_integrity`
- RR failure: public WATCHLIST blocked when RR is `2.49`
- Mixed failure: `regime_compatibility` plus `target_integrity` is blocked
- Missing, `None`, empty, unknown, and malformed failed-gate diagnostics are blocked
- Missing regime state / compatibility label blocks public WATCHLIST

## 6. Public WATCHLIST Confirmation

Public WATCHLIST eligibility remains:

- Lifecycle state must be WATCH/STALKING/A_GRADE_WATCH/TRIGGERED compatible, not executable
- Normalized failed-gate class must be exactly `REGIME_MARKET_CONDITION_PENDING`
- RR must be `>= 2.5`
- Required trade fields must be present
- Regime data must be explicit and present
- Target integrity must pass
- Missing, malformed, unknown, mixed, target-integrity, and non-regime diagnostics block output

The WATCHLIST formatter remains labeled as market condition pending and not an active execution signal.

## 7. Public Execution Confirmation

Public execution signal behavior was not weakened:

- Public execution default RR remains `DEFAULT_CONFIRMED_MIN_RR = Decimal("3")` in `app/alerts/telegram_lifecycle.py`
- Public WATCHLIST RR remains `PUBLIC_WATCHLIST_MIN_RR = Decimal("2.5")`
- `LIMIT_HIT` is still not public execution eligible without a prior sent `SIGNAL_CONFIRMED`
- WATCH/STALKING/A_GRADE_WATCH/TRIGGERED are not active execution states
- Public WATCHLIST does not create an active execution signal base

## 8. Files Changed In This Phase

- `app/alerts/telegram_lifecycle.py`
- `tests/test_regime_intelligence.py`

The branch also contains earlier uncommitted Phase 1 / public WATCHLIST / diagnostic-normalization changes.

## 9. Tests Added / Updated

Added scanner-mode contract tests in `tests/test_regime_intelligence.py`:

- `test_strategy_mode_emits_regime_pending_when_only_regime_blocks`
- `test_strategy_mode_non_regime_failure_not_regime_pending`
- `test_strategy_mode_mixed_failures_block_public_watchlist`
- `test_strategy_mode_rr_failure_blocks_public_watchlist`
- `test_strategy_mode_missing_regime_data_blocks_public_watchlist`
- `test_strategy_mode_missing_or_malformed_diagnostics_block_public_watchlist`
- `test_strategy_mode_public_watchlist_copy_is_regime_pending_not_execution`
- `test_strategy_mode_public_watchlist_does_not_create_active_execution_signal`
- `test_limit_hit_still_not_public_execution_eligible_after_scanner_contracts`

Existing public WATCHLIST tests continue covering dedupe by setup id, no active signal base creation, later confirmed-signal upgrade requiring full execution gates, formatter copy, and LIMIT_HIT safety.

## 10. Test Commands And Results

Focused scanner contract slice:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_regime_intelligence.py -k "strategy_mode or limit_hit"
```

Result: `19 passed, 5 deselected`

Targeted requested suite:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -k "scanner or strategy_mode or scalp or swing or challenge or regime or market_condition or failed_gate or diagnostic or public_watchlist or public_signal or limit_hit or lifecycle or telegram or target_integrity or risk_reward"
```

Result: `598 passed, 609 deselected, 1 warning`

Full suite:

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Result: `1207 passed, 1 warning`

## 11. Remaining Risks

- The scanner-mode contract tests use synthetic scanner symbols and a synthetic `MarketRegimeResult` to exercise the real scanner regime overlay path without live exchange/network access.
- They do not run a full live-data `ScannerRunner.run` cycle.
- Future scanner raw failed-gate names must be added deliberately to the explicit allowlist and covered by tests before public WATCHLIST output can use them.

## 12. Recommended Next Phase

Add recorded-candle end-to-end scanner fixtures for challenge, swing, and scalp modes through `ScannerRunner.run`, with market-regime context fixtures and temp SQLite alert-attempt verification. This should prove the complete dry-run scanner-to-Telegram path without live network or Telegram listener use.

## Final Status

- Branch name: `fix/public-signal-gate-lifecycle-hardening`
- Tests passed: yes, `1207 passed, 1 warning`
- Files changed in this phase: 2
- Actual scanner/strategy modes covered: `challenge`, `swing`, `scalp`
- Exact normalized regime/market raw codes covered by scanner-mode contracts: `regime_compatibility`
- Full allowlisted regime/market raw codes retained: `regime_compatibility`, `regime_blocked`, `regime_not_confirmed`, `market_condition_blocked`, `market_condition_not_ready`, `btc_eth_regime_blocked`, `rejected_by_regime`
- Exact non-regime failure types tested: liquidity, reclaim, structure, pullback/OB-FVG, target integrity, RR below minimum, mixed regime plus target integrity
- Public WATCHLIST min RR: `2.5`
- Public execution RR threshold/source: `DEFAULT_CONFIRMED_MIN_RR = Decimal("3")` in `app/alerts/telegram_lifecycle.py`
- Public execution gate was not weakened: confirmed
- Public WATCHLIST does not create active execution signal: confirmed
- LIMIT_HIT is still not public execution eligible without prior sent `SIGNAL_CONFIRMED`: confirmed
- Telegram listener was not run: confirmed
- Live runtime DB `scan_runs\main_live_runtime.sqlite` was not used: confirmed
- `.env` and secrets were not touched: confirmed
- Commit was not made: confirmed
