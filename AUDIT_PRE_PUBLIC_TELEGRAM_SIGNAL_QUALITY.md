# Pre-Public Telegram Signal Quality Audit

Repository: `C:\CandleCraftDev`

Date: 2026-06-09

Scope: bot/scanner engine, lifecycle engine, Telegram output pipeline, persistence, config, tests, and recent drift from the baseline commit around one week ago.

Safety constraints followed:
- Did not run `python scripts/run_telegram_bot.py`.
- Did not run a continuous scanner watch loop.
- Did not touch `scan_runs\main_live_runtime.sqlite`.
- Did not edit `.env`.
- Did not commit, push, merge, or revert anything.
- Did not change code. This markdown report is the only file written.
- Did not run optional live-market scanner smoke because it can hit exchange APIs and write a DB; the audit evidence was sufficient without it.

## 1. Executive verdict

Verdict: Needs fixes before public.

The scanner has strong core liquidity-grab and pullback logic, and the test suite is extensive. However, recent lifecycle/Telegram changes intentionally treat limit-zone touch as an active/public signal path. That is not aligned with the protected Candle Craft philosophy: WATCH/STALKING are not public execution signals, and Entry Zone / Limit Zone Hit must not automatically mean CONFIRMED or active.

Top 5 risks:
1. Entry-zone touch can promote WATCHLISTED/STALKING/A_GRADE_WATCH into EXECUTING without a post-touch confirmation gate.
2. Telegram `LIMIT_HIT` output says `SCALP SIGNAL` and "active for manual execution", which can present a pullback touch as a public action signal.
3. Active signal/admin views treat `LIMIT_HIT`/`LIMIT_ZONE_HIT` as active signals.
4. The `LIMIT_HIT` public field requirements are weaker than confirmed signals and do not require TP3.
5. Recent top-volume watch queue and active-on-limit-zone-hit changes plausibly explain why recent signals look different from about one week ago.

Severity count:
- Critical: 1
- High: 3
- Medium: 8
- Low: 4

## 2. Signal path map

