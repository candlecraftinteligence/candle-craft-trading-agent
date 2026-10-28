# CCI P0B3B2A — Telegram Sender Adapter Contract

## Review status and repository boundary

Prepared in the DEV repository `C:\CandleCraftDev` on 2026-10-28 for independent
architectural review and eventual P0B3B2B integration. This phase does not activate
the adapter in production, approve a deployment, or replace existing orchestration.

- Branch: `feature/p0b3b2a-telegram-sender-adapter-contract`.
- Starting main: `ad07a97866e087df7754f0eac58f7b9cca9deb13`.
- Verification: `git fetch origin main` followed by matching `FETCH_HEAD` and
  `origin/main` to that exact SHA. Local `main` was stale and was not moved.
- Implementation commit: `6c3f58243075c9c4abe67f5874e44c9839513c2e`
  (adapter and all 150 new tests; subsequent handoff-only commit does not change code).
- The final PR-head SHA and its GitHub Actions run/job evidence are recorded in
  the PR description and delivery report. They are not embedded recursively in
  their own commit. Compare that SHA with `git rev-parse HEAD` when reviewing.
- Existing untracked `.worktrees/`, `CCI_P0B3B1_RELEASE_HANDOFF.md`, and
  `docs/operations/CCI_P0B3B1_RELEASE_HANDOFF.md` were present at entry and excluded
  from all commits. Other linked worktrees were inspected and left untouched.

## Scope and architecture

Only three new files belong to this phase:

1. `app/alerts/telegram_sender_adapter.py`: pure evidence translator and an
   explicitly injectable, asynchronous single-part transport callable.
2. `tests/test_telegram_sender_adapter.py`: independent contract assertions,
   fake-provider integration, and durable-protocol regression scenarios.
3. This handoff.

No existing application, test, workflow, dependency, schema, or configuration file
is changed. In particular, `telegram.py`, `telegram_sender.py`,
`telegram_lifecycle.py`, `telegram_routing.py`, `telegram_outbox.py`, and
`telegram_outbox_protocol.py` remain byte-for-byte unchanged from the baseline.
No production application or script imports the new module.

The intended, opt-in dependency chain is:

```text
Existing Stage 1 worker: epoch + public economic ownership + canonical reservation
  -> committed immutable reservation receipt
Existing Stage 2: detached TelegramTransportRequest
  -> TelegramSenderTransport(sender, message_type=explicit_type)
     -> request integrity / destination binding / routing / sender preflight
     -> exactly one TelegramSender.send_part invocation
     -> adapt_telegram_send_result(result, request=request)
     -> TelegramTransportOutcome
Existing Stage 2 normalizer
Existing Stage 3 worker: fenced, verified durable persistence
```

The adapter contains no SQL, database handles, task scheduling, timers, retry loop,
message splitting, settings loading, credential discovery, or delivery activation.
It reuses the sender supplied by its caller. It does not call `send_text` or the
synchronous `send_message` convenience API.

## Success evidence contract

`SENT` requires all of the following, with no inferred defaults:

- A `TelegramSendResult` with exactly one dictionary in `telegram_results`.
- A structurally valid reserved request: positive integer event/reservation/part
  identifiers, nonempty event/attempt/message identity, supported public
  destination kind, part index within count, bounded nonempty text, and an exact
  SHA-256 match for that part's payload.
- Strict integer part number and count matching the request; booleans, numeric
  strings, and floats do not satisfy integer identity.
- Consistent top-level and provider `status="sent"` / `delivery_state="SENT"`.
- An actual provider-record integer `http_status=200`; a missing status is never
  replaced with 200.
- A strictly positive integer Telegram message ID. Zero, negative IDs, booleans,
  strings, missing IDs, and other types fail closed.
- A nonzero integer provider chat ID whose string representation exactly matches
  the reserved destination. The callable also binds that destination and kind to
  the supplied sender before invoking it.
- No sender/provider error, error category, rate-limit flag, or retry delay.
  The rate-limit field must be explicitly `False`.
- If present, a parseable timezone-aware provider timestamp. Missing timestamps
  remain `None`; the adapter never invents provider send time.

Successful outcomes are detached immutable dataclasses. Subsequent mutation of
the sender's dictionary cannot change the returned acknowledgment.

## Classification and retry policy

