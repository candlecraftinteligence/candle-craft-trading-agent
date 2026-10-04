# F05 — Discovery fail-closed

## Phase

F05_DISCOVERY_FAIL_CLOSED

This phase stops new strict market-cap discovery when exchange-contract evidence or another required source payload is unusable. It does not reimplement P5A owner monitoring. It does not add a ranking cache, a freshness policy, another provider, setup admission, or a public new-setup path.

Application schema remains 26. Public quality remains 88 / grade A / RR 3. F03 population isolation, F04 replay and transaction guarantees, and F04 capture default OFF are unchanged. P5A owner definitions, plan binding, and cursors are unchanged.

Capture stays off unless `SOURCE_REPLAY_CAPTURE_ENABLED=true` and `SOURCE_REPLAY_EVIDENCE_PATH` are both set. This phase did not enable capture, did not deploy, and did not merge.

## Delivery

- Branch: `fix/f05-discovery-fail-closed`
- Base SHA: `44efa90daff8476c480eb21ed8ba43654ee8e5de` (`origin/main` at start; merge of PR #128)
- F04 accepted head `54bd2385dcf3b6265b3cdff8e68ecb484f9cc3a8` is an ancestor of that base, with no file changes in the merge commit
- Implementation SHA: `fcaccf2080f27c4a5cd151c437717aa497d6f8a7`
- Handoff SHA whose CI already passed: `476274188d3fb7836de8387f405e08b661c19222`
- Draft PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/129
- Schema version: remains 26
- Runtime database: not opened, copied, or modified

Local `main` was 17 commits behind `origin/main` and was not fast-forwarded. The branch was created from `origin/main`. The working tree had no unrelated local files to preserve.

Acceptance is not claimed. Adam owns merge and any later Runtime rollout.

## Acceptance question

Can unusable strict-universe evidence produce new discovery membership, or prevent an already-owned plan from receiving its existing independent monitoring?

For an unusable source, new discovery stops with `UniverseResolutionError`. P5A monitoring still runs when lifecycle tracking is enabled and candle evidence permits. A monitoring failure stays visible beside the discovery error.

## Implementation plan (executed)

1. Confirm `44efa90daff8476c480eb21ed8ba43654ee8e5de` contains the merged F04 head and branch from it.
2. Inventory strict market-cap callers and separate source-validation failures from CLI configuration errors.
3. Require usable exchange-contract metadata on the strict path. Remove ticker-presence inference.
4. Validate top-level ranking, ticker, and exchange-info shapes before row iteration. Keep conservative exclusion of individual bad rows.
5. Raise `UniverseResolutionError` for unusable payloads and for an empty strict intersection so the existing P5A classifier still reaches owner monitoring.
6. Leave operator include/exclude/max-symbol empty queues as configuration `SystemExit` values without a universe-resolution cause.
7. Preserve cancellation and `KeyboardInterrupt`. Do not relabel `ValueError` configuration failures as provider outages.
8. Add regressions for the preflight gaps, malformed and empty payloads, valid metadata controls, membership bypasses, and one-shot/watch monitoring independence.
9. Run focused tests, related regression suites, full pytest, and `compileall`.

## Safety constraints

- DEV only. Synthetic databases and injected or mocked clients. No HTTP calls in the new tests.
- `LOCAL_MANUAL_MODE=true`, `ORDER_EXECUTION_ENABLED=false`, `TELEGRAM_DRY_RUN=true`, `TELEGRAM_SIGNALS_ENABLED=false` in the orchestration tests.
- No order execution, withdrawals, live Telegram delivery, scanner watch loop left running, or historical backfill.
- No live Runtime database access. The older schema-25 readiness runbook is not schema-26 or F04-writer approval.
- Strict failure does not fall back to all tickers, quote-volume discovery, manual includes, persisted watch or history, unvalidated prior rankings, or another provider.
- Manual, top-volume, and top-tradable modes stay explicit user-selected modes.
- `--max-symbols`, strict `--include-symbols`, watch continuation, lifecycle priority, cooldown, and adaptive priority cannot enlarge strict membership.
- No strategy redesign, quality-gate change, RR change, or lifecycle-semantics change.

## Caller and failure-routing inventory

Production strict market-cap entry is `resolve_symbol_universe` for `binance_usdt_perp_top_market_cap`. The provider is CoinPaprika rankings intersected with Binance USDT perpetual tickers and `exchangeInfo` contracts.

| Caller | Role | Failure behavior |
| --- | --- | --- |
| `app/universe/symbol_universe.py::resolve_symbol_universe` | Fetches rankings, tickers, and exchange info, then builds the strict universe | Acquisition exceptions become `UniverseResolutionError` and keep the original cause. `CancelledError` and `KeyboardInterrupt` propagate. Shape failures are raised by the builder |
| `build_symbol_universe_from_market_caps` | Strict intersection | Unusable top-level payloads and an empty intersection raise `UniverseResolutionError`. A non-empty short intersection returns normally |
| `scripts/run_scan.py::_resolve_universe_watchlist` | One-shot and watch universe selection, then include/exclude/cap | `UniverseResolutionError` becomes `SystemExit` with that cause. Include symbols outside the resolved set are ignored |
| `_resolve_watchlist_for_args` | Startup selection | Strict `--watch-symbols-from-latest-run` resolves the membership boundary first. Source failure happens before persisted symbols are read |
| `_resolve_watchlist_from_latest_run`, `_extend_watchlist_for_continue_watch`, `_filter_watchlist_to_prior_watch_symbols` | Persisted watch and resume inputs | Strict mode intersects. Outsiders are ignored, not added |
| `_watchlist_with_lifecycle_priority` | Lifecycle priority | Active owners outside `resolved_symbols` are ignored for the new-discovery queue |
| `_queued_symbols_for_scan` | Adaptive and lifecycle queue defense | Out-of-membership extras are dropped |
| `main` | One-shot and watch startup | If `discovery_failure_continues_owner_monitoring` is true, `_surface_monitoring_during_discovery_failure` runs, then the discovery error is re-raised |
| `_attempt_watch_scan_iteration` | Watch refresh | Same continuation with `publish_on_system_exit=False`, so the iteration summary can show both failures |
| `app/lifecycle/owner_monitoring.py::discovery_failure_continues_owner_monitoring` | Classifier | True when `UniverseResolutionError` appears on the exception or its cause chain |
| `app/watch_supervisor.py::classify_watch_exception` | Watch disposition | `UniverseResolutionError`, including as the cause of `SystemExit`, stays recoverable |

Source-validation boundary: missing, wrong-type, empty, or non-mapping ranking, ticker, or exchange-info payloads; ticker rows with no usable USDT perpetual; contract rows with no admissible crypto USDT perpetual. These raise `UniverseResolutionError`.

Configuration boundary, unchanged: manual mode, unsupported mode, `universe_size < 1`, invalid symbols, and an include/exclude/max-symbols queue that empties an already resolved non-empty universe. Those stay `ValueError` or `SystemExit` without a universe-resolution cause, so the P5A classifier does not treat them as provider outages.

Related path left unchanged: top-volume and top-tradable builders still consume ticker rows directly. The production Binance adapter already rejects a non-list ticker payload and a non-mapping exchange-info payload. An injected `None` ticker list on those non-strict builders can still raise `TypeError`. They are not fallbacks for strict discovery.

## Failure semantics

| Condition | Result | P5A continuation |
| --- | --- | --- |
| Valid ranking, tickers, and admissible contracts, including a short intersection | Resolved symbols, `contract_metadata_used=true`, absolute rank `<= N` | Not a discovery failure |
| `exchange_info` missing, not a mapping, or without a symbol list | `UniverseResolutionError` malformed or empty exchange-info. No inferred contract | Yes |
| Contract rows present but none admissible | `UniverseResolutionError` no admissible USDT perpetual contracts | Yes |
| Ticker payload missing, wrong type, empty, or without mappings | `UniverseResolutionError` malformed or empty ticker source | Yes |
| Ticker rows present but none usable | `UniverseResolutionError` no usable USDT tickers | Yes |
| Ranking payload missing, wrong type, or empty | Existing CoinPaprika malformed or empty `UniverseResolutionError` | Yes |
| Usable sources whose rank `<= N` intersection is empty | `UniverseResolutionError` strict market-cap discovery intersection is empty. No backfill from rank `> N` | Yes |
| Fetch timeout, transport error, HTTP error, rate limit, or malformed JSON | Existing `UniverseResolutionError`, original cause preserved | Yes |
| Cancellation or keyboard interrupt during fetch | Propagates | No relabel |
| Operator include/exclude/cap empties a resolved universe | `SystemExit` configuration text, no universe cause | No |
| Owned-plan candle gap during a discovery failure | Discovery error remains. Gap stays on outcome progress. No terminal outcome is invented | Monitoring attempted |
| Unexpected monitoring failure during a discovery failure | Discovery text remains. `owner_monitoring:` is attached on the one-shot exit and on the watch iteration summary | Both visible |

`generated_at` is still a local timestamp. This phase does not treat it as provider publication time or as proof of possession before a cutoff.

## Before and after

Synthetic preflight on the base, injected responses only:

| Case | Before (`44efa90`) | After (`fcaccf2080f27c4a5cd151c437717aa497d6f8a7`) |
| --- | --- | --- |
| Valid ranking, ticker, and contract metadata | `BTCUSDT`, `contract_metadata_used=true` | Preserved |
| Exchange metadata `None` | `BTCUSDT` with `contract_metadata_used=false`; ticker presence became membership | `UniverseResolutionError`: exchange-info malformed. No symbol resolved |
| Ticker payload `None` | Bare `TypeError`; P5A classifier false | `UniverseResolutionError`: ticker source malformed. Classifier true. Cause is not a `TypeError` |
| Ranking payload `None` | `UniverseResolutionError`; classifier true | Preserved |

These cases exercise the resolver and helper boundary. They do not claim a live Binance or CoinPaprika outage. The production Binance adapter already rejects a non-list ticker body and a non-mapping exchange-info body.

The direct builder fixture in `tests/test_symbol_universe.py` now passes explicit synthetic BTC, ETH, SOL, USDC, and XRP contract metadata. Membership assertions stay `("BTCUSDT", "ETHUSDT")`, with `USDCUSDT` excluded.

## Tests

| Suite | Result |
| --- | --- |
| `tests/test_strict_market_cap_universe.py` | 42 passed |
| `tests/test_symbol_universe.py` | 10 passed |
| `tests/test_f05_discovery_fail_closed.py` | 9 passed |
| Focused total | 61 passed |
| Scanner, watch, P5A, F03, F04, evaluation policy, evaluation semantics, lifecycle, observation, public quality, RR, setup quality, source-evidence boundary | 519 passed |
| `python -m pytest` | 2858 passed, 0 failed |
| `python -m compileall -q app scripts src tests` | success |

No assertion was weakened and no test was marked xfail. Orchestration tests use the real resolver through patched CoinPaprika and Binance client objects, synthetic operational databases, and a mocked kline client. On source failure, `ScannerRunner.run` is not called, `symbol_results`, `setup_candidates`, and `public_alert_events` stay empty, and the owned `CAKEUSDT` plan is not inserted into discovery. Where candles are usable, that plan's cursor advances and a repeated window does not add another event. A monitoring `ValueError` remains beside the discovery error on both the one-shot exit and the watch refresh summary.

## Non-regression

- Schema version constant remains 26.
- Public minimums remain quality 88, grade A, and RR 3. `tests/test_public_signal_quality.py` and `tests/test_authoritative_minimum_rr.py` passed.
- F03 population tests passed.
- F04 durable-replay tests passed. Capture code was not modified. Default remains off unless both capture environment variables are set.
- P5A owner-monitoring, binding, and failure-visibility tests passed.
- Absolute Top-N, no backfill from rank greater than N, ambiguous-base exclusion, deterministic order, and distinct manual/volume/tradable include behavior remain covered by the existing strict-universe tests.
- Short healthy intersections still return fewer than N symbols. An empty intersection is an explicit universe error, not a successful empty discovery.

## Self-attack

- Omitting `exchange_info` on the strict builder now fails. It no longer copies ticker symbols into the contract set.
- A rank greater than N with a valid contract does not fill a hole left by an in-boundary asset that has no contract.
- `--include-symbols` and latest-run symbols cannot add names outside a real strict resolution. A failed resolution does not read persisted symbols.
- Excluding the only resolved symbol stays a configuration `SystemExit`. Owner monitoring does not run, and the cursor does not move.
- A successful strict scan queues only the intersection. An owned symbol outside that intersection is monitored and is not scanned as a new opportunity.
- Watch classifies the wrapped universe error as recoverable. One failed refresh does not hide the discovery text.
- Fetch wrappers still attach the original acquisition exception. Cancellation is not converted into `UniverseResolutionError`.
- A programming error inside later ranking arithmetic is not caught by a blanket builder `except Exception`.

## CI

- Handoff head `476274188d3fb7836de8387f405e08b661c19222` contains implementation `fcaccf2080f27c4a5cd151c437717aa497d6f8a7`.
- CI run [37194439433](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/37194439433), attempt 1, success, on that exact head. Job: Python 3.11 tests.
- The documentation commit that records this paragraph is the branch tip after this commit. Its PR check is the CI result for the exact final head.
- Independent acceptance is not claimed.

## Deferred

- Runtime capacity, retention, restore, and rollout approval remain a separate checkpoint before F04 capture activation.
- No ranking cache, no new source-freshness rule, no provider change, and no historical backfill.
- Top-volume and top-tradable injected `None` ticker payloads are not retargeted in this phase.
- This document is not independent acceptance and not Runtime deployment approval.