Baseline inspected:
- Baseline commit around seven days ago: `12e22fe10c88be94d20b24904bd1958712cd51ea`
- Current HEAD: `20224d3` (`main`, merge PR #29 `feature/public-research-watch-alerts`)
- Drift size from baseline to HEAD: 76 files changed, 32,514 insertions, 744 deletions.

End-to-end path:

`scripts/run_scan.py` -> universe/watch queue -> `ScannerRunner` -> liquidity strategy -> risk/scoring/quality gates -> lifecycle service/state machine -> SQLite storage -> Telegram delivery -> active/watchlist admin views.

### Data fetching/loading

- `app\data\exchange_clients\base.py:21`, `app\data\exchange_clients\binance_futures.py:23`, `app\data\exchange_clients\bybit_linear.py:23`
  - Public HTTP exchange clients and base client abstractions.
  - Risk: no public order execution path observed in this audit area.

- `app\pipeline\scanner_runner.py:155` `ScannerRunConfig`
  - Defines scan defaults: interval, candle limits, min score, exchange, cache, timeframes, market regime, dry-run alert defaults.
  - Risk: config is broad, but no hard safety issue found here.

- `app\pipeline\scanner_runner.py:711` `_scan_symbol`
  - Fetches candles, optional data, technical structure, derivatives, strategy result, risk, scoring, target integrity, and final trade idea.
  - Risk: the scanner generally rejects weak/missing data, but downstream lifecycle can later promote a watched setup based on entry touch.

- `app\pipeline\scanner_runner.py:1057` `_fetch_primary_candles`
  - Primary candle loading.
  - Risk: missing candles reject the symbol path.

- `app\pipeline\scanner_runner.py:1252` `_fetch_strategy_timeframe_candles`
  - Loads HTF/bias/execution/confirmation timeframes for liquidity strategy.
  - Risk: good separation of strategy timeframes; missing timeframe data is tracked.

### Symbol universe and watch queue

- `scripts\run_scan.py:426` `--universe`, `scripts\run_scan.py:427` `--universe-size`, `scripts\run_scan.py:441` `--min-rr`
  - CLI defaults: manual universe, size 50, min RR 2.5.

- `scripts\run_scan.py:1972` `_watchlist_with_lifecycle_priority`
  - Adds lifecycle-priority symbols to a watchlist and appends dropped requested symbols.
  - Risk: queue composition can differ materially from old manual-only behavior.

- `scripts\run_scan.py:2066` `_queued_symbols_for_scan`
  - Selects queued symbols for adaptive watch mode.
  - Risk: recent queue-selection changes likely changed which symbols appear in watch runs.

- `app\universe\symbol_universe.py:200` `build_symbol_universe_from_tickers`
  - Sorts/builds top-volume, top-tradable, and market-cap universes.

- `app\universe\symbol_universe.py:328` `_binance_usdt_symbols_from_tickers`
  - Filters Binance USDT perpetual symbols.
  - Risk: stablecoin/leveraged-token filtering is only applied when `tradable_only` is true (`app\universe\symbol_universe.py:356`). `binance_usdt_perp_top_volume` can include undesirable symbols that `top_tradable` would filter.

### Liquidity grab, wick/body, reclaim, BOS/CHoCH, pullback

- `app\strategies\liquidity_grab_pullback.py:476` `_analyze_mode`
  - Main mode analysis for swing/challenge/scalp. Requires sweep, structure confirmation, pullback zone, RR, risk constraints, and entry status.
  - Risk: core setup analysis is strict; lifecycle promotion after watch is the larger risk.

- `app\strategies\liquidity_grab_pullback.py:785` `detect_liquidity_sweep`
  - Requires ATR/magnitude-aware wick through prior swing and close back inside.
  - Risk: this is consistent with liquidity-grab logic.

- `app\strategies\liquidity_grab_pullback.py:849` `detect_structure_shift`
  - Looks for a close beyond the relevant LTF swing after sweep.
  - Risk: protects against random wick-only setups.

- `app\analytics\wick_close_structure.py:74` `analyze_wick_close_structure`
  - Classifies clean pullback, body acceptance, structural breakdown, wick reclaim, and deep reclaim.
  - Risk: when no post-BOS sample exists, it returns clean pullback; acceptable for setup formation but insufficient as post-entry confirmation.

- `app\analytics\pullback_zones.py:191` `analyze_pullback_zone`
  - Combines OB/FVG/fib overlap, wick/body structure, stop, RR, target path, and invalidation checks.
  - Risk: strong for candidate selection; not a replacement for post-touch micro-confirmation.

- `app\analytics\pullback_zones.py:562` `_detect_fvg`, `app\analytics\pullback_zones.py:605` `_detect_order_block`
  - Finds FVG/OB zones and later filters them by fib overlap and mitigation.

- `app\analytics\pullback_zones.py:727` `_stop_price`
  - Computes stop/invalidation with ATR buffer and rejects wrong-side stop.

- `app\analytics\pullback_zones.py:790` `_risk_reward`
  - Computes RR and returns `N/A` for invalid geometry.

### RR, scoring, hard gates, final selection

- `app\strategies\liquidity_grab_pullback.py:35` `BASE_MIN_RR = 2.5`
- `app\strategies\liquidity_grab_pullback.py:36` `CHALLENGE_MIN_RR = 3.0`
- `app\strategies\liquidity_grab_pullback.py:1488` `_gate_result`
  - Rejects missing RR, RR below min, no-trade, stale entry window for challenge/scalp, deep invalidation, and challenge-specific constraints.
  - Risk: swing mode has less explicit entry-age protection than scalp/challenge.

- `app\agents\risk_manager.py:158` `_hard_rule_violations`
  - Rejects bad entry/SL/TP/invalidation and bad data quality.

- `app\scoring\opportunity_scoring.py:304` hard filters
  - Blocks low RR, missing invalidation, poor data quality, rejected risk.

- `app\agents\trade_idea.py:227` `_quality_gate_result`
  - Requires score, risk approval, invalidation, stop, entry zone, targets, and RR >= 2.

- `app\analytics\setup_quality.py:237` `validate_setup_quality`
  - Quality gate and watchlist/near-miss classification.

- `app\pipeline\scanner_runner.py:2517` `_is_valid_strategy_setup`
  - Requires `setup.is_valid` and grade A/B.

- `app\pipeline\scanner_runner.py:2521` `_candidate_from_strategy_setup`
  - Converts valid strategy setup to scanner candidate. Requires bias, entry, stop, TP1, TP2, invalidation, facts.

- `app\pipeline\scanner_runner.py:1925` `_target_integrity_decision`
  - Blocks invalid/unsafe target structure.

- `app\pipeline\scanner_runner.py:1966` `_strategy_execution_with_target_integrity_block`
  - Risk: when target integrity is blocked and no gates were passed, it injects `sweep`, `bos_choch`, and `pullback_zone` into `gates_passed`. This can make blocked diagnostics appear stronger than the actual evidence.

### Lifecycle

- `app\lifecycle\service.py:229` `observation_from_symbol_result`
  - Converts scanner result into lifecycle observation.

- `app\lifecycle\service.py:625` `_a_grade_watch_candidate`
  - Allows A/A+ setups with valid map and RR >= 3.0 into A-grade watch when the only waiting gate is limit/entry touch.

- `app\lifecycle\service.py:800` `_entry_zone_touched_for_result`
  - Uses diagnostics/trade idea and latest range candidates to detect entry zone touch.

- `app\lifecycle\state_machine.py:77` `ALLOWED_TRANSITIONS`
  - Allows WATCHLISTED/STALKING -> EXECUTING.

- `app\lifecycle\state_machine.py:289` `evaluate_lifecycle_transition`
  - Main transition evaluation and record update.

- `app\lifecycle\state_machine.py:424` `observed_state`
  - Maps observations to WATCH/STALKING/TRIGGERED/CONFIRMED/A_GRADE_WATCH/EXECUTING.

- `app\lifecycle\state_machine.py:448` `next_state_for_observation`
  - Risk: direct entry touch branches promote WATCHLISTED/STALKING to EXECUTING.

- `app\lifecycle\state_machine.py:588` `_stored_monitoring_entry_zone_touched`
  - Uses stored plan and current price/latest candle to detect touch.

- `app\lifecycle\state_machine.py:707` `_confirmation_gated_target`
  - Gating exists for target confirmed/executing states, but the direct entry-touch branch can bypass it.

- `app\lifecycle\state_machine.py:768` `_plan_or_observed_value`
  - Plan lock only applies to WATCHLISTED/STALKING/EXECUTING/MANAGING, not CONFIRMED/A_GRADE_WATCH.

### Database and Telegram

- `app\storage\database.py:136` `setup_lifecycle_records`
  - Current lifecycle row per setup, updated over time.

- `app\storage\database.py:177` `setup_lifecycle_events`
  - Append-only lifecycle event history.

- `app\storage\database.py:229` `telegram_alert_attempts`
  - Stores alert attempts, alert type, status, message hash, and trade map snapshot fields.

- `app\storage\database.py:265` `UNIQUE(signal_id, alert_type)`
  - Public duplicate control by signal and alert type.

- `app\alerts\telegram_lifecycle.py:822` `TelegramLifecycleDeliveryService.deliver_for_run`
  - Delivery entry point for scanner run output.

- `app\alerts\telegram_lifecycle.py:911` `deliver_for_symbol`
  - Dedupes, blocks, sends, and records Telegram alert attempts.

- `app\alerts\telegram_lifecycle.py:1595` `telegram_alert_decision_for_symbol`
  - Decides whether a lifecycle transition becomes a Telegram alert.

- `app\alerts\telegram_lifecycle.py:1665` `telegram_signal_message_from_symbol`
  - Builds Telegram signal message from current scanner/lifecycle state.

- `app\alerts\telegram_lifecycle.py:2335` `_alert_type_for_transition`
  - Maps entry-zone touch into `LIMIT_HIT`.

- `app\alerts\telegram_lifecycle.py:2376` `_direct_a_grade_limit_hit_public_signal`
  - Allows direct public A-grade limit-hit signal from A_GRADE_WATCH.

- `app\alerts\telegram_lifecycle.py:5422` `_message_with_prior_public_plan`
  - Reuses prior public plan for terminal updates. This is good snapshot behavior after a prior public alert exists.

- `app\formatters\telegram_signal_formatter.py:138` `format_premium_public_signal_message`
  - Public confirmed signal card.

- `app\formatters\telegram_signal_formatter.py:171` `format_premium_watchlist_message`
  - Watchlist card.

- `app\formatters\telegram_signal_formatter.py:236` `format_limit_hit_update`
  - Limit-hit public update. Risk: language presents touch as active scalp signal.

## 3. Recent drift summary

Suspicious drift from the last week:

- `35dc9c1` `Activate A-grade setups on limit zone touch`
  - Changed `app\lifecycle\state_machine.py`, `app\lifecycle\service.py`, `app\alerts\telegram_lifecycle.py`, `app\telegram_admin\active_watchlists.py`, and tests.
  - Effect: more permissive public/active behavior. A-grade setups can become public/active on limit-zone touch.
  - Likely explains different recent signal behavior.
  - Follow-up: reverse public active semantics for limit-hit; require post-touch confirmation.

- `52d56a0` `Fix lifecycle watchlist and public alert hygiene`
  - Added `app\lifecycle\eligibility.py`, DB fields, Telegram hygiene tests.
  - Effect: stricter in many places, but also centralizes eligibility definitions that include limit-hit as active.
  - Follow-up: split watchlist-progress eligibility from public-active eligibility.

- `8eea042` `Fix top-volume watch queue selection`
  - Changed `scripts\run_scan.py`, `app\storage\repositories.py`, and tests.
  - Effect: different symbols enter the watch queue. This can change signal mix without changing strategy logic.
  - Follow-up: public watch queues should prefer top_tradable/top market cap filters, not raw top-volume.

- `bd93ae0` `fix active signal details and lifecycle hit detection`
  - Changed active signal detail/watchlist rendering and lifecycle hit detection.
  - Effect: more reliable stored-map rendering, but active detail still treats LIMIT_HIT as active.
  - Follow-up: active detail should not surface limit-hit as execution-active unless confirmation state exists.

- `25b96ed` `Fix active signal data integrity gate`
  - Changed active signal data integrity filtering.
  - Effect: stricter active detail reliability, but built around the current definition that limit-hit is active.

- `56a02b6` `Fix Telegram watchlist plan filtering`
  - Changed watchlist display filters.
  - Effect: likely stricter display hygiene; should be retained but retested after changing limit-hit semantics.

- `2904f5d` `Hotfix TP SL live price validation`
  - Added live-price safeguards for TP/SL.
  - Effect: stricter and positive. Terminal updates now require live price freshness in more paths.

- `d1e1ef2` `Add public research watch alerts`
  - Adds research watch alerting.
  - Effect: new public-adjacent alert type; can increase alert volume if enabled.
  - Follow-up: keep research watch separate from official public signal feed.

## 4. Telegram output risks

Public signal issues:
- `app\formatters\telegram_signal_formatter.py:236` `format_limit_hit_update` renders "SCALP SIGNAL" and says setup is active for manual execution.
- `app\alerts\telegram_lifecycle.py:2335` `_alert_type_for_transition` emits `LIMIT_HIT` for entry-zone touch transitions.
- `app\alerts\telegram_lifecycle.py:2376` `_direct_a_grade_limit_hit_public_signal` allows A_GRADE_WATCH -> EXECUTING limit-hit without a prior public active alert.
- This can make a pullback touch look like a confirmed execution signal.

Active signal detail issues:
- `app\telegram_admin\active_watchlists.py:45` includes `LIMIT_HIT` as active base type.
- `app\telegram_admin\active_watchlists.py:83` includes limit-hit state keys as active.
- `app\lifecycle\eligibility.py:16` includes `limit_hit` and `limit_zone_hit` in active signal states.
- Stored trade levels are mostly good (`app\telegram_admin\active_watchlists.py:1317`, `app\telegram_admin\signal_detail.py:112`), but the status category is too permissive.

Watchlist issues:
- Watchlist output has good filters, but watchlist outcome tracking can upgrade touch to limit-hit and public active status.
- `app\alerts\telegram_lifecycle.py:3730` `_watchlist_outcome_for_current_result` sends `LIMIT_HIT` once when a stored entry zone is touched.

Duplicate/stale signal issues:
- `telegram_alert_attempts` has `UNIQUE(signal_id, alert_type)` at `app\storage\database.py:265`, and delivery checks `has_attempt`; duplicate controls are strong.
- Terminal updates use `_message_with_prior_public_plan` (`app\alerts\telegram_lifecycle.py:5422`), which is good.
- Stale active detail risk remains because active views consult latest symbol rows for blocking and status validation while levels come from stored attempts.

Dry-run/live safety:
- `.env.example:10` `TELEGRAM_DRY_RUN=true`
- `.env.example:18` `TELEGRAM_SIGNALS_ENABLED=false`
- `.env.example:37` `LOCAL_MANUAL_MODE=true`
- `.env.example:38` `ORDER_EXECUTION_ENABLED=false`
- `app\alerts\telegram_sender.py:100` blocks if local manual mode is false or signals disabled.
- `app\core\config.py:87` rejects order execution enabled.
- Residual routing risk: `app\alerts\telegram_sender.py:167` can fall back to legacy `TELEGRAM_CHAT_ID` when local manual mode is true.

## 5. Strategy quality risks

Liquidity grab logic:
- The core sweep logic is strict and aligned with the original strategy: ATR/magnitude, prior swing, wick through level, close back inside.
- No evidence that the scanner itself became a random indicator bot.

Reclaim logic:
- Wick/body reclaim checks are present in `app\analytics\wick_close_structure.py:74` and pullback-zone gating.
- Risk: post-entry touch confirmation is not enforced before active/public status.

BOS/CHoCH logic:
- `app\strategies\liquidity_grab_pullback.py:849` requires structure shift after sweep.
- Risk: lifecycle promotion can later use stored setup + entry touch rather than requiring fresh structure confirmation after touch.

OB/FVG/fib pullback logic:
- `app\analytics\pullback_zones.py:191` requires zone confluence, rejects invalid structure, and computes stops/RR.
- Risk: no one-touch rule or post-touch micro-confirmation is visible for public scalping.

RR/invalidation:
- Core strategy requires RR >= 2.5, challenge >= 3.0.
- Public A-grade watch requires RR >= 3.0.
- Lower-level risk/trade/scoring gates still use RR >= 2.0; acceptable as intermediate gating, but public gates must remain stricter.
- `LIMIT_HIT` missing-field requirements omit TP3, unlike `SIGNAL_CONFIRMED`.

Setup age/staleness:
- Challenge/scalp modes enforce `entry_window_expired`; swing is looser.
- Lifecycle has stale/decay handling, but entry-zone touch of old watch states needs stronger max-age and max-candles-after-sweep controls for scalp publication.

## 6. Lifecycle risks

Invalid promotions:
- Critical issue: `app\lifecycle\state_machine.py:448` `next_state_for_observation` promotes WATCHLISTED/STALKING to EXECUTING on entry touch.
- Tests explicitly validate this (`tests\test_lifecycle.py:642`, `tests\test_lifecycle.py:734`, `tests\test_telegram_lifecycle_delivery_phase42.py:3728`).

Entry Zone Hit / Limit Zone Hit treatment:
- Current code treats entry touch as active/executing in multiple paths.
- This violates the required philosophy: Limit Zone Hit should generally mean TRIGGERED unless additional confirmation exists.

TP/SL detection:
- Recent live-price guards are positive. `app\alerts\telegram_lifecycle.py:4047` checks live price freshness/status/age for TP/SL outcome updates.
- Same-candle ambiguity is audited, and conservative replay policy exists.
- Remaining risk: current-price fallback can mark limit touch without full candle wick/body context.

Cooldown/expiry/revival:
- Terminal states and cooldown blockers are present.
- Risk is lower here than promotion semantics. No clear invalidated/expired revival bug was found in the audited paths.

Stored/displayed state mismatch:
- `setup_lifecycle_records` update current state while `setup_lifecycle_events` append history.
- Telegram attempts store snapshots, but active detail combines stored alert map with latest lifecycle/symbol rows. That is useful for health checks but can display a newer lifecycle condition over an older public setup.

## 7. Scalping readiness

Present:
- Scalp mode exists in liquidity grab strategy.
- Min RR 2.5 base and 3.0 challenge/A-grade public watch.
- Entry-window expiry for scalp/challenge.
- Sweep, reclaim, structure shift, OB/FVG/fib pullback, invalidation, target integrity, and RR gates.
- Live-price TP/SL safeguards.
- Outcome/replay infrastructure exists.

Missing or incomplete for public scalping:
- Separate public scalp profile with explicit immutable rules.
- Max candles after sweep for all public scalp states.
- Max candles after entry touch.
- One-touch rule.
- Anti-chase rule after entry zone touch.
- Micro-confirmation after entry zone touch.
- Fee/slippage-aware public RR.
- Volatility shock filter.
- BTC/ETH impulse filter for scalps.
- Session/time-of-day behavior.
- Hard symbol liquidity/spread constraints in public scalp gate.
- MFE/MAE tracking tied to public signal snapshots.
- Time-to-TP1 and time-to-SL tracking tied to public signal snapshots.

Do not add leverage recommendations or execution functionality.

## 8. Weird/risky code findings

### Finding 1

Severity: Critical

File: `app\lifecycle\state_machine.py:448`

Code/function: `next_state_for_observation`

Why it matters: WATCHLISTED/STALKING can move directly to EXECUTING when `entry_filled` is true.

Telegram/signal impact: Public/admin output can treat a pullback-zone touch as an active execution signal without post-touch confirmation.

Recommended fix: Change entry-zone touch from WATCHLISTED/STALKING/A_GRADE_WATCH to TRIGGERED or `LIMIT_TOUCHED_PENDING_CONFIRMATION`. Require a fresh micro-confirmation/reclaim/BOS candle before CONFIRMED/EXECUTING.

### Finding 2

Severity: High

File: `app\alerts\telegram_lifecycle.py:2335`

Code/function: `_alert_type_for_transition`

Why it matters: ENTRY_ZONE_TOUCHED transitions map to `LIMIT_HIT`.

Telegram/signal impact: Public Telegram can emit a limit-hit card as a signal update from entry touch.

Recommended fix: Treat limit-hit as private/watchlist progress unless a separate public confirmation gate passes.

### Finding 3

Severity: High

File: `app\formatters\telegram_signal_formatter.py:236`

Code/function: `format_limit_hit_update`

Why it matters: Message says `SCALP SIGNAL` and "active for manual execution."

Telegram/signal impact: This can mislead users into interpreting a zone touch as a confirmed trade.

Recommended fix: Remove execution-active language. If retained publicly, label it as "Watchlist update: limit zone touched, awaiting confirmation."

### Finding 4

Severity: High

File: `app\alerts\telegram_lifecycle.py:2376`

Code/function: `_direct_a_grade_limit_hit_public_signal`

Why it matters: A_GRADE_WATCH -> EXECUTING can bypass the prior active public alert requirement.

Telegram/signal impact: A setup can appear publicly at the moment of limit-zone touch without a prior official signal card.

Recommended fix: Remove this direct public exception or require confirmed post-touch evidence.

### Finding 5

Severity: Medium

File: `app\telegram_admin\active_watchlists.py:45`

Code/function: `_ACTIVE_SIGNAL_BASE_TYPES`

Why it matters: `LIMIT_HIT` is treated as an active signal base type.

Telegram/signal impact: Active signal views can show limit-hit items as active public signals.

Recommended fix: Active base types should require `SIGNAL_CONFIRMED` or a new confirmed-active event, not limit-hit.

### Finding 6

Severity: Medium

File: `app\lifecycle\eligibility.py:16`

Code/function: `ACTIVE_SIGNAL_STATE_KEYS`

Why it matters: `limit_hit` and `limit_zone_hit` are active eligibility states.

Telegram/signal impact: Eligibility APIs encode the same permissive active semantics.

Recommended fix: Split `watchlist_progress_state` from `public_active_signal_state`.

### Finding 7

Severity: Medium

File: `app\alerts\telegram_lifecycle.py:2729`

Code/function: `_missing_required_fields`

Why it matters: `LIMIT_HIT` requires TP1/TP2 but not TP3, while confirmed signals require TP3.

Telegram/signal impact: Public limit-hit output can be less complete than confirmed public signal output.

Recommended fix: Use the same immutable public trade-map requirements for every public active signal, or render missing fields as `N/A` with a reason and block active status.

### Finding 8

Severity: Medium

File: `app\lifecycle\eligibility.py:126`

Code/function: `has_valid_trade_map`, `has_valid_rr`

Why it matters: The generic eligibility helper only requires TP1 and can compute RR to TP1.

Telegram/signal impact: Callers that do not add stricter checks could approve weaker public maps.

Recommended fix: Add a stricter public-active eligibility function requiring the full public map and the public RR target policy.

### Finding 9

Severity: Medium

File: `app\pipeline\scanner_runner.py:1966`

Code/function: `_strategy_execution_with_target_integrity_block`

Why it matters: When target integrity is blocked and gates are empty, it injects `sweep`, `bos_choch`, and `pullback_zone` into `gates_passed`.

Telegram/signal impact: Watchlist/admin diagnostics can show confirmations as passed even though the result is blocked by target integrity.

Recommended fix: Do not synthesize passed gates for blocked setups; keep explicit blocked reason codes.

### Finding 10

Severity: Medium

File: `app\lifecycle\state_machine.py:70`

Code/function: `PLAN_LOCK_STATES`, `_plan_or_observed_value`

Why it matters: CONFIRMED and A_GRADE_WATCH are not plan-lock states.

Telegram/signal impact: Lifecycle plan fields can drift before public emission; snapshots are stored later in Telegram attempts.

Recommended fix: Lock plan fields at first watch/public visibility and include A_GRADE_WATCH/CONFIRMED.

### Finding 11

Severity: Medium

File: `app\telegram_admin\active_watchlists.py:525`

Code/function: `_latest_runtime_database`

Why it matters: Admin views can select the newest `scan_runs\*.sqlite` with sent attempts.

Telegram/signal impact: A test/audit DB with sent attempts could be selected if runtime config is ambiguous.

Recommended fix: Require explicit runtime DB path for public/admin bot, or exclude audit/test DB patterns from auto-discovery.

### Finding 12

Severity: Medium

File: `app\alerts\telegram_lifecycle.py:4817`

Code/function: `_watchlist_candle_snapshot`

Why it matters: If candle high/low are missing, current price/latest close can become a high=low snapshot.

Telegram/signal impact: Limit-zone touch can be detected without robust wick/body candle context.

Recommended fix: Require fresh OHLC candle evidence for public limit touch; use current price only for private diagnostics.

### Finding 13

Severity: Low

File: `app\universe\symbol_universe.py:328`

Code/function: `_binance_usdt_symbols_from_tickers`

Why it matters: Stablecoin/leveraged-token filtering is conditional on `tradable_only`.

Telegram/signal impact: Raw top-volume queues may include symbols unsuitable for public signal quality.

Recommended fix: Use top_tradable or enforce public-safe universe filtering for all public watch queues.

### Finding 14

Severity: Low

File: `app\lifecycle\state_machine.py:788`

Code/function: `_setup_consistent`

Why it matters: Missing price/logic values are treated as similar.

Telegram/signal impact: Lifecycle identity can merge when comparison evidence is incomplete.

Recommended fix: For public lifecycle identity, missing stored/observed plan values should fail consistency or force a new lifecycle record.

### Finding 15

Severity: Low

File: `app\alerts\telegram_sender.py:167`

Code/function: `resolve_public_signal_destination`

Why it matters: Public destination can fall back to legacy `TELEGRAM_CHAT_ID` when local manual mode is true.

Telegram/signal impact: Public/private routing may be confusing in dev/runtime separation.

Recommended fix: For public delivery, require explicit public chat/channel settings when `TELEGRAM_SIGNALS_ENABLED=true`.

### Finding 16

Severity: Low

File: `app\strategies\liquidity_grab_pullback.py:1650`

Code/function: `_entry_status`

Why it matters: Entry touch and age behavior is stricter for scalp/challenge than swing.

Telegram/signal impact: If swing/watchlist outputs feed public alerts, older pullbacks can remain relevant longer than desired for scalping.

Recommended fix: Public scalp output should use a dedicated scalp profile and never inherit swing-age tolerance.

## 9. Test results

Commands run:

1. `python -m pytest`
   - Result: failed before running tests.
   - Failure: `No module named pytest` for system Python at `C:\Users\aspir\AppData\Local\Programs\Python\Python311\python.exe`.

2. `.\.venv\Scripts\python.exe -m pytest`
   - Result: passed.
   - Output: 1164 passed, 1 warning in 177.77s.
   - Warning: Starlette/httpx deprecation warning from FastAPI testclient.

Coverage observed:
- Telegram lifecycle delivery: extensive.
- Duplicate alerts: covered.
- Active signal detail: covered.
- Lifecycle transitions: covered.
- RR enforcement: covered.
- Liquidity sweep/reclaim/pullback: covered.
- Watchlist filtering: covered.
- Top100 queue: covered.
- Setup expiry: covered.

Important coverage caveat:
- Existing tests assert the risky behavior. Examples:
  - `tests\test_lifecycle.py:642` WATCHLISTED -> EXECUTING on entry-zone touch.
  - `tests\test_lifecycle.py:734` STALKING -> EXECUTING on stored entry-zone touch.
  - `tests\test_lifecycle_eligibility.py:77` `LIMIT_HIT` is active eligible.
  - `tests\test_telegram_lifecycle_delivery_phase42.py:3694` limit-hit says active for manual execution.
  - `tests\test_telegram_active_watchlists_phase50b.py:723` LIMIT_HIT appears in active signal fixtures.

Missing regression tests:
- Limit-zone touch must not become public active without post-touch confirmation.
- WATCH/STALKING/TRIGGERED separation in public Telegram output.
- A_GRADE_WATCH direct public alert should be blocked until confirmation.
- LIMIT_HIT should render watchlist-progress language, not execution-active language.
- Public-active eligibility should require full immutable trade map and the chosen public RR target policy.
- Active signal detail should hide limit-hit-only items unless confirmed.
- Public scalp max-candles-after-sweep and max-candles-after-touch.
- One-touch and anti-chase rules.
- Fee/slippage-aware public RR.
- Runtime DB auto-discovery should not select audit/test DBs.
- Target-integrity blocked setups should not display synthetic passed gates.

## 10. Recommended implementation phases

### Phase 1: Public Signal Gate Pack

- Define one canonical public-active gate.
- Require: clean data, symbol health, regime, HTF alignment, liquidity sweep, wick/body reclaim, BOS/CHoCH, valid pullback zone, invalidation, min RR, not stale, and lifecycle public state.
- Make score ranking secondary only; score must not override missing confirmations.
- Require full immutable trade map for every public active alert.

### Phase 2: Lifecycle promotion hardening

- Remove direct WATCHLISTED/STALKING/A_GRADE_WATCH -> EXECUTING on entry touch.
- Add `LIMIT_TOUCHED_PENDING_CONFIRMATION` or map to TRIGGERED.
- Require post-touch micro-confirmation before CONFIRMED/EXECUTING.
- Retest cooldown, expiry, duplicate Entry Zone Hit, TP/SL after limit touch.

### Phase 3: Telegram snapshot integrity

- Public Telegram should render from stored immutable setup snapshot after emission.
- Limit-hit should not be public execution-active copy.
- Active signal detail should use stored levels and confirmed lifecycle state.
- Public/admin DB path should be explicit in runtime config.

### Phase 4: Scalp profile separation

- Add public scalp profile with stricter age/touch/anti-chase rules.
- Include BTC/ETH impulse and volatility shock filters.
- Add public-safe symbol liquidity/spread constraints.
- Keep leverage and execution out of scope.

### Phase 5: Drift replay against one-week baseline

- Replay identical fixtures/current market snapshots against `12e22fe` and current HEAD.
- Compare symbol universe, selected setups, lifecycle transitions, Telegram alert types, and reason codes.
- Focus on commits `35dc9c1`, `8eea042`, `52d56a0`, `bd93ae0`, and `d1e1ef2`.

### Phase 6: Research outcome/MFE/MAE tracking

- Store MFE/MAE from immutable public snapshots.
- Track time-to-TP1, time-to-SL, invalidation time, and same-candle ambiguity.
- Use this for research only; do not add execution or leverage guidance.

## 11. Final pre-public checklist

Go/no-go checklist:

- [ ] WATCH and STALKING cannot create public active execution alerts.
- [ ] Limit Zone Hit does not automatically mean CONFIRMED or EXECUTING.
- [ ] A_GRADE_WATCH cannot bypass public confirmation gates.
- [ ] Public active signals require full immutable entry/SL/TP/RR/invalidation snapshot.
- [ ] Telegram copy clearly separates watchlist, triggered, confirmed, executing, terminal updates.
- [ ] Active signal detail hides limit-hit-only setups unless confirmed.
- [ ] Public alerts never render rejected, expired, cooldown, or target-integrity-blocked setups.
- [ ] Duplicate public alerts are deduped by immutable signal identity.
- [ ] TP/SL updates require fresh live price and stored public plan.
- [ ] Top100/public universe excludes unsuitable stablecoin/leveraged/illiquid symbols.
- [ ] Public scalp profile enforces max age, max candles after sweep, max candles after touch, one-touch, and anti-chase rules.
- [ ] Config defaults keep Telegram public delivery disabled unless explicitly enabled.
- [ ] Runtime DB path is explicit and cannot fall back to audit/test DBs.
- [ ] `.\.venv\Scripts\python.exe -m pytest` passes after changes.

Final recommendation: do not publish the current Telegram signal pipeline as-is. The system has many strong gates, but the public lifecycle semantics around limit-zone touch are too permissive for the stated Candle Craft strategy. Fix promotion semantics and Telegram active-status language first, then replay recent drift against the one-week baseline before public launch.