| Observed evidence | Adapter outcome | Meaning |
| --- | --- | --- |
| Verified success satisfying every check above | `SENT` | Acknowledgment for this reserved part |
| Callable rejects request integrity, destination binding, or route before send | `FAILED_FINAL` | Confirmed local non-transmission |
| Callable directly observes a recognized disabled/dry-run/missing-credentials preflight block with no provider rows | `FAILED_FINAL`, `sender_preflight:<reason>` | Confirmed local non-transmission; reason preserved |
| Standalone or late `skipped` sender result without observed preflight provenance | `UNCERTAIN` | A summary label cannot prove no transmission |
| Consistent sender evidence of connect/pool failure before transmission, without HTTP/acknowledgment evidence | `RETRYABLE` | Eligible for durable protocol retry admission only |
| Consistent direct HTTP 429 rejection with a valid optional finite nonnegative retry delay | `RETRYABLE`, rate-limit metadata preserved | Existing durable scheduling and attempt limit remain authoritative |
| Consistent direct HTTP 4xx permanent rejection, excluding 408 and 429 | `FAILED_FINAL` | Terminal rejection; no automatic retry |
| HTTP 5xx, including legacy sender `RETRYABLE` labels | `UNCERTAIN` | Upstream acceptance cannot be ruled out |
| HTTP 408, read/write/generic timeout or HTTP failure, unexpected sender exception | `UNCERTAIN` | Transmission may have occurred |
| Malformed JSON/body, absent acknowledgment, invalid message/chat ID, wrong part, contradictory metadata or multiple results | `UNCERTAIN` | Never promote incomplete evidence to success or retry |
| HTTP-200 Telegram API rejection represented by `telegram_api_rejection:*` | `UNCERTAIN` | Existing sender may rewrite `http_status` from body error code; wire status is not recoverable |

Before authorizing retry/final rejection, failure summaries must agree with their
provider record, and failure records must not carry message/chat/timestamp
acknowledgments. Unknown cases remain `UNCERTAIN`.

The four-state P0B3B1 transport normalizer does not support `POLICY_DISABLED` or
`SKIPPED_DRY_RUN`. This phase deliberately maps an observed preflight stop to
terminal `FAILED_FINAL` with a distinct diagnostic reason. Widening protocol or
lifecycle state semantics belongs to a separate reviewed change. A persisted
preflight stop is not automatically resurrected when settings later change.

Errors emitted by the adapter use fixed categories. Provider descriptions,
payload text, exception messages, and credential-bearing URLs are not echoed.
Cancellation propagates to the existing protocol, which fences persistence of an
uncertain outcome before re-raising cancellation.

## Test evidence

Existing DEV environment: `.venv\Scripts\python.exe`, Python 3.11.9.

| Verification | Result |
| --- | --- |
| Adapter-only collection | 150 cases across 23 test functions; no wrapper re-execution of existing scenarios |
| Focused adapter + P0B3B1 + sender + routing run | 219 passed in 30.81s; zero failures/skips |
| Full repository pytest | 4,115 passed, 1 warning in 786.98s (13:06); zero failures/skips |
| Python compilation (`app scripts src tests`) | Passed, exit code 0 |
| Dependency verification (`python -m pip check`) | Passed: `No broken requirements found.` |
| `git diff --cached --check` | Passed |

Focused command:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_telegram_sender_adapter.py tests/test_p0b3b1_telegram_outbox_protocol.py tests/test_telegram_sender_phase42.py tests/test_telegram_signal_channel_routing.py --basetemp=.pytest_tmp/p0b3b2a-focused-final
```

Final full-suite command (approved outside the sandbox):

```powershell
$env:LOCAL_MANUAL_MODE='true'
$env:ORDER_EXECUTION_ENABLED='false'
$env:TELEGRAM_DRY_RUN='true'
$env:TELEGRAM_SIGNALS_ENABLED='false'
.\.venv\Scripts\python.exe -m pytest -o faulthandler_timeout=60 --basetemp=.pytest_tmp/p0b3b2a-full-verified
```

The one warning is the installed FastAPI/Starlette TestClient deprecation notice
about its httpx integration. It is unrelated to the adapter; no dependencies were
changed to suppress it. Full repository coverage includes the unchanged P0A,
P0B1, P0B2, P0B3B1, epoch, canonical identity, delivery and risk-warning suites.

Its counts are 150 new adapter cases, 52 existing P0B3B1 tests, 11 sender tests,
and 6 routing tests. Existing P0B3B1 accounting includes its documented shared
wrappers; these are not presented as new independent adapter scenarios.

Every new HTTP integration uses `httpx.MockTransport` against
`https://telegram.invalid` with synthetic data and asserts actual HTTP handler
counts. An autouse fixture fails any attempt to invoke a real synchronous or
asynchronous httpx network transport in the new suite. No real Telegram request
is needed or permitted by these tests.

New evidence includes:

