# P0A — Scanner event-loop isolation handoff

BASE_SHA: ff85a7de744507b593ef118e322e47eba171f311
FEATURE_BRANCH: feature/p0a-scanner-event-loop-isolation
FINAL_IMPLEMENTATION_SHA: 23fcf6f03d4281aa930119ef20da246551642f7b
PR_URL: pending
GITHUB_CI: pending exact-head verification

RUNTIME_DEPLOYMENT_APPROVED: NO

This phase moves three independently owned file operations off the scanner
event-loop thread. It does not shorten the scanner cycle by itself, and it
does not move SQLite work. Measurements below are DEV simulations on this
worktree. They are not Runtime improvements.

## CONFIRMED_BLOCKING_BOUNDARIES

The watch loop in `scripts/run_scan.py` (`_run_watch_mode`) still runs on one
asyncio thread. Before this phase, every persistence call in that loop was a
synchronous call on that thread. A synchronous call does not return to the
loop until it finishes, so a WebSocket `recv()` scheduled on the same loop
cannot run.

Confirmed safe to leave the loop, and offloaded:

- `save_watch_state` — one file, atomic `Path.replace`, no SQLite, no OS lock held by the caller.
- `_append_scan_run_manifest` plus `_write_nightly_scan_history` — one job, so the JSONL append and the existing nightly rewrite stay ordered.
- `append_watch_output` — one append-only file.

Confirmed not safe to offload in this phase:

- Lifecycle repository, owner monitoring, Telegram outbox, and scan storage open or commit SQLite connections on the calling thread. Moving the connection, or letting a timeout return while a worker still writes, would overlap transactions. `sqlite3` default `check_same_thread` stays in force. Schema version stays 27.
- Watch session receipt and journal take an OS file lock on the caller handle and reject receipts above 32 KB.
- One-shot `_update_continue_watch_state` still calls `save_watch_state` synchronously. It is not on the watch-loop path that shares the loop with WebSocket tasks.
- CMC `Collector.collect` already uses `asyncio.to_thread` for sidecar writes inside `asyncio.wait_for`. Cancelling that waiter does not join those threads. This phase does not change that.

`asyncio.to_thread` is not used for the new file worker. A bare `to_thread` would not keep admission, cancellation, and the next iteration's write serialized.

## OFFLOADED_OPERATIONS

Classification `SAFE_FOR_SEPARATE_WORKER`, serialized through one worker, and awaited before the next file operation:

- `watch_state_persistence`
- `scan_manifest` (manifest append and nightly history rewrite)
- `watch_output`

The worker is `app/runtime/disk_isolation.py` `SerializedDiskWorker`.
`_run_watch_mode` constructs it. Importing the scanner does not start it.
The thread is named `cci-p0a-disk` and is not a daemon, so process teardown
does not drop a write that `aclose` is joining.

Profiled wrappers are unwrapped before the worker runs them. `run_in_executor`
copies context vars, and a `@profiled` function would update the shared
`PhaseProfile` from the worker thread. Wall-clock time is recorded on the
loop with `PhaseProfile.acall`.

## DEFERRED_OPERATIONS

`REQUIRES_SQLITE_THREAD_REDESIGN` (P0B, not this PR):

- lifecycle (`apply_lifecycle_to_run_result` / `SQLiteSetupLifecycleRepository`)
- owner monitoring
- telegram outbox
- scan storage (`store_scan_result`, still `profile.call("scan_storage", ...)`)

`REQUIRES_SERIALIZED_OWNERSHIP`, kept on the loop after the worker is settled:

- `_record_failed_watch_iteration` and other diagnostic manifest appends

`NOT_SAFE_TO_OFFLOAD`:

- watch session receipt and journal
- one-shot continue-watch state save

CMC sidecar `to_thread` plus the 30-second deadline is an existing hazard.
It is documented and tested. It is not fixed here.

## SQLITE_THREAD_OWNERSHIP

Unchanged. Connections are opened and closed on the calling thread.
`check_same_thread` is not disabled. No schema change, no new index, no
historical-alert prune, no change to transaction order:

scanner async, then lifecycle SQLite, owner monitoring, Telegram outbox,
watch-state file, scan-storage SQLite, manifest file, watch-output file,
admin report, session receipt.

The disk worker rejects a top-level `sqlite3.Connection` argument.

## WATCH_STATE_ATOMICITY

`save_watch_state` is unchanged: refuse the canonical legacy path, compare
`expected_updated_at`, write `.{name}.{uuid}.tmp`, `Path.replace`, and delete
the temp file on `OSError`. No extra `fsync` was added.

The loop stamps `runtime_epoch_id` before the call. The worker runs
`_reject_mismatched_watch_epoch` immediately before the write. The same
`WatchState` object is passed. It is not copied.

