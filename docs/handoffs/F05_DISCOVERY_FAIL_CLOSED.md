# F05 — Discovery fail-closed

## Phase

F05_DISCOVERY_FAIL_CLOSED

This phase stops new strict market-cap discovery when exchange-contract evidence, another required source payload, or owned source-client cleanup is unusable. It does not reimplement P5A owner monitoring. It does not add a ranking cache, a freshness policy, another provider, setup admission, or a public new-setup path.

Application schema remains 26. Public quality remains 88 / grade A / RR 3. F03 population isolation, F04 replay and transaction guarantees, and F04 capture default OFF are unchanged. P5A owner definitions, plan binding, and cursors are unchanged.

Capture stays off unless `SOURCE_REPLAY_CAPTURE_ENABLED=true` and `SOURCE_REPLAY_EVIDENCE_PATH` are both set. This phase did not enable capture, did not deploy, and did not merge.

## Delivery

- Branch: `fix/f05-discovery-fail-closed`
- Base SHA: `44efa90daff8476c480eb21ed8ba43654ee8e5de` (`origin/main` at the F05 start; merge of PR #128)
- F04 accepted head `54bd2385dcf3b6265b3cdff8e68ecb484f9cc3a8` is an ancestor of that base, with no file changes in the merge commit
- Initial strict-boundary implementation SHA: `fcaccf2080f27c4a5cd151c437717aa497d6f8a7`
- Rejected review head: `fb5839f89956dc7bf3582975350abee609dbc436` (disposition REQUEST_CHANGES_PR_129). Its green suite did not cover the source-cleanup matrix
- Cleanup repair SHA: `cb4389a2d96f75718ae732f3cf9291a32c7aa7cb` (owned-client cleanup capture, regressions, and this handoff)
- The documentation commit that records the verified repair CI below is the branch tip. Its pull-request check is the CI result for the exact final head
- Draft PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/129
- Schema version: remains 26
- Runtime database: not opened, copied, or modified

No second PR was opened. Local `main` was not fast-forwarded. Unrelated local work was not present on this branch when the repair started (`git status` was clean at `fb5839f`).

Acceptance is not claimed. Adam owns merge and any later Runtime rollout.

## Acceptance question

Can unusable strict-universe evidence produce new discovery membership, or prevent an already-owned plan from receiving its existing independent monitoring?

For an unusable source, including an owned source-client cleanup failure, new discovery stops on a recognizable `UniverseResolutionError` route. P5A monitoring still runs when lifecycle tracking is enabled and candle evidence permits. A monitoring failure stays visible beside the discovery error. `CancelledError` and `KeyboardInterrupt` stay the escaping control exception and do not start monitoring.

## Implementation plan (executed)

1. Confirm `44efa90daff8476c480eb21ed8ba43654ee8e5de` contains the merged F04 head and branch from it.
2. Inventory strict market-cap callers and separate source-validation failures from CLI configuration errors.
3. Require usable exchange-contract metadata on the strict path. Remove ticker-presence inference.
4. Validate top-level ranking, ticker, and exchange-info shapes before row iteration. Keep conservative exclusion of individual bad rows.
5. Raise `UniverseResolutionError` for unusable payloads and for an empty strict intersection so the existing P5A classifier still reaches owner monitoring.
6. Leave operator include/exclude/max-symbol empty queues as configuration `SystemExit` values without a universe-resolution cause.
7. Preserve cancellation and `KeyboardInterrupt`. Do not relabel `ValueError` configuration failures as provider outages.
8. On the independent review of `fb5839f`, close the resolver-owned Binance client and the owned CoinPaprika client without letting `aclose()` replace the primary outcome.
9. Keep a payload or acquisition failure as the primary error and attach cleanup as `source_cleanup_error`. If only cleanup fails after valid payloads, raise `UniverseResolutionError` whose cause is the original cleanup exception.
10. If close itself raises `CancelledError` or `KeyboardInterrupt`, that stop propagates and is not converted into a provider outage.
11. Add owned-client regressions for the eight owner-continuity cases and the four control-exception cases, plus combined diagnostics, gaps, and monitoring-failure visibility.
12. Run focused tests, related regression suites, full pytest, `compileall`, and `git diff --check`.

## Safety constraints

- DEV only. Synthetic databases and injected or mocked clients. No HTTP calls in the new tests.
- `LOCAL_MANUAL_MODE=true`, `ORDER_EXECUTION_ENABLED=false`, `TELEGRAM_DRY_RUN=true`, `TELEGRAM_SIGNALS_ENABLED=false`, `SOURCE_REPLAY_CAPTURE_ENABLED=false` for the repair verification.
- No order execution, withdrawals, live Telegram delivery, scanner watch loop left running, or historical backfill.
- No live Runtime database access. The older schema-25 readiness runbook is not schema-26 or F04-writer approval.
- Strict failure does not fall back to all tickers, quote-volume discovery, manual includes, persisted watch or history, unvalidated prior rankings, or another provider.
- Manual, top-volume, and top-tradable modes stay explicit user-selected modes. Their `finally: aclose()` path is unchanged.
- `--max-symbols`, strict `--include-symbols`, watch continuation, lifecycle priority, cooldown, and adaptive priority cannot enlarge strict membership.
- No strategy redesign, quality-gate change, RR change, lifecycle-semantics change, ranking-cache change, provider change, writer change, or delivery-policy change.
- Cleanup capture does not swallow every exception and does not relabel later builder arithmetic or configuration `ValueError` as a provider outage.
- No F06 or F08 work. No merge and no deployment.

## Caller and failure-routing inventory

Production strict market-cap entry is `resolve_symbol_universe` for `binance_usdt_perp_top_market_cap`. The provider is CoinPaprika rankings intersected with Binance USDT perpetual tickers and `exchangeInfo` contracts.

| Caller | Role | Failure behavior |
| --- | --- | --- |
| `app/universe/symbol_universe.py::resolve_symbol_universe` | Fetches rankings, tickers, and exchange info, then builds the strict universe | Acquisition exceptions become `UniverseResolutionError` and keep the original cause. `CancelledError` and `KeyboardInterrupt` propagate as the same object. Shape failures are raised by the builder |
| `_close_owned_source_client` | Closes the strict resolver-owned Binance client and the owned CoinPaprika client | Returns a cleanup `Exception` or a close-time control exception. It does not raise from `finally` |
| `fetch_coinpaprika_market_cap_rankings` | Owned ranking client when no client is injected | HTTP, timeout, transport, and payload errors stay primary. Cleanup-only failure is `UniverseResolutionError`. An injected client is not closed here |
| `build_symbol_universe_from_market_caps` | Strict intersection | Unusable top-level payloads and an empty intersection raise `UniverseResolutionError`. A non-empty short intersection returns normally. Internal programming errors stay their original type |
| `scripts/run_scan.py::_resolve_universe_watchlist` | One-shot and watch universe selection, then include/exclude/cap | `UniverseResolutionError` becomes `SystemExit` with that cause. Include symbols outside the resolved set are ignored |
| `_resolve_watchlist_for_args` | Startup selection | Strict `--watch-symbols-from-latest-run` resolves the membership boundary first. Source failure happens before persisted symbols are read |
| `_resolve_watchlist_from_latest_run`, `_extend_watchlist_for_continue_watch`, `_filter_watchlist_to_prior_watch_symbols` | Persisted watch and resume inputs | Strict mode intersects. Outsiders are ignored, not added |
| `_watchlist_with_lifecycle_priority` | Lifecycle priority | Active owners outside `resolved_symbols` are ignored for the new-discovery queue |
| `_queued_symbols_for_scan` | Adaptive and lifecycle queue defense | Out-of-membership extras are dropped |
| `main` | One-shot and watch startup | If `discovery_failure_continues_owner_monitoring` is true, `_surface_monitoring_during_discovery_failure` runs, then the discovery error is re-raised. Control exceptions are not caught here |
| `_attempt_watch_scan_iteration` | Watch refresh | Same continuation with `publish_on_system_exit=False`, so the iteration summary can show both failures |
| `app/lifecycle/owner_monitoring.py::discovery_failure_continues_owner_monitoring` | Classifier, unchanged | True when `UniverseResolutionError` appears on the exception or its cause chain |
| `app/watch_supervisor.py::classify_watch_exception` | Watch disposition, unchanged | `UniverseResolutionError`, including as the cause of `SystemExit`, stays recoverable |

Source-validation boundary: missing, wrong-type, empty, or non-mapping ranking, ticker, or exchange-info payloads; ticker rows with no usable USDT perpetual; contract rows with no admissible crypto USDT perpetual. These raise `UniverseResolutionError`.

Source-cleanup boundary: an owned Binance or CoinPaprika `aclose()` failure after a successful read is itself `UniverseResolutionError` (`Binance USDT perpetual source cleanup failed` or `CoinPaprika market-cap source cleanup failed`) with `__cause__` set to the original cleanup exception and `source_cleanup_error` set to that same exception. If a payload, HTTP, timeout, transport, or acquisition error already exists, that error stays primary. On a `UniverseResolutionError`, the exit text gains a suffix `source_cleanup_error:<type>:<message>` after the payload text. Other exception types keep their message and gain only the attribute.

Configuration boundary, unchanged: manual mode, unsupported mode, `universe_size < 1`, invalid symbols, and an include/exclude/max-symbols queue that empties an already resolved non-empty universe. Those stay `ValueError` or `SystemExit` without a universe-resolution cause, so the P5A classifier does not treat them as provider outages. A later `_market_cap_usd` programming `ValueError` is also not wrapped, even if cleanup failed.

Control boundary: `CancelledError` and `KeyboardInterrupt` from acquisition or from close remain the escaping exception object. Cleanup is attached as `source_cleanup_error` and is not turned into `UniverseResolutionError`. `main` does not catch those control exceptions, so monitoring does not start.

Related path left unchanged: top-volume and top-tradable builders still consume ticker rows directly and still close their Binance client in `finally`. The production Binance adapter already rejects a non-list ticker payload and a non-mapping exchange-info payload. An injected `None` ticker list on those non-strict builders can still raise `TypeError`. They are not fallbacks for strict discovery.

## Failure semantics

| Condition | Result | P5A continuation |
| --- | --- | --- |
| Valid ranking, tickers, and admissible contracts, including a short intersection, and a successful close | Resolved symbols, `contract_metadata_used=true`, absolute rank `<= N` | Not a discovery failure |
| `exchange_info` missing, not a mapping, or without a symbol list | `UniverseResolutionError` malformed or empty exchange-info. No inferred contract | Yes |
| Contract rows present but none admissible | `UniverseResolutionError` no admissible USDT perpetual contracts | Yes |
| Ticker payload missing, wrong type, empty, or without mappings | `UniverseResolutionError` malformed or empty ticker source | Yes |
| Ticker rows present but none usable | `UniverseResolutionError` no usable USDT tickers | Yes |
| Ranking payload missing, wrong type, or empty | Existing CoinPaprika malformed or empty `UniverseResolutionError` | Yes |
| Usable sources whose rank `<= N` intersection is empty | `UniverseResolutionError` strict market-cap discovery intersection is empty. No backfill from rank `> N` | Yes |
| Fetch timeout, transport error, HTTP error, rate limit, or malformed JSON | Existing `UniverseResolutionError`, original cause preserved | Yes |
| Owned-client cleanup fails after valid payloads | `UniverseResolutionError` source cleanup failed. Cause is the original cleanup exception | Yes |
| Payload or acquisition failure and cleanup failure together | Primary payload or acquisition error and cause stay. Cleanup is `source_cleanup_error` and, for universe errors, a message suffix | Yes |
| Cancellation or keyboard interrupt during fetch or close | Same control exception propagates. Cleanup, if any, is only the attribute | No. Monitoring is not started |
| Operator include/exclude/cap empties a resolved universe | `SystemExit` configuration text, no universe cause | No |
| Later internal builder programming error | Original exception type. Cleanup may be attached and is not a universe wrapper | No |
| Owned-plan candle gap during a discovery failure | Discovery error remains. Gap stays on outcome progress. No terminal outcome is invented | Monitoring attempted |
| Unexpected monitoring failure during a discovery failure | Discovery text remains. `owner_monitoring:` is attached on the one-shot exit and on the watch iteration summary | Both visible |

`generated_at` is still a local timestamp. This phase does not treat it as provider publication time or as proof of possession before a cutoff.

## Before and after

Synthetic preflight on the base, injected responses only:

| Case | Before (`44efa90`) | After strict boundary (`fcaccf2`) |
| --- | --- | --- |
| Valid ranking, ticker, and contract metadata | `BTCUSDT`, `contract_metadata_used=true` | Preserved |
| Exchange metadata `None` | `BTCUSDT` with `contract_metadata_used=false`; ticker presence became membership | `UniverseResolutionError`: exchange-info malformed. No symbol resolved |
| Ticker payload `None` | Bare `TypeError`; P5A classifier false | `UniverseResolutionError`: ticker source malformed. Classifier true. Cause is not a `TypeError` |
| Ranking payload `None` | `UniverseResolutionError`; classifier true | Preserved |

Independent cleanup review of `fb5839f`, owned clients, synthetic `CAKEUSDT` owner, usable closed candles, cursor `2026-03-23T06:10:00+00:00`, and `aclose()` raising `RuntimeError: synthetic HTTP client cleanup failure`:

| Case | Before (`fb5839f`) | After this cleanup repair |
| --- | --- | --- |
| Exchange metadata `None` plus cleanup | Bare cleanup `RuntimeError`. Classifier false. Monitoring calls 0. Cursor unchanged | Malformed exchange-info stays primary. Cleanup suffix and attribute retained. Monitoring runs. Cursor advances |
| Ticker payload `None` plus cleanup | Same bare cleanup escape | Malformed ticker source stays primary. Monitoring runs |
| `exchange_info={"symbols": []}` plus cleanup | Same bare cleanup escape. Builder empty-contract error never ran | Empty exchange-info stays primary. Monitoring runs |
| `tickers=[]` plus cleanup | Same bare cleanup escape | Empty ticker source stays primary. Monitoring runs |
| Contract `status="BREAK"` plus cleanup | Same bare cleanup escape | No admissible USDT perpetual contracts stays primary. Monitoring runs |
| Valid ranking, tickers, and contracts, cleanup only | Same bare cleanup escape | `UniverseResolutionError` cleanup failed. Cause is the cleanup `RuntimeError`. Monitoring runs |
| Watch refresh, empty contracts after a successful startup, cleanup only on the refresh | Summary `iteration:RuntimeError:synthetic HTTP client cleanup failure`. No monitoring | Failed iteration keeps the empty exchange-info text and the cleanup suffix. Status `FAILED`. Monitoring runs. No new discovery queue |
| Watch refresh, valid source data, cleanup only on the refresh | Same `iteration:RuntimeError` summary | Failed iteration text is the cleanup `UniverseResolutionError`. Monitoring runs |
| Binance fetch `CancelledError` or `KeyboardInterrupt`, close raises `RuntimeError` | Cleanup `RuntimeError` escaped | The original control exception escapes. Cleanup is `source_cleanup_error`. Monitoring does not start |
| CoinPaprika `get` `CancelledError` or `KeyboardInterrupt`, owned ranking close raises `RuntimeError` | Outer resolver turned cleanup into `UniverseResolutionError` | The original control exception escapes. It is not a universe outage |

No new symbols were queued on those failure paths. `symbol_results`, `setup_candidates`, and `public_alert_events` stayed empty. The owned `CAKEUSDT` plan was not inserted into discovery. Where candles were usable, the cursor advanced and a repeated one-shot window did not add another event. A candle `TimeoutError` left the cursor unchanged, kept a nonterminal outcome, and left the discovery text in place. A monitoring `ValueError` stayed beside the discovery text. An HTTP 503 ranking response stayed ahead of a ranking-client cleanup suffix. A builder `ValueError` stayed a `ValueError`.

These cases use synthetic payloads and mocked transports. They do not claim a live Binance or CoinPaprika outage.

## Tests

| Suite | Result |
| --- | --- |
| `tests/test_f05_source_cleanup.py` | 22 passed. Eight owner-continuity paths (six one-shot payload/cleanup cases, empty-contract watch refresh, valid watch refresh), watch startup, four control exceptions, plus acquisition-plus-cleanup, ranking HTTP-plus-cleanup, gap and subsystem monitoring on one-shot and watch refresh, owned-client success, and a builder programming error |
| `tests/test_strict_market_cap_universe.py` | 42 passed |
| `tests/test_symbol_universe.py` | 10 passed |
| `tests/test_f05_discovery_fail_closed.py` | 9 passed |
| Focused total | 83 passed |
| Related scanner, watch, P5A, F03, F04, evaluation policy, evaluation semantics, evaluation context, lifecycle, observation, public quality, RR, setup quality, and source-evidence files, excluding the four focused files above | 681 passed |
| That related command together with the focused files | 764 passed in 164.64s |
| `python -m pytest` | 2880 passed, 0 failed, 1 existing Starlette deprecation warning, 457.88s |
| `python -m compileall -q app scripts src tests` | success |
| `git diff --check` | clean |

The related file set was `tests/test_scanner_runner.py`, `tests/test_run_scan.py`, `tests/test_watch_mode.py`, `tests/test_watch_supervisor_reliability.py`, `tests/test_p5a_active_owner_monitoring.py`, `tests/test_p5a_monitoring_binding_query_repair.py`, `tests/test_p5a_monitoring_failure_visibility.py`, `tests/test_p5a_local_setup_failure_visibility.py`, `tests/test_f03_prospective_research_population.py`, `tests/test_f04_durable_source_replay.py`, `tests/test_evaluation_policy_manifest.py`, `tests/test_evaluation_semantics_contract.py`, `tests/test_evaluation_context_p3b2a.py`, `tests/test_lifecycle.py`, `tests/test_lifecycle_eligibility.py`, `tests/test_lifecycle_outcomes.py`, `tests/test_lifecycle_ownership_repair_p2b.py`, `tests/test_lifecycle_geometry_hygiene.py`, `tests/test_observation_unit_contract_r0.py`, `tests/test_public_signal_quality.py`, `tests/test_authoritative_minimum_rr.py`, `tests/test_setup_quality.py`, and `tests/test_source_evidence_boundary.py`.

No assertion was weakened and no test was marked xfail. The independent review probe files were not edited. Orchestration tests construct the real resolver's owned Binance and CoinPaprika clients through patches, use synthetic operational databases, and use a mocked kline client. On source or cleanup failure, `ScannerRunner.run` is not called.

## Non-regression

- Schema version constant remains 26.
- Public minimums remain quality 88, grade A, and RR 3. `tests/test_public_signal_quality.py` and `tests/test_authoritative_minimum_rr.py` passed inside the related run.
- F03 population tests passed.
- F04 durable-replay tests passed. Capture code was not modified. Default remains off unless both capture environment variables are set.
- P5A owner-monitoring, binding, and failure-visibility tests passed. The classifier function was not changed.
- Absolute Top-N, no backfill from rank greater than N, ambiguous-base exclusion, deterministic order, and distinct manual/volume/tradable include behavior remain covered by the existing strict-universe tests.
- Short healthy intersections still return fewer than N symbols. An empty intersection is an explicit universe error, not a successful empty discovery.
- A successful owned-client close still resolves `BTCUSDT` with `contract_metadata_used=true`.

## Self-attack

- Omitting `exchange_info` on the strict builder now fails. It no longer copies ticker symbols into the contract set.
- A rank greater than N with a valid contract does not fill a hole left by an in-boundary asset that has no contract.
- `--include-symbols` and latest-run symbols cannot add names outside a real strict resolution. A failed resolution does not read persisted symbols.
- Excluding the only resolved symbol stays a configuration `SystemExit`. Owner monitoring does not run, and the cursor does not move.
- A successful strict scan queues only the intersection. An owned symbol outside that intersection is monitored and is not scanned as a new opportunity.
- Watch classifies the wrapped universe error as recoverable. One failed refresh does not hide the discovery text.
- On `fb5839f`, `finally: await client.aclose()` replaced both validation failures and control exceptions. The repair closes the owned client after the read and chooses the escaping exception explicitly.
- Payload text stays ahead of the cleanup suffix. Cleanup-only failure still names the cleanup exception as `__cause__`.
- A programming error inside later ranking arithmetic is not wrapped as `UniverseResolutionError`.
- A requested stop is not resumed and is not monitored because cleanup also failed.

## CI

Historical, not this repair:

- Handoff head `476274188d3fb7836de8387f405e08b661c19222`: CI run [37194439433](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/37194439433), attempt 1, success. Job: Python 3.11 tests.
- Rejected review head `fb5839f89956dc7bf3582975350abee609dbc436`: CI run [37194685853](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/37194685853), attempt 1, success, 2,858 tests. Disposition remained REQUEST_CHANGES_PR_129 because the source-cleanup matrix failed outside that suite.

Cleanup repair head `cb4389a2d96f75718ae732f3cf9291a32c7aa7cb`: CI run [37198079234](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/37198079234), attempt 1, success. Job: Python 3.11 tests, ID 111423973368, about 3m41s. That run is the repair commit, not the rejected `fb5839f` run.

The documentation commit that records this paragraph is the branch tip after this commit. Its pull-request check is the CI result for the exact final head. Independent acceptance is not claimed.

## Operational actions

None. No merge, no deployment, no Runtime capture activation, no live Runtime database access, no scanner or Telegram listener, no network scan, no Telegram send, and no order.

## Deferred

- Runtime capacity, retention, restore, and rollout approval remain a separate checkpoint before F04 capture activation.
- No ranking cache, no new source-freshness rule, no provider change, and no historical backfill.
- Top-volume and top-tradable injected `None` ticker payloads are not retargeted in this phase.
- This document is not independent acceptance and not Runtime deployment approval.
