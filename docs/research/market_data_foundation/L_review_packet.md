# L. Review packet

- Repository: `candlecraftinteligence/candle-craft-trading-agent`
- Base SHA: `962dbbb35db942beef0423c0763555e665da668d`
- Head SHA: the pull request head that contains this packet. Review that exact head. A later commit requires a new review.
- PR: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/136
- Cause: `test_scanner_does_not_request_binance_2d_and_uses_synthetic_2d` and `test_replay_candles_1000_clamps_execution_timeframes_only` left both the mock daily alignment and the scanner decision cutoff on the wall clock. The 220-day pullback fixture is resampled into Unix-epoch 2D buckets. On 2026-10-18 UTC that window emits 110 complete candles. From 2026-10-19 00:00:00 UTC it emits 109, because the first day is an incomplete bucket. The same 109 result was reproduced on this branch and on a clean checkout of base `962dbbb` on 2026-10-19. Main CI last passed on 2026-10-18 (`gh` run 39147457349). Production scanner code is unchanged.
- Repair: `tests/test_scanner_runner.py` pins `_SYNTHETIC_2D_DECISION` to 2026-10-18 12:00:00 UTC. Both tests pass that instant as `candle_as_of` and `decision_timestamp`. The expected synthetic count remains 110. The replay test still requires limits `("15m", 500)`, `("12h", 220)`, and `("1d", 440)`, and still rejects a 2D fetch and a 2000-bar daily fetch. `test_pullback_fixture_synthetic_2d_count_follows_the_explicit_clock` checks 00:00 and 23:59:59.999 on 2026-10-18, the 2026-10-19 midnight boundary, 2024-02-29, and the 2025 year boundary. `test_pinned_synthetic_2d_decision_ignores_a_different_runner_clock` keeps the count at 110 when the runner clock is 2026-10-19 and the explicit decision stays on 2026-10-18.
- Tests: Python 3.11.9. `python -m pip check` reported no broken requirements. `python -m compileall -q app scripts src tests` succeeded. `python -m pytest` passed 3101 tests, 1 warning, 557.02s.
- CI: required GitHub CI on this pull request head. The run URL is recorded on the pull request after that check completes.

Production paths touched: none. This repair changes the scanner tests and this packet.

Independent reviewer: Astra. This document is not Astra approval.

Allowed next action after a verdict: only what the verdict names. No Runtime pull, restart, or database action is authorized by this phase.
