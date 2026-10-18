# CCI Signal Accuracy Phase 1 — essential candle freshness

Status: IMPLEMENTED_AND_VERIFIED — AWAITING_ASTRA_REVIEW.
This is not merged, not deployed, and not independently approved.
Astra's review of `142cb789c7a72b16d6c2eac240923af7398f7383` was REQUIRES_CORRECTION. That verdict is not withdrawn. The corrected head needs a new independent review. Do not treat this handoff as approval or as authorization to merge.

Base: `e461c9c501196c3377d53beedae2c930c6d5c21f` (merge of PR #134).
Branch: `fix/cci-signal-accuracy-p1-candle-freshness`.
Earlier reviewed heads: `338d134ac46d8801c8318838cb9f5f1a239f6a73`, `96bdff361499d40633802517c13111d860d1accc`.
Latest reviewed head: `142cb789c7a72b16d6c2eac240923af7398f7383`.
Final behavior commit: `5ac009adcb85f349a1f4b943c97ec3876b4d1c02`.
The documentation commit that names that SHA does not change behavior. Review the pull request head, which contains the correction.
Pull request: https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/135
Scope: correctness of essential candle coverage at the scan decision cutoff. No Runtime database, listener, watch loop, order execution, or deploy.

## Objective

A continuous, fully closed, but outdated essential candle batch could create a trade idea and contribute to lifecycle CONFIRMED without a required-data freshness failure. HTTP fetch time, cache insertion, processing time, and scan id do not prove the candles are current.

Hypothesis: require the latest supplied closed candle's scheduled close `C` to cover the latest required completed interval `E` at the scan decision cutoff `D`. Stale or unverifiable essential evidence must not create a new trade idea, a qualifying confirmation increment, a new plan latch, or a new public signal. An existing owned plan keeps its geometry, monitoring, and outcome history.

## Temporal contract

Policy `essential-candle-freshness-v1`. Anchor `unix_epoch_utc`. Publication allowance is zero. Cache TTL is not an allowance.

For a supported fixed duration with canonical anchor `A` and duration `delta`:

`E = A + floor((D - A) / delta) * delta`

`C` is the latest supplied closed candle's scheduled close (open + duration). A venue close stamp of `boundary - 1ms` still counts as that scheduled close. The check passes only when `C >= E`, after the existing sequence and closed-as-of checks. Arithmetic is integer milliseconds.

`closed_candles_as_of` is unchanged. It remains valid for historical prefixes and outcome catch-up. The stricter check runs only at discovery consumers that require current essential inputs.

Supported anchors are the existing minute, hour, and day durations, including synthetic 2d on the Unix-epoch UTC grid. Weekly and other undocumented anchors are unverified, not guessed. A still-forming next candle cannot repair a missing completed interval. Each timeframe uses its own `E`. A naturally older 12h or 2d close is not judged stale by the 15m interval.

Operational tradeoff: with a zero allowance, a decision made before the required closed candle is available is rejected. Cache lifetime or the next scan cadence can extend that interruption. It is an availability cost. It is not evidence of better expectancy. No feed contract in this repository documents a bounded publication delay.

## What changed

- `app/data/candle_freshness.py` is a pure helper. It returns pass, stale (`N/A`), missing (`N/A`), or unverified (`Unverified`), plus compact evidence: symbol, timeframe, role, decision cutoff, latest observed close, required close, lag, policy version, allowance, and anchor. A missing assessment is not reported as a pass.
- The scanner assesses primary, execution, confirmation, bias, structure, and the synthetic 2d source when that higher timeframe is configured. Shared timeframes are assessed once. Optional 4h and 1h are not a new global gate. Optional BTC and regime candles that are not current are withheld through the existing optional path.
- A stale present series does not run strategy scoring, so passed gates are not manufactured from outdated candles. A missing series still runs the existing missing-data diagnostics, and the discovery gate still refuses a trade idea when the receipt does not pass.
- A freshness failure returns a normal scanned-no-setup result and keeps execution candles, so one symbol cannot abort the scan and owner monitoring is not starved.
- A new lifecycle row with a freshness failure starts REJECTED with confirmation count 0. An owned plan in a hold state is left in place when freshness is the only required-data failure. Closed-prefix outcome evaluation still runs. A directionless stale rescan keeps the original scanner observation as the decision identity and records each held owner as a binding. It does not insert a second setup row.
- Schema stays 27. No migration. Diagnostics use the existing missing and unverified strings plus the strategy diagnostic receipt. Outbox state, public quality 88, public grade A, and public RR 3 are unchanged. The internal 2.5R floor is unchanged.

`app/lifecycle/state_machine.py` is part of the durable-replay implementation fingerprint. This change moves that fingerprint. Existing captures must report an unsupported implementation rather than a silent match. `app/lifecycle/outcomes.py` and `app/data/candle_integrity.py` were not edited, so outcome-evaluator bytes and the historical closed-prefix utility are unchanged.

## Base reproduction

On a separate worktree at `e461c9c`, with decision `2026-03-01 14:00 UTC` and a 15m series whose last open is `2026-02-22 13:45 UTC`:

- `closed_candles_as_of` accepted all 220 closed candles.
- `ScannerRunner` created a trade idea, status `JOURNAL_ENTRY_CREATED`, best RR `3.06779661`.
- No freshness field was present.

That probe was run after the candidate existed, against the unchanged base commit. It was not a pytest captured before the first edit. The candidate tests now reject that class of input.

## Astra correction

Review comment on PR #135 required four lifecycle corrections. The full suite on `338d134` was already green (3043 passed, GitHub run 39113072506). These probes were not in that suite. They failed on that head and pass after `31ca289`.

1. Multiple owners reached TP_HIT, SL_HIT, or BREAKEVEN, and `setup_outcome_analytics` stayed empty. The multi-owner hold wrote lifecycle rows and skipped the shared analytics insert, keeping only the last owner's in-memory context. Each owner now gets the same terminal analytics insert, in the same transaction, including an identical retry that does not add a second row.
2. One swing/long owner plus a stale challenge/N/A result stored the receipt under `challenge` and pointed it at the swing lifecycle. The identical retry raised `RuntimeEpochIdentityCollisionError`: "Observation consumption integrity exception: conflicting lifecycle owner". `31ca289` rebound the observation to the owner before hashing it. That removed the retry collision and is superseded by the final correction below: the original observation is hashed, and the owner mode and direction stay on the binding. Identical, restarted, and later processing of the same decision are idempotent. A changed decision cutoff still raises "conflicting decision content".
3. Two current owners, applied twice, both returned SUCCESS, left `lifecycle_consumption_json` at `{"version":1,"modes":{}}`, left `duplicate_confirmation_observations` at 0, and advanced `last_seen_at` again. `31ca289` stored one mode slot per owner. The final correction stores those owners on the original source decision instead. A retry reports those owners as duplicate confirmation observations and does not move `last_seen_at`. An owner that appears after the first binding is not added to that origin.
4. A current CONFIRMED legacy row, and a foreign-epoch row, were eligible for the hold because selection only checked `is_current` and lifecycle state. Writing them hit the existing epoch guard (`Legacy lifecycle identity collision: refusing to overwrite, relabel, or supersede an untagged row.` or `Lifecycle ownership does not match the active runtime epoch.`). Selection now requires the active runtime epoch. Legacy-only, foreign-only, current-only, and mixed populations were tested. The write-side ownership guards are unchanged.

A 5m batch is not applied to an owner whose outcome progress already records a different execution timeframe. That used to mark `execution_timeframe_changed:15m->5m`. Outcome monitoring still runs when the timeframe matches. A symbol savepoint still rolls back a failed owner write, and the receipt with it.

The public regression uses `2026-03-02 14:00 UTC`, an even Unix-epoch day on which this fixture scores 89 and the fake sender fires. A later 15m-stale scan adds no sender call and no public-alert row. The March 1 score-86 block remains a separate threshold check. The 88 floor was not lowered.

`app/lifecycle/outcomes.py`, `app/lifecycle/breakeven.py`, and `app/data/candle_integrity.py` were not edited. `app/lifecycle/state_machine.py` was not edited again, so neither lifecycle correction moves the durable-replay fingerprint a second time. The fingerprint change from the phase 1 `state_machine.py` edit remains.

## Final correction

Review comment https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/135#issuecomment-6250676151 on exact head `96bdff361499d40633802517c13111d860d1accc` was REQUIRES_CORRECTION. GitHub run 39132090968 on that SHA passed 3061 tests. The two probes below failed on that head and pass after `14436983ff9630ea2bbed7afb1a2569757ae6022`.

1. Same-mode owners lost terminal accounting. A current swing/long owner and a current swing/short owner were both valid. Stale discovery wrote both into one mode slot. The second binding raised "conflicting decision content", the symbol savepoint removed the receipt, and owner monitoring then committed SL_HIT and TP_HIT with empty `setup_outcome_analytics` and empty `lifecycle_transitions`. The original challenge observation is now the receipt. Both owners are explicit bindings. The same closed-candle sequence persists SL_HIT for the long plan and TP_HIT with TP3_HIT analytics for the short plan. Each returned transition keeps that owner's lifecycle id, plan version, and stored geometry. An identical retry adds no analytics row, lifecycle event, or public alert.
2. Rebinding lost the original decision. A stale challenge/N/A discovery held a swing owner and recorded only the rebound swing receipt. The same registered scan and symbol could then supply changed qualifying challenge/long content, with the evaluation origin, completion time, decision cutoff, acquisition delivery, execution timeframe, and execution candles unchanged. That call returned SUCCESS, `new_candidates=1`, `confirmation_pending=1`, and a new challenge/long lifecycle in TRIGGERED with `confirmation_count=1`. The digest is now the original scanner observation. The changed content raises "conflicting decision content" and adds no lifecycle or confirmation. An identical retry of the original decision stays idempotent.

Source identity and owner binding are separate. One discovery may hold several owners, including opposite directions of one mode. A later observation of a different mode remains a separate contribution under the existing per-mode contract. A retry uses the recorded bindings: an owner created later does not join, and rotation or a terminal state does not redirect the decision to a replacement owner.

Version 1 receipts stay one owner per mode, with keys `lifecycle_id`, `digest`, and `consumed_at`. Version 2 keeps the top-level keys `version` and `modes`. A copied mode slot may add an `owners` list of that single lifecycle. A source slot is `digest`, `consumed_at`, and `owners`, with no `lifecycle_id`. Each binding is `lifecycle_id`, `mode`, and `direction`. Unknown versions, extra top-level keys, and malformed slots fail closed. A version 1 document is not reread as a multi-owner receipt. Adding a source binding beside an existing version 1 mode slot copies those slots forward explicitly.

Schema stays 27. `lifecycle_consumption_json` is already text, so version 2 is a document contract, not a migration. The monitoring path calls the same terminal-analytics helper inside the owner savepoint. If that insert fails, the savepoint removes the terminal progress. A discovery failure that continues into monitoring cannot leave a terminal progress row without its analytics row.

`app/lifecycle/outcomes.py`, `app/lifecycle/breakeven.py`, `app/data/candle_integrity.py`, and `app/lifecycle/state_machine.py` were not edited in this correction. The durable-replay fingerprint does not move again.

## Transition handoff correction

Review comment https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/135#issuecomment-6251925179 on exact head `142cb789c7a72b16d6c2eac240923af7398f7383` was REQUIRES_CORRECTION. The source-decision identity and same-mode accounting fixes stay.

A swing/long stale observation closed swing-long at SL_HIT during direct processing. Owner monitoring then closed swing-short at TP_HIT and wrote both analytics rows. The service copied monitoring transitions onto the symbol only when the direct list was empty, so the returned `lifecycle_transitions` and the delivery adapter contained swing-long only. An identical retry could not restore the short transition because both owners were already terminal.

`test_direct_and_monitored_owner_transitions_are_both_returned` failed on that head with `{'swing-long'} == {'swing-long', 'swing-short'}`.

`5ac009adcb85f349a1f4b943c97ec3876b4d1c02` records a monitoring evaluation only after `RELEASE owner_monitor`, then appends those transitions after the direct sequence. A monitoring transition is omitted only when its lifecycle, states, reason, timestamp, and event notes match a transition already in the direct list. Different milestones for one owner stay in that owner's order. A rolled-back evaluation is not added.

After that commit the combined result delivers both owners with their lifecycle ids, directions, plan versions, and stored stops. The long owner's transitions finish before the short owner's begin. The short owner's stored events stay TP1, then TP2, then the terminal take-profit. An identical retry adds no lifecycle events, analytics rows, or delivery transitions. Direct-only and monitoring-only batches still include the owners they closed. A short-owner analytics failure leaves that owner CONFIRMED and out of the delivery batch.

## Fixture results

`tests/test_essential_candle_freshness.py` collected 26 tests. `tests/test_essential_candle_freshness_owner_corrections.py` collected 36 tests. `tests/test_confirmation_observation_consumption.py` collected 37 tests. All passed.

Stale false acceptances in that module: 0. The covered stale inputs are a seven-day 15m, 12h, and 2h shift, a one-interval 15m lag, a cache hit after the next close, and a fresh HTTP response that still carries old candles. None created a trade idea.

Stale qualifying confirmation contributions: 0. Two fresh registered scans reach CONFIRMED. A later 15m-stale scan leaves that owner row unchanged and does not add a lifecycle row or a sender call.

Unexplained fresh-control changes: 0. The fresh long control keeps its entry zone, stop, targets, and RR. Flat candles remain a legitimate no-setup. The short mirror must pass freshness; an idea, when one exists, must be short with RR at least 3.

Unassessable: there is no eligible Runtime outcome cohort. Empirical metrics are N/A. No profitability or net expectancy is inferred.

The public fixture scores 89 and reaches the fake sender when the decision falls on an even Unix-epoch day, which preserves the unshifted synthetic 2d pairing. On `2026-03-01` (an odd epoch day) the same OHLC scores 86 and the public sender stays blocked by `public_setup_quality_score_below_min:86<88`. The 88 floor was not lowered and the fixture was not patched upward. The public-path test clock is `2026-03-02 14:00 UTC` so the existing sender assertion observes the unchanged 89 score.

## Evidence matrix

| Question | Disposition |
| --- | --- |
| Direction versus structure | Protected. No new trend filter. |
| Sweeps, BOS/CHoCH, OB/FVG, pullback | Protected. The defect was tail coverage, not those detectors. |
| Multi-timeframe alignment | Protected. Each timeframe has its own required close. |
| Open or stale inputs | Reproduced defect. Open and future candles were already excluded. Tail coverage is new. |
| Repeated observations of one candle | Protected by the existing confirmation-consumption policy. Distinct registered scans of a still-current candle still count. |
| Countertrend evidence | Unverified. Deferred. |
| Target path and TP1 reference breakeven | Protected. Outcome evaluation and breakeven economics were not edited. Multi-owner stale discovery now writes the existing terminal analytics row for every owner. |
| BTC, ETH, and regime context | Optional. Stale series are withheld. They are not a new required symbol gate. |
| 2h structure | Essential when it is the configured structure role. An informational 2h layer is not by itself a strategy defect. |
| Owner monitoring | A freshness failure keeps execution candles and still evaluates the closed prefix. |
| Owner invalidation | Sole freshness failure holds the owner. Any other required-data failure keeps the existing invalidation path. |

## Verification

- `python -m pytest`: exit 0. 3080 collected tests. The run took about 1513 seconds. The only warning was the existing Starlette `httpx` deprecation.
- Before `31ca289`, the four earlier owner probes failed on `338d134`. Before `1443698`, the two accounting probes failed on `96bdff3`. Before `5ac009a`, the combined handoff probe failed on `142cb78` because `lifecycle_transitions` contained only `swing-long`. Those probes pass after `5ac009a`.
- GitHub CI for reviewed head `142cb789c7a72b16d6c2eac240923af7398f7383` is run 39141451547, 3076 passed. CI for the earlier reviewed head `96bdff3` is run 39132090968, 3061 passed. The pull request description records CI for the review head that contains this documentation.
- `python -m compileall -q app scripts src tests`: exit 0.
- `python -m pip check`: no broken requirements.
- `git diff --check`: exit 0.
- No new Cursor Bugbot pass was run for this correction. An earlier Bugbot note is not Astra's review.

Bounded fake scan, one symbol, public-fixture candles, three timed iterations after one warmup. Same six kline calls on base and candidate. No database write in this in-memory runner.

| Measure | Base `e461c9c` | Candidate |
| --- | --- | --- |
| Seconds per scan | 5.217 | 5.928 |
| Tracemalloc peak bytes | 5670044 | 5688015 |
| Diagnostic JSON bytes | 37968 | 46707 |
| Freshness receipt bytes | n/a | 2881 |

The receipt is stored on each mode diagnostic, which accounts for most of the JSON growth. Timing is a single-process sample, not a statistical benchmark. No extra external calls and no candle-array duplication.

## Rollback

Revert this branch. There is no schema migration and no Runtime data rewrite. Reverting restores the previous behavior in which a closed but outdated essential tail can still create a trade idea. Do not roll back by editing the Runtime database.

## Remaining limitations

- Zero publication allowance rejects a decision made before the required closed candle is available. Cache lifetime or the next scan cadence can extend that interruption. That is an availability cost, not an expectancy result.
- The durable-replay implementation fingerprint changes because `state_machine.py` changed in the original phase 1 commit. The later lifecycle corrections do not move it again.
- The public fixture's synthetic 2d trend, and therefore the 86 versus 89 score, depends on whether the daily grid preserves the original 2d pairing. The quality floors were not changed.
- No empirical Runtime cohort was available.
