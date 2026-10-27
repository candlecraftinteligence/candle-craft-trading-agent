# P0 follow-up: isolate scanner synchronous work from market-data tasks

This phase is specified, not implemented by WebSocket PR #150. No Runtime action
is authorized by this document.

## Established evidence and limits

Owner-exported Runtime rows on 2026-10-27 report lifecycle calls of 19.420–31.395 s,
watch-state persistence of 11.256–17.242 s, and scan storage of 6.384–7.739 s. The
current scanner calls these synchronous functions on its async event-loop thread.
The phase values include waiting/scheduling and are inclusive; do not sum them.

`tests/test_websocket_event_loop_starvation.py` independently exercises the real
synchronous lifecycle boundary with a substituted storage operation. A separate
loopback provider thread sends a frame while that call is executing; the async
receiver processes it only after the call returns. It accesses no database or
external network and changes no lifecycle semantics.

- CONFIRMED_EVENT_LOOP_BLOCKING: yes, source boundaries plus Runtime phase timing.
- MEASURED_WEBSOCKET_CORRELATION: synthetic DEV ordering only.
- Historical Runtime disconnect/handshake cause: PLAUSIBLE_BUT_UNPROVEN_CAUSALITY;
  correlated error timestamps / actual loop-lag observations are missing.

## Why this PR does not offload those calls

A bare `to_thread` around existing operations does not prove connection ownership,
transaction completion after cancellation, atomic watch-state ordering, or the
runtime-epoch/public-effect guarantees. A timed-out worker can continue writing
after the next iteration begins. Avoiding those hazards needs its own reviewed
phase; no lifecycle/SQLite/epoch refactor is bundled with transport recovery.

## Required implementation and verification

1. Map the three top-level operations and every opened SQLite connection. Keep
   each connection created, used and closed on its owning worker; do not change
   `check_same_thread`, share live connections, or change transaction boundaries.
2. Serialize worker admission and finalization across scan iterations. Await or
   safely fence cancelled work until durable completion is known. A timeout must
   not launch an overlapping writer, duplicate public event, or second scanner.
3. Preserve lifecycle order, public identity/reservation ownership, epoch checks,
   atomic watch-state writes and restart receipts. Do not repair the 11 unresolved
   `public_canonical_reservation_missing` observations or resend historical events.
4. Test deliberately slow lifecycle, watch-state and scan-storage operations with
   an independently publishing loopback WebSocket. Assert frame/heartbeat progress,
   fail-closed freshness, complete writes, no overlapping work and bounded shutdown.
   Cover cancellation, worker exception, disk failure, repeated restart and epoch
   mismatch using only temporary databases. Preserve every existing trading gate.
5. Record phase durations and loop lag with source/receipt timestamps. Verify full
   pytest and CI on the final commit. Runtime rollout needs separate operator
   approval and correlation with actual scanner-owned stream health afterwards.

Scope excludes CMC decisions/identity/budgets/activation, strategy/scoring/RR,
Telegram relinking/replay, schema changes and order execution. The separate CMC
audit remains outside both this WebSocket PR and this isolation specification.