## MANIFEST_ORDERING

Each watch iteration awaits the manifest job before watch output. The nightly
history rewrite stays inside that same job. A failed append is not retried,
so a failure cannot write the same receipt twice. After cancellation is
settled, the existing synchronous cancelled-iteration diagnostic may append
one more row. That matches the previous behavior when cancellation arrived
after a successful append.

## WORKER_SERIALIZATION

The queue holds at most one job (`maxsize=1`). Admission is one running
operation plus one waiting caller. A third caller raises `DiskWorkerSaturated`
and does not enqueue another payload. Release of the admission lock is
exactly once, from the caller on success or from the worker thread via
`call_soon_threadsafe` after the job. A cancelled caller does not release
the lock.

## CANCELLATION_SAFETY

Cancelling the awaiting coroutine does not mean the thread has finished
writing. The caller records `caller_cancelled`, starts a fence task, and
does not observe the result. The thread keeps the admission until
`release_once` runs. `settle` waits for that fence and then acquires the
admission lock, which is only possible after the thread has released it.

`settle_after_cancellation` clears the current task's cancellation count so
the join can run. The watch loop already treats `CancelledError` as an
orderly stop. The cancel handler settles before
`_record_failed_watch_iteration`, so that diagnostic append cannot overlap
the in-flight file write. `aclose` settles, sends one shutdown sentinel, and
joins the thread.

## RESTART_SAFETY

`aclose` joins the worker before `_run_watch_mode` returns. A later watch
session constructs a new worker. An interrupted temp file is removed by the
existing `save_watch_state` error path when the write fails. A replace that
already completed is the new file. Tests cover shutdown during an in-flight
write, restart after `aclose`, and a second caller that stays fenced until
the cancelled call's write finishes.

## RUNTIME_EPOCH_SAFETY

Epoch mismatch on the loop still raises `SystemExit` from
`_stamp_runtime_watch_state`. The worker checks the same expected epoch
again before the replace and raises `WatchModeError` without writing.
No public economic identity, reservation, or plan version is modified.

## BEFORE_LOOP_LAG

DEV simulation, not Runtime. A 0.60 second `time.sleep` on the loop thread,
sampled every 0.05 seconds:

- sampled scheduling lag max: 0.590 seconds
- concurrent 0.02 second ticker beats during the sleep: 0
- wall clock: 0.609 seconds

The same shape of block is what the existing starvation test shows for
synchronous lifecycle work: a loopback receiver does not run until the
synchronous call returns. Lifecycle, scan storage, and the Telegram outbox
still have that shape.

## AFTER_LOOP_LAG

Same 0.60 second `time.sleep`, inside the disk worker:

- sampled scheduling lag max: 0.013 seconds
- ticker beats during the wait: 21
- wall clock: 0.610 seconds

Wall clock did not improve. The loop could run other tasks because
`time.sleep` releases the GIL.

A `json.dumps` of about 2,500 small records did not show the same split.
On the loop: lag 0.059 seconds, wall 0.078 seconds, 0 ticker beats.
In the worker: lag 0.043 seconds, wall 0.062 seconds, 2 ticker beats.
`json.dumps` holds the GIL and only yields at the interpreter switch
interval. Moving it off the loop thread lets the loop run between those
switches. It does not make serialization free, and it does not promise a
shorter scan cycle. Watch-state dumps on Runtime are much larger than this
fixture. Production-sized copies were not allocated.

The production monitor interval is 0.5 seconds, not the 0.05 second DEV
sample interval. Stalls shorter than the sample interval can be
under-reported. Synchronous phases also record `induced_synchronous_block`,
because the monitor cannot sample until the blocking call returns.

## WORKER_QUEUE_LATENCY

DEV simulation. One job held the worker for about 0.08 seconds and a second
caller waited:

- admission `queue_wait_seconds_max`: 0.093 seconds
- caller-observed wait until the second job finished: 0.156 seconds

A single uncontended call records no admission wait above 1 millisecond.
A third concurrent caller is rejected.

## PERSISTENCE_WALL_TIME

The offloaded 0.60 second stall took 0.610 seconds of wall clock, against
0.609 seconds on the loop. Offload changes where the time is spent, not
the disk's own duration. Watch-state replace, manifest append, and the
nightly rewrite still take whatever the disk takes.

## MEMORY_OVERHEAD

Passing an existing 500,000-byte buffer into the worker by reference
increased traced Python allocation by about 11 KB at peak (9.6 KB retained
after the call). The worker does not copy the watch-state object. The
bounded queue holds one job tuple, not a second snapshot.

