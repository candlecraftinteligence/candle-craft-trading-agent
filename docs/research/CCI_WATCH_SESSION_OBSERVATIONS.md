# Watch session observations

## Verified DEV gap and scope

The watch CLI had no shared startup lease or durable pre-acquisition session-start
contract. Completion summaries existed, but duration/status could precede manifest,
watch-output, admin-report and presentation work. Source inspection reproduces
these prospective gaps; it does not explain the historical 16h54m absence of normal
recorded scanning. That supplied audit interval is missing observation coverage,
with cause Unverified and exact UTC boundaries N/A in the supplied summary.

The correction applies to cooperating watch CLI invocations using the same
canonical database path. It does not inspect database contents, start a restart
supervisor, kill processes, or operate a Runtime service. Other binaries that do
not honor the lease, database hard-link aliases, and unverified filesystem locking
semantics are outside the demonstrated protection. Runtime storage identity and
actual launch paths still need independent verification.

## Lease and observations

`<database>.watch.lock` uses a nonblocking OS lock: `msvcrt.locking` on Windows,
`fcntl.flock` on Unix. It is acquired before reset/discovery/operational startup
and retained through every service cleanup attempt. Empty reserved markers may be
initialized only under their OS lock; a nonempty unrecognized header is preserved
and startup is refused. The file is never truncated, replaced or deleted. PIDs,
timestamps and age never break a lease. OS release on abrupt helper exit is tested.

`<database>.watch-session.json` is a bounded current observation, at most 32 KiB.
It records actual session entry, PID, iteration start/finalization and shutdown
finalization observations. It does not attest to its own subsequent OS process
exit. Unknown/corrupt receipts are preserved and startup fails closed. Writes use
an exclusively created same-directory temporary, flush/fsync and atomic replace.
Only an exclusively created temporary is disposable. Original and secondary
write/cleanup/release errors remain visible. Storage failure/power-loss guarantees
remain properties of the independently verified filesystem, not inferred here.

`<database>.watch-observations.jsonl` preserves bounded start, session-boundary and
iteration-finalization records separately from the scan manifest. Its recognized
header and complete tail are checked with bounded reads. Unknown headers or torn
tails are preserved without append/repair. Historical middle rows are not fully
re-audited on each append. Records are at most 32 KiB; the history is append-only,
with no automatic retention, pruning or cleanup. Capacity planning must include
its measured growth. A bounded receipt retains only one compact prior session;
prior-session nesting cannot grow recursively.

Unobserved prior shutdown or iteration completion is explicit. Cross-session UTC
elapsed time is an observation with clock/coverage Unverified. Negative differences
and missed-slot counts across sessions are N/A. No crash, power failure, missing
candle or outage cause is inferred. An independent observer is still needed to
notice absence while the process is not running; the scanner does not restart itself.

## Finalization and schedule semantics

Iteration start is persisted before acquisition. Finalization observations include
manifest/report/presentation work and recompute phase status and failure policy
after the tail. A watch-output failure cannot retain an earlier SUCCESS. Repeated
tail failures preserve the consecutive-failure/backoff policy. Cancellation remains
CANCELLED even when the existing loop handles it without raising. Failed service
startup still attempts every cleanup while holding the lease; both startup and
cleanup errors are reported.

`watch_session_id` and `iteration_id` link the new observations to existing watch
summaries. Existing summary duration is explicitly scoped before downstream
manifest/report/presentation work; its population and scan-manifest row semantics
are preserved. The new finalization duration reaches the declared boundary before
its own receipt/journal write; observation persistence and final service cleanup
are not silently counted as measured inside it. Per-phase/resource instrumentation
is the next correction. This is not a matched Runtime latency result.

## Verification checkpoint

The broader watch/command/owner/provenance/inventory subset passed 232 tests.
After Astra preflight findings, 25 focused tests passed for original-error
preservation, tail-failure status, OS contention, abrupt exit, bounded writes,
unknown files, cancellation, startup cleanup and reporting-tail timing. The final
corrected session/cadence/inventory subset passed **76 tests in 27.73 seconds**,
including consecutive tail-failure coverage. Full existing-environment Windows
pytest on `c8d2a2fdc78f7b31c5d93152c8ed4b4c0b3ed65b`: **3455 passed,
1 existing dependency warning in 1641.43 seconds**, no failures or skips.
Astra APPROVE for that exact head, independently 44 session/cadence/inventory
tests passed. [Required CI](https://github.com/candlecraftinteligence/candle-craft-trading-agent/actions/runs/39994803059)
succeeded. [PR #144](https://github.com/candlecraftinteligence/candle-craft-trading-agent/pull/144)
is unmerged and undeployed; its final documentation checkpoint has separate
CI/review requirements. One early oversized fixture label
exceeded Windows' environment limit; explicit short IDs corrected fixture setup.

A 100-repetition DEV filesystem-only probe measured receipt start/finish writes
and journal append, including flush/fsync: median 22.210 ms, p95 37.617 ms. The
high-resolution QueryPerformanceCounter timer reported 0.1 microsecond resolution;
the separate existing monotonic scheduling clock reported 15.625 ms resolution.
The final receipt was 1,183 bytes; 100 synthetic iteration finalizations plus
header/session-start records occupied 94,704 journal bytes. This excludes scanner,
database and network work and is not matched Runtime overhead or storage growth.

Public quality 88/A/3, confirmation, economics, lifecycle outcomes, deduplication,
schema 27 and disabled/non-enforcing P4 remain unchanged. No Runtime, exchange or
live Telegram operation was performed. No historical outage or coverage gap is repaired.

Primary API references: [Python 3.11 Windows locking](https://docs.python.org/3.11/library/msvcrt.html#msvcrt.locking),
[Python 3.11 Unix locking](https://docs.python.org/3.11/library/fcntl.html#fcntl.flock).
