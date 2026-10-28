# P0B3B2B0 — Telegram delivery preflight and SQLite ownership contract

Audit date: 2026-10-28 (Europe/Bratislava). Scope: DEV source, synthetic tests,
architecture and implementation prerequisites only.

## 1. Verdict and verified baseline

**NO-GO for a direct P0B3B2B1 production-caller integration. GO for a separately
approved, isolated prerequisite phase.** B1's worker and B2A's adapter are useful
foundations, but composing them does not resolve temporary refusal semantics,
outer repository ownership, or authorization between claim and HTTP. B0 does not
change production behavior to resolve these gaps.

Independently fetched `origin/main` before work:
`43a196f849c0646bb7f772ec85dc6e0541688aa0`. GitHub PR metadata confirms
[PR #158](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/158)
merged at that SHA, with adapter head
`0d4694ff39b861f9399626194ea42a3af12ae15a`.
[PR #152](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/152)
was open/unmerged when checked; Fibonacci V2 is outside B0. Independently queried
merged-main push [CI run 40667229251](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/40667229251):
completed/success, with `head_sha=43a196f849c0646bb7f772ec85dc6e0541688aa0`.
The previous 4,116-test count is historical handoff evidence, not B0 validation.

Branch: `feature/p0b3b2b0-telegram-delivery-ownership-contract`, created directly
from the fetched main. Existing untracked `.worktrees/` and the three B1/B2A
release handoff files were preserved. No other worktree was modified.

Evidence labels throughout: **Observed** means source inspection or the named
synthetic test; **Required** means a future acceptance requirement; **Unresolved**
means no current interface establishes it. Fault injection demonstrates a missing
boundary, not evidence of a normal production mutation. No Runtime behavior was
observed directly, and no Runtime database was opened.

### Blocking findings

1. **Observed:** both existing caller and adapter terminalize temporary local
   unavailability, at different boundaries and sometimes under different states.
   `POLICY_DISABLED` and `SKIPPED_DRY_RUN` are terminal too. Re-enabling policy does
   not restore an existing canonical event. [S1], [S3], [S5], [S6]
2. **Observed:** the sender repeats preflight within `send_part()`. A refusal there
   has no per-part provider record, so the adapter conservatively returns
   `UNCERTAIN`, despite the actual sender having made no HTTP call. Its earlier
   preflight cannot prove what happened inside an arbitrary subsequent invocation.
   [S4], [S5]
3. **Observed:** `deliver_for_run()` owns two SQLite repository contexts across
   awaits. The helper commits its alert connection before HTTP but retains it;
   synchronous SQL and connection setup still run on the event loop. An outer
   writer can block a new worker connection. [S1], [S2], [S7], [S8]
4. **Observed:** `mark_terminal_without_send()` checks canonical association and
   epoch but has no event-state or delivery-attempt predicate. A stale refusal
   can overwrite an `IN_FLIGHT` or `SENT` event while leaving its part in that
   original state. It is unsuitable as a concurrent preflight-abort primitive.
   This is an existing inconsistency, not repaired in B0. [S3]
5. **Observed:** the protocol validates authority in stages 1 and 3, not inside
   stage 2. Sender destination is checked before adapter preflight and read again
   by transport. Receipt/request objects omit a full runtime identity. Dynamic
   revocation or arbitrary configuration mutation therefore has no demonstrated
   effect-time contract. Normal epoch initialization and canonical binding are
   immutable; fault-injection tests deliberately bypass those APIs. [S5], [S6],
   [S9], [S10]
6. **Observed:** current helper cancellation during HTTP leaves committed
   `IN_FLIGHT` until lease recovery. The isolated protocol has stronger cooperative
   persistence fences, but no operation or join deadline. Bounded queue capacity
   does not establish a bounded wall-clock lifetime. [S1], [S6]

## 2. Source index

References are anchored to unchanged production source at the audited main SHA.
Line intervals below are navigation aids, not claims that every line executes.

| Ref | File and lines | Authority or behavior |
|---|---|---|
| S1 | [telegram_lifecycle.py:3428–3664](../../app/alerts/telegram_lifecycle.py#L3428) | Existing committed-intent helper, preflight, multipart, raw result conversion |
| S2 | [telegram_lifecycle.py:3869–4122](../../app/alerts/telegram_lifecycle.py#L3869) | Outer delivery run, repository lifetime, orchestration order |
| S3 | [telegram_outbox.py:167–678](../../app/alerts/telegram_outbox.py#L167) | Atomic claim, part/result transactions, no-send terminalization, stale recovery |
| S4 | [telegram_sender.py:64–283](../../app/alerts/telegram_sender.py#L64) | Captured sender configuration, both preflights, watchlist guard |
| S5 | [telegram_sender_adapter.py:47–232](../../app/alerts/telegram_sender_adapter.py#L47) | Strict evidence adapter and directly observed refusal mapping |
| S6 | [telegram_outbox_protocol.py:78–901](../../app/alerts/telegram_outbox_protocol.py#L78) | Immutable values, worker, stages 1–3, cancellation fences |
| S7 | [telegram_lifecycle.py:1142–1180](../../app/alerts/telegram_lifecycle.py#L1142) | Alert repository create, commit, close |
| S8 | [repositories.py:34–111](../../app/lifecycle/repositories.py#L34) | Lifecycle repository create, rollback/commit, close and completion observation |
| S9 | [ownership.py:592–748](../../app/runtime_epoch/ownership.py#L592) | Event/reservation/part/claim associations and write-once canonical pointer |
| S10 | [authority.py:29–158](../../app/runtime_epoch/authority.py#L29) | Immutable active epoch and complete expected-identity validation |
| S11 | [startup.py:91–220](../../app/runtime_epoch/startup.py#L91) | Read-only validation connection, fresh operational connection, fail-closed open |
| S12 | [database.py:10–97](../../app/storage/database.py#L10) | WAL, FULL synchronous, 5,000 ms busy timeout, connection setup |
| S13 | [telegram_lifecycle.py:2525–3291](../../app/alerts/telegram_lifecycle.py#L2525) | Public event insert, watchlist/lifecycle reservation and dedupe |
| S14 | [telegram_lifecycle.py:1960–2115](../../app/alerts/telegram_lifecycle.py#L1960) | Attempt insert and canonical pointer binding in caller transaction |
| S15 | [telegram_lifecycle.py:4124–4360](../../app/alerts/telegram_lifecycle.py#L4124) | Transition ordering/coalescing and recovery selection |
| S16 | [telegram_lifecycle.py:4362–4944](../../app/alerts/telegram_lifecycle.py#L4362) | Gates, public reservation, helper dispatch, residual direct-send branch |
| S17 | [telegram_lifecycle.py:5101–5735](../../app/alerts/telegram_lifecycle.py#L5101) | Canonical outcome projection and established watchlist reconciliation paths |
| S18 | [telegram_lifecycle.py:13745–13952](../../app/alerts/telegram_lifecycle.py#L13745) | Successful outcome lookup and target-intent supersession |
| S19 | [telegram.py:46–204](../../app/alerts/telegram.py#L46) | HTTP boundary, sender error classification, actual request fields |
| S20 | [telegram_routing.py:45–127](../../app/alerts/telegram_routing.py#L45) | Signal-channel allowlist and normalization |
| S21 | [run_scan.py:2909–2990](../../scripts/run_scan.py#L2909) | Selected scanner public route and service entry point |
| S22 | [ownership.py:219–354](../../app/runtime_epoch/ownership.py#L219) and [849–895](../../app/runtime_epoch/ownership.py#L849) | Epoch/lifecycle/root/economic identity checks |
| S23 | [telegram_lifecycle.py:9719–9862](../../app/alerts/telegram_lifecycle.py#L9719) | Event-key construction and economic/canonical plan identity |
| S24 | [integration.py:154–186](../../app/cmc/integration.py#L154) and [selection_audit.py:43–108](../../app/cmc/selection_audit.py#L43) | Additional awaits within delivery contexts; application audit after exit |

## 3. Current-state call graph and ordering

```text
scripts.run_scan._deliver_telegram_manual_signals_if_enabled
  selected manual route OR watch + telegram_live_alerts (selection is not sender policy)
  -> TelegramLifecycleDeliveryService.deliver_for_run
       open alert repository A -> open lifecycle repository L
       optional CMC preparation await
       reconcile_publicly_tracked_outcomes
         canonical lifecycle progress -> coalesce/suppress target intents
         -> _send_canonical_public_outcome -> reserve lifecycle event -> helper H
       recover_public_lifecycle_alerts -> eligible persisted milestone intents -> H
       select/rank watchlists, enforce quality/RR/rate/dedupe gates
       deliver_transitions_for_symbol (ordered/coalesced transitions)
         -> deliver_for_symbol
              internal TRIGGERED / duplicate LIMIT_HIT / policy rejection -> audit
              WATCHLIST -> reserve_public_watchlist_event -> H
              confirmed + supported public milestone -> _reserve_public_lifecycle_event -> H
              other established branch -> sender.send_text -> attempt persistence
       reconcile_sent_watchlists
         established guarded outcome/reconciliation branches -> sender.send_text
       maybe_send_research_watch_alerts -> separate policy -> sender.send_text
       close L -> commit/close A
       optional CMC application audit await

H = _deliver_committed_public_alert_intent
  persist exact payload/destination/parts if absent; commit event + attempt + parts
  dry-run -> terminal SKIPPED_DRY_RUN, return
  sender.preflight -> terminal POLICY_DISABLED or FAILED_FINAL, return
  outbox.claim: BEGIN IMMEDIATE -> validate canonical/epoch -> event IN_FLIGHT -> commit
  each claimed unsent part:
    mark_part_in_flight: BEGIN IMMEDIATE -> commit
    A.commit_before_network_activity (A remains open; L remains open)
    await sender.send_part (or legacy send_text fallback)
      sender.preflight again -> await telegram.send_telegram_message_part -> client.post
    outbox.record_part_result: BEGIN IMMEDIATE -> result + aggregate + attempt -> commit
    IN_FLIGHT -> next part; SENT -> return sent; other state -> stop
```

Observed sources: [S1], [S2], [S15]–[S21], [S24]. Adapter and protocol are not
imported by this production orchestration. A future narrow migration must not
quietly claim to cover all Telegram callers.

Initial watchlists use `initial_watchlist`; confirmed/milestone event keys use
the existing normalized alert family. `PUBLIC_LIFECYCLE_OUTBOX_ALERT_TYPES`
includes triggered, confirmed, limit, TP1/2/3, SL and breakeven
([source:122](../../app/alerts/telegram_lifecycle.py#L122)). Triggered remains
internal and recovered triggered intents are terminalized without transport.
Confirmed recovery rechecks persisted truth. Followups require the owned root;
outcomes require trustworthy canonical progress and causal/coalescing rules.

**Recovery asymmetry:** the milestone recovery query includes only that explicit
family and states `PENDING`, `RETRYABLE`, `IN_FLIGHT`; it excludes
`initial_watchlist`, all terminal states, and lifecycle history without an
explicit intent. Watchlist reservation refuses an existing reserved event rather
than granting it again. The low-level outbox can claim a valid pending watchlist,
but that does not establish an automatic production watchlist recovery caller.
Do not add one implicitly. [S13], [S15]

Stable identity derives from persisted economic geometry and structural anchor,
not new transport attempts. Existing ownership checks compare present `setup_id`
and `plan_version_id` against lifecycle values; B1 receipts do not carry those
identities. Required: detach a verified envelope without reminting any identity,
and revalidate it against the owned rows in every write phase. Missing identity
is `N/A`, contradictory/unreliable evidence is `Unverified`; neither is permission
to reconstruct authority. [S9], [S22], [S23]

## 4. SQLite and async ownership map

Threads: **E** = event-loop thread, **W** = dedicated outbox worker.
An open connection is not necessarily an open transaction. Normal existing
public helper HTTP begins with A committed; the B0 test observes both A and L
open with `in_transaction=False`. It does not establish that every branch is
transaction-free, nor that idle connections hold write locks. [S1], [S2]

| Stage / source | Current thread / connection owner | Transaction and commit boundary | Cancellation / external effect | Required future ownership |
|---|---|---|---|---|
| Route/settings, [S21], [S4] | E, no SQLite yet | None | Captures configuration; no HTTP | Immutable approved dispatch configuration, no secret in receipts |
| Repository open, [S7], [S8], [S11], [S12] | E owns A and L; inspect-only connection opens/closes during each operational open | Schema/identity reads; writable open applies PRAGMAs; no schema migration | Synchronous busy wait/open blocks E; no cooperative cancellation inside SQL | W creates/validates/closes every delivery-owned connection within a call |
| Snapshot/eligibility/reconciliation, [S2], [S15]–[S18] | E, A/L remain live | Reads plus audit writes can open implicit DML transactions; A commits later | CMC, symbol delivery and reconciliation awaits retain contexts | W returns detached values; finish audits/transactions and close both repositories before external awaits |
| Event + reservation, [S13], [S14] | E, A | Insert owned event, exact attempt and bind canonical pointer; helper's intent commit is first explicit boundary on successful path | No await inside reservation helper; failures handled by caller/context | One W-owned atomic intent unit, never split event/pointer/parts across uncommitted owners |
| Payload/parts, [S1]:3451–3478, [outbox:75](../../app/alerts/telegram_outbox.py#L75) | E, A | Existing parts reused; else persist split payload and destination, update pending attempt; `commit()`, error rollback | Durable intent may survive cancellation before claim | Return only committed identity/payload reference; close connection before claim await |
| No-send terminalization, [S1]:3480–3528, [S3]:437 | E, A | `_transaction()` commits any prior transaction, `BEGIN IMMEDIATE`, then commit | No HTTP, but unguarded event-state overwrite possible | Future abort operation must compare canonical pointer + state + exact attempt, never clobber another owner |
| Claim, [S3]:192–297 | E, A | Separate `BEGIN IMMEDIATE`; validate chain, recover expired claim to uncertainty, conditional PENDING/RETRYABLE -> IN_FLIGHT; increment attempts; commit | One process wins; no HTTP | W owns atomic claim; early policy check never replaces it |
| Part start, [S3]:299–331 | E, A | Another `BEGIN IMMEDIATE`, verify claim/canonical/part association; part IN_FLIGHT; commit | Gap between event claim and part mark is crash-sensitive | W closes after verified part-start commit; abort/recovery ownership explicit |
| HTTP, [S1]:3574–3604, [S4], [S19] | E, A/L still owned by outer coroutine | A explicitly committed, no DB operation required by sender | Real public effect; helper catches Exception, not CancelledError | E receives only frozen request. No delivery SQLite connection or transaction crosses HTTP |
| Result, [S3]:333–435 | E, A | `BEGIN IMMEDIATE`; update exact IN_FLIGHT part/attempt; all-parts finalization and attempt projection in same commit | Synchronous SQL; crash before durable result leaves uncertain acceptance | W opens fresh, validates exact authority, persists, reads back, closes |
| Reconciliation after effect, [S17], [S18] | E, A/L | Public ordering/dedupe reads and audits; direct-send branches remain distinct | Later awaits may retain audit DML; must inspect each branch | Separate W snapshot/projection units with causality and canonical identity preserved |
| Repository exit, [S7], [S8] | E | A commits on clean exit then closes; exception closes without explicit rollback. L explicitly rolls back or commits, closes in finally, records completion | A commit failure can prevent its close; L has stronger finally cleanup | Every new W owner closes in finally; bounded completion receipt before cancellation propagates |
| B1 stage 1, [S6]:330–465 | W when invoked through worker; fresh connection | Claim transaction and part-start transaction are separate commits; explicit commit then finally close | Admission/preparation fencing delays cancellation until ownership known | Reuse foundation, address stranded claim on preparation failure; do not claim one combined transaction |
| B1 stage 2, [S6]:560–594 | E, request only | No protocol-owned connection | Await injected transport; no authority reread | Require **all** outer owners closed too; immutable effect authorization contract still prerequisite |
| B1 stage 3, [S6]:596–765 | W, fresh connection | Validates event/reservation/part and attempt; result transaction; fallback; readback; finally close | Fenced admitted persistence; unresolved failure cannot yield sent | Verify aggregate and exact receipt, retain bounded completion ownership even under repeated cancellation |
| Worker teardown, [S6]:179–324 | W + E admission owner | Queue one operation + one waiting caller; close stops admission and joins thread | Cooperative fencing, non-daemon thread; no join timeout | Scope worker to service/task lifetime; finite SQL/admission/completion budget and supervised drain required |

The 5-second SQLite busy timeout bounds lock waits, not an entire multi-statement
operation. WAL allows readers alongside a writer; it still permits one writer.
`_transaction()`'s preliminary commit can commit unrelated writes on the same
connection. It cannot release a write lock owned by **another** connection.
Never call a worker while A/L still own uncommitted work and assume isolation
solves contention. [S3]:667–677, [S12]

### Before / required after

```text
BEFORE: E owns A + L ----------------------------------------------- close
          snapshots -> writes -> commit A -> await HTTP -> SQL -> audits

AFTER (conditional on prerequisites):
 E: immutable inputs -> await intent job -> admission -> await claim job
 W:                  open -> atomic intent -> commit -> verify -> close
 W:                                                   open -> claim/part -> close
 E:                  no live delivery repositories -> await one-part HTTP
 W:                  open -> exact result + projection -> commit/readback -> close
 E:                  verified aggregate receipt -> next part / stop -> drain worker
```

No connection moves threads. A repository object, `sqlite3.Row`, cursor, lazy
iterator or mutable model graph must not cross these boundaries. Detached frozen
scalars/tuples may carry IDs, exact payload/hash, numeric destination, complete
expected epoch identity and policy generation. B1's frozen dataclasses provide
part of this envelope, not all of it. [S6]:78–155

## 5. Preflight decision matrix

Boundaries: **I** before canonical intent, **C** committed intent before claim,
**A** after committed claim before invocation, **H** inside sender immediately
before HTTP, **R** after invocation/result. “Later eligibility” below means the
**same** canonical event; a newly invented event key is never a recovery tool.

| Refusal / nature | Where / HTTP evidence | Observed durable behavior | Required boundary treatment / later eligibility / retry |
|---|---|---|---|
| LOCAL_MANUAL_MODE=false, temporary operational permission | Sender preflight C/A/H; no HTTP on directly observed return | Sender POLICY_DISABLED; current C terminal POLICY_DISABLED; B2A A terminal FAILED_FINAL | I: no public-effect claim; C: leave already claimable intent unchanged if merely deferring, do not call terminalizer. A/H: unresolved safe-abort contract; current terminal semantics remain. No automatic retry or reopening terminal event. [S1], [S4], [S5] |
| TELEGRAM_SIGNALS_ENABLED=false, temporary | Same; sender object captures bool at construction | Same POLICY_DISABLED -> caller POLICY_DISABLED / adapter FAILED_FINAL | Same I/C/A/H rules. Changing .env does not refresh an existing sender. Re-evaluation before a later **existing** admitted claim is not a new retry policy. [S4] |
| TELEGRAM_DRY_RUN=true, operational evaluation mode | C helper checks dry_run even before preflight; H checks policy before dry-run | Current helper terminal SKIPPED_DRY_RUN; B2A directly observed dry-run terminal FAILED_FINAL; terminal states not recoverable | Keep explicit dry-run evaluation terminal SKIPPED_DRY_RUN semantics. A delivery-pausing use case cannot silently reinterpret that terminal state. I bypass would change durable dry-run audit behavior; requires approval. No later eligibility for already terminal dry-run event. |
| Missing bot token or chat ID, repairable configuration | Direct sender return proves no HTTP; chat mismatch can be caught earlier by adapter | Sender FAILED_FINAL; caller/adapter FAILED_FINAL, or adapter destination mismatch before missing-credentials check | Operationally repairable does not imply protocol RETRYABLE. I/C admission may stop without claiming; A/H must not invent recovery. Same-event eligibility after existing FAILED_FINAL: no. Credentials never enter logs/DB/receipts. |
| Destination/kind mismatch or missing destination, unsafe persisted target | Adapter before sender A; sender only checks missing credentials and route category | Adapter FAILED_FINAL; existing helper does not compare persisted destination to current sender at dispatch | I validate approved destination; C compare stored immutable target; A/H mismatch blocks effect. Never retarget committed payload in place. Valid old intent may be retained pending operator resolution only before claim; terminal state never auto-reopened. [S1], [S5] |
| Disallowed signal-channel message type, permanent for that route/type pair | C/A/H directly observed rejection, zero HTTP | Sender POLICY_DISABLED; caller POLICY_DISABLED; adapter FAILED_FINAL via route mismatch or local reason | Fail closed; not transient transport failure. Do not reroute to public_chat to evade allowlist. I validate; C/A/H no effect; no automatic retry. Other destination categories have permissive routing in existing helper; adapter explicitly limits public kinds. [S20], [S5] |
| Missing/invalid public-watchlist guard, invalid authorization evidence | C/A/H; existing guard checks positive IDs/nonempty key/attempt only | Sender POLICY_DISABLED; caller POLICY_DISABLED; adapter constructs a syntactically valid guard from request | Guard is metadata, not a DB claim attestation. I cannot have a final claim guard yet; C use canonical reservation only for validation; A/H require Stage-1-authorized request. Missing authority never becomes RETRYABLE. [S4]:273, [S9] |
| Invalid payload/hash/part identity/request, permanent invalid request | Adapter A before send, zero HTTP | FAILED_FINAL; invalid post-invocation evidence is UNCERTAIN | I validate deterministic rendering/splitting, C verify persisted content; do not repair by silently changing economic identity/payload. A reject; R do not assert non-transmission from bad evidence. [S5] |
| Unknown/malformed preflight result, evidence unreliable | A or H/R; absence of transport not established by result shape | Adapter UNCERTAIN | No resend. Mark evidence Unverified; inspect durable authority. Never treat status=skipped alone as no-transmission proof. [S5] |
| Policy changes between adapter check and sender check | H returns skipped with empty provider tuple; actual sender used no HTTP, adapter lacks observed provenance | B2A `adapter_expected_one_part_result` -> UNCERTAIN | Typed invocation-local no-transmission evidence is prerequisite if safe deferral is required. Earlier preflight never substitutes for it. No automatic retry under current protocol. |
| Epoch/claim/reservation invalid | I/C/stage 1 -> no HTTP; after stage 1 -> stage 2 does not reread SQLite | Preparation blocks/errors; after-claim drift can reach HTTP, stage 3 refuses/unresolved | Normal epoch and canonical binding remain immutable. Any revocation must quiesce/drain admitted effects across processes before changing authority. A fresh DB check alone still has a gap before HTTP. [S6], [S9], [S10] |
| Confirmed truth/quality/root/causal rejection | I and recovered intent validation C | Existing block/audit or FAILED_FINAL; triggered stays internal | Preserve exact quality/RR/lifecycle/coalescing rules. Operational repair must not promote weak setups or replay old transitions. [S15]–[S18], [S22] |

Preflight ordering matters: the helper's standalone `dry_run` property branch
precedes its sender preflight; sender preflight checks local mode, enabled, dry-run,
credentials, watchlist guard, routing, in that order. B2A checks request and target
before sender preflight. Therefore the same combination of disabled settings can
produce different first reasons. Preserve distinct evidence instead of assuming
all refusals are identical. [S1], [S4], [S5]

### Can temporary disablement preserve eligibility today?

**Only under limited conditions.** A valid existing PENDING/RETRYABLE event can
remain unchanged if transport/claim is never entered. A later attempt must still
pass existing canonical authority, retry due/limit, current truth and routing
rules. This is not permission to add a scheduler, retry state or recovery family.

Once existing code records POLICY_DISABLED, SKIPPED_DRY_RUN or FAILED_FINAL,
the event is terminal and recovery excludes it. A post-claim refusal cannot be
returned to PENDING with the current four-state transport protocol. A hypothetical
“skip before creating intent” also does **not** prove eventual delivery: confirmed
transitions may be consumed and recovery only loads explicit existing intents;
watchlist pending recovery has the separate caller gap described above.

**Unresolved prerequisite:** decide whether disabled delivery means durable
terminal evaluation (current behavior) or retention for later revalidated
delivery, for each event family. If retention is required, specify the admission
and safe-abort changes in a future approved phase, including lifecycle freshness
and cancellation proof. B0 neither changes that policy nor promises retention.
Do not overload RETRYABLE, manufacture SENT, or reopen terminal identities.

## 6. TOCTOU and authorization contract

| Scenario | Observed limit | Required future contract |
|---|---|---|
| Enabled initially, disabled before HTTP / dry-run flip | Two sender checks; existing object uses captured fields, not a live policy store | Immutable dispatch snapshot; explicit policy generation and local admission/drain owner. Policy disable completion must mean no new dispatch may begin; already dispatched effect follows uncertainty rules |
| Destination changes after intent/check | Existing helper sends using current sender; B2A checks once before sender preflight; response mismatch detection occurs after public effect | Bind exact numeric chat/kind to immutable invocation. No configuration mutation mid-invocation or reuse of a different sender. Before each part compare persisted target; no alias-to-ID inference |
| Sender replacement or private-field mutation | No supported hot-reload API observed; fault injection can redirect HTTP | Treat object/config generation as immutable for admitted delivery. Hot replacement waits for current dispatch to settle; no implementation based on private fields |
| Canonical pointer superseded | Normal bind only fills NULL; reservation association checks exact pointer | No normal supersession during active dispatch. A future supersession operation must stop admission and drain owners; postclaim readback cannot undo an effect |
| Epoch changes | Initializer refuses different active identity; stage 3 checks expected full identity; stage 2 has no DB validation | Epoch replacement/invalidation is offline/quiescent, across **all** participating processes. Dynamic effect-time revocation is unsupported pending a proved cross-process admission/drain protocol |
| Cancellation after claim before HTTP | B1 preparation completes then pretransport cancellation check raises; may leave IN_FLIGHT with zero HTTP | Preserve conservative stale recovery now; any return-to-PENDING requires an atomic fenced abort and proven no invocation, approved separately |
| Cancellation during HTTP | Current helper leaves IN_FLIGHT; B1 catches cancellation and fences uncertainty persistence | No retry, persist uncertainty if authorized; propagate cancellation only after owned completion or explicit unresolved persistence outcome |
| Two independent processes | BEGIN IMMEDIATE and conditional claim admit one; stale IN_FLIGHT becomes UNCERTAIN rather than reclaimed | Per-process worker is not authority; shared durable claim is. Maintain one exact canonical pointer and no automatic uncertainty replay |
| Lease expires while HTTP is outstanding | Another process may mark stale uncertainty; expiry is not proven non-transmission | No lease-based resend. Define dispatch timeout/drain relative to lease; any late result must respect exact attempt and terminal evidence |
| Restart or multipart interruption | Only persisted receipt survives; uncertainty freezes event, prior SENT parts retained | Restart uses existing recovery admission only; continue only explicitly safe unsent parts under same event/attempt rules; never replay confirmed parts |

There is no atomic transaction spanning SQLite and Telegram HTTP. “Recheck in a
fresh worker immediately before send” narrows a timing gap but cannot eliminate
cross-process revocation between connection close and HTTP. Do not hold a SQLite
transaction through HTTP to fake that guarantee. The bounded recommendation is
immutable durable ownership plus a documented quiescence/drain rule for changes;
if hot cross-process revocation is a requirement, it is a separate protocol
prerequisite, not something B1/B2A currently supply.

## 7. Proposed integration sequence (requirements, not implemented)

The following is the target ownership contract **after** prerequisites are met.
It is not a claim that current interfaces can implement every step unchanged.
For the first bounded integration, one service task should own one private worker
and admit at most one logical part-delivery pipeline through persistence at a
time. Do not share that worker with competing preparations during HTTP. This
avoids creating a new concurrent dispatch scheduler or relying on queue slots as
post-effect persistence admission. Independent processes still contend through
the same SQLite canonical claim.

1. **Eligibility and policy evaluation:** load persisted lifecycle/root/progress
   and dedupe/rate inputs in a worker-owned snapshot; close repositories. Preserve
   ranking, quality, RR, causal milestone and public policy decisions. Evaluate
   immutable sender configuration outside SQLite. Audit operational refusal
   separately from strategy rejection. Do not consume a one-time transition
   under an unapproved deferred-delivery assumption.
2. **Intent persistence:** one worker owns event insertion, exact reservation
   attempt, canonical pointer binding, frozen payload, destination and parts.
   Existing reservation helpers must not be naively split between connections;
   their implicit transaction ownership and preliminary commits require a
   wrapper designed and tested in the future phase. Commit/readback/close on
   success, rollback/close on failure. Existing unique key/equivalent economic
   setup rules remain authority; reread a competing winner rather than creating
   a second reservation. The current reservation insert is not itself the send
   claim.
3. **Canonical reservation:** verify the durable pointer references the exact
   owned attempt, event family, lifecycle/root, economics, registered run lineage
   and full epoch identity. Preserve event_key/setup_id/plan_version_id/lifecycle_id.
   Only detached committed identifiers leave the worker.
4. **Preclaim admission and atomic claim:** a repeated policy observation can
   block dispatch but cannot grant it. Once admission is held, Stage 1 performs
   its cross-process claim, retry admission and part-start commits. No live outer
   repository remains. A preparation failure after claim needs conservative
   recovery; never assume exception implies no committed mutation.
5. **Worker preparation completion:** require a committed immutable receipt with
   exact event/reservation/attempt/part/payload/destination and full epoch binding.
   Close every connection before completion is released. No detached writes or
   live connection objects in callbacks. Current B1 receipt needs an approved
   envelope or interface extension for missing binding/config evidence.
6. **Async one-part transport:** check cancellation, immutable dispatch authority
   and current admission; invoke exactly one `send_part`, with sender's final
   preflight retained. No splitting/retry in adapter, no DB context on E. A
   typed local refusal may establish no HTTP only if observed inside the same
   invocation before the transport-start boundary. The disposition of that
   refusal after claim remains prerequisite work; do not map it to RETRYABLE.
7. **Worker result persistence:** fresh owned connection validates current exact
   association/attempt and expected identity, records conservative outcome, updates
   aggregate plus canonical attempt atomically, commits and closes. Repeated
   cancellation cannot abandon admitted persistence. A fatal authority mismatch
   must not mutate a new owner's rows. If no durable result can be established,
   report unresolved persistence; never label local acknowledgment as durable SENT.
8. **Durable receipt verification:** validate actual committed event, canonical
   pointer, attempt, part association/state, provider evidence, aggregate and
   completion status. A worker return alone is insufficient. For partial multipart
   success current B1 returns aggregate IN_FLIGHT and `all_parts_sent=False`;
   never project that as event SENT or pass it through as an unexplained public
   success. Current readback selects event `attempt_id` without re-comparing it
   after commit. Its RETRYABLE/FAILED_FINAL branch accepts either of those part
   states without requiring the exact expected event/part state equality. Those
   are source-observed verification limits, not a demonstrated false receipt in
   normal execution; strengthen acceptance tests before claiming complete proof.
9. **Lifecycle/public status projection:** lifecycle market facts are committed
   before delivery and never rolled back because Telegram is unavailable. Public
   SENT projection follows durable all-parts acknowledgment only. Gate child
   effects by existing owned root and causal/coalescing semantics. Any additional
   audit/status projection is a separate bounded worker unit with idempotent
   identifiers, not a shared connection held during transport.
10. **Reconciliation/restart:** inventory canonical existing intents and parts in
    short worker jobs. Existing due RETRYABLE may retry under present limits;
    SENT/UNCERTAIN/FAILED_FINAL/POLICY_DISABLED/SKIPPED_DRY_RUN never auto-retry.
    Expired IN_FLIGHT becomes UNCERTAIN. Preserve unsupported watchlist-recovery
    behavior pending explicit scope approval. Teardown stops admission, waits
    for bounded owned persistence, verifies drain and joins the worker.

Minimum future interfaces are responsibilities, not B0 API additions:
`prepare_committed_intent(frozen_input) -> committed_identity`, a frozen dispatch
configuration bound to destination/epoch generation, invocation-local
`NoTransmissionEvidence(reason, generation, exact_attempt)` distinct from a
provider outcome, and a conditional claimed-attempt completion/abort primitive.
If safe deferral after claim is selected, the precise PENDING transition and
attempt-accounting rules require protocol approval and adversarial tests first.
No new retry state, broad scheduler or schema design is proposed here.

## 8. Failure and recovery matrix

All outcomes require correct canonical ownership; an invalid/legacy owner is
read-only and cannot “repair” rows to fit the desired state.

| Failure | Current safe evidence / expected state | Recovery and required proof |
|---|---|---|
| Telegram disabled / local mode false | Direct preflight no HTTP; caller POLICY_DISABLED, adapter after claim FAILED_FINAL | Both terminal today. Preclaim retention is conditional future policy; never auto-reopen |
| Dry-run | Caller SKIPPED_DRY_RUN, adapter FAILED_FINAL; zero HTTP | Terminal, no SENT, no retry. Distinguish durable dry-run evaluation from proposed operational pause |
| Missing credentials | Direct local return, FAILED_FINAL | Repair configuration only; existing terminal intent remains terminal |
| Invalid route/guard/destination | POLICY_DISABLED or FAILED_FINAL by boundary; zero HTTP only with direct observation | No reroute or fabricated claim; destination repair cannot mutate committed target silently |
| Concurrent claims | One claim commit wins; other sees IN_FLIGHT/not-claimable | Loser does not call transport. Exact canonical pointer + BEGIN IMMEDIATE + conditional state is proof |
| SQLite busy/locked before claim | SQL/open error; intent may remain PENDING/RETRYABLE; earlier preparation commit could already be IN_FLIGHT | No HTTP without completed receipt. Inspect committed state; no blind replay/retry of whole job |
| SQLite busy/locked after HTTP | Outcome may be accepted but persistence unresolved; fallback UNCERTAIN only if verified | No second HTTP. Report unresolved if fallback fails; later stale recovery is conservative |
| Worker saturation/closed | No admitted job/HTTP for rejected preparation; current generic preparation error may report failed without durable failure state | Defer locally before claim. Persistence saturation after HTTP is unresolved; future admission must reserve completion capacity |
| Cancellation before claim | If no job admitted, no mutation/HTTP; B1 stage-labelled admission may still finish preparation before cancellation | Determine durable claim outcome, do not infer it from caller cancellation alone |
| Cancellation after committed claim, before HTTP | IN_FLIGHT can remain with zero HTTP | No current safe release; stale recovery -> UNCERTAIN. Future abort prerequisite needs exact no-start evidence |
| Cancellation during HTTP | Acceptance unknown; helper leaves IN_FLIGHT; B1 fences UNCERTAIN persistence then raises | No automatic retry. Repeated cancellation cannot cancel completion owner |
| Cancellation during result persistence | B1 fences admitted job through completion then re-raises when possible | Verify durable receipt/closed connection; failed persistence reports unresolved and must remain visible even during cancellation |
| HTTP read/write timeout, lost connection after possible transmission | UNCERTAIN | No retry; absence of acknowledgment is not rejection |
| Connect/pool timeout or ConnectError before transmission | Sender/adapter RETRYABLE with exact `connect_failure_before_transmission` evidence | Existing backoff/due/max-attempt admission only; do not generalize every connection failure |
| HTTP 408 | Existing raw sender FAILED_FINAL; adapter UNCERTAIN because its permanent-4xx rule excludes 408 | Integration changes classification conservatively; characterize and approve explicitly, never auto-retry 408 |
| HTTP 429 | Adapter RETRYABLE only with consistent rate-limit/retry_after and no acceptance metadata | Existing due time, max attempts; retry_after must be finite/nonnegative. Exhaustion -> FAILED_FINAL |
| HTTP 5xx | Existing raw sender RETRYABLE; B1 normalization/B2A UNCERTAIN | B2A integration intentionally differs from current raw helper. No auto-retry without separate proven rejection contract |
| Permanent HTTP 4xx (excluding 408/429) | Strict matching rejection -> FAILED_FINAL | No automatic retry; body-code rewrite is weaker evidence than wire HTTP |
| Malformed/missing acknowledgment or HTTP-200 error envelope | B2A UNCERTAIN; no invented provider IDs | No retry. Flattened sender record cannot reconstruct wire/body provenance |
| Restart after committed IN_FLIGHT | Lease live -> no claim; expired -> UNCERTAIN (including crash before any HTTP) | Conservative evidence loss; no lease-based resend, retain prior SENT parts |
| Partial multipart success | Persist each SENT part; event IN_FLIGHT until all confirmed; later uncertainty freezes event | Continue same active attempt only with verified authority and unsent part. Due RETRYABLE can claim unsent parts under existing rules; uncertainty never can |
| Epoch invalidation / canonical mismatch | Before stage 1 -> blocked/error, no effect. After claim fault -> HTTP may occur; stage 3 refuses/unresolved | Quiesce/drain across processes before any authority replacement. Do not write under obsolete identity |
| Incomplete durable receipt / failed commit verification | `unresolved_persistence`, no durable receipt; DB may still be IN_FLIGHT or verified UNCERTAIN | Never SENT, no resend. Inspect canonical durable rows read-only; acceptance remains Unverified |
| Stale no-send result races active/SENT owner | Existing terminalizer can produce event POLICY_DISABLED while part IN_FLIGHT/SENT | Do not use this primitive for concurrent abort. Future compare-and-set must preserve active and completed evidence |
| Failed worker teardown | Current aclose can wait indefinitely if its synchronous job never returns | No detached completion. Future finite operations + supervised drain; Python threads cannot safely be force-killed |

## 9. Invariants and guarantee limits

- One canonical public reservation pointer per event; stable event_key, setup_id,
  plan_version_id, economic setup identity and lifecycle_id. New delivery attempts
  never create new economic identities or bypass equivalent-setup dedupe.
- Atomic cross-process send authority comes from SQLite, not a Python lock,
  guard metadata, adapter invocation or earlier preflight.
- Runtime epoch and registered lifecycle/root lineage are mandatory at all
  mutation boundaries. No legacy/unattributed backfill into public authority.
- Existing eligibility, routing, quality/RR, public-effect and lifecycle ordering
  remain unchanged in B0 and are explicit migration acceptance criteria.
- No delivery connection/transaction survives HTTP; connections are opened,
  used and closed by one thread/owner. No detached background writes.
- Provider SENT needs strict positive acknowledgment evidence. Public event SENT
  additionally needs committed all-parts proof; incomplete evidence is N/A or
  Unverified, never a fabricated successful delivery.
- Uncertain acceptance is never automatically retried. Confirmed multipart
  parts are not resent; interruption freezes unsafe continuation.
- Cancellation ownership includes claim completion, persistence completion and
  teardown. Bounded queue capacity must be supplemented by finite operation and
  drain budgets before a bounded-lifetime guarantee can be claimed.

The system can establish exclusive **delivery authority** and conservative
duplicate suppression under its ownership assumptions. It cannot prove external
exactly-once Telegram delivery after lost acknowledgments/crashes. The implemented
`sendMessage` request contains chat/text/preview fields and no external idempotency
key or receipt-reconciliation mechanism [S19]:60–68. This is a statement about the
client implementation; it is not a claim about undocumented Telegram capabilities.
An accepted message whose receipt is lost remains uncertain. Sacrificing automatic
redelivery is the existing defense against duplicates; it can lose an unsent
message after a pre-HTTP crash. No broader delivery guarantee is made.

## 10. Regression evidence

New [characterization module](../../tests/test_telegram_delivery_ownership_characterization.py)
adds 20 cases, without changing production source:

| Cases | Observed behavior protected |
|---|---|
| 4 existing-helper preclaim refusal cases | Disabled/local-mode/dry-run/credentials terminalize; re-enable cannot reclaim, zero HTTP |
| 4 adapter postclaim refusal cases | Same refusals become FAILED_FINAL; replay blocked, zero HTTP |
| 3 second-preflight cases | Late disabled/dry-run/credential return becomes UNCERTAIN, zero HTTP |
| 1 existing connection-lifetime case | Both outer repositories are usable on event loop during mock HTTP, normally no transaction |
| 1 independent-owner lock case | Uncommitted outer writer blocks fresh connection; rollback releases it |
| 1 worker connection-lifetime case | Stage 1 closes on worker before HTTP; stage 3 opens/closes there afterward |
| 2 terminalizer race-state cases | Existing no-send primitive overwrites active or synthetic confirmed event, retaining incompatible part state |
| 1 existing-helper cancellation case | One mock HTTP invocation, IN_FLIGHT until expired recovery, then UNCERTAIN/no claim |
| 2 injected authority-drift cases | After claim, invalid epoch binding/canonical pointer does not block stage-2 mock HTTP; persistence unresolved |
| 1 injected destination-drift case | Mutable sender destination after adapter check reaches mock HTTP; mismatching acknowledgment then UNCERTAIN |

The test module blocks real httpx sync/async network transports. Every HTTP path
uses MockTransport; state-only paths invoke no sender. Synthetic fault mutations
are confined to temporary databases/private sender instances. Existing fixtures
bootstrap synthetic operational origin/epoch; tests reuse B1/B2A fixture helpers,
not production configuration or Runtime data.

Reviewed and run existing suites: B1 protocol (52 cases), B2A adapter (151),
outbox recovery (including independent processes), sender, signal-channel routing,
production lifecycle delivery and triggered/confirmed public delivery. B1 tests
cover pre/postclaim cancellation, repeated cancellation during persistence,
shutdown, failed commit/fallback, readback, independent process contention,
quarantined history, multipart and restart; these do not prove outer caller
ownership until caller integration itself is tested.

Validation commands/results are recorded below after final execution. Initial
test-authoring runs had one collection error (incorrect enum import), then two
mock-client configuration failures (missing base URL); these were corrected in
the new test module only. No production defect was silently repaired. A first
focused run collected 444 cases before the final three fault cases were added;
it passed in 71.49s. Final runs supersede it.

Commands use the existing `C:\CandleCraftDev\.venv\Scripts\python.exe` (3.11.9).
No dependency was installed or changed.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_telegram_delivery_ownership_characterization.py tests/test_p0b3b1_telegram_outbox_protocol.py tests/test_telegram_sender_adapter.py tests/test_telegram_outbox_recovery.py tests/test_telegram_sender_phase42.py tests/test_telegram_signal_channel_routing.py tests/test_telegram_lifecycle_delivery_phase42.py tests/test_triggered_confirmed_telegram_delivery.py --basetemp=.pytest_tmp/b0-focused-final
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -m compileall -q app scripts src tests
git diff --cached --check
```

Final focused result: **447 passed in 67.40s**, no warnings, failures or skips.
Dependency check: **No broken requirements found**. Compilation and staged diff
whitespace check: exit 0. The final focused run includes all 20 B0 cases.

Full-suite diagnostic attempts (reported rather than hidden):

- `python -m pytest --basetemp=.pytest_tmp/b0-full` stopped progressing after 16
  passes and was explicitly terminated; it is not a successful full-suite run.
- A system-temp attempt with `--basetemp=$env:TEMP/cci-operational/b0-full-20261028`
  initially produced setup errors because the new parent directory did not exist.
  It was stopped before pytest produced aggregate totals (error total N/A).
  A bounded `python -m pytest tests/test_activation_accounting_truth.py --maxfail=1
  --basetemp=$env:TEMP/cci-operational/b0-diagnostic-20261028` diagnostic returned
  **1 setup error in 0.80s**, confirming `FileNotFoundError` from pytest's basetemp
  creation. The parent directory was then created; no production code changed.
- `python -m pytest --basetemp=$env:TEMP/cci-operational/b0-full-verified-20261028
  -o faulthandler_timeout=45 --maxfail=1` again stalled after 16 passes. Its stack
  dump identified `socket.accept -> socket.socketpair -> asyncio` Proactor loop
  creation inside the existing `test_alert_agent.py::test_dry_run_does_not_call_telegram`,
  before the mock sender call. That test process was explicitly stopped.
- The final full-suite invocation runs outside the restricted process sandbox,
  with the same existing environment and safe flags, to avoid that observed local
  socketpair deadlock. This is an execution-environment adjustment, not a test
  skip or a production behavior repair.

Final full-suite command (outside the restricted process sandbox):

```powershell
$env:LOCAL_MANUAL_MODE='true'
$env:ORDER_EXECUTION_ENABLED='false'
$env:TELEGRAM_DRY_RUN='true'
$env:TELEGRAM_SIGNALS_ENABLED='false'
$b0TestRoot = Join-Path $env:TEMP 'cci-operational'
New-Item -ItemType Directory -Path $b0TestRoot -Force | Out-Null
$b0TestTemp = Join-Path $b0TestRoot 'b0-full-final-20261028'
.\.venv\Scripts\python.exe -m pytest --basetemp=$b0TestTemp -o faulthandler_timeout=60 --maxfail=1
```

Final full result: **4,136 passed, 1 warning in 800.47s (13m20s)**, exit 0,
zero failures/errors/skips. Warning:
`StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated;
install httpx2 instead`, emitted by `.venv/Lib/site-packages/fastapi/testclient.py:1`.
It is a dependency warning; B0 does not change dependencies.

The test file is unchanged between final focused/full execution and commit.
Final review confirms only this document and the characterization module differ
from main. Exact-head GitHub CI and immutable commit/PR identifiers are recorded
in the review PR and final implementation handoff after publication; no earlier
run is substituted for the final PR head.

## 11. Next implementation boundary and release requirements

**Prerequisite phase first; production caller integration remains NO-GO.** The
next approval should be limited to isolated protocol/sender evidence and
transaction-owner extraction, with no production wiring. Suggested work order:

1. Settle per-family operational disablement semantics. Keep existing terminal
   evaluations terminal. If retention is required, approve exactly how a retained
   canonical intent is reevaluated, how consumed lifecycle transitions are handled,
   and whether watchlist recovery is separately in scope.
2. Specify invocation-local no-transmission evidence and an exact-attempt,
   state-conditional abort/completion contract. Prove it cannot clobber SENT,
   UNCERTAIN, active competing claims or prior multipart confirmations. Any
   PENDING release/attempt accounting is a protocol change requiring approval;
   no blanket RETRYABLE conversion.
3. Bind immutable sender destination/configuration and full expected runtime
   identity to dispatch. Establish quiescence/drain for epoch/policy changes across
   processes; if hot revocation is required, design and verify its shared authority
   before wiring transport. A fresh worker recheck is insufficient by itself.
4. Extract worker-owned intent/snapshot/projection units while retaining all
   existing atomic reservation/economic/dedupe semantics. Remove both outer
   repository lifetimes from every migrated await; no thread-moving connections.
   Provide finite admission, SQL operation and teardown budgets, plus persistence
   capacity under saturation. Audit failure receipt consistency and cancellation
   error precedence explicitly.
5. Only after those checks pass, choose one supported production event family
   and caller with a verified restart path. A single-part confirmed path is a
   candidate, not authorization: root/confirmed truth, batch coalescing, rate caps,
   direct reconciliation branches and retry semantics must still be proved.

Future acceptance requires process contention + disable-after-claim + destination
mutation + epoch quiescence + lease expiry + repeated cancellation + saturated
persistence + multipart/partial-result tests. Adversarial interleavings must
assert HTTP invocation counts and durable row evidence, not just return statuses.
The no-send race and current late-refusal characterization assertions must change
only alongside an explicitly approved fix and new expected behavior.

Migration must inventory existing canonical terminal/uncertain/pending intents
read-only under separate operational authorization. Do not backfill, reset or
resend them. No schema migration is needed or authorized in B0. If future schema
work becomes necessary, stop for a separate reviewed migration plan.

Rollback of a future integration must first stop admission and drain transport
and persistence, then select one owner/caller implementation. Never run old and
new dispatch paths concurrently. Preserve ledger/part/attempt state and all
terminal/uncertain evidence; do not restore eligibility by deleting history.
B0 rollback is simply reverting documentation/tests; it has no runtime wiring.

Open decisions: terminal evaluation versus retained eligibility per family;
postclaim proven-no-send disposition; hot revocation versus immutable/quiescent
configuration; precise completion/drain budgets; watchlist recovery scope; failure
receipt readback strengthening. None is represented as already verified behavior.

B0 completed the bounded source investigation and documented a safe stop before
integration. Production Telegram policy/orchestration, schema, .env, strategy,
RR/Fibonacci and lifecycle price-contact code are unchanged. No real Telegram
message, listener/scanner operation, deployment, merge, order/transfer operation
or Runtime database access occurred.

[S1]: ../../app/alerts/telegram_lifecycle.py#L3428
[S2]: ../../app/alerts/telegram_lifecycle.py#L3869
[S3]: ../../app/alerts/telegram_outbox.py#L167
[S4]: ../../app/alerts/telegram_sender.py#L64
[S5]: ../../app/alerts/telegram_sender_adapter.py#L47
[S6]: ../../app/alerts/telegram_outbox_protocol.py#L78
[S7]: ../../app/alerts/telegram_lifecycle.py#L1142
[S8]: ../../app/lifecycle/repositories.py#L34
[S9]: ../../app/runtime_epoch/ownership.py#L592
[S10]: ../../app/runtime_epoch/authority.py#L29
[S11]: ../../app/runtime_epoch/startup.py#L91
[S12]: ../../app/storage/database.py#L10
[S13]: ../../app/alerts/telegram_lifecycle.py#L2525
[S14]: ../../app/alerts/telegram_lifecycle.py#L1960
[S15]: ../../app/alerts/telegram_lifecycle.py#L4124
[S16]: ../../app/alerts/telegram_lifecycle.py#L4362
[S17]: ../../app/alerts/telegram_lifecycle.py#L5101
[S18]: ../../app/alerts/telegram_lifecycle.py#L13745
[S19]: ../../app/alerts/telegram.py#L46
[S20]: ../../app/alerts/telegram_routing.py#L45
[S21]: ../../scripts/run_scan.py#L2909
[S22]: ../../app/runtime_epoch/ownership.py#L219
[S23]: ../../app/alerts/telegram_lifecycle.py#L9719
[S24]: ../../app/cmc/integration.py#L154