An attached phase-profile snapshot, including the loop-lag and disk-worker
summaries, was 4,842 bytes in a small DEV profile. Summaries cap phase names
(24) and per-operation execution maxima (16). They do not log tokens,
payloads, or environment values.

## STRATEGY_REGRESSION

No strategy, quality, RR, timeframe, sweep, BOS/CHoCH, OB/FVG, or Fibonacci
change. `DEFAULT_CONFIGURED_MINIMUM_RR` remains `Decimal("2.5")`.
`SCHEMA_VERSION` remains 27. The market-data inventory anchor for
`--watch-interval-sec` was moved from line 615 to 617 because the two new
imports shifted that line. The default remains 60.0.

## TELEGRAM_REGRESSION

Public delivery, reservation, and deduplication are unchanged and still run
on the loop. The eleven historical unresolved reservation records were not
repaired or replayed. No send path was added.

## CMC_REGRESSION

`CMC_MODE`, `CMC_COLLECTION_PROFILE`, credit settings, and both manifests
are unchanged. `COLLECTION_DEADLINE_SECONDS` remains 30.
`deadline_extended` is false. A timed-out refresh keeps the previous
snapshot and records the exception type name only.

The 30-second deadline is not fixed. A test with the deadline patched to
0.5 seconds shows `TimeoutError`, an unchanged snapshot, and the sidecar
thread still inside its write after the waiter has returned. Observing that
thread after `asyncio.run` returns is too late: loop shutdown joins the
default executor. A separate bounded cancellation/join fix is still
required. Better loop responsiveness can make the deadline fire closer to
wall time. That is not evidence the timeout has been resolved.

## WEBSOCKET_REGRESSION

Fail-closed parsing is unchanged. `FeedRecovery.note_receive` runs after
`recv()` and before payload handling on the microstructure, liquidation,
and order-book readers. `receive_count`, `last_receive_age_seconds`, and
`heartbeat_timeout_count` are additive health fields. Heartbeat timeouts
use the existing `heartbeat_timeout` failure category. Payload text is not
stored. `start_session` does not reset the lifetime counters.

The liquidation payload rejection root cause is still unverified. This
phase does not change the parser.

## REMAINING_SQLITE_BLOCKING

Lifecycle, owner monitoring, Telegram outbox, and scan storage still block
the loop for their full wall-clock time. Those were the largest phases in
the Runtime audit (Telegram outbox about 59–65 seconds, lifecycle about
40–45 seconds, scan storage about 11–23 seconds). They stay on the calling
thread until P0B.

## REMAINING_CMC_TIMEOUT_RISK

The collection waiter can time out or be cancelled while sidecar
`asyncio.to_thread(store.put, ...)` calls continue. The deadline must not
be raised to hide that. Loop-lag work does not join those threads.

## REMAINING_LIQUIDATION_PARSER_RISK

Unverified liquidation payloads still fail closed. Receive health now
counts the frame. It does not explain why a payload was rejected.

## REMAINING_PUBLIC_IDENTITY_RISK

No public identity, reservation, or economic-plan change in this phase.
The historical reservation gap is untouched. Risk remains the pre-existing
gap, not a new writer.

## P0B_NEXT_PHASE

Do not implement this in the P0A pull request.

Next bounded phase: SQLite lifecycle and public-outbox isolation, in this
order, each with its own worker-owned connection:

1. Lifecycle repository. The connection is created, used, and closed on the
   worker. Commit and rollback stay in that connection's owner. An async
   timeout must not release the logical transaction while the worker writes.
2. Telegram outbox, with the same connection rule, plus completion receipts
   and tests that a second writer cannot reserve or send the same public
   identity. Do not repair or replay the eleven historical
   `public_canonical_reservation_missing` records.
3. Scan storage, after the two writers above have a tested sequence.

Each step needs runtime-epoch checks, cancellation fencing, and tests
against accidental double writers. Watch-session receipt locking stays on
the caller unless that lock is redesigned on purpose.

## Local verification

`python -m compileall -q app scripts src tests` completed with no errors.

The first full `pytest` failed only
`tests/test_market_data_foundation.py::test_inventory_anchors_match_source`
because two new imports moved `--watch-interval-sec` from line 615 to 617.
After that anchor was updated, the second full run failed only
`test_reports_name_every_required_section`, which compares the rendered
inventory with the checked-in research snapshot. `A_inventory.md` and
`inventory.json` now cite line 617. The default is still 60.0.

The confirmation `python -m pytest -q --tb=line` then exited 0. The progress
output reached 100% with no failures section. Collection reports 3856 tests.
DEV loop figures above are simulated. They are not a claim about the
4 GB Runtime host, the 479/730/809 second iterations, or the 264 MB
memory headroom.
