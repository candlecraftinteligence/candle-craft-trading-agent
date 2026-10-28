# CCI P0B3B2B0A implementation handoff

Date: 2026-10-28 (Europe/Bratislava). Scope: atomic no-send terminalization
correction, adversarial tests and a DEV-only Runtime readiness assessment.

## Release identity and repository separation

- Freshly fetched main/base: `a5ffec2e7446ed6ec274fa8fb6482b4bcb9c24ce`, PR #159.
- Feature branch: `codex/p0b3b2b0a-atomic-outbox-safety`.
- Isolated worktree: `C:\CandleCraftDev\.worktrees\p0b3b2b0a-atomic-outbox-safety`.
- Implementation commit: `f3de472e62d54e93826f2fd87af4acf2c44d2aac`.
- PR: [#160](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/160).
- Final PR/head and exact-head CI: recorded with immutable SHA/run pointers in
  the PR description and final handoff message after verification. A document
  cannot embed its own containing commit SHA without changing that SHA.
- Original checkout branch and untracked handoffs/other worktrees preserved.
  No merge, Runtime operation or next-phase work is authorized. PR #152 separate.

## Confirmed defect and bounded correction

Previously `SQLitePublicTelegramOutbox.mark_terminal_without_send` checked epoch
and canonical reservation association but unconditionally terminalized the event
and reservation while only updating PENDING parts. A stale policy/dry-run result
could overwrite IN_FLIGHT or SENT and leave an incompatible active/SENT part.
B0's two characterization cases explicitly asserted that defect.

B0A checks canonical event/reservation/active epoch and the complete part set
inside the same `BEGIN IMMEDIATE` transaction as all mutations. It compares event
and reservation delivery states, exact delivery attempt ID and count, and rejects
terminal/active/contradictory states, incomplete parts, mismatched part keys,
indexes/counts, prior receipts or success/uncertainty/completion evidence. A SQL
compare-and-set matches event ID, canonical reservation, state, attempt ID/count.
No competing process can claim between validation and the commit. Failure in
part or reservation persistence rolls the event CAS back too.

Only these preclaim snapshots are eligible:

| Source | Required evidence | Allowed result |
| --- | --- | --- |
| Original PENDING | No attempt, zero counts on event/reservation/parts, no claim fields, complete PENDING parts, no receipts/retry/uncertainty evidence | POLICY_DISABLED, SKIPPED_DRY_RUN or FAILED_FINAL |
| RETRYABLE after explicit rejection | Caller-observed exact prior attempt ID/count and state match event/reservation; parts are untouched PENDING or matching RETRYABLE; no SENT/UNCERTAIN/IN_FLIGHT or receipt evidence | Same terminal semantics, attempt budget retained; no retry due-time waiver/reclaim |
| Active claim, terminal, partial success or contradictory history | Even a matching claimed attempt is insufficient for no-send authority | False/no ledger writes; durable evidence retained |

The API keeps the original keyword arguments and adds optional expected snapshot
fields. Defaults authorize only the never-claimed PENDING intent. Return is now
True for a committed transition and False for refusal/repetition. All five
production callsites were inspected: two in the committed-intent helper, three
in lifecycle recovery. The helper snapshots before preflight; recovery passes
its observed row. No caller refreshes a stale attempt merely to gain authority.
Existing callers' skipped/failed results remain local refusal information;
they do not prove a successful durable terminal mutation when the guard refuses.

The B0 overwrite assertions now require refusal and unchanged IN_FLIGHT/SENT
event/part state. Other B0 characterization findings remain unchanged. No
postclaim abort, claim release, retention policy or protocol redesign is added.
Missing parts fail closed rather than being repaired or treated as no-send proof.
Partial success remains governed by the existing delivery state machine; this
terminalizer cannot discard it or force its event into a no-send terminal state.

## Changed files

1. [telegram_outbox.py](app/alerts/telegram_outbox.py): guarded atomic terminalizer,
   private snapshot validation; preserves attempt counts/identity/receipts.
2. [telegram_lifecycle.py](app/alerts/telegram_lifecycle.py): observed snapshot at
   the five existing callers; no protocol/adapter imports or new transport path.
3. [B0 characterization](tests/test_telegram_delivery_ownership_characterization.py):
   corrected two old-defect expectations and renamed that regression.
4. [B0A regressions](tests/test_telegram_no_send_terminalization.py): 40 cases.
5. This implementation handoff.
6. [Runtime readiness/runbook](CCI_RUNTIME_ROLLOUT_READINESS_AND_RUNBOOK.md).

Strategy, RR/Fibonacci, setup quality, public eligibility, price-touch lifecycle,
CMC, schema 27, exchange clients, retries/max attempts, event ordering and economic
provenance are unchanged. No credentials/configuration files are read or changed.

## Concurrency and regression evidence

All tests use synthetic pytest temporary SQLite databases. HTTP is MockTransport
or fake protocol transport; real httpx sync/async network transports are blocked
in the new test module. Spawned children explicitly construct MockTransport.

| Coverage | Assertions |
| --- | --- |
| PENDING terminalization / repeated calls | All three supported terminal states, multipart projection agreement, zero attempts, repeated call changes no ledger columns, no reclaim |
| Active/SENT/UNCERTAIN and prior terminal states | No active-attempt authority accepted and no prior terminal reopened; B0 checks default stale refusal against active/confirmed state |
| Stale retry / wrong reservation / missing event | Old ID/count or missing authority cannot mutate; legitimate exact rejected retry terminalizes without increasing attempts or budget |
| Contradictory ownership | 18 fault cases: canonical pointer, epoch, reservation key/state/attempt/count, event attempt/claim/uncertainty/receipt, part receipt/state/key/count/attempt and missing parts; full ledger snapshots unchanged |
| Independent Windows-compatible processes | `multiprocessing.get_context('spawn')`, independent SQLite connections, bounded Barrier/Event coordination; simultaneous race, refusal during fake HTTP and refusal after receipt commit. Exactly one authority wins; mock HTTP count exactly 0 if terminalizer wins, exactly 1 if claim wins; receipt retained |
| Partial multipart success / restart | First part SENT with durable ID, next RETRYABLE; no-send refused, every ledger column preserved across reopened connection |
| Cancellation / restart | Cancel after mock HTTP entered: exactly 1 invocation, refuse no-send against IN_FLIGHT, expired recovery -> UNCERTAIN, refuse terminalization/replay, unchanged uncertainty evidence |
| Rollback | Injected trigger failure on parts or reservation undoes every projection, including event CAS |
| Existing caller interleaving | Legitimate retry refusal terminalizes; a PENDING snapshot overtaken by a rejected attempt during preflight is refused. Both invoke HTTP exactly 0 times in the refusing caller |

State-only tests construct no transport and invoke HTTP zero times. Direct
state-result fixtures are synthetic evidence, never claims of real provider
acceptance. Existing protocol/adapter suites additionally exercise cancellation,
epoch mismatch, replay, receipt verification, process ownership and malformed
provider outcomes. These tests do not establish Runtime exactly-once delivery.

## Verification

Existing interpreter: `C:\CandleCraftDev\.venv\Scripts\python.exe` (Python 3.11.9).
No dependency installed or changed. DEV safety flags on every executing suite:
`LOCAL_MANUAL_MODE=true`, `ORDER_EXECUTION_ENABLED=false`,
`TELEGRAM_DRY_RUN=true`, `TELEGRAM_SIGNALS_ENABLED=false`.

Tests execute outside the restricted process sandbox because the preceding B0
phase documented a Windows asyncio socketpair hang there. Synthetic data/network
boundaries remain unchanged. Full and focused tests use distinct temporary roots.

- Initial two-module compatibility run: **39 passed in 6.93s**.
- Intermediate focused run: **518 passed in 81.38s**; superseded after stronger
  process HTTP barriers and caller tests were added.
- Final focused run: **522 passed in 88.85s**, exit 0, no warnings/failures/skips.
- First full run: **1 failed, 4,052 passed, 1 warning in 753.11s**, exit 1.
  The unchanged `test_watch_session.py::test_completion_scope_and_nonrecursive_prior_observation_are_bounded`
  received WinError 5 / PermissionError from `os.replace` on its synthetic JSON
  receipt under `C:\CandleCraftDev\.pytest_tmp\b0a-full`. No watch-session source
  or tests differ from main. Root cause remains Unverified; no unrelated retry,
  implementation change or skip was introduced. A fresh system-temp diagnostic
  of the entire watch-session module then **passed 26 tests in 6.23s**.
- Final complete pytest rerun in system temp: **4,176 passed, 1 warning in
  738.04s (12m18s)**, exit 0, zero failures/errors/skips. The warning is the
  existing `StarletteDeprecationWarning` from `fastapi/testclient.py:1` about
  httpx integration. No dependency change was made. Source and test content
  match the passing focused run and implementation commit throughout both full
  runs; only the temporary-root location changed for the successful rerun.
- `python -m pip check`: **No broken requirements found**, exit 0.
- `python -m compileall -q app scripts src tests`: exit 0.
- `git diff --check`: exit 0; staged/final review repeated before commit.
- Implementation-head GitHub CI: **PASS** on
  `f3de472e62d54e93826f2fd87af4acf2c44d2aac`,
  [run 40701897679](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40701897679),
  including dependency verification, compilation and full pytest. This run is
  not substituted for final documentation-head CI. The final PR head/result/run
  is recorded in PR #160's release record after that exact-head run completes.

Final focused command (from the isolated worktree, using the interpreter above):

```powershell
$env:LOCAL_MANUAL_MODE='true'
$env:ORDER_EXECUTION_ENABLED='false'
$env:TELEGRAM_DRY_RUN='true'
$env:TELEGRAM_SIGNALS_ENABLED='false'
& 'C:\CandleCraftDev\.venv\Scripts\python.exe' -m pytest tests/test_telegram_no_send_terminalization.py tests/test_telegram_delivery_ownership_characterization.py tests/test_p0b3b1_telegram_outbox_protocol.py tests/test_telegram_sender_adapter.py tests/test_telegram_outbox_recovery.py tests/test_telegram_sender_phase42.py tests/test_telegram_signal_channel_routing.py tests/test_telegram_lifecycle_delivery_phase42.py tests/test_triggered_confirmed_telegram_delivery.py tests/test_f08_telegram_risk_warning.py --basetemp=C:/CandleCraftDev/.pytest_tmp/b0a-focused-final -o faulthandler_timeout=60 --maxfail=1
$b0aTempRoot = Join-Path $env:TEMP 'cci-operational'
New-Item -ItemType Directory -Path $b0aTempRoot -Force | Out-Null
$b0aTestTemp = Join-Path $b0aTempRoot 'b0a-full-final-20261028'
& 'C:\CandleCraftDev\.venv\Scripts\python.exe' -m pytest --basetemp=$b0aTestTemp -o faulthandler_timeout=60 --maxfail=1
```

An initial compatibility invocation returned **1 setup error** because its
basetemp parent directory did not exist. The parent was created; the same two
modules then passed all 39 cases. No application fix or test skip masked it.

## Runtime decision and remaining prerequisites

**Runtime rollout NO-GO; production B1/B2A Telegram integration NO-GO.**
The companion runbook supplies explicit PASS/FAIL gates, source/schema/epoch
compatibility requirements, the 150 GB / 4 GB resource plan, recovery proof,
single-listener stop/drain/start order, observations, aborts and coordinated
rollback. Runtime facts are N/A/Unverified because this phase is DEV-only.

No further source correction is demonstrated as mandatory for the integrated
P0A/P0B1/P0B2 release alone. Actual baseline compatibility, resource/restore proof,
quiescence and separate deployment approval are the smallest current rollout
prerequisites. Schema other than 27 requires a separate reviewed phase; do not
run a migration here. Remaining cross-stage ownership/destination revocation,
postclaim no-send semantics, outer repository extraction, bounded completion and
direct reconciliation risks still block future Telegram integration. Existing
raw delivery risks are not declared solved by this fix. Other result/fallback
mutators still rely on exclusive completion ownership and are not generally
state-fenced/idempotent; the runbook records their source-level limitation and
the stop condition if competing/stale completions are required or demonstrated.

Next recommendation: owner reviews this bounded PR and Runtime evidence packet
separately. Do not start integration, migration or deployment as an automatic
next phase. No Runtime access, real Telegram sends, persistent scanner/listener,
merge, live order execution, transfer or withdrawal occurred.