- Strict success/failure type and metadata matrices, missing/contradictory
  acknowledgments, malformed aggregates, multipart identity, invalid JSON and
  provider dates, all major httpx exception families, safe error output.
- Zero sender calls for local policy, dry-run, credential, request-integrity,
  cross-destination and signal-channel routing blocks.
- Exact one-part invocation, exact payload, part number/count, explicit message
  type, and unmodified reservation guard identity; no `send_text` invocation.
- Real sender + fake HTTP + all three protocol stages: terminal `SENT` and
  `UNCERTAIN` states cannot trigger another HTTP call.
- Partial multipart success leaves the aggregate in `IN_FLIGHT`; sent parts
  cannot replay, uncertainty blocks continuation, and aggregate `SENT` requires
  every part's acknowledgment.
- Two independent protocol workers contending for the same reservation produce
  one actual HTTP invocation.
- HTTP 429 causes one invocation per admitted attempt, no early retry, and no
  fourth request after the existing three-attempt limit.
- Missing canonical ownership, wrong reservation, foreign event epoch, and fatal
  runtime identity mismatch cause zero adapter/sender invocations. Fatal identity
  exceptions remain fatal.
- Cancellation during HTTP persists `UNCERTAIN` and blocks replay with exactly
  one invocation; dry-run terminal reasons survive durable persistence.

The first focused run had one incorrect new multipart expectation: it expected
the aggregate durable receipt to be `SENT` before all parts completed. Inspection
of existing Stage 3 confirmed `IN_FLIGHT` is correct; the new test was corrected,
and the final focused run passed. No existing code or assertion was weakened.

The initial sandboxed full run and diagnostic runs were interrupted after stack
traces showed Windows asyncio blocked inside local `socket.socketpair().accept()`.
A minimal `asyncio.run(asyncio.sleep(0))` reproduced the same sandbox block without
project code. The required full suite was rerun outside the sandbox with explicit
CI safety flags, using the existing environment and temporary databases. No
application workaround, test exclusion, or dependency change was introduced.

## Verified safety boundaries and remaining limitations

Canonical reservation ownership, runtime epoch validation, public economic
identity, worker-owned SQLite, fenced cancellation, persistence verification,
durable duplicate suppression, and retry scheduling remain owned by P0B3B1 and
its existing dependencies. The adapter does not create, rebind, or reinterpret
those authorities. The protocol must admit every invocation; calling this
transport directly is not a standalone authorization or deduplication mechanism.

The translator trusts records produced by the injected existing sender. It is not
cryptographic authentication of arbitrary dictionaries, and it cannot recover
discarded original JSON, headers, request text, or wire HTTP status. In particular,
body-code rewrites remain uncertain. Optional malformed metadata discarded by the
legacy sender cannot be reconstructed. Full provider-envelope capture would
require a separately scoped sender-contract change.

Numeric destination identity is required for a verified acknowledgment. Username
aliases cannot be resolved from the flattened response; an alias response that
cannot match the reserved numeric identity remains `UNCERTAIN`. The adapter does
not resolve aliases through extra network calls.

The part hash is checked, but a detached part cannot re-derive the complete
multipart message hash or public economic identity. Existing Stage 1/3 durable
association checks remain authoritative. Uncertain delivery requires operator
reconciliation under existing rules and must never be force-retried by this
adapter. Custom sender/client implementations must preserve the existing
single-attempt transport contract.

No Runtime PC, production database, `.env`, credential, production delivery switch,
scanner/listener operation, schema, lifecycle, strategy, Fibonacci setting, RR
requirement, signal-quality gate, order execution, withdrawal, or transfer is
modified or activated. No merge or deployment is part of this phase.

## P0B3B2B readiness and recommendations

Review the strict adapter contract and terminal preflight mapping before wiring
any production caller. B2B must continue to obtain requests exclusively from
committed Stage 1 receipts, preserve event/part/attempt identity across Stage 2,
await fenced Stage 3, and keep the current cancellation/epoch failure semantics.

Decide explicitly whether orchestration performs disabled/dry-run preflight
before claiming, preserving existing policy-specific lifecycle semantics, or
accepts this adapter's documented terminal four-state mapping after claiming.
Do not silently widen this phase to solve that integration decision.

Use reserved numeric chat IDs, route with an explicit correct message type, and
retain the existing public-watchlist guard. Preserve durable retry admission;
never add an outer sender retry loop, whole-message resend, or retry of an
uncertain part. Extend the same fake-HTTP count assertions to whichever production
orchestration path is integrated in B2B. Any rollout needs separate authorization
and its own regression/operational verification.
